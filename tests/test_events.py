import asyncio
import time

import pytest

import app.service as service_module
from app.config import Settings
from app.extract import ReconcileItem, ReconcileResult, ReconcileTask
from app.service import AppService


def _settings(tmp_path, llm_enabled=True) -> Settings:
    return Settings(
        data_source="demo",
        db_path=tmp_path / "events.db",
        app_token="test",
        llm_enabled=llm_enabled,
        llm_api_key="test-key" if llm_enabled else "",
        llm_base_url="http://127.0.0.1:9/v1",
        whitelist_mode="all",
        include_private=True,
        quiet_start="00:00",
        quiet_end="00:00",
    )


def _msg(rawid: str, content: str, session: str = "123@chatroom") -> dict:
    return {
        "rawid": rawid,
        "session_id": session,
        "group_name": "项目群",
        "sender": "张三",
        "content": content,
        "ts": int(time.time()),
        "is_group": True,
        "event": "message.new",
        "msg_type": 1,
        "render_type": "text",
    }


def test_reconcile_updates_existing_task(tmp_path, monkeypatch):
    service = AppService(_settings(tmp_path))
    calls = []

    async def fake_reconcile(messages, events, settings, now=None, identity=None):
        calls.append((messages, events))
        if len(calls) == 1:
            assert events == []
            return ReconcileResult(
                items=[ReconcileItem(
                    event_id=None, event_title="交材料", event_summary="明天下午交材料",
                    tasks=[ReconcileTask(
                        task_id=None, title="交材料", notes="明天下午3点",
                        due_at="2026-10-25T15:00:00",
                        source_msg_ids=[messages[0]["rawid"]],
                    )],
                )],
                summary="新事项",
            )
        event = events[0]
        assert event["title"] == "交材料"
        assert len(event["tasks"]) == 1
        task_id = event["tasks"][0]["id"]
        return ReconcileResult(
            items=[ReconcileItem(
                event_id=event["id"], event_title="交材料",
                event_summary="改到后天下午4点",
                tasks=[ReconcileTask(
                    task_id=task_id, title="交材料（延期）", notes="后天下午4点",
                    due_at="2026-10-26T16:00:00",
                    source_msg_ids=[messages[0]["rawid"]],
                )],
            )],
            summary="更新截止时间",
        )

    monkeypatch.setattr(service_module, "llm_reconcile", fake_reconcile)

    asyncio.run(service.process_batch([_msg("m1", "明天下午3点交材料")]))
    tasks = service.db.list_tasks("pending")
    assert len(tasks) == 1
    event_id = tasks[0]["event_id"]
    assert event_id
    assert service.db.count_open_events() == 1

    asyncio.run(service.process_batch([_msg("m2", "记得交材料，改到后天下午4点")]))
    tasks = service.db.list_tasks("pending")
    assert len(tasks) == 1, "后续消息应更新原任务，而不是新建"
    assert tasks[0]["due_at"] == "2026-10-26T16:00:00"
    assert tasks[0]["title"] == "交材料（延期）"
    assert set(tasks[0]["source_msg_ids"]) == {"m1", "m2"}
    assert tasks[0]["event_id"] == event_id
    service.db.close()


def test_one_message_maps_to_multiple_tasks(tmp_path, monkeypatch):
    service = AppService(_settings(tmp_path))

    async def fake_reconcile(messages, events, settings, now=None, identity=None):
        return ReconcileResult(
            items=[ReconcileItem(
                event_id=None, event_title="周五前办两件事", event_summary="交表+缴费",
                tasks=[
                    ReconcileTask(task_id=None, title="交报名表", due_at="2026-10-30T18:00:00",
                                  source_msg_ids=[messages[0]["rawid"]]),
                    ReconcileTask(task_id=None, title="缴报名费", due_at="2026-10-30T18:00:00",
                                  source_msg_ids=[messages[0]["rawid"]]),
                ],
            )],
            summary="一条消息拆出两个待办",
        )

    monkeypatch.setattr(service_module, "llm_reconcile", fake_reconcile)
    asyncio.run(service.process_batch([_msg("m1", "周五前交报名表，然后缴报名费")]))
    tasks = service.db.list_tasks("pending")
    assert len(tasks) == 2
    assert {task["event_id"] for task in tasks} == {tasks[0]["event_id"]}
    service.db.close()


def test_rule_fallback_creates_event(tmp_path):
    service = AppService(_settings(tmp_path, llm_enabled=False))
    asyncio.run(service.process_batch([_msg("m1", "明天下午3点交材料，记得带身份证")]))
    tasks = service.db.list_tasks("pending")
    assert len(tasks) == 1
    assert tasks[0]["event_id"]
    assert service.db.get_event(tasks[0]["event_id"])["status"] == "open"
    service.db.close()


def test_short_private_reminder_reaches_llm(tmp_path, monkeypatch):
    """像“明早8点去上课”这种短提醒，不能被行动词预过滤挡在 LLM 之前。"""
    service = AppService(_settings(tmp_path))
    seen = []

    async def fake_reconcile(messages, events, settings, now=None, identity=None):
        seen.extend(message["content"] for message in messages)
        return ReconcileResult(
            items=[ReconcileItem(
                event_id=None, event_title="明早上课", event_summary="明早8点去上课",
                tasks=[ReconcileTask(
                    task_id=None, title="明早8点去上课", notes="",
                    due_at="2026-10-26T08:00:00",
                    source_msg_ids=[messages[0]["rawid"]],
                )],
            )],
            summary="短提醒",
        )

    monkeypatch.setattr(service_module, "llm_reconcile", fake_reconcile)
    message = _msg("m1", "明早8点去上课", session="wxid_contact")
    message["is_group"] = False
    message["group_name"] = "项目群"
    asyncio.run(service.process_batch([message]))

    assert seen == ["明早8点去上课"]
    tasks = service.db.list_tasks("pending")
    assert len(tasks) == 1
    assert tasks[0]["due_at"] == "2026-10-26T08:00:00"
    service.db.close()


def _identity_for_test():
    from app.identity import IdentityContext

    return IdentityContext(
        session_id="123@chatroom", group_name="项目群", is_group=True,
        account="wxid_me", aliases=["大王", "001大王"], group_nickname="001大王",
        members={"某某": "wxid_wudi"},
        member_ids={"wxid_wudi": "某某"},
        auto=True,
    )


def _fake_reconcile_per_message(seen):
    async def fake_reconcile(messages, events, settings, now=None, identity=None):
        seen.extend(message["rawid"] for message in messages)
        return ReconcileResult(
            items=[ReconcileItem(
                event_id=None, event_title="本轮事项", event_summary="",
                tasks=[ReconcileTask(
                    task_id=None, title=f"待办{message['rawid']}",
                    source_msg_ids=[message["rawid"]],
                ) for message in messages],
            )],
            summary="x",
        )

    return fake_reconcile


def test_identity_filter_drops_other_addressed_message(tmp_path, monkeypatch):
    """@别人/点名别人的消息不建待办；@我 / 未指定对象的照旧。"""
    service = AppService(_settings(tmp_path))

    async def fake_for_session(*args, **kwargs):
        return _identity_for_test()

    monkeypatch.setattr(service.identity, "for_session", fake_for_session)
    seen = []
    monkeypatch.setattr(service_module, "llm_reconcile", _fake_reconcile_per_message(seen))

    me = _msg("m1", "@001大王 明天交表")
    me["at_usernames"] = ["wxid_me"]
    other = _msg("m2", "@某某 你去谈一个")
    other["at_usernames"] = ["wxid_wudi"]
    plain = _msg("m3", "明天下午三点开组会，记得带材料")
    stats = asyncio.run(service.process_batch([me, other, plain]))

    assert seen == ["m1", "m3"], "指向他人的消息不应进入 LLM"
    assert stats["dropped_other"] == 1
    tasks = {task["source_msg_ids"][0]: task for task in service.db.list_tasks("pending")}
    assert set(tasks) == {"m1", "m3"}
    assert tasks["m1"]["match_reason"] == "@我（001大王）"
    assert tasks["m3"]["match_reason"] == ""
    assert service._judge_stats["dropped_other"] == 1
    assert service._recent_drops[-1]["targets"] == ["某某"]
    service.db.close()


def test_identity_filter_off_keeps_old_behavior(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    settings.identity_filter = False
    service = AppService(settings)

    async def fake_for_session(*args, **kwargs):
        return _identity_for_test()

    monkeypatch.setattr(service.identity, "for_session", fake_for_session)
    seen = []
    monkeypatch.setattr(service_module, "llm_reconcile", _fake_reconcile_per_message(seen))

    other = _msg("m1", "@某某 你去谈一个")
    other["at_usernames"] = ["wxid_wudi"]
    stats = asyncio.run(service.process_batch([other]))

    assert seen == ["m1"]
    assert stats["dropped_other"] == 0
    service.db.close()

