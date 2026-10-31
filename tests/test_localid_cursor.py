"""localId 游标采集语义。

覆盖：
- 首轮建立游标（最大 localId），之后每轮只扫描游标之后的消息；
- 游标扫描零新增时不重复扫描/入库；
- 「写入晚、业务时间旧」的晚到消息由周期深扫兜底（快速扫描看不到它）；
- localId 重装/回绕（对话换库后从 1 重新开始）时回退时间窗并重设游标；
- window 模式（新置顶回补 / 深扫）不推进 localId 游标。
"""
import asyncio
import time

from app.config import Settings
from app.service import AppService


class FakePagedWcda:
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


def _settings(tmp_path, **overrides) -> Settings:
    data = dict(
        data_source="wcda",
        wcda_base="",
        db_path=tmp_path / "cursor.db",
        app_token="test",
        whitelist_mode="all",
        include_private=True,
        llm_enabled=False,
        quiet_start="00:00",
        quiet_end="00:00",
        wcda_catchup_seconds=0,
        wcda_catchup_lookback_minutes=30,
        wcda_catchup_page_size=50,
        wcda_catchup_max_hours=168,
        wcda_catchup_empty_history_hours=24,
        wcda_catchup_round_budget=0,
        wcda_deep_scan_minutes=60,
        wcda_deep_scan_lookback_minutes=1440,
        identity_filter=True,
    )
    data.update(overrides)
    return Settings(**data)


def _raw(local_id: int, content: str, ts: int, *, rawid: str | None = None) -> dict:
    return {
        "id": rawid or f"raw-{local_id}",
        "localId": local_id,
        "serverId": local_id,
        "createTime": int(ts),
        "senderUsername": "wxid_a",
        "senderDisplayName": "张三",
        "isSent": False,
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


def _session_state(service: AppService, session_id: str) -> dict:
    state = service.db.get_catchup_state(session_id)
    assert state is not None, "采集成功后应写入会话游标"
    return state


def test_first_round_establishes_cursor_then_only_fetches_new_messages(tmp_path):
    """首轮建游标；后续轮只扫描/入库游标之后的消息，零新增时不再扫描老消息。"""
    now = int(time.time())
    session = "room@chatroom"
    sessions = [{"username": session, "name": "项目群", "isGroup": True, "isTop": True}]
    messages = {session: [_raw(i, "哈哈哈哈这也行", now - 100 + i) for i in range(1, 6)]}
    service, fake = _service(tmp_path, sessions, messages)

    first = asyncio.run(service.collect(mode="watermark"))
    assert first["inserted"] == 5
    assert first["processed"] == 5
    state = _session_state(service, session)
    assert int(state["last_local_id"]) == 5
    assert int(state["last_ts"]) >= now - 10

    fake.calls.clear()
    messages[session].extend([
        _raw(6, "明天下午3点开会，记得带材料", now - 5),
        _raw(7, "周五前填写破冰人数统计表", now - 2),
    ])
    second = asyncio.run(service.collect(mode="watermark"))
    assert second["inserted"] == 2
    assert second["scanned"] == 2, "游标之前的老消息不应再被扫描"
    assert second["pages"] == 1, "游标之外只有 2 条新消息，一页就够"
    assert int(_session_state(service, session)["last_local_id"]) == 7

    third = asyncio.run(service.collect(mode="watermark"))
    assert third["inserted"] == 0
    assert third["scanned"] == 0
    assert third["processed"] == 0
    assert int(_session_state(service, session)["last_local_id"]) == 7
    service.db.close()


def test_window_round_does_not_advance_cursor(tmp_path):
    """window 模式（新置顶回补 / 深扫）只扫时间窗，不推进 localId 游标。"""
    now = int(time.time())
    session = "room@chatroom"
    sessions = [{"username": session, "name": "项目群", "isGroup": True, "isTop": True}]
    messages = {session: [_raw(i, "哈哈哈哈这也行", now - 60 + i) for i in range(1, 4)]}
    service, _ = _service(tmp_path, sessions, messages)

    asyncio.run(service.collect(mode="watermark"))
    before = _session_state(service, session)
    assert int(before["last_local_id"]) == 3

    window = asyncio.run(service.collect(minutes=180, mode="window"))
    assert window["inserted"] == 0
    after = _session_state(service, session)
    assert int(after["last_local_id"]) == int(before["last_local_id"])
    assert int(after["last_ts"]) == int(before["last_ts"])
    service.db.close()


def test_late_arrived_old_timestamp_message_is_caught_by_deep_scan(tmp_path):
    """晚到消息（新 localId、旧 createTime）在快速游标扫描深度之外时由深扫兜底捞回。"""
    now = int(time.time())
    session = "room@chatroom"
    sessions = [{"username": session, "name": "项目群", "isGroup": True, "isTop": True}]
    # 80 条最近消息：游标扫描第一页（50 条）全是已处理过的旧消息
    messages = {session: [_raw(i, "哈哈哈哈这也行", now - 3000 + i) for i in range(1, 81)]}
    service, _ = _service(tmp_path, sessions, messages)
    asyncio.run(service.collect(mode="watermark"))
    assert int(_session_state(service, session)["last_local_id"]) == 80

    # 晚到消息：写入顺序最新（localId=200），但业务时间是 2 小时前 → 落在第 2 页
    messages[session].append(_raw(200, "周五前填写破冰人数统计表，记得提交", now - 7200))
    fast = asyncio.run(service.collect(mode="watermark"))
    assert fast["inserted"] == 0, "游标快速扫描只看第一页，扫不到深层的晚到消息"

    service._last_deep_scan_at = 0  # 让深扫到期
    deep = asyncio.run(service._maybe_deep_scan())
    assert deep is not None and deep["deep_scan"] is True
    assert deep["inserted"] == 1, "深扫应按时间窗把晚到消息补进来"
    assert deep["processed"] == 1
    assert service.db.list_tasks("pending"), "晚到消息里的可执行事项应生成待办"
    assert int(_session_state(service, session)["last_local_id"]) == 80, \
        "深扫是 window 模式，不应推进 localId 游标"
    service.db.close()


def test_local_id_reset_falls_back_to_time_window_and_resets_cursor(tmp_path):
    """对话换库导致 localId 从 1 重新开始时，不能卡死：回退时间窗并重设游标。"""
    now = int(time.time())
    session = "room@chatroom"
    sessions = [{"username": session, "name": "项目群", "isGroup": True, "isTop": True}]
    messages = {session: [_raw(i, "哈哈哈哈这也行", now - 1000 + i) for i in range(1, 4)]}
    service, _ = _service(tmp_path, sessions, messages)
    asyncio.run(service.collect(mode="watermark"))
    assert int(_session_state(service, session)["last_local_id"]) == 3
    # 模拟「上一轮采集在 1 小时前」：localId 重装后新消息的 ts 会晚于上轮水位
    service.db.conn.execute(
        "UPDATE catchup_state SET last_ts = ? WHERE session_id = ?", (now - 3600, session))
    service.db.conn.commit()

    # 模拟换库重装：localId 从 1 重新开始，rawid 也换了（新库表名不同）
    messages[session] = [
        _raw(1, "明天下午3点开会，记得带材料", now - 5, rawid="message_9:Msg_x:1"),
        _raw(2, "周五前填写破冰人数统计表", now - 3, rawid="message_9:Msg_x:2"),
    ]
    stats = asyncio.run(service.collect(mode="watermark"))
    assert stats["inserted"] == 2, "localId 失效时应退回时间窗，把新消息采进来"
    assert stats["processed"] == 2
    assert int(_session_state(service, session)["last_local_id"]) == 2, \
        "游标应重设为新库的 localId"
    service.db.close()


def test_deep_scan_window_boundary_is_documented_limit(tmp_path):
    """深扫兜底上限由 WCDA_DEEP_SCAN_LOOKBACK_MINUTES 决定：30h 晚到消息默认 24h 窗口捞不到。"""
    now = int(time.time())
    session = "room@chatroom"
    sessions = [{"username": session, "name": "项目群", "isGroup": True, "isTop": True}]
    # 80 条最近消息占满第一页，游标停在 80
    messages = {session: [_raw(i, "哈哈哈哈这也行", now - 3000 + i) for i in range(1, 81)]}
    service, _ = _service(tmp_path, sessions, messages)
    asyncio.run(service.collect(mode="watermark"))
    assert int(_session_state(service, session)["last_local_id"]) == 80

    # 晚到 30 小时（新 localId、旧 createTime）
    messages[session].append(_raw(200, "周五前填写破冰人数统计表，记得提交", now - 30 * 3600))

    service._last_deep_scan_at = 0
    default_window = asyncio.run(service._maybe_deep_scan())
    assert default_window is not None and default_window["inserted"] == 0, \
        "默认 24h 深扫窗口覆盖不到 30h 前的晚到消息（已文档化的边界）"

    service.settings.wcda_deep_scan_lookback_minutes = 2880  # 48 小时
    service._last_deep_scan_at = 0
    widened = asyncio.run(service._maybe_deep_scan())
    assert widened is not None and widened["inserted"] == 1, "调大深扫窗口后应能捞回"
    service.db.close()
