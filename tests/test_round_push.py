"""同轮合并推送：一次采集轮里多个会话/多条消息产生的待办只推一条通知。"""
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

    async def fetch_sessions(self, limit: int = 500) -> list[dict]:
        return list(self._sessions)

    async def fetch_messages_page(self, username: str, *, limit: int = 500,
                                  offset: int = 0, order: str = "desc") -> dict:
        rows = sorted(
            self._messages.get(username, []),
            key=lambda row: int(row.get("createTime") or 0),
            reverse=order != "asc",
        )
        page = rows[max(0, int(offset)):max(0, int(offset)) + max(1, int(limit))]
        return {"ok": True, "messages": page, "has_more": int(offset) + len(page) < len(rows)}


def _settings(tmp_path) -> Settings:
    return Settings(
        data_source="wcda",
        wcda_base="",
        db_path=tmp_path / "push.db",
        app_token="test",
        whitelist_mode="all",
        include_private=True,
        llm_enabled=False,
        quiet_start="00:00",
        quiet_end="00:00",
        wcda_catchup_seconds=0,
        wcda_catchup_page_size=100,
        wcda_catchup_round_budget=0,
        wcda_deep_scan_minutes=0,
        identity_filter=True,
    )


def _raw(local_id: int, content: str, ts: int, sender: str = "张三") -> dict:
    return {
        "id": f"push-{local_id}",
        "localId": local_id,
        "serverId": local_id,
        "createTime": int(ts),
        "senderUsername": "wxid_a",
        "senderDisplayName": sender,
        "isSent": False,
        "content": content,
        "type": 1,
        "renderType": "text",
        "atUsernames": [],
        "quoteUsername": "",
    }


def test_same_round_pushes_one_merged_notification(tmp_path):
    now = int(time.time())
    sessions = [
        {"username": "a@chatroom", "name": "项目群", "isGroup": True, "isTop": True},
        {"username": "b@chatroom", "name": "课程群", "isGroup": True, "isTop": True},
    ]
    messages = {
        "a@chatroom": [_raw(1, "周五前填写破冰人数统计表，记得提交", now - 60)],
        "b@chatroom": [_raw(2, "明天下午3点开会，记得带材料", now - 30)],
    }
    service = AppService(_settings(tmp_path))
    service.wcda = FakePagedWcda(sessions, messages)
    calls: list[dict] = []

    async def fake_notify(title, body, priority="normal", *, bypass_quiet=False):
        calls.append({"title": title, "body": body, "priority": priority})
        return []

    service._notify = fake_notify

    stats = asyncio.run(service.collect(mode="watermark"))
    assert stats["tasks_created"] == 2, "两个会话各应生成一项待办"
    assert len(calls) == 1, f"同轮应只推一条，实际 {[call['title'] for call in calls]}"
    merged = calls[0]
    assert "微信待办 +2" in merged["title"]
    assert "项目群" in merged["body"] and "课程群" in merged["body"], \
        "合并推送应包含本轮所有会话的待办"

    # 轮内没有新待办时不应额外推送
    again = asyncio.run(service.collect(mode="watermark"))
    assert again["tasks_created"] == 0
    assert len(calls) == 1

    # 采集轮之外的即时推送仍保持原行为（逐次推送）
    asyncio.run(service.push_new_tasks(
        [{"title": "手动补的待办", "group_name": "手动添加", "priority": "normal"}], ""))
    assert len(calls) == 2
    assert "微信待办 +1" in calls[1]["title"]
    service.db.close()


def test_concurrent_collect_rounds_are_serialized(tmp_path):
    """并发采集（周期轮 vs 置顶回补）被锁串行化：取数不交错、推送收集器不互相覆盖。"""
    now = int(time.time())
    sessions = [
        {"username": "a@chatroom", "name": "项目群", "isGroup": True, "isTop": True},
        {"username": "b@chatroom", "name": "课程群", "isGroup": True, "isTop": True},
    ]
    messages = {
        "a@chatroom": [_raw(1, "周五前填写破冰人数统计表，记得提交", now - 40)],
        "b@chatroom": [_raw(2, "明天下午3点开会，记得带材料", now - 20)],
    }

    class SlowWcda(FakePagedWcda):
        def __init__(self, sessions, messages):
            super().__init__(sessions, messages)
            self.active = 0
            self.max_active = 0

        async def fetch_messages_page(self, username, *, limit=500, offset=0, order="desc"):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                await asyncio.sleep(0.01)
                return await super().fetch_messages_page(
                    username, limit=limit, offset=offset, order=order)
            finally:
                self.active -= 1

    service = AppService(_settings(tmp_path))
    fake = SlowWcda(sessions, messages)
    service.wcda = fake
    calls: list[dict] = []

    async def fake_notify(title, body, priority="normal", *, bypass_quiet=False):
        calls.append({"title": title, "body": body})
        return []

    service._notify = fake_notify

    async def run_two_rounds():
        return await asyncio.gather(
            service.collect(mode="watermark"),
            service.collect(mode="watermark"),
        )

    first, second = asyncio.run(run_two_rounds())
    assert fake.max_active == 1, "第二轮的取数不能与第一轮交错（应被 _collect_lock 串行化）"
    assert sorted([first["processed"], second["processed"]]) == [0, 2]
    assert len(calls) == 1, "两轮并发也只应推送一条合并通知（后一轮没有新待办）"
    assert "微信待办 +2" in calls[0]["title"]
    assert len(service.db.list_tasks("pending")) == 2

    service.db.close()
