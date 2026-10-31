"""SQLite 持久层（消息、任务、推送日志）。"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from .utils import now_iso, now_local, parse_iso, to_iso

logger = logging.getLogger(__name__)

# 用户手动添加的待办：网页「手动添加」、手机 App「手机添加」。
# 自动流程默认只补空白字段；但模型明确识别到「改期/新的截止时间」时，
# 允许覆盖用户已填的截止时间（supplement_task(overwrite_due=True)），标题与备注仍不覆盖。
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
    -- 处理状态：0=已入库未处理（采集兜底队列），1=处理成功（过滤/归并决策已完成），
    -- 2=失败队列（重试超过上限的毒消息，不再自动重试，不阻塞同会话后续消息）
    processed INTEGER DEFAULT 0,
    -- 采集时的标准消息 JSON（含 @名单/引用/链接卡片等未落列字段），用于未处理兜底重放
    payload TEXT DEFAULT '',
    -- 处理失败重试：已尝试次数 / 最近一次错误 / 进入失败队列的时间
    attempts INTEGER DEFAULT 0,
    last_error TEXT DEFAULT '',
    failed_at TEXT DEFAULT ''
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

-- 采集游标：last_local_id = 该会话已采到的最大 localId（微信 WCDB 行序，
-- 与 createTime 解耦，能兜住「写入晚、业务时间旧」的晚到消息）；last_ts 保留为
-- 时间水位（首次建立游标 / 游标失效兜底 / 排障展示）。
CREATE TABLE IF NOT EXISTS catchup_state (
    session_id TEXT PRIMARY KEY,
    last_ts INTEGER DEFAULT 0,
    last_local_id INTEGER DEFAULT 0,
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
        self._migrate_messages()
        self._migrate_catchup()

    def _migrate_messages(self) -> None:
        """messages 表升级：补 payload 列，并把旧版已入库消息一次性标记为已处理。

        旧版采集（原「补采」）是「先归纳成功、再入库」，因此本次升级前已入库的消息
        都可以视为已处理；不做这个一次性标记，升级后首轮采集会把全部历史消息重新
        处理一遍，产生重复事项/待办。标记只执行一次（meta 表记录版本），之后新入库
        的消息才从 processed=0 开始走「已入库未处理」兜底。
        """
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(messages)").fetchall()}
        if "processed" not in columns:
            self.conn.execute("ALTER TABLE messages ADD COLUMN processed INTEGER DEFAULT 0")
        if "payload" not in columns:
            self.conn.execute("ALTER TABLE messages ADD COLUMN payload TEXT DEFAULT ''")
        if "attempts" not in columns:
            self.conn.execute("ALTER TABLE messages ADD COLUMN attempts INTEGER DEFAULT 0")
        if "last_error" not in columns:
            self.conn.execute("ALTER TABLE messages ADD COLUMN last_error TEXT DEFAULT ''")
        if "failed_at" not in columns:
            self.conn.execute("ALTER TABLE messages ADD COLUMN failed_at TEXT DEFAULT ''")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_processed ON messages(processed, session_id, ts)")
        # 失败队列按失败时间倒序展示 / 重试
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_failed ON messages(processed, failed_at)")
        migrated = self.conn.execute(
            "SELECT 1 FROM meta WHERE k = 'messages_processed_v1'"
        ).fetchone()
        if migrated is None:
            self.conn.execute("UPDATE messages SET processed = 1")
            self.conn.execute(
                "INSERT OR REPLACE INTO meta(k, v) VALUES ('messages_processed_v1', ?)",
                (now_iso(),),
            )
            logger.info("messages 处理状态迁移完成：历史消息标记为 processed=1（仅本次）")

    def _migrate_catchup(self) -> None:
        """采集游标升级：catchup_state 补 last_local_id（localId 增量游标）。

        老库只有时间水位：先留 0，首轮采集按时间水位补齐后再建立 localId 游标。
        """
        columns = {
            row["name"] for row in self.conn.execute("PRAGMA table_info(catchup_state)").fetchall()
        }
        if "last_local_id" not in columns:
            self.conn.execute("ALTER TABLE catchup_state ADD COLUMN last_local_id INTEGER DEFAULT 0")

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

    # ---------- 采集游标（localId 增量 + 时间水位） ----------
    def get_catchup_state(self, session_id: str) -> dict | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT session_id, last_ts, last_local_id, updated_at FROM catchup_state "
                "WHERE session_id = ?",
                (str(session_id or ""),),
            ).fetchone()
        return dict(row) if row else None

    def set_catchup_state(self, session_id: str, last_ts: int, local_id: int | None = None,
                          *, reset_local_id: bool = False) -> None:
        """推进会话游标。

        - ``local_id`` 为 None 时保留已有 localId 游标（只推进时间水位）；
        - ``reset_local_id=True`` 用于 localId 重装/回绕：直接覆盖为新的游标值；
        - 其余情况取 ``MAX(已有, 新值)``，避免并发/乱序回退游标。
        """
        session = str(session_id or "")
        ts_value = int(last_ts or 0)
        with self._lock:
            if reset_local_id:
                cursor = max(0, int(local_id or 0))
                self.conn.execute(
                    "INSERT INTO catchup_state(session_id, last_ts, last_local_id, updated_at) "
                    "VALUES (?,?,?,?) "
                    "ON CONFLICT(session_id) DO UPDATE SET last_ts = excluded.last_ts, "
                    "last_local_id = excluded.last_local_id, updated_at = excluded.updated_at",
                    (session, ts_value, cursor, now_iso()),
                )
            elif local_id is None:
                self.conn.execute(
                    "INSERT INTO catchup_state(session_id, last_ts, last_local_id, updated_at) "
                    "VALUES (?,?,0,?) "
                    "ON CONFLICT(session_id) DO UPDATE SET last_ts = excluded.last_ts, "
                    "updated_at = excluded.updated_at",
                    (session, ts_value, now_iso()),
                )
            else:
                cursor = max(0, int(local_id or 0))
                self.conn.execute(
                    "INSERT INTO catchup_state(session_id, last_ts, last_local_id, updated_at) "
                    "VALUES (?,?,?,?) "
                    "ON CONFLICT(session_id) DO UPDATE SET last_ts = excluded.last_ts, "
                    "last_local_id = MAX(catchup_state.last_local_id, excluded.last_local_id), "
                    "updated_at = excluded.updated_at",
                    (session, ts_value, cursor, now_iso()),
                )
            self.conn.commit()

    def catchup_states(self) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT session_id, last_ts, last_local_id, updated_at FROM catchup_state "
                "ORDER BY last_ts ASC"
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
        """批量查询**已终结**的消息 id（processed=1 已处理 / processed=2 失败队列）。

        语义要求：只跳过已终结的消息。processed=0 的消息（已入库但未处理）必须
        返回为空集合，交给采集轮兜底重试；processed=2 的消息已进失败队列，不能
        在下一轮被当成新消息重新处理（需要人工重试才回到 processed=0）。
        按 500 分批，规避 SQLite 变量数上限。
        """
        values = [str(item) for item in rawids if str(item)]
        found: set[str] = set()
        for start in range(0, len(values), 500):
            chunk = values[start:start + 500]
            marks = ",".join("?" for _ in chunk)
            with self._lock:
                rows = self.conn.execute(
                    f"SELECT rawid FROM messages WHERE rawid IN ({marks}) AND processed IN (1, 2)",
                    tuple(chunk),
                ).fetchall()
            found.update(str(row["rawid"]) for row in rows)
        return found

    # ---------- messages ----------
    def add_message(self, rawid: str, session_id: str, group_name: str, sender: str,
                    content: str, ts: int, *, processed: int = 0,
                    payload: dict | None = None) -> bool:
        """入库一条消息；采集路径默认 processed=0（等待处理），处理成功后置 1。"""
        if not rawid:
            rawid = f"local-{uuid.uuid4().hex}"
        payload_text = json.dumps(payload, ensure_ascii=False) if isinstance(payload, dict) else ""
        with self._lock:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO messages(rawid, session_id, group_name, sender, content, ts, "
                "created_at, processed, payload) VALUES (?,?,?,?,?,?,?,?,?)",
                (rawid, session_id or "", group_name or "", sender or "", content or "",
                 int(ts or 0), now_iso(), 1 if processed else 0, payload_text),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def add_messages(self, items: list[dict]) -> int:
        """批量入库采集到的标准消息（processed=0，保留完整 payload）；返回新增条数。

        已存在（processed=1 已处理 / processed=2 失败队列）的 rawid 由 INSERT OR IGNORE
        原样保留，不会把已终结消息重置回未处理。
        """
        inserted = 0
        with self._lock:
            for item in items:
                rawid = str(item.get("rawid") or "")
                if not rawid:
                    rawid = f"local-{uuid.uuid4().hex}"
                payload_text = json.dumps(item, ensure_ascii=False)
                cur = self.conn.execute(
                    "INSERT OR IGNORE INTO messages(rawid, session_id, group_name, sender, content, ts, "
                    "created_at, processed, payload) VALUES (?,?,?,?,?,?,?,?,?)",
                    (rawid, str(item.get("session_id") or ""), str(item.get("group_name") or ""),
                     str(item.get("sender") or ""), str(item.get("content") or ""),
                     int(item.get("ts") or 0), now_iso(), 0, payload_text),
                )
                inserted += 1 if cur.rowcount > 0 else 0
            self.conn.commit()
        return inserted

    def mark_messages_processed(self, rawids: list[str], processed: int = 1) -> int:
        """把一批消息的处理状态置为 processed（默认 1）；返回实际更新条数。

        成功置 1 时顺手清空失败重试记录（attempts/last_error/failed_at），
        避免后续排查时误导。
        """
        values = [str(item) for item in rawids if str(item)]
        if not values:
            return 0
        updated = 0
        with self._lock:
            for start in range(0, len(values), 500):
                chunk = values[start:start + 500]
                marks = ",".join("?" for _ in chunk)
                if processed:
                    cur = self.conn.execute(
                        f"UPDATE messages SET processed = ?, attempts = 0, last_error = '', "
                        f"failed_at = '' WHERE rawid IN ({marks})",
                        (1, *chunk),
                    )
                else:
                    cur = self.conn.execute(
                        f"UPDATE messages SET processed = 0 WHERE rawid IN ({marks})",
                        (0, *chunk),
                    )
                updated += max(0, int(cur.rowcount or 0))
            self.conn.commit()
        return updated

    def record_message_errors(self, rawids: list[str], error: str) -> int:
        """只记录 last_error、**不**增加 attempts（用于整体性故障，如 LLM/网络中断）。

        与 :meth:`record_message_failures` 的区别：整体性故障不代表消息本身有问题，
        不消耗重试次数，下一轮仍按待处理自动重试，避免一次短时故障把整批积压打进
        失败队列。
        """
        values = [str(item) for item in rawids if str(item)]
        if not values:
            return 0
        message = str(error or "")[:300]
        updated = 0
        with self._lock:
            for start in range(0, len(values), 500):
                chunk = values[start:start + 500]
                marks = ",".join("?" for _ in chunk)
                cur = self.conn.execute(
                    f"UPDATE messages SET last_error = ? WHERE rawid IN ({marks})",
                    (message, *chunk),
                )
                updated += max(0, int(cur.rowcount or 0))
            self.conn.commit()
        return updated

    def record_message_failures(self, rawids: list[str], error: str,
                                max_attempts: int) -> dict:
        """记录一批消息的处理失败，返回本次达到重试上限、进入失败队列的消息。

        返回 ``{"dead": [rawid, ...], "detail": {rawid: attempts}}``。
        达到 ``max_attempts`` 的消息置 processed=2（失败队列），不再自动重试，
        因此同会话后续消息不会被它一直拖住。
        """
        values = [str(item) for item in rawids if str(item)]
        limit = max(1, int(max_attempts or 1))
        message = str(error or "")[:300]
        at = now_iso()
        dead: list[str] = []
        detail: dict[str, int] = {}
        if not values:
            return {"dead": dead, "detail": detail}
        with self._lock:
            for start in range(0, len(values), 500):
                chunk = values[start:start + 500]
                marks = ",".join("?" for _ in chunk)
                self.conn.execute(
                    f"UPDATE messages SET attempts = attempts + 1, last_error = ? "
                    f"WHERE rawid IN ({marks})",
                    (message, *chunk),
                )
                rows = self.conn.execute(
                    f"SELECT rawid, attempts FROM messages WHERE rawid IN ({marks})",
                    tuple(chunk),
                ).fetchall()
                chunk_dead: list[str] = []
                for row in rows:
                    rawid = str(row["rawid"])
                    attempts = int(row["attempts"] or 0)
                    detail[rawid] = attempts
                    if attempts >= limit:
                        chunk_dead.append(rawid)
                if chunk_dead:
                    marks_dead = ",".join("?" for _ in chunk_dead)
                    self.conn.execute(
                        f"UPDATE messages SET processed = 2, failed_at = ? "
                        f"WHERE rawid IN ({marks_dead})",
                        (at, *chunk_dead),
                    )
                    dead.extend(chunk_dead)
            self.conn.commit()
        return {"dead": dead, "detail": detail}

    def failed_messages(self, limit: int = 100, offset: int = 0) -> list[dict]:
        """失败队列：重试超限、等待人工处理的消息（按失败时间倒序）。"""
        limit = max(1, min(int(limit or 100), 1000))
        offset = max(0, int(offset or 0))
        with self._lock:
            rows = self.conn.execute(
                "SELECT rawid, session_id, group_name, sender, content, ts, attempts, "
                "last_error, failed_at FROM messages WHERE processed = 2 "
                "ORDER BY failed_at DESC, id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [dict(row) for row in rows]

    def count_failed_messages(self) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS c FROM messages WHERE processed = 2"
            ).fetchone()
        return int(row["c"] or 0) if row else 0

    def failed_sessions(self) -> list[dict]:
        """失败队列涉及的会话（排障 / 设置页展示用）。"""
        with self._lock:
            rows = self.conn.execute(
                "SELECT session_id, MAX(group_name) AS group_name, COUNT(*) AS failed "
                "FROM messages WHERE processed = 2 GROUP BY session_id ORDER BY MAX(failed_at) DESC"
            ).fetchall()
        return [{"session_id": str(row["session_id"]),
                 "group_name": str(row["group_name"] or ""),
                 "failed": int(row["failed"] or 0)} for row in rows]

    def requeue_failed_messages(self, rawids: list[str] | None = None,
                                limit: int = 500) -> int:
        """把失败队列消息放回待处理（processed=0，attempts 清零）以便重试。

        不传 rawids 时按失败时间倒序取最多 ``limit`` 条（设置页「重试失败队列」）。
        """
        with self._lock:
            if rawids:
                values = [str(item) for item in rawids if str(item)]
                if not values:
                    return 0
                updated = 0
                for start in range(0, len(values), 500):
                    chunk = values[start:start + 500]
                    marks = ",".join("?" for _ in chunk)
                    cur = self.conn.execute(
                        f"UPDATE messages SET processed = 0, attempts = 0, last_error = '', "
                        f"failed_at = '' WHERE rawid IN ({marks}) AND processed = 2",
                        tuple(chunk),
                    )
                    updated += max(0, int(cur.rowcount or 0))
            else:
                ids = [str(row["rawid"]) for row in self.conn.execute(
                    "SELECT rawid FROM messages WHERE processed = 2 "
                    "ORDER BY failed_at DESC, id DESC LIMIT ?",
                    (max(1, int(limit or 500)),),
                ).fetchall()]
                updated = 0
                if ids:
                    marks = ",".join("?" for _ in ids)
                    cur = self.conn.execute(
                        f"UPDATE messages SET processed = 0, attempts = 0, last_error = '', "
                        f"failed_at = '' WHERE rawid IN ({marks}) AND processed = 2",
                        tuple(ids),
                    )
                    updated = max(0, int(cur.rowcount or 0))
            self.conn.commit()
        return updated

    def unprocessed_messages(self, session_id: str, limit: int = 0) -> list[dict]:
        """该会话「已入库未处理」的消息（时间升序，用于采集兜底重放）。

        payload 里保存了采集时的完整标准消息（@名单、引用、链接卡片等），
        重新处理时与首次处理等价；老数据没有 payload 时退回列字段。
        """
        sql = ("SELECT rawid, session_id, group_name, sender, content, ts, payload "
               "FROM messages WHERE session_id = ? AND processed = 0 ORDER BY ts ASC, id ASC")
        params: list = [str(session_id or "")]
        if int(limit or 0) > 0:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        messages: list[dict] = []
        for row in rows:
            message: dict = {}
            if row["payload"]:
                try:
                    loaded = json.loads(row["payload"])
                    if isinstance(loaded, dict):
                        message = loaded
                except (TypeError, ValueError):
                    message = {}
            message.update({
                "rawid": str(row["rawid"]),
                "session_id": str(row["session_id"]),
                "group_name": str(row["group_name"] or message.get("group_name") or ""),
                "sender": str(row["sender"] or message.get("sender") or ""),
                "content": str(row["content"] or message.get("content") or ""),
                "ts": int(row["ts"] or 0),
            })
            message.setdefault("is_group", str(row["session_id"]).endswith("@chatroom"))
            message.setdefault("event", "message.new")
            messages.append(message)
        return messages

    def unprocessed_sessions(self) -> list[dict]:
        """存在「已入库未处理」消息的会话列表（采集兜底目标）。"""
        with self._lock:
            rows = self.conn.execute(
                "SELECT session_id, MAX(group_name) AS group_name, COUNT(*) AS pending "
                "FROM messages WHERE processed = 0 GROUP BY session_id ORDER BY MAX(ts) ASC"
            ).fetchall()
        return [{"session_id": str(row["session_id"]), "group_name": str(row["group_name"] or ""),
                 "pending": int(row["pending"] or 0)} for row in rows]

    def count_unprocessed(self) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS c FROM messages WHERE processed = 0"
            ).fetchone()
        return int(row["c"] or 0) if row else 0

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

    # ---------- 手动待办：自动补充（默认只补空白；明确改期可覆盖截止时间） ----------
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

    def list_manual_ignored(self, since: str, limit: int = 20) -> list[dict]:
        """最近一段时间内被忽略的手动待办（按忽略时间倒序），用于「同类消息不再重复建」抑制。

        ``since`` 为 ISO 时间下界（= 现在 - MANUAL_SUPPRESS_DAYS 天）；忽略任务不会再被
        改写，``updated_at`` 即忽略时间（取消忽略后再忽略会刷新，窗口随之顺延）。
        """
        marks = ",".join("?" for _ in MANUAL_GROUPS)
        with self._lock:
            rows = self.conn.execute(
                f"SELECT * FROM tasks WHERE status = 'ignored' AND group_name IN ({marks}) "
                "AND updated_at >= ? ORDER BY updated_at DESC LIMIT ?",
                (*MANUAL_GROUPS, str(since or ""), max(1, int(limit))),
            ).fetchall()
        return [self._task_dict(row) for row in rows]

    def supplement_task(self, task_id: str, *, due_at: str | None = None,
                        notes: str | None = None, source_msg_ids: list[str] | None = None,
                        match_reason: str = "",
                        overwrite_due: bool = False) -> tuple[dict | None, list[str]]:
        """给手动待办补字段，默认只补空白：标题 / 备注一律不覆盖。

        overwrite_due=True 时允许用 due_at 覆盖已有截止时间（用于模型明确识别到
        “改到周五/延到下周”这类改期）；不传该参数时与旧行为一致，只补空。
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
            current_due = str(current.get("due_at") or "").strip()
            if due_at and current_due != str(due_at) and (overwrite_due or not current_due):
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
