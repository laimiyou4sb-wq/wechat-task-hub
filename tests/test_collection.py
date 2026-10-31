"""采集（唯一采集方法）语义回归。

覆盖：
- 入库 processed=0、处理成功置 1；
- 每轮按会话一次处理（不再按 80 条切批）；
- 处理失败的消息保留 processed=0，下一轮从 DB 兜底重试；
- existing_message_ids 只跳过 processed=1；
- 迁移只把历史存量标记一次，新消息仍从未处理开始；
- health 的采集状态由采集轮驱动。
"""
import asyncio
import time

from app.config import Settings
from app.db import Database
from app.service import AppService


class FakePagedWcda:
    configured = True
    account = "wxid_me"

    def __init__(self, sessions: list[dict], messages: dict[str, list[dict]],
                 alive: bool = True):
        self._sessions = sessions
        self._messages = messages
        self.alive = alive

    async def fetch_sessions(self, limit: int = 500) -> list[dict]:
        return list(self._sessions)

    async def check_health(self) -> bool:
        return self.alive

    async def fetch_messages_page(self, username: str, *, limit: int = 500,
                                  offset: int = 0, order: str = "desc") -> dict:
        rows = sorted(
            self._messages.get(username, []),
            key=lambda row: int(row.get("createTime") or 0),
            reverse=order != "asc",
        )
        page = rows[max(0, int(offset)):max(0, int(offset)) + max(1, int(limit))]
        return {"ok": True, "messages": page,
                "has_more": int(offset) + len(page) < len(rows)}


def _settings(tmp_path, **overrides) -> Settings:
    data = dict(
        data_source="wcda",
        wcda_base="",              # 避免单测访问真实 WeChatDataAnalysis
        db_path=tmp_path / "collect.db",
        app_token="test",
        whitelist_mode="all",
        include_private=True,
        llm_enabled=False,
        quiet_start="00:00",
        quiet_end="00:00",
        wcda_catchup_seconds=0,
        wcda_catchup_page_size=100,
        identity_filter=True,
    )
    data.update(overrides)
    return Settings(**data)


def _raw(local_id: int, content: str, *, ts: int | None = None,
         sender: str = "张三", sent: bool = False) -> dict:
    return {
        "id": f"raw-{local_id}",
        "localId": local_id,
        "serverId": local_id,
        "createTime": int(ts if ts is not None else time.time()),
        "senderUsername": "wxid_a",
        "senderDisplayName": sender,
        "isSent": sent,
        "content": content,
        "type": 1,
        "renderType": "text",
        "atUsernames": [],
        "quoteUsername": "",
    }


def _service(tmp_path, sessions, messages, **overrides) -> tuple[AppService, FakePagedWcda]:
    service = AppService(_settings(tmp_path, **overrides))
    fake = FakePagedWcda(sessions, messages)
    service.wcda = fake
    return service, fake


def _processed_counts(service) -> dict[int, int]:
    rows = service.db.conn.execute(
        "SELECT processed, COUNT(*) AS c FROM messages GROUP BY processed"
    ).fetchall()
    return {int(row["processed"]): int(row["c"]) for row in rows}


def test_collect_marks_messages_processed_after_success(tmp_path):
    """入库即 processed=0；会话处理成功后置 1，待处理归零。"""
    now = int(time.time())
    session = "room@chatroom"
    sessions = [{"username": session, "name": "项目群", "isGroup": True, "isTop": True}]
    messages = {session: [
        _raw(1, "周五前填写破冰人数统计表，记得提交", ts=now - 60),
        _raw(2, "明早8点去上课", ts=now - 30),
        _raw(3, "哈哈哈哈这也行", ts=now - 10),
    ]}
    service, _ = _service(tmp_path, sessions, messages)

    stats = asyncio.run(service.collect(minutes=60, mode="window"))
    assert stats["ok"] is True
    assert stats["inserted"] == 3
    assert stats["processed"] == 3, "每轮拿到消息后应按会话一次处理完"
    assert stats["pending"] == 0
    assert _processed_counts(service) == {1: 3}
    assert service.db.list_tasks("pending"), "可执行消息应生成待办"
    service.db.close()


def test_collect_retries_unprocessed_after_processing_failure(tmp_path):
    """整批失败 → 逐条隔离：能处理的消息立即处理，失败的那条下一轮重试。"""
    now = int(time.time())
    session = "room@chatroom"
    sessions = [{"username": session, "name": "项目群", "isGroup": True, "isTop": True}]
    messages = {session: [
        _raw(1, "周五前填写破冰人数统计表", ts=now - 60),
        _raw(2, "明天下午3点开会", ts=now - 30),
    ]}
    service, _ = _service(tmp_path, sessions, messages)

    original = service.process_batch
    calls = {"n": 0}

    async def flaky(batch):
        calls["n"] += 1
        if any(str(m.get("rawid")) == "raw-1" for m in batch):
            raise RuntimeError("fake llm failure")
        return await original(batch)

    service.process_batch = flaky
    first = asyncio.run(service.collect(minutes=60, mode="window"))
    assert first["inserted"] == 2
    # 整批失败后逐条隔离：第 2 条能单独处理成功，不应被第 1 条拖住
    assert first["processed"] == 1, "隔离重试后能处理的消息应立即处理"
    assert first["pending"] == 1
    assert _processed_counts(service) == {0: 1, 1: 1}
    row = service.db.conn.execute(
        "SELECT attempts, last_error FROM messages WHERE rawid = 'raw-1'").fetchone()
    assert int(row["attempts"]) == 1 and row["last_error"]

    second = asyncio.run(service.collect(minutes=60, mode="window"))
    assert second["inserted"] == 0, "同一条消息不应重复入库"
    assert second["processed"] == 0
    assert second["pending"] == 1
    row = service.db.conn.execute(
        "SELECT attempts FROM messages WHERE rawid = 'raw-1'").fetchone()
    assert int(row["attempts"]) == 2, "失败消息每轮重试一次"

    # 故障恢复后（未到重试上限）下一轮应正常处理并清空失败计数
    service.process_batch = original
    third = asyncio.run(service.collect(minutes=60, mode="window"))
    assert third["processed"] == 1
    assert third["pending"] == 0
    assert _processed_counts(service) == {1: 2}
    assert len(service.db.list_tasks("pending")) == 2, "重试成功后应生成待办"
    row = service.db.conn.execute(
        "SELECT attempts, last_error FROM messages WHERE rawid = 'raw-1'").fetchone()
    assert int(row["attempts"]) == 0 and not row["last_error"]
    service.db.close()


def test_collect_processes_each_session_in_one_call(tmp_path):
    """每轮按会话一次处理：150 条同会话消息只调用一次，不再按 80 条切批。"""
    now = int(time.time())
    session = "room@chatroom"
    sessions = [{"username": session, "name": "项目群", "isGroup": True, "isTop": True}]
    messages = {session: [
        _raw(i, "周五前填写破冰人数统计表" if i % 10 == 0 else "哈哈哈哈这也行",
             ts=now - 600 + i)
        for i in range(150)
    ]}
    service, _ = _service(tmp_path, sessions, messages)
    assert not hasattr(service, "_catchup_chunks"), "不应再存在 80 条切批实现"

    calls: list[int] = []
    original = service._process_session_batch

    async def counting(session_id, batch):
        calls.append(len(batch))
        return await original(session_id, batch)

    service._process_session_batch = counting
    stats = asyncio.run(service.collect(minutes=60, mode="window"))
    assert stats["inserted"] == 150
    assert stats["processed"] == 150
    assert calls == [150], f"每个会话每轮只应处理一次，实际 {calls}"
    service.db.close()


def test_collect_fallback_processes_unprocessed_without_refetch(tmp_path):
    """已入库未处理的消息，即使会话不在监控范围/本轮无需拉取，也要兜底处理。"""
    session = "old@chatroom"
    service = AppService(_settings(tmp_path))
    service.wcda = FakePagedWcda([], {})  # 没有会话，也没有可拉取消息
    service.db.add_message(
        rawid="stored-1", session_id=session, group_name="旧群", sender="张三",
        content="明天下午3点开会，记得带材料", ts=int(time.time()) - 60,
    )

    stats = asyncio.run(service.collect(minutes=60, mode="window"))
    assert stats["ok"] is True
    assert stats["processed"] == 1
    assert stats["pending"] == 0
    assert _processed_counts(service) == {1: 1}
    assert service.db.list_tasks("pending"), "兜底重试应补建待办"
    service.db.close()


def test_health_collection_is_driven_by_collect_round(tmp_path):
    """health.connected / last_collect_at / collection.last 由采集轮写入。"""
    now = int(time.time())
    session = "room@chatroom"
    sessions = [{"username": session, "name": "项目群", "isGroup": True, "isTop": True}]
    messages = {session: [_raw(1, "周五前填写破冰人数统计表", ts=now - 60)]}
    service, _ = _service(tmp_path, sessions, messages)

    before = service.health()
    assert before["source"]["connected"] is None, "首轮采集前不应误报已连接"
    assert before["collection"] == before["catchup"]

    stats = asyncio.run(service.collect(minutes=60, mode="window"))
    health = service.health()
    assert health["source"]["connected"] is True
    assert health["source"]["last_collect_at"] == stats["at"]
    assert health["collection"]["last"]["inserted"] == 1
    assert health["collection"]["last"]["processed"] == 1
    assert health["events"]["pending_messages"] == 0
    assert health["events"]["pending_sessions"] == 0
    service.db.close()


def test_collect_health_handles_unreachable_wcda(tmp_path):
    """没有监控会话且 WCDA 不在线时，采集轮应把 connected 置 False 并给出原因。"""
    service = AppService(_settings(tmp_path))
    fake = FakePagedWcda([], {}, alive=False)
    service.wcda = fake
    stats = asyncio.run(service.collect(mode="watermark"))
    assert stats["ok"] is False
    assert "未连接" in stats["reason"]
    health = service.health()
    assert health["source"]["connected"] is False
    assert health["source"]["last_error"]
    # 失败轮也要写进采集快照（last 与 last_collect_at 时间一致），设置页才能显示「上次采集失败」
    assert health["collection"]["last"]["ok"] is False
    assert health["source"]["last_collect_at"] == stats["at"]
    service.db.close()


def test_collect_empty_reachable_round_marks_connected(tmp_path):
    """数据源可达但没有待采会话：视为成功的空轮，connected=true 而不是一直等待首轮。"""
    service = AppService(_settings(tmp_path))
    service.wcda = FakePagedWcda([], {}, alive=True)
    stats = asyncio.run(service.collect(mode="watermark"))
    assert stats["ok"] is True and stats["sessions"] == 0
    health = service.health()
    assert health["source"]["connected"] is True
    assert health["source"]["last_collect_at"] == stats["at"]
    assert health["collection"]["last"]["ok"] is True
    service.db.close()


def test_collect_replays_pending_even_when_wcda_unreachable(tmp_path):
    """数据源不可达时，已入库未处理的消息仍应在本地兜底处理，不能等数据源恢复。"""
    service = AppService(_settings(tmp_path))
    service.wcda = FakePagedWcda([], {}, alive=False)
    service.db.add_message(
        rawid="stored-down-1", session_id="old@chatroom", group_name="旧群",
        sender="张三", content="明天下午3点开会，记得带材料", ts=int(time.time()) - 60,
    )
    stats = asyncio.run(service.collect(mode="watermark"))
    # 数据源不可达仍如实标记失败，但本地未处理消息已重放
    assert stats["ok"] is False and "未连接" in stats["reason"]
    assert stats["processed"] == 1
    assert service.db.count_unprocessed() == 0
    assert _processed_counts(service) == {1: 1}
    assert service.db.list_tasks("pending"), "数据源不可达也应从 DB 兜底补建待办"
    health = service.health()
    assert health["source"]["connected"] is False
    assert health["collection"]["last"]["processed"] == 1
    service.db.close()


def test_legacy_messages_migrated_to_processed_once(tmp_path):
    """升级迁移只把历史存量标记一次；之后新入库消息仍从未处理开始。"""
    path = tmp_path / "legacy.db"
    db = Database(path)
    db.add_message(rawid="legacy-1", session_id="s", group_name="g",
                   sender="u", content="旧消息", ts=1)
    # 模拟旧版数据库：没有迁移标记，processed 列全部为 0
    db.conn.execute("DELETE FROM meta WHERE k = 'messages_processed_v1'")
    db.conn.execute("UPDATE messages SET processed = 0")
    db.conn.commit()
    db.close()

    upgraded = Database(path)
    row = upgraded.conn.execute(
        "SELECT processed FROM messages WHERE rawid = 'legacy-1'").fetchone()
    assert int(row["processed"]) == 1, "历史消息升级后视为已处理，避免全量重跑"
    upgraded.add_message(rawid="new-1", session_id="s", group_name="g",
                         sender="u", content="升级后的新消息", ts=2)
    upgraded.close()

    reopened = Database(path)
    new_row = reopened.conn.execute(
        "SELECT processed FROM messages WHERE rawid = 'new-1'").fetchone()
    assert int(new_row["processed"]) == 0, "迁移标记只生效一次，不能反复覆盖新消息"
    reopened.close()
