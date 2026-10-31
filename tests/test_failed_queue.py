"""失败重试 / 失败队列语义。

覆盖：
- 整批处理失败后按条隔离：能处理的消息立即处理，毒消息单独累计 attempts；
- attempts 达到 MESSAGE_MAX_ATTEMPTS → processed=2（失败队列）+ 告警推送；
- 超限消息不再阻塞同会话后续消息（新消息照常处理）；
- 失败队列 API / health 暴露 attempts、last_error，并可一键重试回待处理。
"""
import asyncio
import time

from app.config import Settings
from app.service import AppService

HEADERS = {"X-Auth-Token": "test-token"}


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


def _settings(tmp_path, **overrides) -> Settings:
    data = dict(
        data_source="wcda",
        wcda_base="",
        db_path=tmp_path / "failed.db",
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
        message_max_attempts=2,
        identity_filter=True,
    )
    data.update(overrides)
    return Settings(**data)


def _raw(local_id: int, content: str, ts: int) -> dict:
    return {
        "id": f"raw-{local_id}",
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


def _service(tmp_path) -> tuple[AppService, FakePagedWcda]:
    session = "room@chatroom"
    now = int(time.time())
    sessions = [{"username": session, "name": "项目群", "isGroup": True, "isTop": True}]
    messages = {session: [
        _raw(1, "周五前填写破冰人数统计表，记得提交", now - 60),
        _raw(2, "这条消息会让处理一直失败", now - 50),
        _raw(3, "明天下午3点开会，记得带材料", now - 40),
    ]}
    service = AppService(_settings(tmp_path))
    fake = FakePagedWcda(sessions, messages)
    service.wcda = fake

    original = service.process_batch

    async def poison(batch):
        if any(str(item.get("rawid")) == "raw-2" for item in batch):
            raise RuntimeError("poison message")
        return await original(batch)

    service.process_batch = poison
    return service, fake


def _row(service: AppService, rawid: str):
    return service.db.conn.execute(
        "SELECT processed, attempts, last_error, failed_at FROM messages WHERE rawid = ?",
        (rawid,)).fetchone()


def test_poison_message_is_isolated_then_dead_lettered_with_alert(tmp_path):
    service, fake = _service(tmp_path)
    alerts: list[dict] = []

    async def fake_notify(title, body, priority="normal", *, bypass_quiet=False):
        alerts.append({"title": title, "body": body, "priority": priority,
                       "bypass_quiet": bypass_quiet})
        return []

    service._notify = fake_notify

    # 第 1 轮：整批失败 → 逐条隔离，只有毒消息失败
    first = asyncio.run(service.collect(mode="watermark"))
    assert first["inserted"] == 3
    assert first["processed"] == 2, "整批失败后，可处理的消息应立即处理（不被毒消息拖住）"
    assert first["pending"] == 1
    assert first["dead_lettered"] == 0
    row = _row(service, "raw-2")
    assert int(row["processed"]) == 0
    assert int(row["attempts"]) == 1
    assert "poison message" in row["last_error"]

    # 第 2 轮：再失败一次 → 达到上限 2 → 落失败队列 + 告警
    second = asyncio.run(service.collect(mode="watermark"))
    assert second["processed"] == 0
    assert second["pending"] == 0
    assert second["dead_lettered"] == 1
    assert second["failed_queue"] == 1
    row = _row(service, "raw-2")
    assert int(row["processed"]) == 2, "超限消息应进入失败队列（processed=2）"
    assert int(row["attempts"]) == 2
    assert row["failed_at"]
    assert any("处理失败" in item["title"] for item in alerts), "落失败队列应推送告警"
    alert = next(item for item in alerts if "处理失败" in item["title"])
    assert alert["bypass_quiet"] is True, "失败告警不应被免打扰时段吞掉"
    assert alert["priority"] == "high"

    # 第 3 轮：同会话新消息照常处理，失败队列不阻塞后续消息
    fake._messages["room@chatroom"].append(_raw(4, "新消息：周五前交报名表", int(time.time())))
    third = asyncio.run(service.collect(mode="watermark"))
    assert third["inserted"] == 1
    assert third["processed"] == 1, "失败队列里的消息不能阻塞同会话后续消息"
    assert third["pending"] == 0
    assert service.db.count_failed_messages() == 1
    service.db.close()


def test_failed_queue_api_and_requeue(client):
    """失败队列 API：可查看 attempts/last_error，可一键放回待处理。"""
    service = client.app.state.service
    service.db.add_message(rawid="failed-api-1", session_id="api@chatroom",
                           group_name="接口群", sender="张三", content="处理失败的消息",
                           ts=int(time.time()))
    service.db.record_message_failures(["failed-api-1"], "RuntimeError: boom", 1)

    health = client.get("/api/health").json()
    assert health["collection"]["failed_messages"] >= 1
    assert health["collection"]["max_attempts"] >= 1
    assert "failed_sessions" in health["collection"]
    assert "cursor_sessions" in health["collection"]
    assert health["events"]["failed_messages"] >= 1

    assert client.get("/api/collect/failed").status_code == 401
    body = client.get("/api/collect/failed", headers=HEADERS).json()
    item = next(row for row in body["failed"] if row["rawid"] == "failed-api-1")
    assert int(item["attempts"]) == 1
    assert "boom" in item["last_error"]
    assert body["count"] >= 1

    retried = client.post("/api/collect/failed/retry", headers=HEADERS,
                          json={"rawids": ["failed-api-1"]}).json()
    assert retried["ok"] is True
    assert retried["requeued"] == 1
    row = service.db.conn.execute(
        "SELECT processed, attempts, failed_at FROM messages WHERE rawid = 'failed-api-1'"
    ).fetchone()
    assert int(row["processed"]) == 0 and int(row["attempts"]) == 0
    # 清理：标记已处理，避免影响其它用例的失败队列计数
    service.db.mark_messages_processed(["failed-api-1"])


def _bulk_service(tmp_path, count: int):
    """构造一个会话 count 条待处理消息（用于大积压 / 隔离探测场景）。"""
    now = int(time.time())
    session = "bulk@chatroom"
    sessions = [{"username": session, "name": "大积压群", "isGroup": True, "isTop": True}]
    messages = {session: [
        _raw(i, "周五前填写破冰人数统计表，记得提交" if i % 10 == 0 else "哈哈哈哈这也行",
             now - count + i)
        for i in range(1, count + 1)
    ]}
    service = AppService(_settings(tmp_path))
    service.wcda = FakePagedWcda(sessions, messages)
    return service, messages


def test_large_backlog_global_failure_does_not_fill_failed_queue(tmp_path):
    """> 隔离上限的积压遇到整体性故障（LLM/网络）：只记 last_error，不消耗 attempts。"""
    service, _ = _bulk_service(tmp_path, 31)
    original = service.process_batch
    alerts: list[dict] = []

    async def always_fail(batch):
        raise RuntimeError("global outage")

    async def fake_notify(title, body, priority="normal", *, bypass_quiet=False):
        alerts.append({"title": title, "body": body, "priority": priority,
                       "bypass_quiet": bypass_quiet})
        return []

    service.process_batch = always_fail
    service._notify = fake_notify
    for round_index in range(1, 4):
        stats = asyncio.run(service.collect(mode="watermark"))
        assert stats["processed"] == 0
        assert stats["pending"] == 31, "整体性故障应保留全部待处理消息"
        assert stats["dead_lettered"] == 0
    row = service.db.conn.execute(
        "SELECT COUNT(*) c FROM messages WHERE attempts > 0").fetchone()
    assert int(row["c"]) == 0, "整体性故障不应消耗重试次数"
    row = service.db.conn.execute(
        "SELECT COUNT(*) c FROM messages WHERE last_error != ''").fetchone()
    assert int(row["c"]) == 31, "整体性故障应记录 last_error 便于排障"
    assert service.db.count_failed_messages() == 0
    assert service.last_error, "处理失败应写入采集健康状态，便于设置页排障"
    # 连续 3 轮整体性失败 → 升级告警（不阻塞、不误判写失败队列）
    sustained = [item for item in alerts if "持续失败" in item["title"]]
    assert len(sustained) == 1, f"连续 3 轮失败应升级告警，实际 {[a['title'] for a in alerts]}"
    assert sustained[0]["bypass_quiet"] is True and sustained[0]["priority"] == "high"

    # 故障恢复后正常排空（连续失败计数清零）
    service.process_batch = original
    stats = asyncio.run(service.collect(mode="watermark"))
    assert stats["processed"] == 31
    assert stats["pending"] == 0
    assert service._global_failure_streaks == {}
    service.db.close()


def test_large_backlog_drains_after_single_poison_message(tmp_path):
    """> 隔离上限的积压里只有一条毒消息：先排空探测样本，毒消息下一轮才被隔离淘汰。"""
    service, _ = _bulk_service(tmp_path, 31)
    original = service.process_batch

    async def poison(batch):
        if any(str(item.get("rawid")) == "raw-31" for item in batch):
            raise RuntimeError("poison")
        return await original(batch)

    service.process_batch = poison
    first = asyncio.run(service.collect(mode="watermark"))
    assert first["processed"] == 30, "探测样本（前 30 条）应被正常处理"
    assert first["pending"] == 1

    second = asyncio.run(service.collect(mode="watermark"))
    assert second["processed"] == 0
    assert second["dead_lettered"] == 0, "第 1 次失败只累计 attempts（上限 2）"
    third = asyncio.run(service.collect(mode="watermark"))
    assert third["dead_lettered"] == 1, "毒消息达到上限后应进失败队列"
    assert service.db.count_failed_messages() == 1
    assert service.db.conn.execute(
        "SELECT processed FROM messages WHERE rawid = 'raw-31'").fetchone()["processed"] == 2
    service.db.close()
