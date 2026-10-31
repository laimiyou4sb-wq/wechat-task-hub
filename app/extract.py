"""把一批微信消息归纳成待办任务：优先大模型，失败回退本地规则。"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from .config import Settings
from .filters import has_action_hint, importance, is_worth_summarizing
from .identity import IdentityContext
from .utils import clip, now_local, now_iso, parse_iso, strip_urls

logger = logging.getLogger(__name__)


@dataclass
class ExtractedTask:
    title: str
    notes: str = ""
    due_at: str | None = None
    priority: str = "normal"
    group_name: str = ""
    source_msg_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "notes": self.notes,
            "due_at": self.due_at,
            "priority": self.priority,
            "group_name": self.group_name,
            "source_msg_ids": self.source_msg_ids,
        }


@dataclass
class ExtractResult:
    summary: str
    tasks: list[ExtractedTask]
    mode: str = "rule"


WEEKDAYS = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
             "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _cn_to_int(token: str) -> int | None:
    """把「五」「十」「十二」「二十」转成整数。"""
    if not token:
        return None
    if token == "十":
        return 10
    if "十" in token:
        left, _, right = token.partition("十")
        tens = CN_DIGITS.get(left, 0) if left else 1
        ones = CN_DIGITS.get(right, 0) if right else 0
        return tens * 10 + ones
    if len(token) == 1:
        return CN_DIGITS.get(token)
    return None


def _apply_period(hour: int, text: str) -> int:
    if re.search(r"下午|傍晚|晚上|夜里|今晚|明晚", text) and hour < 12:
        return hour + 12
    if re.search(r"上午|早上|早晨|凌晨", text) and hour == 12:
        return 0
    if re.search(r"中午", text) and hour < 11:
        return hour + 12
    return hour


def _dedupe_key(title: str, due_at: str | None, group_name: str) -> str:
    raw = f"{title.strip().lower()}|{due_at or ''}|{group_name or ''}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _extract_time(text: str) -> tuple[int, int] | None:
    # 中文数字：五点 / 五点半 / 十二点二十
    cn_match = re.search(
        r"(?P<cn>[零一二两三四五六七八九十]{1,3})\s*点\s*(?P<tail>半|\d{1,2}\s*分?)?",
        text,
    )
    if cn_match:
        hour = _cn_to_int(cn_match.group("cn"))
        if hour is not None:
            tail = (cn_match.group("tail") or "").strip()
            if tail.startswith("半"):
                minute = 30
            elif tail:
                minute = int(re.sub(r"\D", "", tail) or 0)
            else:
                minute = 0
            hour = _apply_period(hour, text)
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return hour, minute

    match = re.search(r"(?<!\d)(?P<h>\d{1,2})\s*[:：点]\s*(?P<m>\d{1,2})?(?!\d)", text)
    if not match:
        match = re.search(r"(?P<h>\d{1,2})\s*点\s*(?P<m>\d{1,2})?", text)
    if not match:
        return None
    hour = int(match.group("h"))
    minute = int(match.group("m") or 0)
    hour = _apply_period(hour, text)
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return hour, minute
    return None


def parse_due(text: str, now: datetime | None = None) -> str | None:
    """从中文消息里尽量解析出截止时间；解析不出返回 None。"""
    now = now or now_local()
    text = text or ""
    explicit_time = _extract_time(text)
    target_date = None

    if "今天" in text or "今晚" in text or "本日" in text:
        target_date = now.date()
    elif "明天" in text or "明早" in text or "明晚" in text:
        target_date = now.date() + timedelta(days=1)
    elif "后天" in text:
        target_date = now.date() + timedelta(days=2)
    else:
        # 明确写了“11月1日”时优先用具体日期（同时出现“下周日”时也不冲突）
        for pattern in (
            re.compile(r"(?P<month>\d{1,2})\s*月\s*(?P<day>\d{1,2})\s*[日号]?"),
            re.compile(r"(?<!\d)(?P<month>\d{1,2})[/-](?P<day>\d{1,2})(?!\d)"),
        ):
            match = pattern.search(text)
            if match:
                month, day = int(match.group("month")), int(match.group("day"))
                if 1 <= month <= 12 and 1 <= day <= 31:
                    try:
                        candidate = now.date().replace(month=month, day=day)
                    except ValueError:
                        continue
                    if candidate < now.date():
                        candidate = candidate.replace(year=candidate.year + 1)
                    target_date = candidate
                break
        if target_date is None:
            match = re.search(r"(?:周|星期)(?P<w>[一二三四五六日天])", text)
            if match:
                target = WEEKDAYS[match.group("w")]
                delta = (target - now.weekday()) % 7
                target_date = now.date() + timedelta(days=delta)

    if target_date is None and explicit_time is None:
        return None

    if explicit_time is None:
        # 只有日期：带“前/截止”按当天 23:59，否则默认 18:00
        default = time(23, 59) if re.search(r"前|截止|之前|最晚", text) else time(18, 0)
        hour, minute = default.hour, default.minute
    else:
        hour, minute = explicit_time

    if target_date is None:
        target_date = now.date()
        candidate_dt = datetime.combine(target_date, time(hour, minute))
        if candidate_dt <= now:
            target_date += timedelta(days=1)
    return datetime.combine(target_date, time(hour, minute)).strftime("%Y-%m-%dT%H:%M:%S")


def _clean_title(content: str) -> str:
    text = strip_urls(content)
    text = re.sub(r"^[\s\-•*]+", "", text)
    text = re.sub(r"^【[^】]{1,12}】", "", text).strip()
    text = re.sub(r"^@\S+\s*", "", text).strip()
    # 取第一个句子
    parts = re.split(r"[。！？\n]", text, maxsplit=1)
    title = (parts[0] if parts else text).strip()
    title = clip(title, 36)
    return title or clip(text, 36)


_FORM_TITLE_ACTIONS = (
    ("统计", "填写"), ("问卷", "填写"), ("表格", "填写"), ("填表", "填写"),
    ("填写", "填写"), ("报名", "报名"), ("接龙", "接龙"), ("登记", "填写"),
)


def _action_title(content: str, title: str) -> str:
    """规则模式补一个动词前缀，避免出现「11月1日科创破冰活动人数统计」这种名词标题。"""
    if re.match(r"^(填写|填报|填|报名|参加|参与|接龙|完成|提交|处理)", title):
        return title
    for keyword, verb in _FORM_TITLE_ACTIONS:
        if keyword in content:
            return clip(f"{verb}{title}", 40)
    return title


def rule_extract(messages: list[dict], settings: Settings, now: datetime | None = None,
                 me_names: list[str] | None = None) -> ExtractResult:
    now = now or now_local()
    names = list(dict.fromkeys(list(me_names or []) + list(settings.me_names or [])))
    tasks: list[ExtractedTask] = []
    seen: dict[str, ExtractedTask] = {}

    for message in messages:
        content = (message.get("content") or "").strip()
        if not content or not is_worth_summarizing(content, names):
            continue
        title = _clean_title(content)
        if not title:
            continue
        title = _action_title(content, title)
        due = parse_due(content, now)
        score = importance(content, names)
        priority = "normal"
        if any(word in content for word in ("紧急", "尽快", "马上", "立刻", "务必", "最晚")):
            priority = "high"
        elif due and due[:10] == now.strftime("%Y-%m-%d"):
            priority = "high"
        elif not due and score < 5:
            priority = "low"

        key = _dedupe_key(title, due, message.get("group_name", ""))
        if key in seen:
            seen[key].source_msg_ids.append(message.get("rawid", ""))
            continue
        task = ExtractedTask(
            title=title,
            notes=clip(content, 400),
            due_at=due,
            priority=priority,
            group_name=message.get("group_name", ""),
            source_msg_ids=[message.get("rawid", "")],
        )
        seen[key] = task
        tasks.append(task)
        if len(tasks) >= 12:
            break

    summary = f"本批 {len(messages)} 条消息，筛出 {len(tasks)} 项待办" if tasks else "本批消息无明确待办"
    return ExtractResult(summary=summary, tasks=tasks, mode="rule")


LLM_SYSTEM_PROMPT = """你是用户的微信消息助理，负责把群聊消息整理成少量、可执行的待办任务。
只创建需要用户本人执行或跟进、且能落到具体动作的事项；单纯的提醒、规范、着装/纪律说明、背景解释不是任务。
只输出 JSON，不要输出解释。"""


def _build_llm_prompt(messages: list[dict], now: datetime) -> str:
    lines = []
    for idx, message in enumerate(messages, 1):
        group = message.get("group_name") or "私聊"
        sender = message.get("sender") or ""
        content = clip(message.get("content") or "", 300)
        lines.append(f"{idx}. [{group}] {sender}: {content}")
    joined = "\n".join(lines)
    return f"""现在时间：{now.strftime('%Y-%m-%d %H:%M')}（{['周一','周二','周三','周四','周五','周六','周日'][now.weekday()]}）。

以下是最近收到的 {len(messages)} 条微信消息：
{joined}

请找出其中「需要用户本人执行或跟进、且能落到具体动作」的事项，生成待办；忽略闲聊、表情、广告、纯提醒/规范说明、无行动的通知。
输出 JSON（不要 markdown 代码块）：
{{
  "summary": "一句话概括这批消息的重点",
  "tasks": [
    {{
      "title": "动词开头的短标题，不超过 20 字",
      "due": "YYYY-MM-DD HH:MM 或 null（只有能从原文明确推算时才填）",
      "priority": "high | normal | low",
      "notes": "原文里的关键信息（时间、地点、要求）",
      "source_quote": "对应的原文句子",
      "group": "群名"
    }}
  ]
}}

要求：
- 最多 8 条；宁缺毋滥，没有明确行动就返回空数组；
- 不编造原文没有的信息；
- 时间要结合“现在时间”推算（如“明天下午3点”“本周五前”）。"""


def _normalize_due(raw: str | None, now: datetime) -> str | None:
    if not raw or not str(raw).strip() or str(raw).strip().lower() == "null":
        return None
    text = str(raw).strip()
    parsed = parse_iso(text)
    if parsed is not None:
        return parsed.strftime("%Y-%m-%dT%H:%M:%S")
    return parse_due(text, now)


async def llm_extract(messages: list[dict], settings: Settings, now: datetime | None = None) -> ExtractResult | None:
    if not settings.llm_enabled or not settings.llm_api_key:
        return None
    now = now or now_local()
    try:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=settings.llm_api_key, base_url=settings.llm_base_url,
                             timeout=settings.llm_timeout)
        response = await client.chat.completions.create(
            model=settings.llm_model,
            messages=[
                {"role": "system", "content": LLM_SYSTEM_PROMPT},
                {"role": "user", "content": _build_llm_prompt(messages, now)},
            ],
            temperature=0.2,
            response_format={"type": "json_object"},
        )
        content = (response.choices[0].message.content or "").strip()
        if content.startswith("```"):
            content = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", content).strip()
        data = json.loads(content)
    except Exception as error:  # 网络/额度/格式问题都回退规则
        logger.warning("LLM 提取失败，回退本地规则: %s", error)
        return None

    raw_tasks = data.get("tasks") or []
    if not isinstance(raw_tasks, list):
        return None
    all_ids = [message.get("rawid", "") for message in messages]
    tasks: list[ExtractedTask] = []
    for item in raw_tasks[:8]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        due = _normalize_due(item.get("due"), now)
        priority = str(item.get("priority") or "normal").lower()
        if priority not in ("high", "normal", "low"):
            priority = "normal"
        quote = str(item.get("source_quote") or "").strip()
        source_ids = [message.get("rawid", "") for message in messages
                      if quote and quote[:12] in (message.get("content") or "")]
        tasks.append(ExtractedTask(
            title=clip(title, 40),
            notes=str(item.get("notes") or "").strip() or clip(quote, 400),
            due_at=due,
            priority=priority,
            group_name=str(item.get("group") or "").strip(),
            source_msg_ids=source_ids or all_ids,
        ))
    return ExtractResult(summary=str(data.get("summary") or "").strip(), tasks=tasks, mode="llm")


async def extract_tasks(messages: list[dict], settings: Settings, now: datetime | None = None) -> ExtractResult:
    now = now or now_local()
    result = await llm_extract(messages, settings, now)
    if result is not None:
        return result
    return rule_extract(messages, settings, now)


@dataclass
class ReconcileTask:
    task_id: str | None
    title: str
    notes: str = ""
    due_at: str | None = None
    priority: str = "normal"
    source_msg_ids: list[str] = field(default_factory=list)


@dataclass
class ReconcileItem:
    event_id: str | None
    event_title: str
    event_summary: str
    tasks: list[ReconcileTask] = field(default_factory=list)


@dataclass
class ReconcileResult:
    items: list[ReconcileItem]
    summary: str = ""
    mode: str = "llm"


def task_dedupe_key(task: ExtractedTask) -> str:
    return _dedupe_key(task.title, task.due_at, task.group_name)


RECONCILE_SYSTEM_PROMPT = """你是微信「事项账本」助手。你的任务不是逐条消息生成待办，而是把新消息归并到已有事项（event），或创建新事项。
一个事项可以包含多条消息、可以拆出多个待办任务；后续消息可能补充或修改已存在任务的截止时间/要求。
只创建「需要我本人执行/跟进」的任务：明确 @ 别人、点名别人负责或去做的，不要输出。
群里的填表/接龙/报名/统计人数，以及要求报名/参加/集合的活动通知，只要需要我参与或完成，也要建待办（不必被 @）。
务必区分「任务」和「提醒/规范」：只说明规则、纪律、着装、礼仪、注意事项或原因背景，或提醒大家“注意/别做某事/以后如何”的消息，不是任务，不要生成 task。
拿不准是否要我执行时，宁可不建，也不要把提醒当成待办。
任务不要求有截止时间：没有写时间的任务也要建（due 留空），不要因为缺少截止日期就放过。
只输出 JSON，不要输出解释。"""


def _format_reconcile_messages(messages: list[dict]) -> str:
    lines = []
    for index, message in enumerate(messages, 1):
        group = message.get("group_name") or "私聊"
        sender = message.get("sender") or ""
        content = clip(message.get("content") or "", 400)
        link_title = str(message.get("link_title") or "").strip()
        if link_title and link_title not in content:
            content = f"{content}（链接卡片：{link_title}）" if content else f"（链接卡片：{link_title}）"
        verdict = message.get("_verdict")
        tag = ""
        if verdict is not None and getattr(verdict, "directed", False):
            tag = f"【{verdict.reason}】"
        lines.append(f"[m{index}] {group} | {sender} | {tag}{content}")
    return "\n".join(lines)


def _identity_section(identity: IdentityContext | None) -> str:
    """把“我是谁”写进提示词，让模型能判断任务是否落在我头上。"""
    if identity is None:
        return "（未识别到我的身份信息；按“是否需要我本人行动”判断）"
    lines: list[str] = []
    if identity.group_nickname:
        lines.append(f"- 我在这个群的群昵称：{identity.group_nickname}")
    aliases = [name for name in identity.aliases if name]
    if aliases:
        lines.append(f"- 别人也可能这样称呼我：{'、'.join(dict.fromkeys(aliases))}")
    if identity.account:
        lines.append(f"- 我的微信号：{identity.account}")
    return "\n".join(lines) or "（未识别到我的昵称；按“是否需要我本人行动”判断）"


def _format_reconcile_events(events: list[dict]) -> str:
    if not events:
        return "（暂无打开的事项）"
    lines = []
    for event in events:
        lines.append(f"- event_id={event.get('id')} | {event.get('title') or '未命名'}")
        if event.get("summary"):
            lines.append(f"  摘要：{clip(event['summary'], 200)}")
        for task in event.get("tasks") or []:
            lines.append(
                f"  任务 task_id={task.get('id')} | {task.get('title')} | "
                f"截止={task.get('due_at') or '无'} | 状态={task.get('status')}"
            )
    return "\n".join(lines)


def _build_reconcile_prompt(messages: list[dict], events: list[dict], now: datetime,
                            identity: IdentityContext | None = None) -> str:
    return f"""现在时间：{now.strftime('%Y-%m-%d %H:%M')}（{['周一','周二','周三','周四','周五','周六','周日'][now.weekday()]}）。

# 我的身份
{_identity_section(identity)}

# 本轮新消息（同一个会话，【】里是系统已判定的 @/点名情况）
{_format_reconcile_messages(messages)}

# 该会话仍打开的事项（可以作为归并目标）
{_format_reconcile_events(events)}

请判断这些新消息应该如何归并：
- 如果属于已有事项 → 复用对应 event_id，更新事项标题/摘要；
- 如果是一个新事项 → event_id 留空；
- 一条消息包含多个动作 → 在同一个事项下拆成多个 tasks；
- 多条消息说的是同一件事 → 归到同一个事项；
- 已有任务收到补充/变更（例如截止时间改了）→ 填写对应 task_id 并更新，不要新建重复任务；
- 闲聊、纯表情、广告、无行动信息 → 不要输出；
- 只创建「需要我本人执行/跟进」的任务：明确 @ 别人、点名别人负责/去做的，不要输出；
- 没有明确指向某人的群公告/通知，照常判断是否需要我参与，不要因为“没 @ 我”就丢弃。

# 不要建待办的情况（提醒/规范类，务必区分）
- 消息只是在说明规则、纪律、着装、礼仪、注意事项、时间安排背景，或提醒大家“注意/别做某事/以后如何”，本身没有要我去执行的交付物 → 不要建待办，也不要生成 task（这类消息可以不输出）；
- 反例（这些都不要生成 task）：
  - 「因为前两天有人穿会服去做志愿，所以提醒下」
  - 「会服一般只有开全体大会要穿，平时出志愿不用穿」
  - 「这几天出志愿也是，着装得体，不穿拖鞋，其他没有要求的」
- 判断任务的最低门槛：有明确动作（填写/报名/参加/提交/集合/领取/缴费/联系/确认…），且能说清“要做什么/交什么/去哪”；**不要求有截止时间或具体时间**——原文没写时间也照建，due 填 null，不要因为缺少截止日期就不建；
- 只是“提到”某类活动（如志愿服务、聚餐、培训），但没有让我报名/参加/集合，不算任务。

# 召回要求（以前容易漏，务必注意）
- 群里出现「填写/填表/提交表格/问卷/接龙/报名/统计人数/登记」→ 建一条「填写/报名…」待办，即使原文只有一张链接卡片标题（例如“11月1日科创破冰活动人数统计”），也要建；
- 通知了活动/培训/会议/面试/志愿服务/聚餐，且**明确要求我报名/参加/集合** → 建「参加…」待办；能推算时间就填 due，没有时间也照建（due 填 null）；只是提到这类活动的提醒不算；
- 链接卡片按标题理解：标题含 报名/统计/问卷/表格/接龙/活动/培训/会议/面试/人数/登记 等，视为“要填/要报名/要参加”；
- 面向「大家/同学们/全体」的报名/填写/参加/集合类通知，assignee 填 me（我可能需要参与），不要填 other；纯提醒/规范不算；
- 以上同一件事如果已有任务，只更新，不新建重复任务。

输出 JSON（不要 markdown）：
{{
  "summary": "本轮一句话概括",
  "items": [
    {{
      "event_id": "已有事项 id 或空字符串",
      "event_title": "事项短标题（不超过 20 字）",
      "event_summary": "事项当前进展/要求摘要",
      "tasks": [
        {{
          "task_id": "已有任务 id 或空字符串",
          "title": "动词开头的短标题",
          "due": "YYYY-MM-DD HH:MM 或 null",
          "priority": "high | normal | low",
          "notes": "关键信息：时间/地点/要求",
          "assignee": "me | other | unclear",
          "source_msg_ids": ["m1", "m2"]
        }}
      ]
    }}
  ]
}}

约束：
- event_id 只能引用上面列出的 id；task_id 只能引用该事项下列出的 id；
- source_msg_ids 只能填本轮消息编号（m1、m2…），一个任务可以对应多条消息；
- assignee 表示这件事实际要谁做：要我/我参与 → me；明确是别人 → other；拿不准 → unclear（不要轻易填 other）；
- 最多 8 个事项、总共最多 12 个任务；
- 没有截止时间的任务照常输出（due 填 null），不能因为“缺时间”而不建；
- 宁可少建：提醒/规范类，或拿不准是否需要我执行的内容，一律不建任务；
- 不编造原文没有的信息；没有可执行任务的事项不要输出。"""


def _explicit_message_ids(messages: list[dict]) -> set[str]:
    """带明确 @/点名结论的消息 id（LLM 复核只对这类消息生效，不动“未指定”的待办）。"""
    ids: set[str] = set()
    for message in messages:
        verdict = message.get("_verdict")
        if verdict is not None and getattr(verdict, "directed", False):
            rawid = str(message.get("rawid") or "")
            if rawid:
                ids.add(rawid)
    return ids


def _normalize_reconcile(data: dict, messages: list[dict], events: list[dict],
                         now: datetime, identity: IdentityContext | None = None) -> ReconcileResult | None:
    raw_items = data.get("items")
    if not isinstance(raw_items, list):
        return None
    message_ids = [str(message.get("rawid") or "") for message in messages]
    valid_event_ids = {str(event.get("id")) for event in events if event.get("id")}
    task_event_map = {
        str(event.get("id")): {str(task.get("id")) for task in (event.get("tasks") or [])}
        for event in events if event.get("id")
    }
    explicit_ids = _explicit_message_ids(messages)
    items: list[ReconcileItem] = []
    for raw_item in raw_items[:8]:
        if not isinstance(raw_item, dict):
            continue
        event_id = str(raw_item.get("event_id") or "").strip() or None
        if event_id and event_id not in valid_event_ids:
            event_id = None
        tasks: list[ReconcileTask] = []
        for raw_task in (raw_item.get("tasks") or [])[:12]:
            if not isinstance(raw_task, dict):
                continue
            title = str(raw_task.get("title") or "").strip()
            if not title:
                continue
            task_id = str(raw_task.get("task_id") or "").strip() or None
            if task_id and event_id and task_id not in task_event_map.get(event_id, set()):
                task_id = None
            source_ids = []
            for token in raw_task.get("source_msg_ids") or []:
                token = str(token).strip()
                if not token:
                    continue
                if token.startswith("m") and token[1:].isdigit():
                    index = int(token[1:]) - 1
                    if 0 <= index < len(message_ids):
                        source_ids.append(message_ids[index])
            if not source_ids:
                source_ids = message_ids
            # LLM 复核门：只对“明确 @/点名”的消息生效。模型说是别人的事就丢掉这条，
            # 未指定对象的消息保持原逻辑，不受影响。
            assignee = str(raw_task.get("assignee") or "").strip().lower()
            if assignee in ("other", "别人", "他人") and any(sid in explicit_ids for sid in source_ids):
                logger.info("LLM 复核：任务实际指派给他人，跳过「%s」", clip(title, 40))
                continue
            priority = str(raw_task.get("priority") or "normal").lower()
            if priority not in ("high", "normal", "low"):
                priority = "normal"
            tasks.append(ReconcileTask(
                task_id=task_id,
                title=clip(title, 40),
                notes=str(raw_task.get("notes") or "").strip()[:400],
                due_at=_normalize_due(raw_task.get("due"), now),
                priority=priority,
                source_msg_ids=list(dict.fromkeys(source_ids)),
            ))
        if not tasks:
            continue
        title = str(raw_item.get("event_title") or "").strip() or tasks[0].title
        items.append(ReconcileItem(
            event_id=event_id,
            event_title=clip(title, 60),
            event_summary=clip(str(raw_item.get("event_summary") or "").strip(), 500),
            tasks=tasks,
        ))
    if not items:
        return ReconcileResult(items=[], summary=str(data.get("summary") or "").strip(), mode="llm")
    return ReconcileResult(items=items, summary=str(data.get("summary") or "").strip(), mode="llm")


async def llm_reconcile(messages: list[dict], events: list[dict], settings: Settings,
                        now: datetime | None = None,
                        identity: IdentityContext | None = None) -> ReconcileResult | None:
    if not settings.llm_enabled or not settings.llm_api_key:
        return None
    now = now or now_local()
    try:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=settings.llm_api_key, base_url=settings.llm_base_url,
                             timeout=settings.llm_timeout)
        response = await client.chat.completions.create(
            model=settings.llm_model,
            messages=[
                {"role": "system", "content": RECONCILE_SYSTEM_PROMPT},
                {"role": "user", "content": _build_reconcile_prompt(messages, events, now, identity)},
            ],
            temperature=0.1,
            response_format={"type": "json_object"},
        )
        content = (response.choices[0].message.content or "").strip()
        if content.startswith("```"):
            content = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", content).strip()
        data = json.loads(content)
    except Exception as error:
        logger.warning("LLM 事项归并失败，回退规则模式: %s", error)
        return None
    return _normalize_reconcile(data, messages, events, now, identity)


def is_duplicate_title(tasks: list[ExtractedTask], candidate: ExtractedTask) -> bool:
    key = task_dedupe_key(candidate)
    return any(task_dedupe_key(task) == key for task in tasks)


# ---------------------------------------------------------------------------
# 新建待办 × 手动待办：合并 / 拆分 / 已忽略去重
#
# 背景：手动添加的待办（网页「手动添加」、手机「手机添加」）不属于事项账本，
# 归并模型看不到它们，于是同一件事再被聊到时系统会另建一条重复待办。
# 这里在模型总结出新待办之后再判定一次：
#   - same    ：新待办几乎就是某条手动待办的事 → 只把细节补进手动待办，不新建；
#   - split   ：新待办 = 某条手动待办的事 + 另一件独立的事 → 补细节，剩余部分另建；
#   - suppress：新待办就是用户最近「忽略」过的那件事 → 直接丢弃，不再新建
#               （只抑制新建，不改写已忽略任务；窗口外/新一期活动仍照常新建）；
#   - none    ：无关/拿不准 → 照常新建。
# 底线：手动待办的标题 / 已有截止时间绝不改，补充只写空白字段。
# ---------------------------------------------------------------------------
MANUAL_MATCH_SYSTEM_PROMPT = """你是微信待办的「手动待办合并判定器」，负责判断模型新生成的待办是否与用户手动添加（或已忽略）的待办是同一件事。
只输出 JSON，不要输出解释。"""


def _format_manual_tasks(tasks: list[dict]) -> str:
    if not tasks:
        return "（无）"
    lines = []
    for task in tasks:
        notes = clip(str(task.get("notes") or "").strip(), 80)
        lines.append(
            f"- task_id={task.get('id')} | 标题：{task.get('title') or ''} | "
            f"截止：{task.get('due_at') or '空'} | 备注：{notes or '空'}"
        )
    return "\n".join(lines)


def _format_ignored_tasks(tasks: list[dict]) -> str:
    """已忽略的手动待办：只列标题和忽略时间，禁止补写，只用于判定是否重复。"""
    lines = []
    for task in tasks:
        ignored_at = str(task.get("updated_at") or "")[:16].replace("T", " ")
        lines.append(
            f"- task_id={task.get('id')} | 标题：{task.get('title') or ''} | "
            f"忽略时间：{ignored_at or '未知'}"
        )
    return "\n".join(lines) or "（无）"



def _format_manual_candidates(candidates: list[ExtractedTask]) -> str:
    lines = []
    for index, task in enumerate(candidates):
        notes = clip(task.notes or "", 120)
        lines.append(
            f"- [c{index}] 标题：{task.title} | 截止：{task.due_at or '空'} | "
            f"优先级：{task.priority} | 备注：{notes or '空'}"
        )
    return "\n".join(lines)


def _format_manual_match_messages(messages: list[dict], limit: int = 20) -> str:
    lines = []
    for message in messages[:limit]:
        group = message.get("group_name") or "私聊"
        sender = message.get("sender") or ""
        content = clip(message.get("content") or "", 200)
        lines.append(f"- {group} | {sender}：{content}")
    return "\n".join(lines) or "（无）"


def _build_manual_match_prompt(candidates: list[ExtractedTask], manual_tasks: list[dict],
                               messages: list[dict], now: datetime,
                               ignored_tasks: list[dict] | None = None,
                               suppress_days: int = 30) -> str:
    ignored_block = ""
    if ignored_tasks:
        ignored_block = f"""
# 最近 {suppress_days} 天内用户「已忽略」的手动待办（同类消息不再新建待办；不要改它们、不要补写）
{_format_ignored_tasks(ignored_tasks)}
"""
    return f"""现在时间：{now.strftime('%Y-%m-%d %H:%M')}。

# 用户手动添加的待办（只能补空白字段；标题、已有截止时间禁止修改）
{_format_manual_tasks(manual_tasks)}
{ignored_block}
# 模型本轮新生成的待办（待判定，编号 c0、c1…）
{_format_manual_candidates(candidates)}

# 本轮消息（判定依据）
{_format_manual_match_messages(messages)}

请逐条判断每个新待办和哪条手动待办是同一件事：
- "same"：新待办说的就是某条手动待办这件事，只是更详细（补了时间/地点/要求）→ 补进手动待办，不再新建；
- "split"：新待办既包含某条手动待办这件事，又包含另一件独立的、需要单独跟进的事 → 属于手动待办的部分补进去，剩余部分作为新待办保留；
- "suppress"：整条新待办就是某条「已忽略」的那件事（重复提醒、同一活动同一件事）→ 直接丢弃，不再新建待办；
- "none"：和手动待办无关，或者拿不准是不是同一件事 → 照常新建。

判定顺序与要点：
- 先看「用户手动添加的待办」：能 same / split 就优先补进去；
- 再看「已忽略的手动待办」：只有整条新待办都属于那件被忽略的事时才判 suppress；如果消息里还包含另一件独立的事，或拿不准、看起来是新一期/下一次/另一个活动 → 判 none（照常新建，避免误吞真事）；
- 新待办里出现「并 / 然后 / 还要 / 同时」，或明显包含两个不同动作（一个「参加/去/到场」，另一个「填写/接龙/报名/提交/交/登记」）时，必须判 split，把第二个动作放进 remainder，不能因为两件事相关就判 same；
- 只是同一件事的细节更全（多了时间/地点/要求/提醒），没有多出另一个动作 → same。

例1：手动待办「科创破冰」+ 新待办「参加科创破冰活动」→ same；
例2：手动待办「科创破冰」+ 新待办「参加科创破冰活动并填写人数统计接龙」→ split，remainder「填写科创破冰人数统计接龙」；
例3：手动待办「科创破冰」+ 新待办「长江大讲堂报名」→ none；
例4：已忽略「青马工程报名」+ 新待办「青马工程报名截止提醒」→ suppress；
例5：已忽略「青马工程报名」+ 新待办「下期青马工程开始报名」→ none（新一期，照常新建）。

补充规则（必须遵守）：
- 手动待办的标题和已有的截止时间绝对不能改；只能补它目前为空的字段；
- 补充备注只写原文里有依据的信息（时间/地点/要求），不要编造；
- split 的 remainder 只写「不属于手动待办」的那部分，标题动词开头、不超过 20 字；
- suppress 不写 supplement_notes / supplement_due，也不要引用未完成的手动待办；
- 只有明确是同一件事才判 same / split / suppress；只是话题沾边、实际是另一件事的判 none。

输出 JSON（不要 markdown）：
{{
  "decisions": [
    {{
      "index": 0,
      "relation": "same | split | none | suppress",
      "manual_task_id": "same/split 填对应手动待办 task_id；suppress 填对应已忽略待办 task_id；none 时留空",
      "supplement_notes": "要补进手动待办的细节；没有就留空",
      "supplement_due": "要补进手动待办的截止时间 YYYY-MM-DD HH:MM；没有或手动已有截止时间就填 null",
      "remainder": {{
        "title": "split 时：不属于手动待办的那件事",
        "notes": "关键信息",
        "due": "YYYY-MM-DD HH:MM 或 null",
        "priority": "high | normal | low"
      }}
    }}
  ]
}}"""


@dataclass
class ManualDecision:
    """一条新待办与手动待办的判定结果。"""
    index: int
    relation: str  # same | split | none | suppress
    manual_task_id: str | None = None
    supplement_notes: str = ""
    supplement_due: str | None = None
    remainder: ExtractedTask | None = None


def _normalize_manual_decisions(data: dict, candidates: list[ExtractedTask],
                                manual_tasks: list[dict],
                                now: datetime,
                                ignored_tasks: list[dict] | None = None) -> list[ManualDecision]:
    raw = data.get("decisions")
    if not isinstance(raw, list):
        return []
    manual_ids = {str(task.get("id")) for task in manual_tasks if task.get("id")}
    ignored_ids = {str(task.get("id")) for task in (ignored_tasks or []) if task.get("id")}
    by_index: dict[int, ManualDecision] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            index = int(item.get("index"))
        except (TypeError, ValueError):
            continue
        if index < 0 or index >= len(candidates):
            continue
        relation = str(item.get("relation") or "").strip().lower()
        if relation not in ("same", "split", "none", "suppress"):
            continue
        manual_task_id = str(item.get("manual_task_id") or "").strip() or None
        if relation == "suppress":
            # 已忽略去重：必须引用窗口内真实存在的已忽略待办，否则按无关处理
            if manual_task_id not in ignored_ids:
                logger.warning("已忽略去重引用了未知 task_id，按无关处理：%s", manual_task_id)
                relation = "none"
                manual_task_id = None
            by_index[index] = ManualDecision(
                index=index, relation=relation, manual_task_id=manual_task_id,
            )
            continue
        if relation in ("same", "split") and manual_task_id not in manual_ids:
            # 模型引用了不存在的任务：宁可不合并，也不能乱写
            logger.warning("手动待办合并判定引用了未知 task_id，按无关处理：%s", manual_task_id)
            relation = "none"
            manual_task_id = None
        supplement_notes = clip(str(item.get("supplement_notes") or "").strip(), 500)
        supplement_due = _normalize_due(item.get("supplement_due"), now)
        remainder = None
        if relation == "split":
            raw_remainder = item.get("remainder")
            if isinstance(raw_remainder, dict):
                title = str(raw_remainder.get("title") or "").strip()
                if title:
                    priority = str(raw_remainder.get("priority") or "normal").lower()
                    if priority not in ("high", "normal", "low"):
                        priority = "normal"
                    remainder = ExtractedTask(
                        title=clip(title, 40),
                        notes=clip(str(raw_remainder.get("notes") or "").strip(), 400),
                        due_at=_normalize_due(raw_remainder.get("due"), now),
                        priority=priority,
                    )
            if remainder is None:
                # 说了要拆却没给剩余部分：按「完全相同」处理，避免又建一条重复待办
                logger.info("拆分判定缺少剩余待办，按完全相同处理：%s", candidates[index].title)
                relation = "same"
        by_index[index] = ManualDecision(
            index=index,
            relation=relation,
            manual_task_id=manual_task_id,
            supplement_notes=supplement_notes,
            supplement_due=supplement_due,
            remainder=remainder,
        )
    return list(by_index.values())


async def llm_match_manual(candidates: list[ExtractedTask], manual_tasks: list[dict],
                           messages: list[dict], settings: Settings,
                           now: datetime | None = None,
                           ignored_tasks: list[dict] | None = None) -> list[ManualDecision] | None:
    """判定新待办与手动待办的关系：补充 / 拆分 / 已忽略去重。

    - 未完成的手动待办：几乎相同 → 补充；部分相同 → 补充 + 拆分；
    - 最近 N 天内被忽略的手动待办：同一件事 → suppress（丢弃新待办，不改写已忽略任务）；
    - 都不匹配 → none（照常新建）。

    返回 None 表示本次没有判定（LLM 未启用 / 调用失败 / 输出不可用），调用方按原逻辑新建。
    """
    ignored_tasks = ignored_tasks or []
    if not candidates or (not manual_tasks and not ignored_tasks):
        return None
    if not settings.llm_enabled or not settings.llm_api_key:
        return None
    now = now or now_local()
    try:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=settings.llm_api_key, base_url=settings.llm_base_url,
                             timeout=settings.llm_timeout)
        response = await client.chat.completions.create(
            model=settings.llm_model,
            messages=[
                {"role": "system", "content": MANUAL_MATCH_SYSTEM_PROMPT},
                {"role": "user", "content": _build_manual_match_prompt(
                    candidates, manual_tasks, messages, now,
                    ignored_tasks=ignored_tasks,
                    suppress_days=int(getattr(settings, "manual_suppress_days", 30) or 30))},
            ],
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        content = (response.choices[0].message.content or "").strip()
        if content.startswith("```"):
            content = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", content).strip()
        data = json.loads(content)
    except Exception as error:  # 网络/额度/格式问题都回退到「不合并」
        logger.warning("手动待办合并判定失败，按未重复处理: %s", error)
        return None
    if not isinstance(data, dict):
        return None
    return _normalize_manual_decisions(data, candidates, manual_tasks, now,
                                       ignored_tasks=ignored_tasks)

