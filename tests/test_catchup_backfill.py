"""补采增强：长时间离线后按会话水位 + 分页补齐全部未采集消息。"""
import asyncio
import time

from app.config import Settings
from app.db import Database
from app.service import AppService

HEADERS = {"X-Auth-Token": "test-token"}


class FakePagedWcda:
    """模拟 WCDA 分页接口：offset 从最新消息往前计数，页内按 order 排序。"""

    configured = True
    account = "wxid_me"

    def __init__(self, sessions: list[dict], messages: dict[str, list[dict]]):
        self._sessions = sessions
        self._messages = messages
        self.calls: list[tuple[str, int, int]] = []

    async def fetch_sessions(self, limit: int = 500) -> list[dict]:
        return list(self._sessions)

    async def fetch_messages_page(self, username: str, *, limit: int = 500,
                                  offset: int = 0, order: str = "desc") -> dict:
        self.calls.append((username, int(limit), int(offset)))
        rows = sorted(
            self._messages.get(username, []),
            key=lambda row: int(row.get("createTime") or 0),
            reverse=order != "asc",
        )
        page = rows[max(0, int(offset)):max(0, int(offset)) + max(1, int(limit))]
        return {"ok": True, "messages": page, "has_more": int(offset) + len(page) < len(rows)}

    async def fetch_recent_messages(self, username: str, limit: int = 300) -> list[dict]:
        rows = sorted(
            self._messages.get(username, []),
            key=lambda row: int(row.get("createTime") or 0),
            reverse=True,
        )
        return rows[:max(1, int(limit))]


def _settings(tmp_path, **overrides) -> Settings:
    data = dict(
        data_source="wcda",
        wcda_base="",              # 避免单测访问真实 WeChatDataAnalysis
        db_path=tmp_path / "backfill.db",
        app_token="test",
        whitelist_mode="all",
        include_private=True,
        only_new_messages=True,
        llm_enabled=False,
        quiet_start="00:00",
        quiet_end="00:00",
        wcda_catchup_seconds=0,
        wcda_catchup_lookback_minutes=30,
        wcda_catchup_limit=100,
        identity_filter=True,
        wcda_catchup_page_size=100,
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


def _service(tmp_path, sessions, messages, pre_messages=None,
             **overrides) -> tuple[AppService, FakePagedWcda]:
    """pre_messages = 服务启动前已入库的消息（用于模拟「上次采集到哪」）。"""
    settings = _settings(tmp_path, **overrides)
    if pre_messages:
        seed = Database(settings.db_path)
        for item in pre_messages:
            seed.add_message(**item)
            # 强制标记为「启动前」，避免与 started_at 同一秒导致 < 比较失效
            seed.conn.execute("UPDATE messages SET created_at = '2000-01-01T00:00:00' WHERE rawid = ?",
                              (item["rawid"],))
        seed.conn.commit()
        seed.close()
    service = AppService(settings)
    fake = FakePagedWcda(sessions, messages)
    service.wcda = fake
    return service, fake


def test_watermark_backfills_long_offline_gap_and_dedupes(tmp_path):
    """停机 3 天后：从上次水位起分页补齐整个空档，重复补采不重复入库/建任务。"""
    now = int(time.time())
    session = "room@chatroom"
    sessions = [{"username": session, "name": "项目群", "isGroup": True, "isTop": True}]
    start = now - 3 * 86400 + 60
    step = max(1, (3 * 86400 - 120) // 249)
    actionable = {10, 120, 220}
    messages = {session: [
        _raw(i, "周五前填写破冰人数统计表" if i in actionable else "哈哈哈哈这也行",
             ts=start + i * step)
        for i in range(250)
    ]}
    service, fake = _service(tmp_path, sessions, messages, pre_messages=[dict(
        rawid="old-1", session_id=session, group_name="项目群",
        sender="张三", content="停机前的旧消息", ts=start - 60,
    )])

    stats = asyncio.run(service.catchup(mode="watermark", minutes=7 * 24 * 60))
    assert stats["ok"] is True
    assert stats["mode"] == "watermark"
    assert stats["inserted"] == 250, "整个空档的消息都要补进来"
    assert stats["pages"] >= 3, "250 条 / 每页 100 条应分页读取"
    assert stats["tasks_created"] >= 1
    assert stats["scanned"] >= 250
    # 分页 offset 必须递增回溯
    offsets = [call[2] for call in fake.calls]
    assert offsets == sorted(offsets) and len(set(offsets)) == len(offsets)
    state = service.db.get_catchup_state(session)
    assert state is not None and int(state["last_ts"]) >= now - 10

    again = asyncio.run(service.catchup(mode="watermark", minutes=7 * 24 * 60))
    assert again["inserted"] == 0, "同一批消息不应重复入库"
    assert again["tasks_created"] == 0, "同一批消息不应重复建待办"
    service.db.close()


def test_watermark_without_history_uses_one_day_window(tmp_path):
    """没有水位且启动前没有该会话历史：自动回看 1 天（不是 30 分钟，也不是 7 天）。"""
    now = int(time.time())
    session = "room@chatroom"
    sessions = [{"username": session, "name": "项目群", "isGroup": True, "isTop": True}]
    messages = {session: [
        *[_raw(i, "周五前填写破冰人数统计表", ts=now - 2 * 86400 + i) for i in range(10)],
        *[_raw(100 + i, "周五前填写破冰人数统计表", ts=now - 3600 + i) for i in range(5)],
    ]}
    service, _ = _service(tmp_path, sessions, messages)

    stats = asyncio.run(service.catchup(mode="watermark", minutes=7 * 24 * 60))
    assert stats["inserted"] == 5, "1 天内的消息应补回，2 天前的历史不自动拉"
    assert stats["minutes"] == 7 * 24 * 60
    count = service.db.conn.execute("SELECT COUNT(*) c FROM messages").fetchone()["c"]
    assert count == 5
    state = service.db.get_catchup_state(session)
    assert state is not None and int(state["last_ts"]) >= now - 10
    service.db.close()


def test_watermark_respects_max_hours_floor(tmp_path):
    """水位/库里最新消息很旧：最多只回看 WCDA_CATCHUP_MAX_HOURS。"""
    now = int(time.time())
    session = "room@chatroom"
    sessions = [{"username": session, "name": "项目群", "isGroup": True, "isTop": True}]
    messages = {session: [_raw(i, "周五前填写破冰人数统计表", ts=now - 10 * 86400 + i)
                          for i in range(10)]}
    service, _ = _service(tmp_path, sessions, messages, wcda_catchup_max_hours=24, pre_messages=[dict(
        rawid="old-1", session_id=session, group_name="项目群",
        sender="张三", content="10 天前的旧消息", ts=now - 10 * 86400,
    )])

    stats = asyncio.run(service.catchup(mode="watermark"))
    assert stats["inserted"] == 0
    assert stats["minutes"] == 24 * 60
    service.db.close()


def test_round_budget_resumes_until_all_collected(tmp_path):
    """单轮预算不足时按水位分批推进，多轮后全部采集且不重复建任务。"""
    now = int(time.time())
    session = "room@chatroom"
    sessions = [{"username": session, "name": "项目群", "isGroup": True, "isTop": True}]
    start = now - 25 * 60
    messages = {session: [
        _raw(i, "周五前填写破冰人数统计表" if i == 0 else "哈哈哈哈这也行",
             ts=start + i * 4)
        for i in range(300)
    ]}
    service, _ = _service(tmp_path, sessions, messages,
                          wcda_catchup_round_budget=100, wcda_catchup_page_size=100)

    inserted = []
    for _ in range(6):
        stats = asyncio.run(service.catchup(mode="watermark"))
        inserted.append(stats["inserted"])
        if sum(inserted) >= 300:
            break
    assert sum(inserted) == 300, f"应分多轮补齐 300 条，实际 {inserted}"
    assert service.db.list_tasks("pending"), "最老的可执行消息应生成待办"
    assert len(service.db.list_tasks("pending")) == 1, "重复轮次不应重复建任务"
    state = service.db.get_catchup_state(session)
    assert state is not None and int(state["last_ts"]) >= now - 10
    service.db.close()


def test_window_round_does_not_advance_watermark(tmp_path):
    """P1 回归：积压续补期间点「立即补采」（window）不能把水位推到 now，否则剩余漏采。"""
    now = int(time.time())
    session = "room@chatroom"
    sessions = [{"username": session, "name": "项目群", "isGroup": True, "isTop": True}]
    start = now - 3 * 86400 + 60
    step = max(1, (3 * 86400 - 3600 - 120) // 299)
    messages = {session: [
        _raw(i, "周五前填写破冰人数统计表" if i == 0 else "哈哈哈哈这也行",
             ts=start + i * step)
        for i in range(300)
    ]}
    service, _ = _service(tmp_path, sessions, messages,
                          wcda_catchup_round_budget=100, wcda_catchup_page_size=100,
                          pre_messages=[dict(
                              rawid="old-1", session_id=session, group_name="项目群",
                              sender="张三", content="停机前的旧消息", ts=start - 60,
                          )])

    first = asyncio.run(service.catchup(mode="watermark", minutes=7 * 24 * 60))
    assert first["inserted"] > 0 and first["budget_exhausted"] is True
    state_before = service.db.get_catchup_state(session)
    assert state_before is not None

    # 窗口内没有消息，但旧逻辑会在这里把水位写成 now
    window = asyncio.run(service.catchup(minutes=30, mode="window"))
    assert window["inserted"] == 0
    state_after = service.db.get_catchup_state(session)
    assert state_after is not None and int(state_after["last_ts"]) == int(state_before["last_ts"]), \
        "window 模式不应推进 catchup_state"

    total = first["inserted"]
    for _ in range(6):
        stats = asyncio.run(service.catchup(mode="watermark", minutes=7 * 24 * 60))
        total += stats["inserted"]
        if total >= 300:
            break
    assert total == 300, f"水位被 window 污染后剩余消息应仍能补齐，实际 {total}"
    assert len(service.db.list_tasks("pending")) == 1
    service.db.close()


def test_first_watermark_ignores_post_start_realtime_writes(tmp_path):
    """P2 回归：首次接管用「启动前」消息最大时间，启动后实时流写入不能掩盖离线积压。"""
    now = int(time.time())
    session = "room@chatroom"
    sessions = [{"username": session, "name": "项目群", "isGroup": True, "isTop": True}]
    start = now - 3 * 86400 + 60
    step = max(1, (3 * 86400 - 3600 - 120) // 249)
    messages = {session: [_raw(i, "哈哈哈哈这也行", ts=start + i * step) for i in range(250)]}
    service, _ = _service(tmp_path, sessions, messages, pre_messages=[dict(
        rawid="old-1", session_id=session, group_name="项目群",
        sender="张三", content="停机前的旧消息", ts=start - 60,
    )])
    # 启动 8 秒内实时流先写一条「最新消息」到库里
    service.db.add_message(rawid="realtime-1", session_id=session, group_name="项目群",
                           sender="张三", content="刚收到的新消息", ts=now)

    stats = asyncio.run(service.catchup(mode="watermark", minutes=7 * 24 * 60))
    assert stats["inserted"] == 250, "离线 3 天的积压应全部补回，不能被启动后的实时消息跳过"
    service.db.close()


def test_existing_message_ids_chunks_large_batches(tmp_path):
    db = Database(tmp_path / "chunk.db")
    db.add_message(rawid="keep-1", session_id="s", group_name="g", sender="u", content="c", ts=1)
    ids = [f"missing-{i}" for i in range(1200)] + ["keep-1"]
    assert db.existing_message_ids(ids) == {"keep-1"}
    db.close()


def test_catchup_state_endpoint(client):
    assert client.get("/api/catchup/state").status_code == 401
    body = client.get("/api/catchup/state", headers=HEADERS).json()
    # conftest 的数据源是 demo、补采关闭 → mode=off
    assert body["mode"] == "off"
    assert body["empty_history_hours"] == 24
    assert "watermark_sessions" in body and "states" in body
