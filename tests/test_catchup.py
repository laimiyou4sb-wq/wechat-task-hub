"""wcda 采集单测：补 SSE 漏消息 + 指定会话回补历史 + 去重。"""
import asyncio
import time

from app.config import Settings
from app.service import AppService


class FakeWcda:
    configured = True

    def __init__(self, sessions: list[dict], messages: dict[str, list[dict]]):
        self.account = "wxid_me"
        self._sessions = sessions
        self._messages = messages
        self.fetched: list[str] = []

    async def fetch_sessions(self, limit: int = 500) -> list[dict]:
        return list(self._sessions)

    async def fetch_recent_messages(self, username: str, limit: int = 300) -> list[dict]:
        self.fetched.append(username)
        return list(self._messages.get(username, []))


def _settings(tmp_path, **overrides) -> Settings:
    data = dict(
        data_source="wcda",
        wcda_base="",              # 避免单测访问真实 WeChatDataAnalysis
        db_path=tmp_path / "catchup.db",
        app_token="test",
        whitelist_mode="pinned",
        include_private=True,
        llm_enabled=False,
        quiet_start="00:00",
        quiet_end="00:00",
        wcda_catchup_seconds=0,
        wcda_catchup_lookback_minutes=60,
        wcda_catchup_limit=100,
        identity_filter=True,
    )
    data.update(overrides)
    return Settings(**data)


def _raw(local_id: int, content: str, *, ts: float | None = None,
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


def _service(tmp_path, sessions, messages) -> tuple[AppService, FakeWcda]:
    service = AppService(_settings(tmp_path))
    fake = FakeWcda(sessions, messages)
    service.wcda = fake
    return service, fake


def test_catchup_ingests_missed_messages_and_dedupes(tmp_path):
    sessions = [{"username": "room@chatroom", "name": "项目群", "isGroup": True, "isTop": True}]
    messages = {"room@chatroom": [
        _raw(1, "周五前填写破冰人数统计表，记得提交"),
        _raw(2, "哈哈哈哈这也行"),
        _raw(3, "明早8点去上课"),
    ]}
    service, fake = _service(tmp_path, sessions, messages)

    stats = asyncio.run(service.catchup(minutes=180))
    assert stats["ok"] is True
    assert stats["inserted"] == 3
    assert stats["sessions"] == 1
    assert stats["tasks_created"] >= 1
    assert "room@chatroom" in fake.fetched
    assert service.db.list_tasks("pending"), "采集后的可执行消息应生成待办"

    again = asyncio.run(service.catchup(minutes=180))
    assert again["inserted"] == 0, "同一批消息不应重复入库/重复建待办"
    service.db.close()


def test_catchup_respects_lookback_window(tmp_path):
    now = time.time()
    sessions = [{"username": "room@chatroom", "name": "项目群", "isGroup": True, "isTop": True}]
    messages = {"room@chatroom": [
        _raw(1, "周五前填写破冰人数统计表", ts=now - 2 * 86400),
        _raw(2, "明天填表", ts=now - 60),
    ]}
    service, _ = _service(tmp_path, sessions, messages)
    stats = asyncio.run(service.catchup(minutes=60))
    assert stats["inserted"] == 1
    service.db.close()


def test_catchup_explicit_session_can_backfill_unmonitored_group(tmp_path):
    """未加入监控的群，也可以通过显式 session_ids 回补（用户手动操作）。"""
    session = "49675822102@chatroom"
    sessions = [{"username": session, "name": "项目群", "isGroup": True, "isTop": False}]
    messages = {session: [
        _raw(1, "11月1日科创破冰活动人数统计"),
        _raw(2, "我们最终决定11月1日下午两点半开始破冰，有空的话大家尽量来参与"),
    ]}
    service, _ = _service(tmp_path, sessions, messages)
    stats = asyncio.run(service.catchup(minutes=1440, session_ids=[session]))
    assert stats["ok"] is True
    assert stats["inserted"] == 2
    assert stats["tasks_created"] >= 1
    service.db.close()


def test_explicit_catchup_reprocesses_messages_stored_but_unprocessed(tmp_path):
    """消息可能已入库（当时群不在监控范围），但没有生成待办：显式回补要重新提取。"""
    session = "49675822102@chatroom"
    sessions = [{"username": session, "name": "项目群", "isGroup": True, "isTop": False}]
    messages = {session: [_raw(1, "11月1日科创破冰活动人数统计")]}
    service, _ = _service(tmp_path, sessions, messages)
    # 模拟上一次只入库、未处理
    service.db.add_message(rawid="raw-1", session_id=session, group_name="项目群",
                           sender="25机电金正豪", content="11月1日科创破冰活动人数统计",
                           ts=int(time.time()))
    stats = asyncio.run(service.catchup(minutes=1440, session_ids=[session]))
    assert stats["inserted"] == 0
    assert stats["tasks_created"] >= 1
    service.db.close()


def test_catchup_skips_sent_messages_and_noise(tmp_path):
    sessions = [{"username": "room@chatroom", "name": "项目群", "isGroup": True, "isTop": True}]
    messages = {"room@chatroom": [
        _raw(1, "我自己发的一条", sent=True),
        _raw(2, "[图片]"),
        _raw(3, "周五前填写破冰人数统计表"),
    ]}
    service, _ = _service(tmp_path, sessions, messages)
    stats = asyncio.run(service.catchup(minutes=180))
    # 「自己发的」不入库；[图片] 会入库但后续噪音过滤不会建待办
    assert stats["inserted"] == 2
    assert stats["tasks_created"] == 1
    service.db.close()


def test_pin_changes_update_monitoring_scope(tmp_path):
    """监控范围必须跟着置顶一起变：新置顶 → 立即纳入，取消置顶 → 移出。"""
    sessions = [
        {"username": "a@chatroom", "name": "A群", "isGroup": True, "isTop": True},
        {"username": "b@chatroom", "name": "B群", "isGroup": True, "isTop": False},
    ]
    service = AppService(_settings(tmp_path, whitelist_mode="pinned"))
    fake = FakeWcda(sessions, {})
    service.wcda = fake

    added = asyncio.run(service.refresh_pinned())
    # 第一次同步只作为基线，不把已有置顶当“新置顶”回补
    assert added == []
    assert service.pinned_sessions == {"a@chatroom"}
    assert service._message_allowed({"session_id": "a@chatroom", "group_name": "A群", "is_group": True})
    assert not service._message_allowed({"session_id": "b@chatroom", "group_name": "B群", "is_group": True})

    # 用户在微信里改成置顶 B、取消 A
    fake._sessions = [
        {"username": "a@chatroom", "name": "A群", "isGroup": True, "isTop": False},
        {"username": "b@chatroom", "name": "B群", "isGroup": True, "isTop": True},
    ]
    added = asyncio.run(service.refresh_pinned())
    assert added == ["b@chatroom"]
    assert service.pinned_sessions == {"b@chatroom"}
    assert service._message_allowed({"session_id": "b@chatroom", "group_name": "B群", "is_group": True})
    assert not service._message_allowed({"session_id": "a@chatroom", "group_name": "A群", "is_group": True})
    service.db.close()


def test_monitored_sessions_follow_pin_changes(tmp_path):
    sessions = [
        {"username": "a@chatroom", "name": "A群", "isGroup": True, "isTop": True},
        {"username": "b@chatroom", "name": "B群", "isGroup": True, "isTop": True},
    ]
    service = AppService(_settings(tmp_path, whitelist_mode="pinned"))
    fake = FakeWcda(sessions, {})
    service.wcda = fake
    targets, error = asyncio.run(service._monitored_sessions())
    assert error is None
    assert {item["session_id"] for item in targets} == {"a@chatroom", "b@chatroom"}

    fake._sessions = [{"username": "a@chatroom", "name": "A群", "isGroup": True, "isTop": False}]
    targets, error = asyncio.run(service._monitored_sessions())
    assert error is None
    assert {item["session_id"] for item in targets} == set()
    service.db.close()
