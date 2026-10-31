"""@ 与「指定人物」判定：这条消息在找谁？是不是找我？

原则（对应需求「不要影响非指定的待办」）：
- 只有**能确定消息在找某个具体的人**时才给结论：
  - 微信原始 @ 名单（``atUsernames``，wxid 级）里是我 → ``me``；
  - 名单里明确是别人（不含我、不是 @所有人）→ ``other``；
  - 正文里手动 @ 昵称 / 开头直呼「昵称，…」/「让、请、叫…某人」→ 命中我 ``me``，命中群成员 ``other``；
  - 回复我的消息（``quoteUsername`` 是我）→ ``me``；
- 其余情况（没 @、没点名、认不出来、群成员表没拉到……）一律 ``none``：
  交给原来的 LLM/规则逻辑，**行为不变**。
- 模糊匹配（错别字/同音）只在上述强上下文里生效，宁可判不准也不误伤。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from .identity import IdentityContext, normalize_name

__all__ = ["Verdict", "classify_message", "parse_at_tokens", "leading_targets",
           "assignment_targets", "ALL_TOKENS"]

ALL_TOKENS = {
    "所有人", "全体成员", "全体同学", "全体", "all", "everyone", "everybody",
    "所有同学", "大家",
}

_INVISIBLE_CHARS = "\u200b\u200c\u200d\u2060\ufeff"
_SPECIAL_SPACES = "\u2005\u00a0\u2002\u2003\u2004\u2006\u2007\u2008\u2009\u200a\u202f\u3000"
_SEPARATOR_CHARS = set(" \t\n\u2005,，。.、:：;；!！?？~～…—-)）]】·'\"“”‘’（(【[<《>")

_AT_NAME_EXCLUDE = r"@＠,，。;；:：!！?？、()（）\[\]【】<>《》\"'“”‘’"
# 微信原生 @ 的昵称可能含普通空格，真正的分隔符是 U+2005；手打的 @ 没有 U+2005 时按空白截断
_AT_RE = re.compile(rf"[@＠](?P<name>[^\s{_AT_NAME_EXCLUDE}]{{1,48}})")
_AT_RE_SPACED = re.compile(rf"[@＠](?P<name>[^\u2005\n{_AT_NAME_EXCLUDE}]{{1,48}})")

# 直呼后常见的“指令/称呼”开头：命中才认为是“在叫这个人”，不是正文里顺带提到
_FOLLOW_WORDS = (
    "你", "您", "请", "帮", "帮忙", "麻烦", "记得", "别忘了", "别忘", "一定", "务必",
    "来", "去", "把", "交", "看", "听", "快", "赶紧", "快点",
    "明天", "今天", "后天", "今晚", "明早", "明晚", "本周", "这周", "下周",
    "周", "下午", "上午", "中午", "晚上", "早上", "月底", "尽快",
    "在吗", "在不在", "有空", "能不能", "可以", "方便", "来一下", "来一趟",
)

# 「让/请/叫…某人」：动词前面必须是句首或标点/空白，避免“邀请”之类误命中
_ASSIGN_VERB_RE = re.compile(
    r"(?:^|[ \t\n，,。；;！!？?、:：])(?:让|叫|请|麻烦|通知|提醒|告诉|催|喊|找)\s*[@＠]?\s*$"
)

_VERB_TAIL = ("负责", "来负责", "主要负责")


@dataclass
class Verdict:
    kind: str = "none"          # me / all / other / none
    source: str = "none"        # at / text_at / vocative / assign / quote / none
    reason: str = ""
    targets: list[str] = field(default_factory=list)
    matched: str = ""

    @property
    def directed(self) -> bool:
        """是否明确在找某个人/某些人（不含“未指定对象”）。"""
        return self.kind in ("me", "all", "other")

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "source": self.source,
            "reason": self.reason,
            "targets": list(self.targets),
            "matched": self.matched,
        }


def _prepare(content: str) -> str:
    # U+2005 是微信 @ 昵称后的分隔符：NFKC 会把它归一成普通空格，先用哨兵保住
    value = str(content or "").replace("\u2005", "\x00")
    value = unicodedata.normalize("NFKC", value)
    for char in _SPECIAL_SPACES:
        value = value.replace(char, " ")
    value = "".join(char for char in value if char not in _INVISIBLE_CHARS)
    value = re.sub(r"[^\S\n]+", " ", value)
    value = value.replace("\x00", "\u2005")
    return value.strip()


def parse_at_tokens(content: str) -> list[str]:
    """正文里手动 @ 出来的昵称（微信原生 @ 的名单见 ``at_usernames``）。"""
    text = _prepare(content)
    pattern = _AT_RE_SPACED if "\u2005" in text else _AT_RE
    tokens: list[str] = []
    for match in pattern.finditer(text):
        token = match.group("name").strip().strip("。.，,、:：;；!！?？~～…—-)）]】")
        if token:
            tokens.append(token)
    return tokens


def _is_vocative_tail(tail: str) -> bool:
    """称呼后面是不是“在跟这个人说话”的样子。"""
    if not tail:
        return True
    if tail[0] in _SEPARATOR_CHARS:
        return True
    rest = tail.lstrip(" ")
    if not rest:
        return True
    return rest.startswith(_FOLLOW_WORDS)


def leading_targets(text: str, identity: IdentityContext,
                     fuzzy: bool = True) -> list[tuple[str, str, str]]:
    """开头直呼（含 @ 前缀、错别字/同音）。返回 [(称呼, role, 命中名)]。"""
    prepared = text.lstrip(" \t\n@＠")
    if not prepared or prepared[0] in _SEPARATOR_CHARS:
        return []
    candidates = list(identity.aliases) + list(identity.members.keys())
    # 1) 精确前缀：长名优先（“001大王”先于“大王”）
    for candidate in sorted(candidates, key=lambda item: len(normalize_name(item)), reverse=True):
        name = str(candidate or "").strip()
        if len(normalize_name(name)) < 2:
            continue
        if prepared.casefold().startswith(name.casefold()) and _is_vocative_tail(prepared[len(name):]):
            match = identity.match(name, fuzzy=False)
            if match.role != "unknown":
                return [(name, match.role, match.matched or name)]
    # 2) 模糊：从长到短取开头片段，命中别名/成员且在“叫人的语气”里
    max_length = min(8, len(prepared))
    for length in range(max_length, 1, -1):
        piece = prepared[:length]
        match = identity.match(piece, fuzzy=fuzzy)
        if match.role == "unknown":
            continue
        if _is_vocative_tail(prepared[length:]):
            return [(piece, match.role, match.matched or piece)]
    return []


def assignment_targets(text: str, identity: IdentityContext,
                       fuzzy: bool = True) -> list[tuple[str, str, str]]:
    """「让/请/叫…某人」或「某人负责…」的指派句式。返回 [(称呼, role, 命中名)]。"""
    hits: list[tuple[str, str, str]] = []
    candidates = list(identity.aliases) + list(identity.members.keys())
    for candidate in sorted(set(candidates), key=lambda item: len(normalize_name(item)), reverse=True):
        name = str(candidate or "").strip()
        if len(normalize_name(name)) < 2:
            continue
        for found in re.finditer(re.escape(name), text, flags=re.IGNORECASE):
            start, end = found.span()
            before = text[max(0, start - 12):start]
            if _ASSIGN_VERB_RE.search(before):
                match = identity.match(name, fuzzy=False)
                if match.role != "unknown":
                    hits.append((name, match.role, match.matched or name))
                break
            after = text[end:end + 8].lstrip(" ")
            if after.startswith(_VERB_TAIL):
                match = identity.match(name, fuzzy=False)
                if match.role != "unknown":
                    hits.append((name, match.role, match.matched or name))
                break
    return hits


def _other_label(wxid: str, identity: IdentityContext) -> str:
    return identity.display_name(wxid) or str(wxid or "")


def _is_at_all_token(token: str) -> bool:
    """微信 @ 名单里的“所有人”特殊项：notify@all、announcement@all 等。"""
    value = str(token or "").strip().casefold()
    return value in ("notify@all", "@all", "all") or value.endswith("@all")


def _display_targets(ats: list[str], text: str, identity: IdentityContext) -> list[str]:
    """@ 名单是 wxid，正文里是“发送者看到的昵称/备注”，按顺序对齐后更好读。"""
    targets = [_other_label(item, identity) for item in ats]
    tokens = parse_at_tokens(text)
    if tokens and len(tokens) == len(ats):
        return [token for token in tokens]
    return targets


def classify_message(content: str, *, at_usernames: list[str] | None = None,
                     at_names: list[str] | None = None, quote_username: str = "",
                     identity: IdentityContext | None = None,
                     fuzzy: bool = True) -> Verdict:
    """判定一条消息在找谁：me / all / other / none。"""
    identity = identity or IdentityContext()
    text = _prepare(content)
    account = str(identity.account or "").strip()
    ats = [str(item or "").strip() for item in (at_usernames or []) if str(item or "").strip()]

    # 1) 微信原始 @ 名单（最可靠）：notify@all / announcement@all 表示 @所有人
    if ats:
        lowered = [item.casefold() for item in ats]
        if any(_is_at_all_token(item) for item in lowered):
            return Verdict("all", "at", "@所有人", ["所有人"])
        if account:
            if account in ats or account.casefold() in lowered:
                name = identity.group_nickname or (identity.aliases[0] if identity.aliases else account)
                return Verdict("me", "at", f"@我（{name}）", [name], name)
            targets = _display_targets(ats, text, identity)
            return Verdict("other", "at", f"@他人（{'、'.join(targets)}）", targets)
        # 认不出自己的 wxid：用 @ 的显示名 / 正文 @ 匹配
        me_name = ""
        other_name = ""
        for name in at_names or []:
            match = identity.match(name, fuzzy=fuzzy)
            if match.role == "me" and not me_name:
                me_name = match.matched or name
            elif match.role == "other" and not other_name:
                other_name = match.matched or name
        if me_name:
            return Verdict("me", "at", f"@我（{me_name}）", [me_name], me_name)
        if other_name:
            return Verdict("other", "at", f"@他人（{other_name}）", [other_name])

    # 2) 正文里手动 @ 昵称（消息没带 atUsernames 时）
    tokens = parse_at_tokens(text)
    me_token = ""
    other_token = ""
    for token in tokens:
        if token.casefold() in ALL_TOKENS:
            return Verdict("all", "text_at", f"@所有人（{token}）", ["所有人"])
    for token in tokens:
        match = identity.match(token, fuzzy=fuzzy)
        if match.role == "me" and not me_token:
            me_token = match.matched or token
        elif match.role == "other" and not other_token:
            other_token = match.matched or token
    if me_token:
        return Verdict("me", "text_at", f"手动@我（{me_token}）", [me_token], me_token)
    if other_token:
        return Verdict("other", "text_at", f"手动@他人（{other_token}）", [other_token])

    # 3) 开头直呼 + “让/请/叫…某人”指派
    vocatives = leading_targets(text, identity, fuzzy=fuzzy)
    assignments = assignment_targets(text, identity, fuzzy=fuzzy)
    me_hit = next((item for item in vocatives + assignments if item[1] == "me"), None)
    other_hit = next((item for item in vocatives + assignments if item[1] == "other"), None)
    if me_hit:
        source = "vocative" if me_hit in vocatives else "assign"
        label = "直呼我" if source == "vocative" else "指派给我"
        return Verdict("me", source, f"{label}（{me_hit[0]}）", [me_hit[0]], me_hit[0])
    if other_hit:
        source = "vocative" if other_hit in vocatives else "assign"
        label = "点名他人" if source == "vocative" else "指派给他人"
        return Verdict("other", source, f"{label}（{other_hit[0]}）", [other_hit[0]])

    # 4) 回复我的消息：弱信号，放在最后（不改变“明确指向别人”的结论）
    if account and str(quote_username or "").strip() == account:
        name = identity.group_nickname or (identity.aliases[0] if identity.aliases else "我")
        return Verdict("me", "quote", f"回复我（{name}）", [name], name)

    return Verdict()
