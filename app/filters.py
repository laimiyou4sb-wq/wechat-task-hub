"""消息过滤与「重要性」打分。"""
from __future__ import annotations

import re

from .utils import clip

NOISE_PREFIXES = (
    "[图片]", "[表情]", "[动画表情]", "[语音]", "[视频]", "[文件]", "[链接]",
    "[位置]", "[名片]", "[小程序]", "[转账]", "[红包]", "[音乐]", "[收藏]",
)

NOISE_CONTAINS = (
    "人数满了", "已满", "满了", "结束了", "已完成", "已经完成", "收到",
    "可以吗", "怎么办", "为什么", "有问题的", "只发通知",
    # 广告/拼车/外卖类群消息
    "车找人", "人找车", "网约车", "顺风车", "拼车", "包车", "帮邦行",
    "有位置", "马上走", "联系电话", "联系方式", "今日特惠", "特惠",
    "优惠券", "拼单", "外卖",
)

SMALL_TALK = {
    "收到", "好的", "好", "嗯", "嗯嗯", "ok", "okay", "谢谢", "多谢", "哈哈",
    "哈哈哈", "哈哈哈哈", "早", "早安", "晚安", "在吗", "在", "1", "+1", "顶",
    "666", "牛", "可以", "行", "没问题", "辛苦了", "辛苦",
}

# 只保留偏“任务/通知”的词；"通知/请/确认/回复/安排/约" 太宽泛，会制造大量假阳性
ACTION_KEYWORDS = (
    "记得", "截止", "ddl", "DDL", "报名", "提交", "交材料", "交作业",
    "开会", "会议", "面试", "务必", "尽快", "前完成", "报名表", "接龙",
    "填写", "参加", "考试", "作业", "签到", "打卡", "缴费", "付款",
    "转账", "快递", "取件", "排练", "集合", "出发", "预订", "预约",
    "办理", "领取", "聚餐", "汇报", "验收", "截止到", "最后期限",
    # 填表 / 问卷 / 统计 / 活动类（群里常见但以前容易漏召回）
    "填表", "填一下", "填好", "填报", "表格", "表单", "问卷", "统计",
    "人数统计", "收集", "登记", "参与", "招募", "志愿", "培训", "活动",
    "投票", "交表", "文档", "接龙报名", "报名接龙", "确认参加", "参加活动",
)

URGENT_KEYWORDS = (
    "紧急", "尽快", "马上", "立刻", "今天", "今晚", "最晚", "务必", "重要",
    "注意", "最后", "逾期", "过期", "ASAP", "asap",
)

# 链接卡片标题里出现这些词，说明“卡片背后有要填/要报/要参加的事”，别当普通卡片过滤掉
EVENT_HINTS = (
    "报名", "统计", "问卷", "表格", "接龙", "填写", "填表", "活动",
    "培训", "会议", "面试", "志愿", "招募", "投票", "文档", "登记",
    "人数", "预约", "集合",
)

DATE_PATTERNS = [
    re.compile(r"(?P<month>\d{1,2})\s*月\s*(?P<day>\d{1,2})\s*[日号]?"),
    re.compile(r"(?<!\d)(?P<month>\d{1,2})[/-](?P<day>\d{1,2})(?!\d)"),
]

TIME_PATTERN = re.compile(
    r"(今天|今晚|明天|明早|明晚|后天|本周|这周|下周|"
    r"周[一二三四五六日天]|星期[一二三四五六日天]|"
    r"\d{1,2}\s*[:：点])"
)

DEADLINE_WORDS = ("截止", "ddl", "DDL", "之前", "前完成", "最后期限", "最晚", "前交", "前提交")


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def is_allowed_group(session_id: str, group_name: str, whitelist: list[str],
                     is_group: bool, include_private: bool) -> bool:
    if not is_group and not include_private:
        return False
    if not whitelist:
        return True
    haystack = f"{session_id} {group_name}".lower()
    return any(item.lower() in haystack for item in whitelist)


def should_keep(content: str, me_names: list[str] | None = None) -> bool:
    text = normalize(content)
    if not text:
        return False
    if text.startswith(NOISE_PREFIXES):
        return False
    if len(text) <= 2:
        return False
    if text.lower() in SMALL_TALK:
        return False
    # 纯表情 / 纯标点
    if not re.search(r"[\u4e00-\u9fffA-Za-z0-9]", text):
        return False
    # 只有链接
    if re.fullmatch(r"https?://\S+", text):
        return False
    # 明确的“闲聊/已结束”信号；但若正文里同时带着动作/时间/截止/@我，
    # 说明只是顺带说了句“收到/已满”，正事不能整条丢掉（P2-1）
    if any(item in text for item in NOISE_CONTAINS) and not _has_task_signal(text, me_names):
        return False
    # 以“吗/呢/吧”结尾且没有时间/截止/@我：大概率是提问或闲聊
    if (re.search(r"[吗呢吧]\s*[。！!？?…]*$", text)
            and not mentions_me(text, me_names)
            and not has_time_signal(text)):
        return False
    # 短句且无任何行动词：大概率是闲聊
    if len(text) < 8 and not has_action_hint(text) and not mentions_me(text, me_names):
        return False
    return True


def mentions_me(content: str, me_names: list[str] | None) -> bool:
    text = content or ""
    if "@所有人" in text or "@全体成员" in text:
        return True
    for name in me_names or []:
        if name and (f"@{name}" in text or name in text):
            return True
    return False


def should_send_to_llm(content: str, me_names: list[str] | None = None) -> bool:
    """LLM 模式下的轻量前置过滤：只挡明显噪音，把判断交给模型。

    与 should_keep 不同，这里不因“句子短/没有行动词”而丢弃，
    否则像“明早8点去上课”这种短提醒会到不了模型。
    """
    text = normalize(content)
    if not text:
        return False
    if text.startswith(NOISE_PREFIXES):
        return False
    if len(text) <= 2:
        return False
    if text.lower() in SMALL_TALK:
        return False
    if not re.search(r"[\u4e00-\u9fffA-Za-z0-9]", text):
        return False
    if re.fullmatch(r"https?://\S+", text):
        return False
    if any(item in text for item in NOISE_CONTAINS) and not _has_task_signal(text, me_names):
        return False
    return True


def has_action_hint(content: str) -> bool:
    text = content or ""
    return any(keyword in text for keyword in ACTION_KEYWORDS)


def has_time_signal(content: str) -> bool:
    text = content or ""
    return bool(TIME_PATTERN.search(text)) or any(pattern.search(text) for pattern in DATE_PATTERNS)


def has_deadline_signal(content: str) -> bool:
    text = content or ""
    return any(keyword in text for keyword in DEADLINE_WORDS)


def _has_task_signal(content: str, me_names: list[str] | None = None) -> bool:
    """正文里是否还带着“要做的事”的信号（动作/时间/截止/@我）。

    噪音词是子串匹配（“收到”“已满”“结束了”…），单独出现才是噪音；
    如果同一条消息还带着待办信号，就交给后面的规则/模型判断，不能按噪音整条过滤。
    """
    return (has_action_hint(content) or has_time_signal(content)
            or has_deadline_signal(content) or mentions_me(content, me_names))


def importance(content: str, me_names: list[str] | None = None) -> int:
    """0-10 分，越大越值得看。"""
    text = content or ""
    score = 0
    if mentions_me(text, me_names):
        score += 4
    if has_action_hint(text):
        score += 3
    if any(keyword in text for keyword in URGENT_KEYWORDS):
        score += 2
    if has_time_signal(text):
        score += 1
    if has_deadline_signal(text):
        score += 2
    if "？" in text or "?" in text:
        score += 1
    if len(text) >= 30:
        score += 1
    return min(score, 10)


def is_worth_summarizing(content: str, me_names: list[str] | None = None) -> bool:
    if not should_keep(content, me_names):
        return False
    return importance(content, me_names) >= 3


def summarize_for_log(content: str, limit: int = 60) -> str:
    return clip(content, limit)
