"""身份识别：我是谁、我的别名、各群群昵称、群成员。

数据来源（仅 wcda 数据源，本机 WeChatDataAnalysis 接口）：
- ``/api/current-account``：我的 wxid；
- ``/api/chat/contacts/profile``：我的昵称 / 备注 / 微信号（alias）；
- ``/api/chat/contacts/group_members``：某个群的成员，其中 ``displayName`` 就是该群的群昵称。

同一个微信号在不同群的群昵称不同，所以别名是「全局别名 + 当前群群昵称」的合集。
自动数据拿不到（接口失败/离线/其它数据源）时退回手动 ``ME_NAMES``，**绝不阻断原有待办流程**。

匹配规则见 :func:`fuzzy_equal`：先精确匹配；开启模糊匹配时，额外接受同音（装了
``pypinyin`` 时）或长度相同只差一个字（覆盖打错字/同音字）的写法。模糊匹配只在
“@昵称 / 直呼 / 指派句式”这类强上下文里使用，避免误伤正文里的普通词语。
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .config import Settings, local_tz

logger = logging.getLogger(__name__)

GLOBAL_TTL_SECONDS = 3600
FAILURE_RETRY_SECONDS = 90
MAX_GROUP_MEMBERS = 500

_INVISIBLE_CHARS = "\u200b\u200c\u200d\u2060\ufeff"
_SPECIAL_SPACES = "\u2005\u00a0\u2002\u2003\u2004\u2006\u2007\u2008\u2009\u200a\u202f\u3000"


def normalize_name(text: str) -> str:
    """统一全角/半角、不可见空格、首尾 @，用于称呼比较。"""
    value = unicodedata.normalize("NFKC", str(text or ""))
    for char in _SPECIAL_SPACES:
        value = value.replace(char, " ")
    value = "".join(char for char in value if char not in _INVISIBLE_CHARS)
    value = re.sub(r"\s+", " ", value).strip()
    return value.strip("@＠").strip().casefold()


def _levenshtein(left: str, right: str) -> int:
    if left == right:
        return 0
    if len(left) < len(right):
        left, right = right, left
    previous = list(range(len(right) + 1))
    for i, char_left in enumerate(left, 1):
        current = [i]
        for j, char_right in enumerate(right, 1):
            current.append(min(
                previous[j] + 1,
                current[j - 1] + 1,
                previous[j - 1] + (char_left != char_right),
            ))
        previous = current
    return previous[-1]


_PINYIN_CACHE: dict[str, tuple[str, ...] | None] = {}


def _pinyin(text: str) -> tuple[str, ...] | None:
    """可选依赖：装了 pypinyin 就能识破同音不同字（如 大王/大旺 → da wang）。"""
    key = normalize_name(text)
    if key in _PINYIN_CACHE:
        return _PINYIN_CACHE[key]
    try:
        from pypinyin import lazy_pinyin  # type: ignore
    except Exception:
        _PINYIN_CACHE[key] = None
        return None
    try:
        value = tuple(str(item).casefold() for item in lazy_pinyin(key))
    except Exception:
        value = None
    _PINYIN_CACHE[key] = value
    return value


def _strip_decorations(name: str) -> str:
    """去掉群昵称常见的前缀编号/符号（如 001大王 → 大王）。"""
    core = re.sub(r"^[\d\W_]+", "", name)
    return core or name


def fuzzy_equal(left: str, right: str) -> bool:
    """两个称呼是否“可能指同一个人”（错别字/同音）。

    只在强上下文（@、直呼、指派）里用；宁可返回 False，也不要到处命中。
    """
    a, b = normalize_name(left), normalize_name(right)
    if not a or not b:
        return False
    if a == b:
        return True
    if len(a) < 2 or len(b) < 2:
        return False
    if a.isascii() or b.isascii():
        # 英文/数字 ID：长度 >=4 时允许 1 个字符差异
        return len(a) >= 4 and abs(len(a) - len(b)) <= 1 and _levenshtein(a, b) <= 1
    pinyin_a, pinyin_b = _pinyin(a), _pinyin(b)
    if pinyin_a and pinyin_b and pinyin_a == pinyin_b:
        return True
    if len(a) == len(b) and _levenshtein(a, b) == 1:
        return True
    core_a, core_b = _strip_decorations(a), _strip_decorations(b)
    if core_a != a or core_b != b:
        if core_a and core_b and (core_a != a or core_b != b):
            if core_a == core_b:
                return True
            if len(core_a) == len(core_b) and _levenshtein(core_a, core_b) == 1:
                return True
    return False


@dataclass
class NameMatch:
    role: str = "unknown"   # me / other / unknown
    matched: str = ""       # 命中的别名或群昵称
    fuzzy: bool = False


@dataclass
class IdentityContext:
    """一次判定所需的身份快照（手动别名 + 全局昵称 + 当前群昵称/成员）。"""

    session_id: str = ""
    group_name: str = ""
    is_group: bool = True
    account: str = ""
    aliases: list[str] = field(default_factory=list)
    group_nickname: str = ""
    members: dict[str, str] = field(default_factory=dict)      # 群昵称 -> wxid（不含我）
    member_ids: dict[str, str] = field(default_factory=dict)   # wxid -> 群昵称
    auto: bool = False
    fetched_at: float = 0.0
    error: str = ""

    def display_name(self, wxid: str) -> str:
        return self.member_ids.get(str(wxid or "").strip(), "")

    def match(self, token: str, fuzzy: bool = True) -> NameMatch:
        """判断一个称呼指向谁。先精确、后模糊；模糊同时命中我和别人时按未知处理。"""
        token_norm = normalize_name(token)
        if not token_norm:
            return NameMatch()
        for alias in self.aliases:
            if normalize_name(alias) == token_norm:
                return NameMatch("me", alias)
        for name in self.members:
            if normalize_name(name) == token_norm:
                return NameMatch("other", name)
        if not fuzzy:
            return NameMatch()
        me_hit = ""
        for alias in self.aliases:
            if len(normalize_name(alias)) >= 2 and fuzzy_equal(alias, token):
                me_hit = alias
                break
        other_hit = ""
        for name in self.members:
            if len(normalize_name(name)) >= 2 and fuzzy_equal(name, token):
                other_hit = name
                break
        if me_hit and not other_hit:
            return NameMatch("me", me_hit, True)
        if other_hit and not me_hit:
            return NameMatch("other", other_hit, True)
        return NameMatch()


def _dedupe_names(names: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for name in names:
        value = str(name or "").strip()
        key = normalize_name(value)
        if not value or key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


class IdentityResolver:
    """缓存“我”的账号、别名，以及每个群的群昵称/成员。"""

    def __init__(self, settings: Settings, wcda: Any = None):
        self.settings = settings
        self.wcda = wcda
        self._account = ""
        self._account_source = "none"
        self._auto_names: list[str] = []
        self._global_at = 0.0
        self._global_error = ""
        self._sessions: dict[str, IdentityContext] = {}
        self._group_names: dict[str, str] = {}
        self._last_failure: dict[str, float] = {}
        self._lock = asyncio.Lock()

    # ---------- 基础 ----------
    @property
    def account(self) -> str:
        return self._account or self.settings.me_wxid.strip()

    @property
    def account_source(self) -> str:
        return self._account_source

    def group_name_for(self, session_id: str) -> str:
        return self._group_names.get(str(session_id or ""), "")

    def _manual_names(self) -> list[str]:
        return _dedupe_names(list(self.settings.me_names or []))

    def _base_aliases(self) -> list[str]:
        return _dedupe_names(self._manual_names() + list(self._auto_names))

    def plain_context(self, session_id: str = "", group_name: str = "",
                      is_group: bool = True) -> IdentityContext:
        """只带手动/全局别名的上下文（自动数据不可用时的兜底）。"""
        return IdentityContext(
            session_id=session_id,
            group_name=group_name,
            is_group=is_group,
            account=self.account,
            aliases=self._base_aliases(),
        )

    def refresh_manual(self) -> None:
        """手动别名/账号变化后，更新已缓存上下文里的 aliases（不重新拉群成员）。"""
        base = self._base_aliases()
        for context in self._sessions.values():
            context.aliases = _dedupe_names(base + [context.group_nickname])

    # ---------- 全局身份 ----------
    async def ensure_global(self, *, force: bool = False) -> None:
        await self._ensure_global(force=force)

    async def _ensure_global(self, *, force: bool = False) -> None:
        now = time.time()
        if not force and self._global_at and now - self._global_at < GLOBAL_TTL_SECONDS:
            return
        if self.wcda is None or not getattr(self.wcda, "configured", False):
            # 没有数据源：仅使用 ME_WXID / ME_NAMES
            manual_wxid = self.settings.me_wxid.strip()
            if manual_wxid:
                self._account = manual_wxid
                self._account_source = "manual"
            self._global_at = now
            return
        manual_wxid = self.settings.me_wxid.strip()
        if manual_wxid:
            account, source = manual_wxid, "manual"
        else:
            account = await self.wcda.fetch_current_account()
            source = "auto" if account else "none"
        names: list[str] = []
        if account:
            profile = await self.wcda.fetch_contact_profile(account)
            for key in ("displayName", "nickname", "remark", "alias"):
                value = str(profile.get(key) or "").strip()
                if value:
                    names.append(value)
        self._account = account
        self._account_source = source
        self._auto_names = _dedupe_names(names)
        self._global_at = now
        self._global_error = "" if account else "未能自动识别我的 wxid"
        if not account:
            logger.warning("未能自动识别当前微信 wxid，可设置 ME_WXID 或手动别名兜底")

    # ---------- 群身份 ----------
    async def for_session(self, session_id: str, group_name: str = "",
                          is_group: bool = True) -> IdentityContext:
        """取（或拉取）某个会话的身份上下文；任何异常都退回手动别名。"""
        session = str(session_id or "")
        if group_name:
            self._group_names[session] = group_name
        group_name = self._group_names.get(session, group_name)
        if not session:
            return self.plain_context()
        if not is_group or self.wcda is None or not getattr(self.wcda, "configured", False):
            return self.plain_context(session, group_name, is_group)

        now = time.time()
        ttl = max(60, int(self.settings.identity_ttl_seconds))
        cached = self._sessions.get(session)
        if cached is not None:
            if cached.error:
                if now - cached.fetched_at < FAILURE_RETRY_SECONDS:
                    return cached
            elif now - cached.fetched_at < ttl:
                return cached

        async with self._lock:
            cached = self._sessions.get(session)
            if cached is not None and now - cached.fetched_at < min(ttl, FAILURE_RETRY_SECONDS):
                return cached
            await self._ensure_global()
            try:
                members = await self.wcda.fetch_group_members(session, limit=MAX_GROUP_MEMBERS)
            except Exception as error:  # noqa: BLE001 - 身份识别失败不影响主流程
                logger.warning("读取群成员失败（%s）：%s", session, error)
                members = []
            if not members:
                if cached is not None and cached.member_ids:
                    # 拉取失败但手里有旧数据：继续用旧的，失败时间用于退避
                    cached.fetched_at = now
                    cached.error = "群成员刷新失败（沿用缓存）"
                    self._last_failure[session] = now
                    return cached
                context = self.plain_context(session, group_name, is_group)
                context.fetched_at = now
                context.auto = False
                context.error = "群成员读取失败"
                self._last_failure[session] = now
                self._sessions[session] = context
                return context

            context = IdentityContext(
                session_id=session,
                group_name=group_name,
                is_group=True,
                account=self.account,
                fetched_at=now,
                auto=True,
            )
            for member in members:
                wxid = str(member.get("username") or "").strip()
                name = str(member.get("displayName") or "").strip()
                if not wxid:
                    continue
                if self._account and wxid == self._account:
                    context.group_nickname = name or context.group_nickname
                    continue
                if name:
                    context.members[name] = wxid
                context.member_ids[wxid] = name
            context.aliases = _dedupe_names(self._base_aliases() + [context.group_nickname])
            self._sessions[session] = context
            self._last_failure.pop(session, None)
            logger.info("身份已同步：群=%s，我的群昵称=%s，成员=%s",
                        group_name or session, context.group_nickname or "（未找到）",
                        len(context.member_ids))
            return context

    # ---------- 对外操作 ----------
    def remember_group(self, session_id: str, group_name: str) -> None:
        if session_id and group_name:
            self._group_names[str(session_id)] = group_name

    def invalidate(self, session_ids: list[str] | None = None) -> None:
        if not session_ids:
            self._sessions.clear()
            self._last_failure.clear()
            return
        for session in session_ids:
            self._sessions.pop(str(session), None)
            self._last_failure.pop(str(session), None)

    async def refresh(self, session_ids: list[str] | None = None) -> dict:
        await self._ensure_global(force=True)
        self.invalidate(session_ids if session_ids else None)
        refreshed = 0
        if session_ids:
            for session in session_ids:
                context = await self.for_session(str(session), self._group_names.get(str(session), ""))
                if context.auto:
                    refreshed += 1
        return self.snapshot() | {"refreshed": refreshed}

    def snapshot(self) -> dict:
        now = time.time()
        ttl = max(60, int(self.settings.identity_ttl_seconds))
        groups = []
        for session, context in self._sessions.items():
            fetched = context.fetched_at
            groups.append({
                "session_id": session,
                "group_name": context.group_name or self._group_names.get(session, ""),
                "my_nickname": context.group_nickname,
                "members": len(context.member_ids),
                "fetched_at": _timestamp_iso(fetched),
                "stale": bool(context.error) or (now - fetched > ttl),
                "error": context.error,
            })
        groups.sort(key=lambda item: (item["group_name"] or item["session_id"]))
        return {
            "account": self.account,
            "account_source": self._account_source,
            "data_source": self.settings.data_source,
            "filter_enabled": bool(self.settings.identity_filter),
            "fuzzy_enabled": bool(self.settings.identity_fuzzy),
            "manual_names": self._manual_names(),
            "auto_names": list(self._auto_names),
            "aliases": self._base_aliases(),
            "ttl_seconds": ttl,
            "groups": groups,
        }

    def check_context(self, session_id: str = "", group_name: str = "") -> IdentityContext:
        """判定测试用：有缓存就用缓存，没有就拿手动别名兜底（不触发网络请求）。"""
        session = str(session_id or "")
        if session:
            context = self._sessions.get(session)
            if context is not None:
                return context
        return self.plain_context(session, group_name)

    def describe_context(self, context: IdentityContext) -> dict:
        return {
            "session_id": context.session_id,
            "group_name": context.group_name,
            "group_nickname": context.group_nickname,
            "account": context.account,
            "aliases": list(context.aliases),
            "members": len(context.member_ids),
            "auto": bool(context.auto),
            "error": context.error,
        }


def _timestamp_iso(value: float) -> str:
    if not value:
        return ""
    return datetime.fromtimestamp(value, local_tz()).replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%S")
