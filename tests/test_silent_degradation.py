"""静默问题回归：降级/抽取饱和/部分失败/深扫/读取放大必须对外可见。

覆盖压力实验报告（output/stress-20261031/实验报告-采集可靠性压力测试.md）里的：
- P1-1 洪峰下 LLM 归并饱和：按 LLM_BATCH_MESSAGES 分片，并暴露 cap_hits/未生成待办；
- P1-2 LLM 故障/上下文溢出：回退规则模式时 degraded=true + last_llm_error 非空；
- P1-3 sessions 接口 500：ok=false/connected=false/last_error 非空，不再“空成功”；
- P1-4 整体性处理失败：ok=false + degraded_reasons 含 sustained_failure；
- P2-2 规则模式跨轮重复建待办：相似标题并入已有待办；
- P2-3 分页漂移读取放大：read_rows/duplicate_rows 暴露；
- P2-4 晚到消息深扫：health.collection.deep_scan 可观测；
- P2-1 规则模式噪音词救援（“记得…收到请回复”仍建待办）。
"""
import asyncio
import time

import app.service as service_module
from app.config import Settings
from app.extract import (LLMUnavailable, ReconcileItem, ReconcileResult,
                         ReconcileTask)
from app.service import AppService


# ---------------------------------------------------------------------------
# fakes / helpers
# ---------------------------------------------------------------------------
class FakeWcda:
    configured = True
    account = "wxid_me"

    def __init__(self, sessions: list[dict] | None = None,
                 messages: dict[str, list[dict]] | None = None,
                 sessions_error: str | None = None,
                 fail_sessions: set[str] | None = None):
        self._sessions = list(sessions or [])
        self._messages = dict(messages or {})
        self.sessions_error = sessions_error
        self.fail_sessions = set(fail_sessions or ())
        self.fetch_calls: list[tuple[str, int, int]] = []

    async def fetch_sessions(self, limit: int = 500) -> list[dict]:
        result = await self.fetch_sessions_result(limit=limit)
        return list(result.get("sessions") or [])

    async def fetch_sessions_result(self, limit: int = 500) -> dict:
        if self.sessions_error:
            return {"ok": False, "sessions": [], "error": self.sessions_error}
        return {"ok": True, "sessions": list(self._sessions), "error": None}

    async def check_health(self) -> bool:
        return True

    async def fetch_messages_page(self, username: str, *, limit: int = 500,
                                  offset: int = 0, order: str = "desc") -> dict:
        self.fetch_calls.append((username, int(offset), int(limit)))
        if username in self.fail_sessions:
            return {"ok": False, "messages": [], "has_more": False, "error": "HTTP 500"}
        rows = sorted(self._messages.get(username, []),
                      key=lambda row: int(row.get("createTime") or 0),
                      reverse=order != "asc")
        page = rows[max(0, int(offset)):max(0, int(offset)) + max(1, int(limit))]
        return {"ok": True, "messages": page,
                "has_more": int(offset) + len(page) < len(rows)}


def _settings(tmp_path, **overrides) -> Settings:
    data = dict(
        data_source="wcda",
        wcda_base="",
        db_path=tmp_path / "silent.db",
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


def _raw(rawid: str, content: str, *, ts: int | None = None,
         local_id: int | None = None) -> dict:
    stamp = int(time.time()) if ts is None else int(ts)
    lid = local_id if local_id is not None else abs(hash(rawid)) % 1_000_000
    return {
        "id": rawid,
        "localId": lid,
        "serverId": lid,
        "createTime": stamp,
        "senderUsername": "wxid_a",
        "senderDisplayName": "张三",
        "isSent": False,
        "content": content,
        "type": 1,
        "renderType": "text",
        "atUsernames": [],
        "quoteUsername": "",
    }


def _service(tmp_path, wcda: FakeWcda, **overrides) -> AppService:
    service = AppService(_settings(tmp_path, **overrides))
    service.wcda = wcda
    return service


def _msg(rawid: str, content: str, session: str = "room@chatroom",
         ts: int | None = None) -> dict:
    return {
        "rawid": rawid, "session_id": session, "group_name": "项目群",
        "sender": "张三", "content": content,
        "ts": int(time.time()) if ts is None else int(ts),
        "is_group": True, "event": "message.new", "msg_type": 1,
        "render_type": "text", "at_usernames": [], "at_names": [],
        "quote_username": "",
    }


def _reconcile(tasks: list[ReconcileTask], *, cap_hit: bool = False,
               items: int | None = None) -> ReconcileResult:
    return ReconcileResult(
        items=[ReconcileItem(event_id=None, event_title="本轮事项", event_summary="",
                             tasks=tasks)],
        summary="x", cap_hit=cap_hit, items_returned=items or 1,
        tasks_returned=len(tasks),
    )


# ---------------------------------------------------------------------------
# P1-2 LLM 故障 / 上下文溢出
# ---------------------------------------------------------------------------
def test_llm_failure_is_visible_and_falls_back_to_rule(tmp_path, monkeypatch):
    """LLM 失败必须暴露 degraded/last_llm_error，而不是只写日志。"""
    now = int(time.time())
    session = "room@chatroom"
    wcda = FakeWcda(
        sessions=[{"username": session, "name": "项目群", "isGroup": True, "isTop": True}],
        messages={session: [_raw("m1", "明天下午3点开会，记得带材料", ts=now - 60)]},
    )
    service = _service(tmp_path, wcda, llm_enabled=True, llm_api_key="test-key")

    async def boom(*args, **kwargs):
        raise LLMUnavailable("context_length_exceeded: max 65536", kind="context_limit")

    monkeypatch.setattr(service_module, "llm_reconcile", boom)
    result = asyncio.run(service.collect(minutes=60, mode="window"))

    # 消息仍按规则模式处理完，但降级必须可见
    assert result["ok"] is True, "LLM 回退不是数据源失败，ok 仍应为 true"
    assert result["processed"] == 1
    assert result["tasks_created"] >= 1, "回退规则模式仍应建待办"
    assert result["degraded"] is True
    assert "llm_fallback" in result["degraded_reasons"]
    assert result["llm_fallbacks"] == 1
    assert result["last_llm_error"] and "context_length" in result["last_llm_error"]

    health = service.health()
    collection = health["collection"]
    assert collection["degraded"] is True
    assert collection["llm_fallbacks"] == 1
    assert collection["llm_fallback_total"] == 1
    assert collection["llm_fallback_streak"] == 1
    assert collection["last_llm_error"]
    # source.last_error 仍只放数据源/处理错误，不因 LLM 回退污染
    assert health["source"]["last_error"] is None
    assert health["source"]["connected"] is True
    service.db.close()


def test_llm_success_resets_fallback_streak(tmp_path, monkeypatch):
    now = int(time.time())
    session = "room@chatroom"
    wcda = FakeWcda(
        sessions=[{"username": session, "name": "项目群", "isGroup": True, "isTop": True}],
        messages={session: [_raw("m1", "明天下午3点开会，记得带材料", ts=now - 60)]},
    )
    service = _service(tmp_path, wcda, llm_enabled=True, llm_api_key="test-key")

    async def boom(*args, **kwargs):
        raise LLMUnavailable("HTTP 500", kind="llm_error")

    monkeypatch.setattr(service_module, "llm_reconcile", boom)
    asyncio.run(service.collect(minutes=60, mode="window"))
    assert service._llm_fallback_streak == 1

    async def ok(messages, events, settings, now=None, identity=None):
        return _reconcile([ReconcileTask(
            task_id=None, title="开会", source_msg_ids=[messages[0]["rawid"]])])

    monkeypatch.setattr(service_module, "llm_reconcile", ok)
    wcda._messages[session] = [_raw("m2", "明天下午3点开会，记得带材料", ts=now - 10)]
    result = asyncio.run(service.collect(minutes=60, mode="window"))
    assert result["llm_fallbacks"] == 0
    assert result["llm_batches"] >= 1
    assert service._llm_fallback_streak == 0
    service.db.close()


# ---------------------------------------------------------------------------
# P1-1 分片归并 + 抽取上限可观测
# ---------------------------------------------------------------------------
def test_llm_batches_split_by_setting(tmp_path, monkeypatch):
    """一轮 100 条消息应按 LLM_BATCH_MESSAGES=40 分 40/40/20 三次归并。"""
    now = int(time.time())
    session = "room@chatroom"
    rows = [_raw(f"b{index}", f"周五前填写第{index}份报名表", ts=now - 600 + index)
            for index in range(100)]
    wcda = FakeWcda(
        sessions=[{"username": session, "name": "项目群", "isGroup": True, "isTop": True}],
        messages={session: rows},
    )
    service = _service(tmp_path, wcda, llm_enabled=True, llm_api_key="test-key",
                       llm_batch_messages=40)
    batches: list[list[str]] = []

    async def fake(messages, events, settings, now=None, identity=None):
        batches.append([m["rawid"] for m in messages])
        return _reconcile([ReconcileTask(
            task_id=None, title=f"填写报名表{len(batches)}",
            source_msg_ids=[m["rawid"] for m in messages])])

    monkeypatch.setattr(service_module, "llm_reconcile", fake)
    result = asyncio.run(service.collect(minutes=60, mode="window"))

    assert [len(batch) for batch in batches] == [40, 40, 20]
    assert result["llm_batches"] == 3
    assert result["tasks_created"] == 3
    assert result["extraction_cap_hits"] == 0
    # 三个新任务的来源覆盖全部 100 条消息
    assert result["messages_without_task"] == 0
    assert service.health()["collection"]["llm"]["batch_messages"] == 40
    service.db.close()


def test_llm_batch_messages_zero_disables_splitting(tmp_path, monkeypatch):
    now = int(time.time())
    session = "room@chatroom"
    rows = [_raw(f"z{index}", f"周五前填写第{index}份报名表", ts=now - 60 + index)
            for index in range(60)]
    wcda = FakeWcda(
        sessions=[{"username": session, "name": "项目群", "isGroup": True, "isTop": True}],
        messages={session: rows},
    )
    service = _service(tmp_path, wcda, llm_enabled=True, llm_api_key="test-key",
                       llm_batch_messages=0)
    sizes: list[int] = []

    async def fake(messages, events, settings, now=None, identity=None):
        sizes.append(len(messages))
        return _reconcile([])

    monkeypatch.setattr(service_module, "llm_reconcile", fake)
    asyncio.run(service.collect(minutes=60, mode="window"))
    assert sizes == [60], "0 = 不切批（兼容旧行为）"
    service.db.close()


def test_extraction_cap_hit_is_degraded_not_silent(tmp_path, monkeypatch):
    """模型输出触到 8 事项/12 任务上限时，必须上报 cap_hits + 未生成待办。"""
    now = int(time.time())
    session = "room@chatroom"
    rows = [_raw(f"c{index}", f"周五前填写第{index}份报名表", ts=now - 60 + index)
            for index in range(10)]
    wcda = FakeWcda(
        sessions=[{"username": session, "name": "项目群", "isGroup": True, "isTop": True}],
        messages={session: rows},
    )
    service = _service(tmp_path, wcda, llm_enabled=True, llm_api_key="test-key",
                       llm_batch_messages=0)

    async def fake(messages, events, settings, now=None, identity=None):
        tasks = [ReconcileTask(task_id=None, title=f"填写报名表{index}",
                               source_msg_ids=[messages[index]["rawid"]])
                 for index in range(2)]
        return _reconcile(tasks, cap_hit=True, items=8)

    monkeypatch.setattr(service_module, "llm_reconcile", fake)
    result = asyncio.run(service.collect(minutes=60, mode="window"))

    assert result["ok"] is True
    assert result["extraction_cap_hits"] == 1
    assert result["degraded"] is True
    assert "extraction_cap" in result["degraded_reasons"]
    assert result["messages_without_task"] == 8
    assert service.health()["collection"]["extraction_cap_hits"] == 1
    service.db.close()


# ---------------------------------------------------------------------------
# P1-3 sessions 接口失败
# ---------------------------------------------------------------------------
def test_sessions_api_failure_is_not_empty_success(tmp_path):
    """sessions 接口 500 必须 ok=false/connected=false/last_error 非空。"""
    service = _service(
        tmp_path,
        FakeWcda(sessions_error="httpx.HTTPStatusError: 500 Server Error"),
    )
    result = asyncio.run(service.collect(minutes=60, mode="watermark"))

    assert result["ok"] is False, "接口失败不能被当成“真的没有会话”"
    assert result["degraded"] is True
    assert "sessions_unavailable" in result["degraded_reasons"]
    assert "读取监控会话失败" in (result.get("reason") or "")
    health = service.health()
    assert health["source"]["connected"] is False
    assert health["source"]["last_error"]
    service.db.close()


def test_sessions_api_failure_still_processes_local_pending(tmp_path):
    """会话列表失败时，本地已入库未处理消息仍要处理（不空转）。"""
    service = _service(
        tmp_path,
        FakeWcda(sessions_error="httpx.ConnectError: connection refused"),
    )
    service.db.add_message(
        rawid="stored-1", session_id="old@chatroom", group_name="旧群", sender="张三",
        content="明天下午3点开会，记得带材料", ts=int(time.time()) - 60,
    )
    result = asyncio.run(service.collect(minutes=60, mode="watermark"))
    assert result["ok"] is False
    assert result["processed"] == 1
    assert service.db.count_unprocessed() == 0
    service.db.close()


# ---------------------------------------------------------------------------
# P1-4 / P2-5 部分失败与整体性失败
# ---------------------------------------------------------------------------
def test_partial_session_failure_sets_ok_false_and_partial(tmp_path):
    now = int(time.time())
    good, bad = "good@chatroom", "bad@chatroom"
    wcda = FakeWcda(
        sessions=[
            {"username": good, "name": "正常群", "isGroup": True, "isTop": True},
            {"username": bad, "name": "故障群", "isGroup": True, "isTop": True},
        ],
        messages={good: [_raw("g1", "明天下午3点开会，记得带材料", ts=now - 60)]},
        fail_sessions={bad},
    )
    service = _service(tmp_path, wcda)
    result = asyncio.run(service.collect(minutes=60, mode="window"))

    assert result["ok"] is False, "部分会话读取失败时 ok 必须为 false（P2-5）"
    assert result["partial"] is True
    assert result["processed"] == 1
    assert bad in result["sessions_failed"]
    assert "partial_failure" in result["degraded_reasons"]
    assert "部分失败" in (result.get("reason") or "")
    health = service.health()
    assert health["source"]["connected"] is True
    assert health["source"]["last_error"]
    assert health["collection"]["partial"] is True
    service.db.close()


def test_sustained_processing_failure_sets_ok_false(tmp_path, monkeypatch):
    """连续整体性失败（大积压 + 探测样本全失败）不能仍报 ok=true。"""
    now = int(time.time())
    session = "room@chatroom"
    rows = [_raw(f"s{index}", f"请于明天上午9点开会，带材料 {index}", ts=now - 500 + index)
            for index in range(50)]
    wcda = FakeWcda(
        sessions=[{"username": session, "name": "项目群", "isGroup": True, "isTop": True}],
        messages={session: rows},
    )
    service = _service(tmp_path, wcda)

    async def boom(*args, **kwargs):
        raise RuntimeError("extraction engine unavailable")

    monkeypatch.setattr(service, "process_batch", boom)
    result = asyncio.run(service.collect(minutes=60, mode="window"))

    assert result["ok"] is False
    assert result["partial"] is False
    assert result["degraded"] is True
    assert "sustained_failure" in result["degraded_reasons"]
    assert result["pending"] == 50, "整体性失败应保留待处理，等恢复后重试"
    assert "失败" in (result.get("reason") or "")
    service.db.close()


# ---------------------------------------------------------------------------
# P2-1 规则模式噪音词救援
# ---------------------------------------------------------------------------
def test_rule_mode_not_capped_at_old_12_limit(tmp_path):
    """规则模式不再被“每批 12 条”硬截断（P1-1：洪峰下静默饱和）。"""
    now = int(time.time())
    session = "room@chatroom"
    rows = [_raw(f"cap{index}", f"请于明天下午3点前提交第{index + 1}份表格",
                 ts=now - 600 + index) for index in range(30)]
    wcda = FakeWcda(
        sessions=[{"username": session, "name": "项目群", "isGroup": True, "isTop": True}],
        messages={session: rows},
    )
    service = _service(tmp_path, wcda)
    result = asyncio.run(service.collect(minutes=60, mode="window"))

    assert result["processed"] == 30
    assert result["tasks_created"] == 30, "30 条不同的待办消息应全部产出，而不是只出 12 条"
    assert result["messages_without_task"] == 0
    assert result["extraction_cap_hits"] == 0
    assert result["degraded"] is False
    service.db.close()


def test_rule_mode_keeps_task_with_noise_word_in_body(tmp_path):
    """正文含“收到请回复”的真实待办，规则模式不能再整条过滤。"""
    now = int(time.time())
    session = "room@chatroom"
    wcda = FakeWcda(
        sessions=[{"username": session, "name": "项目群", "isGroup": True, "isTop": True}],
        messages={session: [
            _raw("n1", "记得今晚8点前交材料，收到请回复", ts=now - 60),
        ]},
    )
    service = _service(tmp_path, wcda)
    result = asyncio.run(service.collect(minutes=60, mode="window"))
    assert result["processed"] == 1
    tasks = service.db.list_tasks("pending")
    assert tasks, "含噪音词的待办不应被整条过滤"
    service.db.close()


def test_rule_mode_noise_and_same_topic_merge_in_one_batch(tmp_path):
    """同一批里“干净版 + 收到请回复版”应合并成同一事项，24 条全部覆盖。"""
    now = int(time.time())
    session = "room@chatroom"
    clean = [_raw(f"clean{index}", f"记得今晚8点前交第{index + 1}份材料，谢谢",
                  ts=now - 3 * 3600 + index) for index in range(12)]
    noisy = [_raw(f"noisy{index}", f"记得今晚8点前交第{index + 1}份材料，收到请回复",
                  ts=now - 3 * 3600 + 100 + index) for index in range(12)]
    wcda = FakeWcda(
        sessions=[{"username": session, "name": "项目群", "isGroup": True, "isTop": True}],
        messages={session: clean + noisy},
    )
    service = _service(tmp_path, wcda)
    result = asyncio.run(service.collect(minutes=1440, mode="window"))

    assert result["processed"] == 24
    tasks = service.db.list_tasks("pending")
    assert len(tasks) == 12, f"同事项应合并为 12 条，实际 {len(tasks)}"
    assert all(len(task["source_msg_ids"]) == 2 for task in tasks), "干净版和含噪音版都应并入来源"
    assert result["messages_without_task"] == 0
    assert result["extraction_cap_hits"] == 0
    service.db.close()


# ---------------------------------------------------------------------------
# P2-2 规则模式跨轮去重
# ---------------------------------------------------------------------------
def test_rule_mode_dedupes_similar_task_across_rounds(tmp_path):
    now = int(time.time())
    session = "room@chatroom"
    wcda = FakeWcda(
        sessions=[{"username": session, "name": "项目群", "isGroup": True, "isTop": True}],
        messages={session: [
            _raw("r1", "报名请填写科创破冰活动人数统计", ts=now - 120, local_id=1),
        ]},
    )
    service = _service(tmp_path, wcda)
    asyncio.run(service.collect(minutes=60, mode="window"))
    assert len(service.db.list_tasks("pending")) == 1

    # 下一轮同一件事换个说法再提醒一次 → 不应新建第二条待办
    wcda._messages[session].append(
        _raw("r2", "再提醒一次：报名请填写科创破冰活动人数统计", ts=now - 30, local_id=2))
    asyncio.run(service.collect(minutes=60, mode="window"))

    tasks = service.db.list_tasks("pending")
    assert len(tasks) == 1, f"同一事项跨轮不应重复建待办：{[t['title'] for t in tasks]}"
    assert set(tasks[0]["source_msg_ids"]) == {"r1", "r2"}, "新消息应并入来源"
    assert service.db.count_open_events() == 1, "同一事项不应每轮新建事项"
    service.db.close()


# ---------------------------------------------------------------------------
# P2-3 分页漂移读取放大可见
# ---------------------------------------------------------------------------
class DriftWcda(FakeWcda):
    """模拟 offset 分页漂移：相邻页重复返回上一页尾部若干行。"""

    def __init__(self, rows: list[dict], page_size: int = 50):
        super().__init__(sessions=[{"username": "drift@chatroom", "name": "漂移群",
                                    "isGroup": True, "isTop": True}])
        self._rows = rows
        self._page_size = page_size
        self._pages = [
            rows[0:page_size],
            rows[page_size - 10:2 * page_size - 10],
            rows[2 * page_size - 20:3 * page_size - 20],
        ]

    async def fetch_messages_page(self, username: str, *, limit: int = 500,
                                  offset: int = 0, order: str = "desc") -> dict:
        index = int(offset) // max(1, int(limit))
        page = self._pages[index] if index < len(self._pages) else []
        return {"ok": True, "messages": page, "has_more": index < len(self._pages) - 1}


def test_pagination_drift_metrics_exposed(tmp_path):
    now = int(time.time())
    rows = [_raw(f"p{index}", f"周五前填写第{index}份报名表", ts=now - 300 + index,
                 local_id=index + 1)
            for index in range(130)]
    service = _service(tmp_path, DriftWcda(rows), wcda_catchup_page_size=50)
    result = asyncio.run(service.collect(minutes=60, mode="window"))

    assert result["ok"] is True
    assert result["duplicate_rows"] == 20, result["duplicate_rows"]
    assert result["read_rows"] == 150
    assert result["read_ratio"] > 1.0
    assert result["pages"] == 4
    service.db.close()


# ---------------------------------------------------------------------------
# P2-4 深扫可观测
# ---------------------------------------------------------------------------
def test_deep_scan_state_is_visible(tmp_path):
    now = int(time.time())
    session = "room@chatroom"
    wcda = FakeWcda(
        sessions=[{"username": session, "name": "项目群", "isGroup": True, "isTop": True}],
        messages={session: [_raw("late-1", "记得今晚8点前交材料，收到请回复", ts=now - 3600)]},
    )
    service = _service(tmp_path, wcda, wcda_deep_scan_minutes=60,
                       wcda_deep_scan_lookback_minutes=1440)
    # 强制“深扫到期”
    service._last_deep_scan_at = time.monotonic() - 4 * 3600
    assert service.health()["collection"]["deep_scan"]["due"] is True

    result = asyncio.run(service._maybe_deep_scan())
    assert result is not None and result.get("deep_scan") is True
    assert result["inserted"] == 1
    assert result["tasks_created"] >= 1

    state = service.health()["collection"]["deep_scan"]
    assert state["last_at"], "深扫完成时间必须可观测"
    assert state["last"]["inserted"] == 1
    assert state["last"]["lookback_minutes"] == 1440
    assert state["due"] is False
    service.db.close()
