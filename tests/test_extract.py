import asyncio
import json
import logging
import sys
import types
from datetime import datetime

import pytest

from app.config import Settings
from app.extract import (LLMUnavailable, _normalize_reconcile, llm_reconcile,
                         normalize_title, parse_due, rule_extract, titles_similar)

NOW = datetime(2026, 10, 23, 21, 0)  # 周五晚 9 点


def _msg(content: str, rawid: str, group: str = "项目群", sender: str = "同学") -> dict:
    return {"rawid": rawid, "session_id": "123@chatroom", "group_name": group,
            "sender": sender, "content": content, "ts": 0, "is_group": True}


def test_parse_due_relative_and_absolute():
    assert parse_due("明天下午3点开组会", NOW) == "2026-10-24T15:00:00"
    assert parse_due("今晚8点前把取件码发我", NOW) == "2026-10-23T20:00:00"
    assert parse_due("10月28日之前交作业", NOW) == "2026-10-28T23:59:00"
    assert parse_due("10/30 18:00 开会", NOW) == "2026-10-30T18:00:00"
    assert parse_due("这消息没有时间", NOW) is None


def test_parse_due_chinese_time():
    morning = datetime(2026, 10, 24, 9, 0)
    assert parse_due("下午五点前收齐", morning) == "2026-10-24T17:00:00"
    assert parse_due("晚上八点前发我", morning) == "2026-10-24T20:00:00"
    assert parse_due("十点前交", morning) == "2026-10-24T10:00:00"
    assert parse_due("12点30分截止", morning) == "2026-10-24T12:30:00"


def test_rule_extract_filters_noise_and_creates_tasks():
    messages = [
        _msg("【通知】本周五18:00前提交报名表，逾期不候", "m1"),
        _msg("明天下午3点开组会，记得带上上周的材料", "m2"),
        _msg("哈哈哈哈这也行", "m3"),
        _msg("收到", "m4"),
        _msg("大家记得10月28日之前交作业，提交到学习通", "m5", group="课程群"),
        _msg("今晚8点前把取件码发我，不然要退回", "m6", group="生活群"),
    ]
    result = rule_extract(messages, Settings(me_names=["小明"]), NOW)
    assert result.mode == "rule"
    assert len(result.tasks) >= 4
    titles = [task.title for task in result.tasks]
    assert not any(title in ("收到", "哈哈哈哈这也行") for title in titles)
    assert any(task.due_at == "2026-10-24T15:00:00" for task in result.tasks)
    assert any(task.due_at == "2026-10-28T23:59:00" for task in result.tasks)
    assert all(task.source_msg_ids for task in result.tasks)


def test_urgent_task_is_high_priority():
    messages = [_msg("紧急：明天上午10点前必须提交材料", "m1")]
    result = rule_extract(messages, Settings(), NOW)
    assert result.tasks
    assert result.tasks[0].priority == "high"


def test_parse_due_prefers_explicit_date_over_weekday():
    # “11月1日 也就是下周日下午两点半”应以具体日期为准（NOW 是 10-23 周五）
    assert parse_due("我们最终决定在11月1日 也就是下周日下午两点半开始破冰", NOW) == "2026-11-01T14:30:00"


def test_rule_extract_recalls_form_and_activity_notice():
    """链接卡片「人数统计」和活动通知「大家尽量来参与」都要能建待办。"""
    messages = [
        _msg("11月1日科创破冰活动人数统计", "m1", group="项目群"),
        _msg("根据投票结果，我们最终决定在11月1日 也就是下周日下午两点半开始破冰，"
             "有空的话大家尽量都来参与一下破冰", "m2", group="项目群"),
    ]
    result = rule_extract(messages, Settings(), NOW)
    titles = [task.title for task in result.tasks]
    assert any("填写" in title or "统计" in title for title in titles), titles
    assert any(task.due_at == "2026-11-01T14:30:00" for task in result.tasks), [
        (task.title, task.due_at) for task in result.tasks
    ]


def test_reconcile_prompt_contains_recall_guidance():
    from app.extract import _build_reconcile_prompt
    from app.identity import IdentityContext

    prompt = _build_reconcile_prompt(
        [_msg("11月1日科创破冰活动人数统计", "m1")], [], NOW,
        IdentityContext(group_nickname="001大王", aliases=["001大王"]),
    )
    assert "填写" in prompt and "人数统计" in prompt
    assert "面向「大家/同学们/全体」" in prompt
    assert "001大王" in prompt


def test_reconcile_prompt_rejects_reminder_notice():
    """提醒/规范类消息（如志愿着装要求）必须在提示词里被明确排除，不能生成待办。"""
    from app.extract import _build_reconcile_prompt

    prompt = _build_reconcile_prompt(
        [
            _msg("因为前两天有人穿会服去做志愿，所以提醒下", "m1"),
            _msg("会服一般只有开全体大会要穿，平时出志愿不用穿", "m2"),
            _msg("所以这几天出志愿也是，着装得体，不穿拖鞋，其他没有要求的", "m3"),
        ],
        [], NOW,
    )
    assert "不要建待办" in prompt
    assert "反例" in prompt
    assert "因为前两天有人穿会服去做志愿" in prompt
    # 召回条件必须收紧为「明确要求我报名/参加/集合」，不能只因为提到“志愿服务”就建待办
    assert "明确要求我报名/参加/集合" in prompt
    assert "只是提到这类活动的提醒不算" in prompt
    # 但不要求必须有截止时间：没有时间的任务也要建（due 填 null）
    assert "不要求有截止时间" in prompt
    assert "没有截止时间的任务照常输出" in prompt


def test_reconcile_prompt_guides_low_confidence_todos():
    """信息不全/指代不明/没点名时，提示词必须要求建低置信度待办，而不是一律不建。"""
    from app.extract import _build_reconcile_prompt

    prompt = _build_reconcile_prompt([_msg("那第2个还是老时间", "m1")], [], NOW)
    assert "低置信度待办" in prompt
    assert "信息不全" in prompt
    assert "[低置信度:" in prompt
    for sample in ("就按昨天说的第1件事来", "那第2个还是老时间",
                   "明天上午把第3批材料带到办公室", "第1次例会改到周五下午了"):
        assert sample in prompt, sample
    # 旧的“拿不准一律不建”会与“低置信度待办”冲突，必须已被替换
    assert "拿不准是否需要我执行的内容，一律不建任务" not in prompt
    assert "输出前自检" in prompt


def test_normalize_reconcile_keeps_low_confidence_task():
    """模型按提示词输出低置信度任务时，字段（low 优先级/notes 标注/来源）不能被归一化丢掉。"""
    from app.extract import _normalize_reconcile

    messages = [_msg("那第2个还是老时间", "r1")]
    data = {
        "summary": "低置信度示例",
        "items": [{
            "event_id": "", "event_title": "跟进第2项安排", "event_summary": "",
            "tasks": [{
                "task_id": "", "title": "跟进第2项安排", "due": None,
                "priority": "low",
                "notes": "[低置信度:缺对象] 原文：那第2个还是老时间",
                "assignee": "unclear", "source_msg_ids": ["m1"],
            }],
        }],
    }
    result = _normalize_reconcile(data, messages, [], NOW)
    assert result is not None and result.items
    task = result.items[0].tasks[0]
    assert task.priority == "low"
    assert task.notes.startswith("[低置信度:缺对象]")
    assert task.source_msg_ids == ["r1"]


# ---------------------------------------------------------------------------
# P2-2：规则模式批内相似标题去重（同一事项不再重复建待办）
# ---------------------------------------------------------------------------

def test_rule_extract_merges_similar_titles_same_due():
    """同一件事被“再提醒一次”，规则模式应并入已有任务而不是再建一条。"""
    messages = [
        _msg("填写11月1日科创破冰活动人数统计，截止11月1日", "m1"),
        _msg("再提醒一次：报名请填写11月1日科创破冰活动人数统计，截止11月1日", "m2"),
    ]
    result = rule_extract(messages, Settings(), NOW)
    assert len(result.tasks) == 1, [(task.title, task.due_at) for task in result.tasks]
    assert result.tasks[0].source_msg_ids == ["m1", "m2"]
    assert result.cap_hit is False


def test_rule_extract_does_not_merge_different_due():
    """标题相似但截止日期不同（不同一期活动）不能合并。"""
    messages = [
        _msg("填写11月1日科创破冰活动人数统计", "m1"),
        _msg("填写11月2日科创破冰活动人数统计", "m2"),
    ]
    result = rule_extract(messages, Settings(), NOW)
    assert len(result.tasks) == 2, [(task.title, task.due_at) for task in result.tasks]


def test_normalize_title_strips_punct_and_fillers():
    assert normalize_title(" 【提醒】记得：今晚，8点前 交材料！ ") == "今晚8点前交材料"
    assert normalize_title("再提醒一次：报名请填写人数统计") == "报名填写人数统计"
    assert normalize_title("请填写报名表") == "填写报名表"


def test_titles_similar_positive_and_negative():
    assert titles_similar(
        "报名请填写11月1日科创破冰活动人数统计",
        "再提醒一次：报名请填写11月1日科创破冰活动人数统计",
    ) is True
    # 无关事项不能被误合并
    assert titles_similar("提交材料", "缴纳班费") is False
    assert titles_similar("明天交材料", "明天交作业") is False


# ---------------------------------------------------------------------------
# P1-1：提取上限可观测（cap_hit）
# ---------------------------------------------------------------------------

def test_rule_extract_cap_hit_when_hitting_12_limit():
    verbs = ["提交材料", "填写表格", "报名活动", "领取快递", "参加面试", "开会讨论",
             "缴纳班费", "打卡签到", "预订餐位", "发送作业", "登记信息", "参加考试",
             "集合出发"]
    dates = [f"10月{day}日" for day in range(24, 32)] + [f"11月{day}日" for day in range(1, 6)]
    messages = [_msg(f"{verbs[i]}，截止{dates[i]}之前", f"m{i + 1}") for i in range(13)]
    result = rule_extract(messages, Settings(), NOW)
    assert result.cap_hit is True
    assert len(result.tasks) == 12


def test_rule_extract_cap_hit_false_below_limit():
    result = rule_extract([_msg("明天上午10点前提交材料", "m1")], Settings(), NOW)
    assert result.cap_hit is False
    assert len(result.tasks) == 1


def test_normalize_reconcile_cap_hit_on_item_limit():
    messages = [_msg("消息", "r1")]
    data = {
        "summary": "",
        "items": [
            {"event_id": "", "event_title": f"事项{i}", "event_summary": "",
             "tasks": [{"task_id": "", "title": f"任务{i}", "due": None,
                        "priority": "normal", "source_msg_ids": ["m1"]}]}
            for i in range(8)
        ],
    }
    result = _normalize_reconcile(data, messages, [], NOW)
    assert result is not None
    assert result.cap_hit is True
    assert result.items_returned == 8
    assert result.tasks_returned == 8


def test_normalize_reconcile_truncates_and_reports_task_count():
    messages = [_msg("消息", "r1")]
    data = {
        "summary": "",
        "items": [{
            "event_id": "", "event_title": "任务很多的事项", "event_summary": "",
            "tasks": [
                {"task_id": "", "title": f"任务{i}", "due": None,
                 "priority": "normal", "source_msg_ids": ["m1"]}
                for i in range(13)
            ],
        }],
    }
    result = _normalize_reconcile(data, messages, [], NOW)
    assert result is not None and result.items
    assert result.cap_hit is True
    assert result.items_returned == 1
    assert result.tasks_returned == 13
    assert len(result.items[0].tasks) == 12


def test_normalize_reconcile_no_cap_hit_below_limit():
    messages = [_msg("消息", "r1")]
    data = {
        "summary": "",
        "items": [{
            "event_id": "", "event_title": "一件事", "event_summary": "",
            "tasks": [{"task_id": "", "title": "交材料", "due": None,
                       "priority": "normal", "source_msg_ids": ["m1"]}],
        }],
    }
    result = _normalize_reconcile(data, messages, [], NOW)
    assert result is not None
    assert result.cap_hit is False
    assert result.items_returned == 1
    assert result.tasks_returned == 1


# ---------------------------------------------------------------------------
# P1-2：LLM 失败不再静默（抛出 LLMUnavailable，带 kind）
# ---------------------------------------------------------------------------

class _FakeLLMMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeLLMChoice:
    def __init__(self, content: str) -> None:
        self.message = _FakeLLMMessage(content)


class _FakeLLMResponse:
    def __init__(self, content: str) -> None:
        self.choices = [_FakeLLMChoice(content)]


def _install_fake_openai(monkeypatch, *, content: str = "{}",
                         error: Exception | None = None) -> None:
    """把 openai.AsyncOpenAI 换成假客户端：不发真实网络请求，按需返回内容或抛错。"""
    class _Completions:
        async def create(self, *args, **kwargs):
            if error is not None:
                raise error
            return _FakeLLMResponse(content)

    class _Chat:
        def __init__(self) -> None:
            self.completions = _Completions()

    class _AsyncOpenAI:
        def __init__(self, *args, **kwargs) -> None:
            self.chat = _Chat()

    monkeypatch.setitem(sys.modules, "openai",
                        types.SimpleNamespace(AsyncOpenAI=_AsyncOpenAI))


def _llm_settings() -> Settings:
    return Settings(llm_enabled=True, llm_api_key="test-key")


def _run_reconcile(settings: Settings):
    return asyncio.run(llm_reconcile([_msg("记得明天交材料", "r1")], [], settings, NOW))


def test_llm_unavailable_constructor():
    error = LLMUnavailable("context overflow", kind="context_limit")
    assert isinstance(error, RuntimeError)
    assert error.detail == "context overflow"
    assert error.kind == "context_limit"
    assert str(error) == "context overflow"
    assert LLMUnavailable("网络错误").kind == "llm_error"


def test_llm_reconcile_raises_context_limit(monkeypatch, caplog):
    _install_fake_openai(
        monkeypatch,
        error=RuntimeError(
            "Error code: 400 - context_length_exceeded: maximum context length is 65536 tokens"),
    )
    with caplog.at_level(logging.WARNING, logger="app.extract"):
        with pytest.raises(LLMUnavailable) as excinfo:
            _run_reconcile(_llm_settings())
    assert excinfo.value.kind == "context_limit"
    assert "context_length" in excinfo.value.detail
    assert any("LLM 事项归并失败" in record.getMessage() for record in caplog.records)


def test_llm_reconcile_raises_format_error(monkeypatch):
    _install_fake_openai(monkeypatch, content="这不是 JSON")
    with pytest.raises(LLMUnavailable) as excinfo:
        _run_reconcile(_llm_settings())
    assert excinfo.value.kind == "format"


def test_llm_reconcile_raises_llm_error(monkeypatch):
    _install_fake_openai(monkeypatch, error=RuntimeError("HTTP 500 Internal Server Error"))
    with pytest.raises(LLMUnavailable) as excinfo:
        _run_reconcile(_llm_settings())
    assert excinfo.value.kind == "llm_error"


def test_llm_reconcile_success_path_unchanged(monkeypatch):
    data = {
        "summary": "有 1 项",
        "items": [{
            "event_id": "", "event_title": "交材料", "event_summary": "",
            "tasks": [{"task_id": "", "title": "交材料", "due": None,
                       "priority": "normal", "assignee": "me",
                       "source_msg_ids": ["m1"]}],
        }],
    }
    _install_fake_openai(monkeypatch, content=json.dumps(data))
    result = _run_reconcile(_llm_settings())
    assert result is not None
    assert result.mode == "llm"
    assert result.cap_hit is False
    assert result.items_returned == 1
    assert result.tasks_returned == 1
    assert result.items[0].tasks[0].source_msg_ids == ["r1"]
