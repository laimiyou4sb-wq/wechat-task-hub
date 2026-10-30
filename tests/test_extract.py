from datetime import datetime

from app.config import Settings
from app.extract import parse_due, rule_extract

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
