"""已忽略的手动待办：一个月内同类消息不再重复新建待办（MANUAL_SUPPRESS_DAYS）。"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta

import pytest

import app.service as service_module
from app.config import Settings
from app.extract import (ExtractedTask, ManualDecision, ReconcileItem, ReconcileResult,
                         ReconcileTask, _build_manual_match_prompt, _normalize_manual_decisions)
from app.service import AppService
from app.utils import to_iso


def _settings(tmp_path, *, suppress_days: int = 30) -> Settings:
    return Settings(
        data_source="demo",
        db_path=tmp_path / "manual-suppress.db",
        app_token="test",
        llm_enabled=True,
        llm_api_key="test-key",
        llm_base_url="http://127.0.0.1:9/v1",
        whitelist_mode="all",
        include_private=True,
        quiet_start="00:00",
        quiet_end="00:00",
        manual_suppress_days=suppress_days,
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


def _fake_reconcile(title: str, notes: str = "") -> object:
    async def fake_reconcile(messages, events, settings, now=None, identity=None):
        return ReconcileResult(
            items=[ReconcileItem(
                event_id=None, event_title=title, event_summary="",
                tasks=[ReconcileTask(
                    task_id=None, title=title, notes=notes,
                    source_msg_ids=[messages[0]["rawid"]],
                )],
            )],
            summary="x",
        )

    return fake_reconcile


def _manual(service: AppService, title: str, *, group: str = "手机添加") -> dict:
    task, _created = service.db.create_task(title=title, group_name=group)
    return task


def _ignore(service: AppService, title: str, *, days_ago: int = 0,
            group: str = "手机添加") -> dict:
    task = _manual(service, title, group=group)
    service.db.update_task(task["id"], {"status": "ignored"})
    if days_ago:
        old = to_iso(datetime.now() - timedelta(days=days_ago))
        service.db.conn.execute(
            "UPDATE tasks SET updated_at = ? WHERE id = ?", (old, task["id"]))
        service.db.conn.commit()
    return service.db.get_task(task["id"])


def test_list_manual_ignored_only_window_and_manual_groups(tmp_path):
    service = AppService(_settings(tmp_path))
    recent = _ignore(service, "新忽略的手机待办")
    old = _ignore(service, "很久以前忽略的手机待办", days_ago=40)
    ignored_web = _ignore(service, "网页忽略的", group="手动添加")
    auto = _manual(service, "自动任务", group="项目群")
    service.db.update_task(auto["id"], {"status": "ignored"})
    done = _manual(service, "已完成的手动任务")
    service.db.update_task(done["id"], {"status": "done"})

    cutoff = to_iso(datetime.now() - timedelta(days=30))
    ids = {task["id"] for task in service.db.list_manual_ignored(cutoff)}
    assert ids == {recent["id"], ignored_web["id"]}
    assert old["id"] not in ids, "超过窗口的已忽略待办不应参与抑制"
    assert auto["id"] not in ids, "非手动待办不参与抑制"
    assert done["id"] not in ids, "已完成不参与抑制"
    service.db.close()


def test_ignored_manual_task_suppresses_duplicate_new_task(tmp_path, monkeypatch):
    service = AppService(_settings(tmp_path))
    ignored = _ignore(service, "青马工程报名")
    seen = {}

    async def fake_match(candidates, manual_tasks, messages, settings, now=None,
                         ignored_tasks=None):
        seen["ignored"] = [task["id"] for task in (ignored_tasks or [])]
        return [ManualDecision(index=0, relation="suppress",
                               manual_task_id=ignored["id"])]

    monkeypatch.setattr(service_module, "llm_reconcile",
                        _fake_reconcile("青马工程报名截止提醒"))
    monkeypatch.setattr(service_module, "llm_match_manual", fake_match)

    stats = asyncio.run(service.process_batch([_msg("m1", "青马工程报名今晚截止")]))

    assert seen["ignored"] == [ignored["id"]], "窗口内的已忽略待办应进入判定"
    assert stats["suppressed"] == 1
    assert stats["supplemented"] == 0
    assert stats["tasks_created"] == 0, "同类消息不应再新建待办"
    pending = service.db.list_tasks("pending")
    assert pending == [], "不应产生新的重复待办"
    stored = service.db.get_task(ignored["id"])
    assert stored["status"] == "ignored"
    assert stored["notes"] == "" and stored["due_at"] is None
    assert stored["match_reason"] == "" and stored["source_msg_ids"] == [], \
        "已忽略待办不应被补写"
    assert service.db.count_open_events() == 0, "任务被抑制后不应建空事项"
    service.db.close()


def test_ignored_outside_window_still_creates_task(tmp_path, monkeypatch):
    service = AppService(_settings(tmp_path))
    _ignore(service, "青马工程报名", days_ago=40)
    _manual(service, "买电池")  # 保证仍有未完成手动待办，判定会被调用
    seen = {}

    async def fake_match(candidates, manual_tasks, messages, settings, now=None,
                         ignored_tasks=None):
        seen["ignored"] = list(ignored_tasks or [])
        return [ManualDecision(index=0, relation="none")]

    monkeypatch.setattr(service_module, "llm_reconcile",
                        _fake_reconcile("青马工程报名截止提醒"))
    monkeypatch.setattr(service_module, "llm_match_manual", fake_match)

    stats = asyncio.run(service.process_batch([_msg("m1", "青马工程报名今晚截止")]))

    assert seen["ignored"] == [], "超过一个月的已忽略待办不再参与抑制"
    assert stats["suppressed"] == 0
    assert stats["tasks_created"] == 1, "窗口外应照常新建（防止真实新活动被吞）"
    service.db.close()


def test_completed_manual_task_does_not_suppress(tmp_path, monkeypatch):
    service = AppService(_settings(tmp_path))
    done = _manual(service, "交实验报告")
    service.db.update_task(done["id"], {"status": "done"})
    _manual(service, "买电池")  # 保证仍有未完成手动待办，判定会被调用
    seen = {}

    async def fake_match(candidates, manual_tasks, messages, settings, now=None,
                         ignored_tasks=None):
        seen["ignored"] = list(ignored_tasks or [])
        return [ManualDecision(index=0, relation="none")]

    monkeypatch.setattr(service_module, "llm_reconcile", _fake_reconcile("提交实验报告"))
    monkeypatch.setattr(service_module, "llm_match_manual", fake_match)

    stats = asyncio.run(service.process_batch([_msg("m1", "实验报告本周五前提交")]))

    assert seen["ignored"] == [], "已完成的任务不参与已忽略去重"
    assert stats["tasks_created"] == 1
    service.db.close()


def test_suppress_disabled_when_days_zero(tmp_path, monkeypatch):
    service = AppService(_settings(tmp_path, suppress_days=0))
    _ignore(service, "青马工程报名")
    _manual(service, "买电池")  # 保证仍有未完成手动待办，判定会被调用
    seen = {}

    async def fake_match(candidates, manual_tasks, messages, settings, now=None,
                         ignored_tasks=None):
        seen["ignored"] = list(ignored_tasks or [])
        return [ManualDecision(index=0, relation="none")]

    monkeypatch.setattr(service_module, "llm_reconcile", _fake_reconcile("青马工程报名截止提醒"))
    monkeypatch.setattr(service_module, "llm_match_manual", fake_match)

    stats = asyncio.run(service.process_batch([_msg("m1", "青马工程报名今晚截止")]))

    assert seen["ignored"] == [], "MANUAL_SUPPRESS_DAYS=0 应关闭该能力"
    assert stats["suppressed"] == 0
    assert stats["tasks_created"] == 1
    service.db.close()


def test_suppress_unknown_task_id_becomes_none():
    candidates = [ExtractedTask(title="青马工程报名截止提醒")]
    ignored = [{"id": "ign-1", "title": "青马工程报名", "updated_at": "2026-10-28T10:00:00"}]
    decisions = _normalize_manual_decisions(
        {"decisions": [{"index": 0, "relation": "suppress", "manual_task_id": "nope"}]},
        candidates, [], datetime(2026, 10, 28, 23, 0), ignored_tasks=ignored,
    )
    assert len(decisions) == 1
    assert decisions[0].relation == "none"
    assert decisions[0].manual_task_id is None


def test_suppress_keeps_valid_reference():
    candidates = [ExtractedTask(title="青马工程报名截止提醒")]
    ignored = [{"id": "ign-1", "title": "青马工程报名", "updated_at": "2026-10-28T10:00:00"}]
    decisions = _normalize_manual_decisions(
        {"decisions": [{
            "index": 0, "relation": "suppress", "manual_task_id": "ign-1",
            "supplement_notes": "不应写入", "supplement_due": "2026-11-01 10:00",
        }]},
        candidates, [], datetime(2026, 10, 28, 23, 0), ignored_tasks=ignored,
    )
    assert decisions[0].relation == "suppress"
    assert decisions[0].manual_task_id == "ign-1"
    assert decisions[0].supplement_notes == "" and decisions[0].supplement_due is None


def test_prompt_contains_ignored_section():
    candidates = [ExtractedTask(title="青马工程报名截止提醒")]
    ignored = [{"id": "ign-1", "title": "青马工程报名", "updated_at": "2026-10-28T10:00:00"}]
    prompt = _build_manual_match_prompt(
        candidates, [], [_msg("m1", "青马工程报名今晚截止")],
        datetime(2026, 10, 28, 23, 0), ignored_tasks=ignored, suppress_days=30,
    )
    assert "已忽略" in prompt
    assert "task_id=ign-1" in prompt
    assert "青马工程报名" in prompt
    assert "suppress" in prompt
    assert "30 天" in prompt
