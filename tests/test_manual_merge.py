"""手动待办自动补充 / 新建去重（模型总结出新待办后与手动待办比对）。"""
import asyncio
import time

import pytest

import app.service as service_module
from app.config import Settings
from app.db import Database
from app.extract import (ExtractedTask, ManualDecision, ReconcileItem, ReconcileResult,
                         ReconcileTask)
from app.service import AppService


def _settings(tmp_path) -> Settings:
    return Settings(
        data_source="demo",
        db_path=tmp_path / "manual-merge.db",
        app_token="test",
        llm_enabled=True,
        llm_api_key="test-key",
        llm_base_url="http://127.0.0.1:9/v1",
        whitelist_mode="all",
        include_private=True,
        quiet_start="00:00",
        quiet_end="00:00",
    )


def _msg(rawid: str, content: str, group: str = "项目群") -> dict:
    return {
        "rawid": rawid,
        "session_id": "49675822102@chat",
        "group_name": group,
        "sender": "25机电金正豪",
        "content": content,
        "ts": int(time.time()),
        "is_group": True,
        "event": "message.new",
        "msg_type": 1,
        "render_type": "text",
    }


def _fake_reconcile_factory(title: str, notes: str = "", due: str | None = None,
                            event_title: str = "科创破冰活动"):
    async def fake_reconcile(messages, events, settings, now=None, identity=None):
        return ReconcileResult(
            items=[ReconcileItem(
                event_id=None, event_title=event_title, event_summary="",
                tasks=[ReconcileTask(
                    task_id=None, title=title, notes=notes, due_at=due,
                    source_msg_ids=[messages[0]["rawid"]],
                )],
            )],
            summary="x",
        )

    return fake_reconcile


def _manual(service: AppService, title: str, *, due: str | None = None,
            notes: str = "", group: str = "手机添加") -> dict:
    task, _created = service.db.create_task(
        title=title, notes=notes, due_at=due, group_name=group,
    )
    return task


def test_same_relation_supplements_manual_task_without_duplicate(tmp_path, monkeypatch):
    """几乎相同：细节补进手动待办，不新建重复待办；标题/已有时间不变。"""
    service = AppService(_settings(tmp_path))
    manual = _manual(service, "科创破冰", due="2026-11-01T18:00:00")
    seen = []

    async def fake_match(candidates, manual_tasks, messages, settings, now=None,
                         ignored_tasks=None):
        seen.append((len(candidates), [task["id"] for task in manual_tasks]))
        return [ManualDecision(
            index=0, relation="same", manual_task_id=manual["id"],
            supplement_notes="11月1日下午2点，地点待定（桑浦山/东海岸）",
            supplement_due="2026-11-01T14:00:00",
        )]

    monkeypatch.setattr(service_module, "llm_reconcile",
                        _fake_reconcile_factory("参加科创破冰活动", notes="11月1日下午2点"))
    monkeypatch.setattr(service_module, "llm_match_manual", fake_match)

    stats = asyncio.run(service.process_batch([_msg("m1", "请问破冰是在桑浦山还是东海岸")]))

    assert seen == [(1, [manual["id"]])], "有手动待办时应做一次合并判定"
    assert stats["tasks_created"] == 0
    assert stats["supplemented"] == 1
    tasks = service.db.list_tasks("pending")
    assert len(tasks) == 1, "不应新建重复待办"
    task = service.db.get_task(manual["id"])
    assert task["title"] == "科创破冰", "手动标题不能改"
    assert task["due_at"] == "2026-11-01T18:00:00", "手动填的截止时间不能改"
    assert "11月1日下午2点" in task["notes"], "空白备注应被补上"
    assert task["source_msg_ids"] == ["m1"], "来源消息应追加，详情页可追溯"
    assert task["match_reason"] == "自动补充（来自新消息归并）"
    assert service.db.count_open_events() == 0, "任务全被并入手动待办时不应建空事项"
    service.db.close()


def test_same_relation_fills_blank_due_when_manual_left_it_empty(tmp_path, monkeypatch):
    """手动没填时间 → 可以自动补；手动填了 → 不改。"""
    service = AppService(_settings(tmp_path))
    manual = _manual(service, "科创破冰")

    async def fake_match(candidates, manual_tasks, messages, settings, now=None,
                         ignored_tasks=None):
        return [ManualDecision(
            index=0, relation="same", manual_task_id=manual["id"],
            supplement_notes="11月1日下午2点，桑浦山校区",
            supplement_due="2026-11-01T14:00:00",
        )]

    monkeypatch.setattr(service_module, "llm_reconcile",
                        _fake_reconcile_factory("参加科创破冰活动"))
    monkeypatch.setattr(service_module, "llm_match_manual", fake_match)

    asyncio.run(service.process_batch([_msg("m1", "破冰改到11月1日下午2点")]))

    task = service.db.get_task(manual["id"])
    assert task["due_at"] == "2026-11-01T14:00:00"
    assert task["notes"] == "11月1日下午2点，桑浦山校区"
    assert task["title"] == "科创破冰"
    assert len(service.db.list_tasks("pending")) == 1
    service.db.close()


def test_partial_overlap_splits_into_supplement_plus_new_task(tmp_path, monkeypatch):
    """部分相同：重叠部分补手动待办，另一件事另建一条。"""
    service = AppService(_settings(tmp_path))
    manual = _manual(service, "科创破冰", due="2026-11-01T18:00:00")

    async def fake_match(candidates, manual_tasks, messages, settings, now=None,
                         ignored_tasks=None):
        return [ManualDecision(
            index=0, relation="split", manual_task_id=manual["id"],
            supplement_notes="11月1日下午2点，地点待定",
            supplement_due="2026-11-01T14:00:00",
            remainder=ExtractedTask(
                title="填写科创破冰人数统计接龙",
                notes="群接龙统计人数，未接龙会被点名",
                due_at="2026-10-31T23:59:00",
                priority="normal",
            ),
        )]

    monkeypatch.setattr(service_module, "llm_reconcile",
                        _fake_reconcile_factory("参加科创破冰活动并填写人数统计接龙"))
    monkeypatch.setattr(service_module, "llm_match_manual", fake_match)

    stats = asyncio.run(service.process_batch([_msg("m1", "接龙统计人数，11月1日下午2点破冰")]))

    assert stats["tasks_created"] == 1
    assert stats["supplemented"] == 1
    tasks = service.db.list_tasks("pending")
    assert len(tasks) == 2, "手动待办 + 剩余部分新待办"
    manual_task = service.db.get_task(manual["id"])
    assert manual_task["title"] == "科创破冰"
    assert manual_task["due_at"] == "2026-11-01T18:00:00"
    assert "11月1日下午2点" in manual_task["notes"]
    new_task = next(task for task in tasks if task["id"] != manual["id"])
    assert new_task["title"] == "填写科创破冰人数统计接龙"
    assert new_task["due_at"] == "2026-10-31T23:59:00"
    assert new_task["source_msg_ids"] == ["m1"]
    assert new_task["event_id"], "剩余部分仍应挂在事项里"
    service.db.close()


def test_unrelated_decision_creates_task_as_before(tmp_path, monkeypatch):
    service = AppService(_settings(tmp_path))
    manual = _manual(service, "交论文")

    async def fake_match(candidates, manual_tasks, messages, settings, now=None,
                         ignored_tasks=None):
        return [ManualDecision(index=0, relation="none")]

    monkeypatch.setattr(service_module, "llm_reconcile",
                        _fake_reconcile_factory("参加科创破冰活动"))
    monkeypatch.setattr(service_module, "llm_match_manual", fake_match)

    stats = asyncio.run(service.process_batch([_msg("m1", "11月1日下午2点破冰")]))

    assert stats["tasks_created"] == 1
    assert stats["supplemented"] == 0
    tasks = service.db.list_tasks("pending")
    assert len(tasks) == 2
    assert service.db.get_task(manual["id"])["notes"] == ""
    service.db.close()


def test_match_failure_falls_back_to_plain_creation(tmp_path, monkeypatch):
    """合并判定失败（网络/格式）不能阻断原有建待办流程。"""
    service = AppService(_settings(tmp_path))
    _manual(service, "科创破冰")

    async def fake_match(candidates, manual_tasks, messages, settings, now=None,
                         ignored_tasks=None):
        return None

    monkeypatch.setattr(service_module, "llm_reconcile",
                        _fake_reconcile_factory("参加科创破冰活动"))
    monkeypatch.setattr(service_module, "llm_match_manual", fake_match)

    stats = asyncio.run(service.process_batch([_msg("m1", "11月1日下午2点破冰")]))
    assert stats["tasks_created"] == 1
    assert stats["supplemented"] == 0
    service.db.close()


def test_no_manual_tasks_skips_match_call(tmp_path, monkeypatch):
    service = AppService(_settings(tmp_path))

    async def boom(*args, **kwargs):
        raise AssertionError("没有手动待办时不应调用合并判定")

    monkeypatch.setattr(service_module, "llm_reconcile",
                        _fake_reconcile_factory("参加科创破冰活动"))
    monkeypatch.setattr(service_module, "llm_match_manual", boom)

    stats = asyncio.run(service.process_batch([_msg("m1", "11月1日下午2点破冰")]))
    assert stats["tasks_created"] == 1
    service.db.close()


def test_supplement_task_only_writes_blank_fields(tmp_path):
    db = Database(tmp_path / "db.db")
    task, _created = db.create_task(
        title="科创破冰", notes="我自己写的备注", due_at="2026-11-01T18:00:00",
        group_name="手机添加",
    )
    updated, changed = db.supplement_task(
        task["id"], notes="群里的细节", due_at="2026-11-01T14:00:00",
        source_msg_ids=["m1"], match_reason="自动补充",
    )
    assert "notes" not in changed and "due_at" not in changed
    assert updated["notes"] == "我自己写的备注"
    assert updated["due_at"] == "2026-11-01T18:00:00"
    assert updated["title"] == "科创破冰"
    assert updated["source_msg_ids"] == ["m1"]
    assert updated["match_reason"] == "自动补充"

    # 重复追加同一来源不应产生变更（避免反复抬 rev / 同步抖动）
    again, changed_again = db.supplement_task(task["id"], source_msg_ids=["m1"])
    assert changed_again == []
    assert again["source_msg_ids"] == ["m1"]
    db.close()


def test_list_manual_pending_only_manual_pending(tmp_path):
    db = Database(tmp_path / "db2.db")
    manual_phone, _ = db.create_task(title="手机加的", group_name="手机添加")
    manual_web, _ = db.create_task(title="网页加的", group_name="手动添加")
    auto, _ = db.create_task(title="自动的", group_name="项目群")
    done_manual, _ = db.create_task(title="已完成的手动任务", group_name="手机添加")
    db.update_task(done_manual["id"], {"status": "done"})

    ids = {task["id"] for task in db.list_manual_pending()}
    assert ids == {manual_phone["id"], manual_web["id"]}
    assert auto["id"] not in ids
    db.close()


def test_all_open_events_are_merge_candidates(tmp_path):
    """不再按「最近 8 个」截断：该会话所有还有待办的事项都进入归并候选。"""
    service = AppService(_settings(tmp_path))
    for index in range(12):
        event = service.db.create_event(
            session_id="49675822102@chatroom", group_name="项目群",
            title=f"事项{index}", last_msg_ts=int(time.time()) - index * 60,
        )
        service.db.create_task(
            title=f"待办{index}", group_name="项目群", event_id=event["id"],
        )
    events = service._open_events_payload("49675822102@chatroom")
    assert len(events) == 12, "12 个事项都应保留在候选里"
    assert events[0]["title"] == "事项0", "仍按最后消息时间倒序"
    service.db.close()


def test_completed_or_ignored_events_not_merge_candidates(tmp_path):
    """已完成 / 已忽略的事项不参与归并；同一事项里只列未完成任务。"""
    service = AppService(_settings(tmp_path))
    session = "49675822102@chatroom"

    active_event = service.db.create_event(
        session_id=session, group_name="g", title="进行中", last_msg_ts=100)
    active_task, _ = service.db.create_task(
        title="待办A", group_name="g", event_id=active_event["id"])
    finished_in_active, _ = service.db.create_task(
        title="进行中事项里已完成的一条", group_name="g", event_id=active_event["id"])
    service.db.update_task(finished_in_active["id"], {"status": "done"})

    done_event = service.db.create_event(
        session_id=session, group_name="g", title="已完成", last_msg_ts=200)
    done_task, _ = service.db.create_task(
        title="已完成的任务", group_name="g", event_id=done_event["id"])
    service.db.update_task(done_task["id"], {"status": "done"})

    ignored_event = service.db.create_event(
        session_id=session, group_name="g", title="已忽略", last_msg_ts=300)
    ignored_task, _ = service.db.create_task(
        title="已忽略的任务", group_name="g", event_id=ignored_event["id"])
    service.db.update_task(ignored_task["id"], {"status": "ignored"})

    service.db.create_event(session_id=session, group_name="g", title="空事项",
                            last_msg_ts=400)

    payload = service._open_events_payload(session)
    assert [event["title"] for event in payload] == ["进行中"]
    assert [task["id"] for task in payload[0]["tasks"]] == [active_task["id"]]
    service.db.close()


def test_done_or_ignored_task_does_not_absorb_new_task(tmp_path):
    """同一去重键命中已完成/已忽略任务时，应另建新待办，不能静默并进旧任务。"""
    db = Database(tmp_path / "dedup.db")
    same = dict(title="填写青马工程信息统计表", due_at="2026-10-28T14:00:00",
                group_name="项目一部4.0", dedupe_key="same-key")
    old, created = db.create_task(**same)
    assert created
    db.update_task(old["id"], {"status": "done"})

    new, created_again = db.create_task(**same)
    assert created_again is True, "已完成任务不应吞掉新待办"
    assert new["id"] != old["id"] and new["status"] == "pending"
    assert db.get_task(old["id"])["status"] == "done"

    # 同一条新待办再次被抽出时，仍要并进未完成的那条，而不是无限新建
    again, created_third = db.create_task(**same)
    assert created_third is False
    assert again["id"] == new["id"]

    ignored, _ = db.create_task(title="同一件已完成的事", due_at="2026-11-01T10:00:00",
                                group_name="g", dedupe_key="key-b")
    db.update_task(ignored["id"], {"status": "ignored"})
    fresh, created_fresh = db.create_task(title="同一件已完成的事",
                                          due_at="2026-11-01T10:00:00",
                                          group_name="g", dedupe_key="key-b")
    assert created_fresh is True
    assert fresh["status"] == "pending"
    db.close()


def test_events_never_auto_close(tmp_path):
    """事项不再按 24 小时 TTL 自动关闭，旧事项仍可作为归并目标。"""
    settings = _settings(tmp_path)
    service = AppService(settings)
    assert not hasattr(service.settings, "event_ttl_hours")
    assert not hasattr(service.db, "close_stale_events")

    old_event = service.db.create_event(
        session_id="49675822102@chat", group_name="项目群",
        title="11月1日科创破冰活动", last_msg_ts=int(time.time()) - 48 * 3600,
    )
    assert service.db.get_event(old_event["id"])["status"] == "open"
    assert [event["id"] for event in service.db.open_events("49675822102@chat")] == [
        old_event["id"]
    ]
    service.db.close()


# ---------------------------------------------------------------------------
# LLM 输出的规整：模型胡填时不能乱写手动待办
# ---------------------------------------------------------------------------
def _now():
    from datetime import datetime

    return datetime(2026, 10, 28, 23, 0)


def test_manual_decision_unknown_task_id_becomes_none():
    from app.extract import _normalize_manual_decisions

    candidates = [ExtractedTask(title="参加科创破冰活动")]
    manual_tasks = [{"id": "abc", "title": "科创破冰", "due_at": None, "notes": ""}]
    decisions = _normalize_manual_decisions(
        {"decisions": [{"index": 0, "relation": "same", "manual_task_id": "nope"}]},
        candidates, manual_tasks, _now(),
    )
    assert len(decisions) == 1
    assert decisions[0].relation == "none"
    assert decisions[0].manual_task_id is None


def test_manual_decision_split_without_remainder_becomes_same():
    from app.extract import _normalize_manual_decisions

    candidates = [ExtractedTask(title="参加科创破冰活动并填表")]
    manual_tasks = [{"id": "abc", "title": "科创破冰", "due_at": None, "notes": ""}]
    decisions = _normalize_manual_decisions(
        {"decisions": [{
            "index": 0, "relation": "split", "manual_task_id": "abc",
            "remainder": {"title": "  "},
        }]},
        candidates, manual_tasks, _now(),
    )
    assert decisions[0].relation == "same"


def test_manual_decision_normalizes_due_and_ignores_bad_index():
    from app.extract import _normalize_manual_decisions

    candidates = [ExtractedTask(title="参加科创破冰活动")]
    manual_tasks = [{"id": "abc", "title": "科创破冰", "due_at": None, "notes": ""}]
    decisions = _normalize_manual_decisions(
        {"decisions": [
            {"index": 9, "relation": "same", "manual_task_id": "abc"},
            {"index": 0, "relation": "same", "manual_task_id": "abc",
             "supplement_due": "2026-11-01 14:00", "supplement_notes": "下午两点"},
        ]},
        candidates, manual_tasks, _now(),
    )
    assert len(decisions) == 1
    assert decisions[0].supplement_due == "2026-11-01T14:00:00"
    assert decisions[0].supplement_notes == "下午两点"

