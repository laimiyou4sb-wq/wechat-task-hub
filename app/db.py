"""SQLite 持久层（消息、任务、推送日志）。"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from .utils import now_iso, now_local, parse_iso, to_iso

# 用户手动添加的待办：网页「手动添加」、手机 App「手机添加」。
# 这类任务的标题 / 截止时间是用户亲手填的，自动流程只允许补空白字段，绝不改已有内容。
MANUAL_GROUPS = ("手动添加", "手机添加")


def is_manual_task(task: dict | None) -> bool:
    return bool(task) and str(task.get("group_name") or "") in MANUAL_GROUPS


SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rawid TEXT UNIQUE,
    session_id TEXT,
    group_name TEXT DEFAULT '',
    sender TEXT DEFAULT '',
    content TEXT DEFAULT '',
    ts INTEGER DEFAULT 0,
    created_at TEXT DEFAULT '',
    processed INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    notes TEXT DEFAULT '',
    due_at TEXT,
    priority TEXT DEFAULT 'normal',
    status TEXT DEFAULT 'pending',
    group_name TEXT DEFAULT '',
    source_msg_ids TEXT DEFAULT '[]',
    dedupe_key TEXT UNIQUE,
    event_id TEXT DEFAULT '',
    match_reason TEXT DEFAULT '',
    created_at TEXT DEFAULT '',
    updated_at TEXT DEFAULT '',
    completed_at TEXT,
    snooze_until TEXT,
    rev INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);

CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    session_id TEXT DEFAULT '',
    group_name TEXT DEFAULT '',
    title TEXT DEFAULT '',
    summary TEXT DEFAULT '',
    status TEXT DEFAULT 'open',
    created_at TEXT DEFAULT '',
    updated_at TEXT DEFAULT '',
    last_msg_ts INTEGER DEFAULT 0,
    closed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id, status, last_msg_ts);

CREATE TABLE IF NOT EXISTS push_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT DEFAULT '',
    channel TEXT DEFAULT '',
    ok INTEGER DEFAULT 1,
    detail TEXT DEFAULT '',
    created_at TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT
);

-- 增量同步：全局 revision 计数器（任务 / 删除墓碑共用），App 用它做游标
CREATE TABLE IF NOT EXISTS revisions (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    value INTEGER NOT NULL DEFAULT 1
);
INSERT OR IGNORE INTO revisions(id, value) VALUES (1, 1);

-- 删除墓碑：手机端靠它知道哪些任务已从电脑端删除
CREATE TABLE IF NOT EXISTS task_tombstones (
    id TEXT PRIMARY KEY,
    deleted_at TEXT DEFAULT '',
    rev INTEGER DEFAULT 0
);

-- App 操作幂等表：重复提交同一个 action_id 直接返回首次结果
CREATE TABLE IF NOT EXISTS app_actions (
    action_id TEXT PRIMARY KEY,
    result TEXT DEFAULT '',
    created_at TEXT DEFAULT ''
);

-- 补采水位：每个会话上次完整扫到的时间（unix 秒），用于长时间离线后的全量补齐
CREATE TABLE IF NOT EXISTS catchup_state (
    session_id TEXT PRIMARY KEY,
    last_ts INTEGER DEFAULT 0,
    updated_at TEXT DEFAULT ''
);
"""


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self._lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(SCHEMA)
            self._migrate()
            self.conn.commit()

    def _migrate(self) -> None:
        """兼容旧库：tasks 表补充 event_id / match_reason / rev 列，并初始化增量同步所需表。"""
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(tasks)").fetchall()}
        if "event_id" not in columns:
            self.conn.execute("ALTER TABLE tasks ADD COLUMN event_id TEXT DEFAULT ''")
        if "match_reason" not in columns:
            self.conn.execute("ALTER TABLE tasks ADD COLUMN match_reason TEXT DEFAULT ''")
        if "rev" not in columns:
            # 老库里已有任务：按 rowid 补一个递增 rev，保证手机端首次增量同步能看到它们
            self.conn.execute("ALTER TABLE tasks ADD COLUMN rev INTEGER DEFAULT 0")
            self.conn.execute("UPDATE tasks SET rev = rowid WHERE rev IS NULL OR rev = 0")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_event ON tasks(event_id)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_rev ON tasks(rev)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_tombstones_rev ON task_tombstones(rev)")
        # 计数器至少要比现有 rev 大，避免旧库降级到 1 之后出现回退
        row = self.conn.execute("SELECT COALESCE(MAX(rev), 0) AS m FROM tasks").fetchone()
        self.conn.execute(
            "UPDATE revisions SET value = MAX(value, ?) WHERE id = 1",
            (int(row["m"] or 0) + 1,),
        )

    # ---------- 增量同步：revision 游标 ----------
    def _next_rev(self) -> int:
        """在锁内调用：全局自增 rev。任务/墓碑变更后手机端靠它拉增量。"""
        row = self.conn.execute("SELECT value FROM revisions WHERE id = 1").fetchone()
        value = int(row["value"] if row else 0) + 1
        self.conn.execute("UPDATE revisions SET value = ? WHERE id = 1", (value,))
        return value

    def current_rev(self) -> int:
        with self._lock:
            row = self.conn.execute("SELECT value FROM revisions WHERE id = 1").fetchone()
        return int(row["value"]) if row else 0

    def tasks_since(self, rev: int, limit: int = 500) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM tasks WHERE rev > ? ORDER BY rev ASC LIMIT ?",
                (int(rev), max(1, int(limit))),
            ).fetchall()
        return [self._task_dict(row) for row in rows]

    def all_tasks(self, limit: int = 20000) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM tasks ORDER BY rev ASC LIMIT ?", (max(1, int(limit)),)
            ).fetchall()
        return [self._task_dict(row) for row in rows]

    def tombstones_since(self, rev: int, limit: int = 500) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, deleted_at, rev FROM task_tombstones WHERE rev > ? ORDER BY rev ASC LIMIT ?",
                (int(rev), max(1, int(limit))),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_app_action(self, action_id: str) -> dict | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT action_id, result, created_at FROM app_actions WHERE action_id = ?",
                (action_id,),
            ).fetchone()
        return dict(row) if row else None

    def save_app_action(self, action_id: str, result: dict) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO app_actions(action_id, result, created_at) VALUES (?,?,?)",
                (action_id, json.dumps(result, ensure_ascii=False), now_iso()),
            )
            self.conn.commit()

    # ---------- 补采水位（长时间离线后的全量补齐） ----------
    def get_catchup_state(self, session_id: str) -> dict | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT session_id, last_ts, updated_at FROM catchup_state WHERE session_id = ?",
                (str(session_id or ""),),
            ).fetchone()
        return dict(row) if row else None

    def set_catchup_state(self, session_id: str, last_ts: int) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO catchup_state(session_id, last_ts, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(session_id) DO UPDATE SET last_ts = excluded.last_ts, "
                "updated_at = excluded.updated_at",
                (str(session_id or ""), int(last_ts or 0), now_iso()),
            )
            self.conn.commit()

    def catchup_states(self) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT session_id, last_ts, updated_at FROM catchup_state ORDER BY last_ts ASC"
            ).fetchall()
        return [dict(row) for row in rows]

    def max_message_ts(self, session_id: str, before_iso: str | None = None) -> int:
        """该会话已入库消息的最新时间（只用于首次建立补采水位）。

        before_iso 传入服务启动时间时，只统计启动前已入库的消息，
        避免启动后实时流刚写入的新消息把离线积压误判成已覆盖。
        """
        sql = "SELECT COALESCE(MAX(ts), 0) AS m FROM messages WHERE session_id = ?"
        params: list = [str(session_id or "")]
        if before_iso:
            sql += " AND created_at < ?"
            params.append(str(before_iso))
        with self._lock:
            row = self.conn.execute(sql, params).fetchone()
        return int(row["m"] or 0) if row else 0

    def existing_message_ids(self, rawids: list[str]) -> set[str]:
        """批量查询已入库的消息 id；按 500 分批，规避 SQLite 变量数上限。"""
        values = [str(item) for item in rawids if str(item)]
        found: set[str] = set()
        for start in range(0, len(values), 500):
            chunk = values[start:start + 500]
            marks = ",".join("?" for _ in chunk)
            with self._lock:
                rows = self.conn.execute(
                    f"SELECT rawid FROM messages WHERE rawid IN ({marks})", tuple(chunk)
                ).fetchall()
            found.update(str(row["rawid"]) for row in rows)
        return found

    # ---------- messages ----------
    def add_message(self, rawid: str, session_id: str, group_name: str, sender: str,
                    content: str, ts: int) -> bool:
        if not rawid:
            rawid = f"local-{uuid.uuid4().hex}"
        with self._lock:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO messages(rawid, session_id, group_name, sender, content, ts, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (rawid, session_id or "", group_name or "", sender or "", content or "", int(ts or 0), now_iso()),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def messages_by_ids(self, ids: list[str]) -> list[dict]:
        if not ids:
            return []
        marks = ",".join("?" for _ in ids)
        with self._lock:
            rows = self.conn.execute(
                f"SELECT rawid, group_name, sender, content, ts FROM messages WHERE rawid IN ({marks}) ORDER BY ts ASC",
                ids,
            ).fetchall()
        return [dict(row) for row in rows]

    def cleanup(self, days: int) -> int:
        cutoff = int((now_local() - timedelta(days=days)).timestamp())
        with self._lock:
            cur = self.conn.execute("DELETE FROM messages WHERE ts < ?", (cutoff,))
            self.conn.commit()
            return cur.rowcount

    # ---------- tasks ----------
    def create_task(self, *, title: str, notes: str = "", due_at: str | None = None,
                    priority: str = "normal", group_name: str = "",
                    source_msg_ids: list[str] | None = None,
                    dedupe_key: str | None = None,
                    event_id: str = "",
                    match_reason: str = "") -> tuple[dict, bool]:
        now = now_iso()
        task_id = uuid.uuid4().hex[:12]
        ids = source_msg_ids or []
        with self._lock:
            rev = self._next_rev()
            try:
                self.conn.execute(
                    "INSERT INTO tasks(id, title, notes, due_at, priority, status, group_name, source_msg_ids, "
                    "dedupe_key, event_id, match_reason, created_at, updated_at, rev) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (task_id, title, notes, due_at, priority, "pending", group_name,
                     json.dumps(ids, ensure_ascii=False), dedupe_key, event_id, match_reason, now, now, rev),
                )
                self.conn.commit()
                row = self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
                return self._task_dict(row), True
            except sqlite3.IntegrityError:
                row = self.conn.execute("SELECT * FROM tasks WHERE dedupe_key = ?", (dedupe_key,)).fetchone()
                if row is None:
                    raise
                if str(row["status"]) in ("done", "ignored"):
                    # 已完成 / 已忽略的任务不参与归并：把去重键让给新待办，
                    # 否则新消息会被静默并进已完成任务，用户看不到新待办。
                    # （dedupe_key 是内部字段，不参与同步 DTO，挪走不影响手机端）
                    self.conn.execute("UPDATE tasks SET dedupe_key = NULL WHERE id = ?", (row["id"],))
                    try:
                        self.conn.execute(
                            "INSERT INTO tasks(id, title, notes, due_at, priority, status, group_name, "
                            "source_msg_ids, dedupe_key, event_id, match_reason, created_at, updated_at, rev) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (task_id, title, notes, due_at, priority, "pending", group_name,
                             json.dumps(ids, ensure_ascii=False), dedupe_key, event_id, match_reason,
                             now, now, rev),
                        )
                    except sqlite3.IntegrityError:
                        # 极端并发下仍撞键：退回不写去重键，保证新待办不丢
                        self.conn.execute(
                            "INSERT INTO tasks(id, title, notes, due_at, priority, status, group_name, "
                            "source_msg_ids, dedupe_key, event_id, match_reason, created_at, updated_at, rev) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (task_id, title, notes, due_at, priority, "pending", group_name,
                             json.dumps(ids, ensure_ascii=False), None, event_id, match_reason,
                             now, now, rev),
                        )
                    self.conn.commit()
                    row = self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
                    return self._task_dict(row), True
                existing_ids = json.loads(row["source_msg_ids"] or "[]")
                merged = list(dict.fromkeys(existing_ids + ids))
                self.conn.execute(
                    "UPDATE tasks SET source_msg_ids = ?, updated_at = ?, rev = ? WHERE id = ?",
                    (json.dumps(merged, ensure_ascii=False), now, self._next_rev(), row["id"]),
                )
                self.conn.commit()
                row = self.conn.execute("SELECT * FROM tasks WHERE id = ?", (row["id"],)).fetchone()
                return self._task_dict(row), False

    def get_task(self, task_id: str) -> dict | None:
        with self._lock:
            row = self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return self._task_dict(row) if row else None

    def list_tasks(self, status: str = "pending", limit: int = 200) -> list[dict]:
        sql = "SELECT * FROM tasks"
        params: list = []
        if status in ("pending", "done", "ignored", "snoozed"):
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY (due_at IS NULL), due_at ASC, created_at DESC LIMIT ?"
        params.append(max(1, min(limit, 1000)))
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._task_dict(row) for row in rows]

    def update_task(self, task_id: str, fields: dict) -> dict | None:
        allowed = {"title", "notes", "due_at", "priority", "status", "snooze_until", "group_name"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        now = now_iso()
        with self._lock:
            row = self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                return None
            if updates.get("status") == "done":
                updates["completed_at"] = now
                updates["snooze_until"] = None
            elif updates.get("status") == "pending":
                updates["completed_at"] = None
                updates["snooze_until"] = None
            updates["updated_at"] = now
            updates["rev"] = self._next_rev()
            sets = ", ".join(f"{key} = ?" for key in updates)
            self.conn.execute(f"UPDATE tasks SET {sets} WHERE id = ?", (*updates.values(), task_id))
            self.conn.commit()
            row = self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return self._task_dict(row)

    def delete_task(self, task_id: str) -> bool:
        """物理删除任务（不可恢复）并写删除墓碑，供手机端增量同步回收。

        返回是否真的删掉了一行。
        """
        with self._lock:
            row = self.conn.execute("SELECT id FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                return False
            self.conn.execute(
                "INSERT OR REPLACE INTO task_tombstones(id, deleted_at, rev) VALUES (?,?,?)",
                (task_id, now_iso(), self._next_rev()),
            )
            self.conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            self.conn.commit()
            return True

    # ---------- events ----------
    def create_event(self, *, session_id: str, group_name: str = "",
                     title: str = "", summary: str = "", last_msg_ts: int = 0) -> dict:
        event_id = uuid.uuid4().hex[:12]
        now = now_iso()
        with self._lock:
            self.conn.execute(
                "INSERT INTO events(id, session_id, group_name, title, summary, status, "
                "created_at, updated_at, last_msg_ts) VALUES (?,?,?,?,?,?,?,?,?)",
                (event_id, session_id or "", group_name or "", title or "", summary or "",
                 "open", now, now, int(last_msg_ts or 0)),
            )
            self.conn.commit()
            row = self.conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        return dict(row)

    def get_event(self, event_id: str) -> dict | None:
        if not event_id:
            return None
        with self._lock:
            row = self.conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        return dict(row) if row else None

    def update_event(self, event_id: str, fields: dict) -> dict | None:
        allowed = {"title", "summary", "status", "last_msg_ts"}
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return self.get_event(event_id)
        updates["updated_at"] = now_iso()
        if updates.get("status") == "closed":
            updates["closed_at"] = now_iso()
        with self._lock:
            sets = ", ".join(f"{key} = ?" for key in updates)
            self.conn.execute(f"UPDATE events SET {sets} WHERE id = ?", (*updates.values(), event_id))
            self.conn.commit()
            row = self.conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        return dict(row) if row else None

    def open_events(self, session_id: str, limit: int | None = None) -> list[dict]:
        """该会话仍打开的事项。

        事项不再按时间自动关闭，也不按「最近 N 个」截断：再次聊到的同一件事
        必须还能在候选里被归并。limit=None 表示全部返回。
        """
        sql = ("SELECT * FROM events WHERE session_id = ? AND status = 'open' "
               "ORDER BY last_msg_ts DESC")
        params: list = [session_id or ""]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(1, int(limit)))
        with self._lock:
            rows = self.conn.execute(sql, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def event_tasks(self, event_id: str) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM tasks WHERE event_id = ? ORDER BY created_at ASC", (event_id,)
            ).fetchall()
        return [self._task_dict(row) for row in rows]

    def count_open_events(self) -> int:
        with self._lock:
            return self.conn.execute(
                "SELECT COUNT(*) AS c FROM events WHERE status = 'open'"
            ).fetchone()["c"]

    def merge_task_sources(self, task_id: str, source_msg_ids: list[str]) -> dict | None:
        if not source_msg_ids:
            return self.get_task(task_id)
        with self._lock:
            row = self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                return None
            existing = json.loads(row["source_msg_ids"] or "[]")
            merged = list(dict.fromkeys(existing + source_msg_ids))
            self.conn.execute(
                "UPDATE tasks SET source_msg_ids = ?, updated_at = ?, rev = ? WHERE id = ?",
                (json.dumps(merged, ensure_ascii=False), now_iso(), self._next_rev(), task_id),
            )
            self.conn.commit()
            row = self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return self._task_dict(row)

    # ---------- 手动待办：自动补充（只补空白，不改用户填的内容） ----------
    def list_manual_pending(self, limit: int = 20) -> list[dict]:
        """未完成的手动待办（最近创建的在前），用于自动补充 / 新建去重。"""
        marks = ",".join("?" for _ in MANUAL_GROUPS)
        with self._lock:
            rows = self.conn.execute(
                f"SELECT * FROM tasks WHERE status = 'pending' AND group_name IN ({marks}) "
                "ORDER BY created_at DESC LIMIT ?",
                (*MANUAL_GROUPS, max(1, int(limit))),
            ).fetchall()
        return [self._task_dict(row) for row in rows]

    def supplement_task(self, task_id: str, *, due_at: str | None = None,
                        notes: str | None = None, source_msg_ids: list[str] | None = None,
                        match_reason: str = "") -> tuple[dict | None, list[str]]:
        """只给手动待办补空白字段：标题 / 已有截止时间 / 已有备注一律不动。

        返回 (任务, 本次实际写入的字段名列表)；无变化时字段列表为空。
        """
        now = now_iso()
        with self._lock:
            row = self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                return None, []
            current = self._task_dict(row)
            fields: dict = {}
            changed: list[str] = []
            if due_at and not str(current.get("due_at") or "").strip():
                fields["due_at"] = due_at
                changed.append("due_at")
            if notes and not str(current.get("notes") or "").strip():
                fields["notes"] = notes
                changed.append("notes")
            if source_msg_ids:
                existing = list(current.get("source_msg_ids") or [])
                merged = list(dict.fromkeys([*existing, *[sid for sid in source_msg_ids if sid]]))
                if merged != existing:
                    fields["source_msg_ids"] = json.dumps(merged, ensure_ascii=False)
                    changed.append("source_msg_ids")
            if not changed:
                return current, []
            if match_reason and not str(current.get("match_reason") or "").strip():
                fields["match_reason"] = match_reason
                changed.append("match_reason")
            fields["updated_at"] = now
            fields["rev"] = self._next_rev()
            sets = ", ".join(f"{key} = ?" for key in fields)
            self.conn.execute(f"UPDATE tasks SET {sets} WHERE id = ?", (*fields.values(), task_id))
            self.conn.commit()
            row = self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return self._task_dict(row), changed

    def counts(self) -> dict:
        now = now_local()
        today = now.date()
        week_end = today + timedelta(days=7)
        today_n = week_n = later_n = 0
        with self._lock:
            rows = self.conn.execute("SELECT due_at FROM tasks WHERE status = 'pending'").fetchall()
            done = self.conn.execute("SELECT COUNT(*) AS c FROM tasks WHERE status = 'done'").fetchone()["c"]
        for row in rows:
            due = parse_iso(row["due_at"])
            if due is None:
                # 无截止时间 → 「以后」（与 web/app.js bucketOf、App TimeUtil.bucketOf、契约 §7 一致）。
                later_n += 1
            elif due.date() <= today:
                today_n += 1
            elif due.date() <= week_end:
                week_n += 1
            else:
                later_n += 1
        return {"today": today_n, "week": week_n, "later": later_n, "done": done}

    def reactivate_snoozed(self) -> int:
        now = now_iso()
        with self._lock:
            rows = self.conn.execute(
                "SELECT id FROM tasks WHERE status = 'snoozed' AND snooze_until IS NOT NULL AND snooze_until <= ?",
                (now,),
            ).fetchall()
            for row in rows:
                self.conn.execute(
                    "UPDATE tasks SET status = 'pending', snooze_until = NULL, updated_at = ?, rev = ? WHERE id = ?",
                    (now, self._next_rev(), row["id"]),
                )
            self.conn.commit()
            return len(rows)

    def log_push(self, kind: str, channel: str, ok: bool, detail: str = "") -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO push_log(kind, channel, ok, detail, created_at) VALUES (?,?,?,?,?)",
                (kind, channel, 1 if ok else 0, detail[:500], now_iso()),
            )
            self.conn.commit()

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self.conn.execute("INSERT INTO meta(k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                              (key, value))
            self.conn.commit()

    def get_meta(self, key: str) -> str | None:
        with self._lock:
            row = self.conn.execute("SELECT v FROM meta WHERE k = ?", (key,)).fetchone()
        return row["v"] if row else None

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    @staticmethod
    def _task_dict(row: sqlite3.Row) -> dict:
        data = dict(row)
        data["source_msg_ids"] = json.loads(data.get("source_msg_ids") or "[]")
        data["source_count"] = len(data["source_msg_ids"])
        return data
