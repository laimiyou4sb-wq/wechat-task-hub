"""时间与文本小工具。本地时间统一使用「无时区 naive datetime」，存库 ISO 字符串。"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from .config import local_tz

ISO_FMT = "%Y-%m-%dT%H:%M:%S"


def now_local() -> datetime:
    return datetime.now(local_tz()).replace(tzinfo=None, microsecond=0)


def now_iso() -> str:
    return now_local().strftime(ISO_FMT)


def to_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.replace(microsecond=0, tzinfo=None).strftime(ISO_FMT)


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    for fmt in (ISO_FMT, "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[: len(datetime.now().strftime(fmt))], fmt)
        except (ValueError, TypeError):
            continue
    try:
        return datetime.fromisoformat(text).replace(tzinfo=None)
    except ValueError:
        return None


def human_due(value: str | None, now: datetime | None = None) -> str:
    dt = parse_iso(value)
    if dt is None:
        return ""
    now = now or now_local()
    delta_days = (dt.date() - now.date()).days
    clock = dt.strftime("%H:%M") if (dt.hour or dt.minute) else ""
    if delta_days < 0:
        return f"已逾期 · {dt.month}月{dt.day}日 {clock}".strip()
    if delta_days == 0:
        return f"今天 {clock}".strip()
    if delta_days == 1:
        return f"明天 {clock}".strip()
    if delta_days == 2:
        return f"后天 {clock}".strip()
    return f"{dt.month}月{dt.day}日 {clock}".strip()


def in_quiet_hours(now: datetime, start: str, end: str) -> bool:
    """支持跨零点，如 23:00-07:30。"""
    try:
        sh, sm = (int(x) for x in start.split(":"))
        eh, em = (int(x) for x in end.split(":"))
    except (ValueError, AttributeError):
        return False
    minutes = now.hour * 60 + now.minute
    start_m, end_m = sh * 60 + sm, eh * 60 + em
    if start_m == end_m:
        return False
    if start_m < end_m:
        return start_m <= minutes < end_m
    return minutes >= start_m or minutes < end_m


def clip(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def strip_urls(text: str) -> str:
    return re.sub(r"https?://\S+", "", text or "").strip()
