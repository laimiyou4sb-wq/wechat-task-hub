"""核心编排：监听 → 批次 → 过滤 → 提取 → 入库 → 推送 → 定时摘要。"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from datetime import datetime, timedelta
from urllib.parse import quote

from .config import Settings
from .db import Database
from .envfile import write_env_values
from .extract import (ExtractedTask, LLMUnavailable, ReconcileResult, ReconcileTask,
                      extract_tasks, llm_match_manual, llm_reconcile, rule_extract,
                      task_dedupe_key, titles_same_topic)
from .filters import EVENT_HINTS, has_action_hint, is_worth_summarizing, should_send_to_llm
from .identity import IdentityContext, IdentityResolver
from .mentions import classify_message
from .push import Pusher
from .utils import clip, human_due, in_quiet_hours, now_iso, now_local, to_iso
from .wcda import WcdaClient

logger = logging.getLogger(__name__)

#: 演示模式（POST /api/demo/seed）用的虚构消息，不访问网络。
DEMO_MESSAGES: list[dict] = [
    {"group": "项目群", "sender": "李老师",
     "content": "【通知】本周五18:00前提交报名表，逾期不候，表格在群文件。", "ago": 300},
    {"group": "项目群", "sender": "王同学",
     "content": "明天下午3点开组会，记得带上上周的材料。", "ago": 240},
    {"group": "项目群", "sender": "张三", "content": "哈哈哈哈这也行", "ago": 200},
    {"group": "课程群", "sender": "助教",
     "content": "大家记得10月28日之前交作业，提交到学习通。", "ago": 160},
    {"group": "课程群", "sender": "李四", "content": "收到", "ago": 120},
    {"group": "生活群", "sender": "快递驿站",
     "content": "今晚8点前把取件码发我，不然要退回。", "ago": 80},
]


def demo_messages() -> list[dict]:
    """演示用虚构消息（不访问网络），字段与采集路径的标准消息一致。"""
    now = int(time.time())
    messages: list[dict] = []
    for index, sample in enumerate(DEMO_MESSAGES):
        messages.append({
            "rawid": f"demo-{now}-{index}",
            "session_id": f"demo-{index}@chatroom",
            "group_name": sample["group"],
            "sender": sample["sender"],
            "content": sample["content"],
            "ts": now - int(sample["ago"]),
            "is_group": True,
            "event": "message.new",
            "at_usernames": [],
            "at_names": [],
            "quote_username": "",
        })
    return messages


class AppService:
    #: 整批处理失败后逐条隔离重试的最大条数；超过后整批只计一次失败，避免放大耗时
    _ISOLATE_LIMIT = 30
    #: LLM 回退告警：连续失败达到首次阈值后提醒，之后每多 N 次再提醒
    _LLM_ALERT_FIRST = 3
    _LLM_ALERT_EVERY = 10
    #: 降级/告警的最小间隔（秒），避免 LLM 抖动的每一批都推一条
    _LLM_ALERT_INTERVAL = 600
    _CAP_ALERT_INTERVAL = 3600
    #: 规则模式单轮单会话最多产出待办数（旧实现硬编码 12/批，洪峰下静默饱和；
    #: 这里放大到 200 作为防噪兜底，真正被截断时会置 cap_hit 并上报降级）
    _RULE_MAX_TASKS = 200

    def __init__(self, settings: Settings):
        self.settings = settings
        self.db = Database(settings.db_path)
        self.pusher = Pusher(settings)
        self.wcda = WcdaClient(settings)
        self.data_source = settings.data_source
        # 唯一数据源是 wcda；demo / 其它不连接真实数据源
        self.source = self.wcda if self.data_source == "wcda" else None
        # @/点名 身份判定（只过滤“明确指向他人”的消息，详见 app/mentions.py）
        self.identity = IdentityResolver(settings, self.wcda if self.data_source == "wcda" else None)
        self._judge_stats = {"kept_me": 0, "kept_all": 0, "kept_none": 0, "dropped_other": 0}
        self._judge_since = now_iso()
        self._recent_drops: deque[dict] = deque(maxlen=20)
        # 连接状态：由采集轮驱动（None=首轮采集尚未完成；True/False=最近一轮结果）
        self.connected: bool | None = None
        self.last_event_at: str | None = None
        self.last_error: str | None = None
        self._sessions_cache: list[dict] = []
        self._last_collection: dict | None = None
        self._last_collect_at: str | None = None
        self._last_collect_ok_at: str | None = None
        self._force_allow_sessions: set[str] = set()
        self._last_digest_date: str | None = None
        self._last_cleanup_date: str | None = None
        self._workers: list[asyncio.Task] = []
        # 采集轮串行化：避免周期轮 / 新置顶回补 / 深扫并发写同一份「同轮推送」收集器
        self._collect_lock = asyncio.Lock()
        self.started_at = now_iso()
        # 同轮合并推送：采集轮开始时建立收集器，轮末只发一条汇总推送
        self._round_push: dict | None = None
        # 晚到消息兜底深扫：距上次深扫超过 WCDA_DEEP_SCAN_MINUTES 才再扫
        self._last_deep_scan_at = time.monotonic()
        # 深扫可观测（P2-4）：上次深扫完成时间与补回条数，health/设置页能看到
        self._last_deep_scan_iso: str | None = None
        self._last_deep_scan_result: dict | None = None
        # LLM 降级可观测（P1-2）：调用失败会静默回退规则模式，必须对外暴露
        self.last_llm_error: str | None = None
        self._llm_fallback_total = 0
        self._llm_fallback_streak = 0
        self._llm_alert_next = self._LLM_ALERT_FIRST
        self._last_llm_alert_at = 0.0
        self._last_cap_alert_at = 0.0
        # 处理整体性故障的连续轮次（每会话）：连续多轮全失败时升级告警，
        # 避免「大积压 + 全毒消息」这类边界被静默卡住
        self._global_failure_streaks: dict[str, int] = {}
        # 置顶白名单（来自 WeChatDataAnalysis 的会话列表）
        self.pinned_sessions: set[str] = set()
        self.pinned_names: dict[str, str] = {}
        self.pinned_ready = False
        self._pinned_warned = False

    # ---------- lifecycle ----------
    async def start(self) -> None:
        self._workers = [
            asyncio.create_task(self._daily_loop(), name="daily"),
        ]
        if self.data_source == "wcda" and self.wcda.configured:
            # wcda 唯一采集方法：后台采集轮（localId 游标 + 按会话轮询 REST）
            if not self._collect_enabled():
                logger.warning("wcda 采集未启用周期轮次（WCDA_CATCHUP_SECONDS=0）：只在启动时采集一次，"
                               "之后不会自动生成待办")
            self._workers.append(asyncio.create_task(self._collect_loop(), name="wcda-collect"))
        if self._needs_pinned() or self.data_source == "wcda":
            self._workers.append(asyncio.create_task(self._pinned_loop(), name="pinned-refresh"))
        if self.settings.wcda_deep_scan_minutes <= 0:
            logger.warning("晚到消息深扫已关闭（WCDA_DEEP_SCAN_MINUTES=0）："
                           "写入晚、业务时间旧的晚到消息可能永久漏采")
        logger.info("服务已启动：%s（数据源 %s，白名单=%s）",
                    self.settings.base_url, self.data_source, self.settings.whitelist_mode)

    async def stop(self) -> None:
        for worker in self._workers:
            worker.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self.db.close()

    # ---------- 数据源 ----------
    def _needs_pinned(self) -> bool:
        return (self.data_source == "wcda"
                and self.settings.whitelist_mode in ("pinned", "pinned_custom"))

    async def _pinned_loop(self) -> None:
        while True:
            try:
                new_sessions = await self.refresh_pinned()
                if new_sessions and self._needs_pinned():
                    minutes = self.settings.wcda_new_pin_backfill_minutes
                    logger.info("检测到新置顶会话 %s 个：%s", len(new_sessions),
                                "、".join(self.pinned_names.get(sid, sid) for sid in new_sessions))
                    if minutes > 0 and self.data_source == "wcda" and self.wcda.configured:
                        try:
                            await self.collect(minutes=minutes, session_ids=new_sessions)
                        except Exception as error:  # noqa: BLE001 - 回补失败不影响监控
                            logger.warning("新置顶会话回补失败：%s", error)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning("刷新置顶会话失败：%s", error)
            await asyncio.sleep(self.settings.pinned_refresh_seconds)

    def _apply_pinned_sessions(self, sessions: list[dict]) -> list[str]:
        """把会话列表里的置顶状态同步到监控集合，返回“新增置顶”的 session_id。

        服务启动后的第一次同步只当作基线（否则每次重启都会把全部置顶当成“新置顶”去回补）。
        """
        first_sync = not self.pinned_ready
        previous = set(self.pinned_sessions)
        pinned = {str(s.get("username")) for s in sessions if s.get("isTop") and s.get("username")}
        self.pinned_sessions = pinned
        self.pinned_names = {
            str(s.get("username")): str(s.get("name") or "")
            for s in sessions if s.get("isTop") and s.get("username")
        }
        self.pinned_ready = True
        added = [] if first_sync else sorted(pinned - previous)
        removed = sorted(previous - pinned)
        if added:
            logger.info("置顶变化：新增 %s", "、".join(self.pinned_names.get(sid, sid) for sid in added))
        if removed:
            logger.info("置顶变化：移出 %s", "、".join(
                self.pinned_names.get(sid, sid) for sid in removed))
        for session_id in added:
            name = self.pinned_names.get(session_id, "")
            if name:
                self.identity.remember_group(session_id, name)
        return added

    async def refresh_pinned(self) -> list[str]:
        """刷新置顶会话；返回这次新增置顶的会话（用于自动采集回补）。"""
        sessions = await self.wcda.fetch_sessions()
        if not sessions:
            return []
        added = self._apply_pinned_sessions(sessions)
        logger.info("已同步置顶会话 %s 个", len(self.pinned_sessions))
        return added

    # ---------- wcda 采集（唯一采集方法） ----------
    async def _monitored_sessions(self) -> tuple[list[dict], str | None]:
        """当前监控范围内的会话（置顶 + 自定义名单），顺带同步置顶变化。

        返回 ``(targets, error)``：``error`` 非空表示**会话列表接口失败**（≠ 真的没有
        会话），调用方必须把它当成采集失败暴露出去，否则会静默空转（P1-3）。
        """
        sessions: list[dict] = []
        error: str | None = None
        reader = getattr(self.wcda, "fetch_sessions_result", None)
        if callable(reader):
            fetched = await reader(limit=500)
            if not fetched.get("ok"):
                error = str(fetched.get("error") or "读取会话列表失败")
                logger.warning("读取监控会话失败，本轮按采集失败处理：%s", error)
                return [], error
            sessions = list(fetched.get("sessions") or [])
        else:  # 兼容旧客户端 / 测试桩
            sessions = await self.wcda.fetch_sessions(limit=500)
            sessions = list(sessions or [])
        if not sessions:
            return [], None
        self._sessions_cache = sessions
        self._apply_pinned_sessions(sessions)
        targets: list[dict] = []
        for session in sessions:
            session_id = str(session.get("username") or "")
            if not session_id:
                continue
            group_name = str(session.get("name") or "")
            probe = {
                "session_id": session_id,
                "group_name": group_name,
                "is_group": bool(session.get("isGroup")),
            }
            if not self._message_allowed(probe):
                continue
            targets.append({"session_id": session_id, "group_name": group_name})
        return targets, None

    async def _fetch_sessions_cache(self) -> list[dict]:
        """读取并缓存会话列表（显式回补会话名用）；失败只记日志，不阻断回补。"""
        reader = getattr(self.wcda, "fetch_sessions_result", None)
        if callable(reader):
            fetched = await reader(limit=500)
            if not fetched.get("ok"):
                logger.warning("读取会话列表失败（不影响指定会话回补）：%s",
                               fetched.get("error"))
                return []
            return list(fetched.get("sessions") or [])
        return list(await self.wcda.fetch_sessions(limit=500) or [])

    def _collection_window_seconds(self, minutes: int | None, mode: str) -> int:
        """本轮采集的回看上限（秒）。

        - ``window``：固定回看 ``minutes``（缺省 WCDA_CATCHUP_LOOKBACK_MINUTES）；
        - ``watermark``：按会话水位采，上限 ``WCDA_CATCHUP_MAX_HOURS``（可传 minutes 覆盖）。
        """
        settings = self.settings
        if mode == "watermark":
            if minutes is None:
                return max(1, int(settings.wcda_catchup_max_hours)) * 3600
            return max(60, min(int(minutes) * 60, 7 * 24 * 3600))
        window_minutes = int(minutes if minutes is not None else settings.wcda_catchup_lookback_minutes)
        return max(1, min(window_minutes, 7 * 24 * 60)) * 60

    def _collection_since_ts(self, session_id: str, *, mode: str,
                             window_seconds: int, now_ts: int) -> int:
        """本轮该会话的采集起始时间（unix 秒）。

        window 模式固定回看；watermark 模式从该会话上次采集水位继续：
        - 水位缺失时用「本进程启动前已入库」的最新消息时间（首次接管），不用启动后
          采集轮刚写入的新消息，否则会把离线积压误判成已覆盖；
        - 启动前也没有该会话任何历史时，自动回看 ``WCDA_CATCHUP_EMPTY_HISTORY_HOURS``（默认 1 天）；
        - 任何时候都至少回看 lookback，覆盖两轮采集之间的新消息；
        - 单轮最远回看到 now - window_seconds，避免一次扫穿全部历史。
        """
        settings = self.settings
        floor = now_ts - max(1, int(window_seconds))
        if mode != "watermark":
            return floor
        lookback = max(1, int(settings.wcda_catchup_lookback_minutes)) * 60
        overlap = max(0, int(settings.wcda_catchup_overlap_seconds))
        empty_history = max(1, int(settings.wcda_catchup_empty_history_hours)) * 3600
        state = self.db.get_catchup_state(session_id)
        watermark = int((state or {}).get("last_ts") or 0)
        if watermark <= 0:
            db_max = self.db.max_message_ts(session_id, before_iso=self.started_at)
            if db_max > 0:
                return max(floor, min(db_max - overlap, now_ts - lookback))
            return max(floor, now_ts - empty_history)
        return max(floor, min(watermark - overlap, now_ts - lookback))

    @staticmethod
    def _collect_row_ts(row: dict) -> int:
        """容错读取 WCDA 消息时间戳（非数字按 0 处理，不让整轮采集中断）。"""
        try:
            return int(row.get("createTime") or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _collect_row_local_id(row: dict) -> int:
        """容错读取 WCDA 行序 localId（缺失/非数字按 0 处理）。

        localId 是微信 WCDB 的写入行序，与 createTime 解耦：PC 断网/休眠后补同步的
        「写入晚、业务时间旧」消息会拿到**新的 localId**，这正是时间窗采不到的晚到消息。
        """
        try:
            return int(row.get("localId") or 0)
        except (TypeError, ValueError):
            return 0

    async def _fetch_collect_rows(self, session_id: str, since_ts: int, *,
                                  cursor: int = 0, watermark_ts: int = 0,
                                  floor_ts: int | None = None) -> dict:
        """读取该会话游标之后的消息（localId 增量游标；首轮/无 localId 时按时间窗）。

        分页按 createTime 倒序，因此采用「逐页过滤 + 边界页停止」：

        - 游标模式（``cursor > 0``）：凡是 ``localId > cursor`` 的行都收下（不管
          createTime 多旧，用来兜住晚到消息）；某一页已经没有游标之后的行，
          说明已经翻过游标边界，停止分页；
        - 首次建立游标（``cursor == 0``）：按 ``since_ts`` 时间窗收消息，并顺带返回
          本批最大 localId，供下一轮作为游标；
        - ``floor_ts``：更深的历史不再翻（默认等于 ``since_ts``）；游标模式传
          ``now - WCDA_CATCHUP_MAX_HOURS``，允许为找晚到消息翻得更深；
        - localId 回绕/重装：首页整页 ``localId <= cursor``，却出现比上轮时间水位
          更新的消息 → 说明本地库 localId 被重置，本轮弃用旧游标、按时间窗重采。

        返回 ``{"ok", "rows", "complete", "pages", "error", "local_id", "cursor_reset",
        "read_rows", "duplicate_rows"}``：
        - ``ok=False`` 表示读取失败或积压超过单轮上限，调用方应跳过该会话、保留游标；
        - ``rows`` 已按时间升序排列；``local_id`` 是本批最大 localId；
        - ``read_rows`` 是实际读到的原始行数、``duplicate_rows`` 是分页漂移造成的重复行
          （P2-3 读取放大观测；已按 rawid 去重，不是数据错误）。
        """
        settings = self.settings
        floor = int(since_ts if floor_ts is None else floor_ts)
        cursor_mode = int(cursor or 0) > 0
        max_local_id = 0
        cursor_reset = False
        read_rows = 0
        duplicate_rows = 0
        fetcher = getattr(self.wcda, "fetch_messages_page", None)
        if not callable(fetcher):
            # 兼容旧客户端 / 测试桩：退化为单次读取最近消息
            reader = getattr(self.wcda, "fetch_recent_messages", None)
            batch = await reader(session_id, limit=settings.wcda_catchup_limit) if callable(reader) else []
            read_rows = len(batch)
            rows: list[dict] = []
            for row in batch:
                if not isinstance(row, dict):
                    continue
                row_local_id = self._collect_row_local_id(row)
                max_local_id = max(max_local_id, row_local_id)
                if cursor_mode:
                    keep = row_local_id > cursor or (
                        row_local_id <= 0 and self._collect_row_ts(row) >= since_ts)
                else:
                    keep = self._collect_row_ts(row) >= since_ts
                if keep:
                    rows.append(row)
            rows.sort(key=self._collect_row_ts)
            return {"ok": True, "rows": rows, "complete": True, "pages": 1,
                    "local_id": max_local_id, "cursor_reset": False,
                    "read_rows": read_rows, "duplicate_rows": duplicate_rows}

        page_size = max(50, min(int(settings.wcda_catchup_page_size), 1000))
        max_rows = max(1000, int(settings.wcda_catchup_max_messages))
        rows = []
        seen: set[str] = set()
        offset = 0
        pages = 0
        while True:
            page = await fetcher(session_id, limit=page_size, offset=offset, order="desc")
            pages += 1
            if not page.get("ok"):
                return {"ok": False, "rows": rows, "complete": False, "pages": pages,
                        "error": page.get("error") or "fetch_failed",
                        "local_id": max_local_id, "cursor_reset": cursor_reset,
                        "read_rows": read_rows, "duplicate_rows": duplicate_rows}
            batch = [row for row in (page.get("messages") or []) if isinstance(row, dict)]
            if not batch:
                break
            read_rows += len(batch)
            page_max_local_id = max(self._collect_row_local_id(row) for row in batch)
            page_max_ts = max(self._collect_row_ts(row) for row in batch)
            if cursor_mode and pages == 1 and page_max_local_id <= cursor \
                    and page_max_ts > int(watermark_ts or 0):
                # localId 回绕/重装：本地库行号已重置，旧游标不能再用
                logger.warning("采集 %s 的 localId 游标失效（重装/回绕？），改用时间水位重采",
                               session_id)
                cursor_mode = False
                cursor = 0
                cursor_reset = True
            new_in_page = 0
            for row in batch:
                # 分页期间若有新消息插入，offset 可能使相邻页重复；按 rawid 去重
                key = str(row.get("id") or row.get("localId") or "")
                if key:
                    if key in seen:
                        duplicate_rows += 1
                        continue
                    seen.add(key)
                row_local_id = self._collect_row_local_id(row)
                max_local_id = max(max_local_id, row_local_id)
                if cursor_mode:
                    keep = row_local_id > cursor or (
                        row_local_id <= 0 and self._collect_row_ts(row) >= since_ts)
                else:
                    keep = self._collect_row_ts(row) >= since_ts
                if keep:
                    rows.append(row)
                    new_in_page += 1
            offset += len(batch)
            if cursor_mode and new_in_page == 0:
                # 该页已全部是游标之前的历史：游标边界到了
                break
            oldest = min(self._collect_row_ts(row) for row in batch)
            if oldest < floor:
                break
            if len(rows) >= max_rows:
                return {"ok": False, "rows": rows, "complete": False, "pages": pages,
                        "error": f"backlog_over_limit:{len(rows)}",
                        "local_id": max_local_id, "cursor_reset": cursor_reset,
                        "read_rows": read_rows, "duplicate_rows": duplicate_rows}
            if pages >= 400:
                return {"ok": False, "rows": rows, "complete": False, "pages": pages,
                        "error": "too_many_pages",
                        "local_id": max_local_id, "cursor_reset": cursor_reset,
                        "read_rows": read_rows, "duplicate_rows": duplicate_rows}
            # WCDA 正常会返回 hasMore；短页且明确没有更多时才认为历史到底
            if len(batch) < page_size and not page.get("has_more"):
                break
        rows.sort(key=self._collect_row_ts)
        return {"ok": True, "rows": rows, "complete": True, "pages": pages,
                "local_id": max_local_id, "cursor_reset": cursor_reset,
                "read_rows": read_rows, "duplicate_rows": duplicate_rows}

    async def _wcda_alive(self) -> bool | None:
        """探测 WCDA 是否在线（只在「没有监控会话」时用于解释 connected）。"""
        checker = getattr(self.wcda, "check_health", None)
        if not callable(checker):
            return None
        try:
            return bool(await checker())
        except Exception:  # noqa: BLE001 - 探测失败按未连接处理
            return False

    async def collect(self, minutes: int | None = None,
                      session_ids: list[str] | None = None,
                      mode: str = "window") -> dict:
        """采集入口：串行化各采集轮（周期轮 / 新置顶回补 / 深扫），避免共享的
        同轮推送收集器被并发轮次互相覆盖。"""
        async with self._collect_lock:
            return await self._collect_round(
                minutes=minutes, session_ids=session_ids, mode=mode)

    async def _collect_round(self, minutes: int | None = None,
                             session_ids: list[str] | None = None,
                             mode: str = "window") -> dict:
        """采集：拉取会话消息 → 入库（processed=0）→ 按会话一次处理 → 标记 processed=1。

        增量依据是**每会话 localId 游标**（WCDB 行序，与 createTime 解耦），用来兜住
        「写入晚、业务时间旧」的晚到消息；首次建立游标（或 localId 失效）时退回时间水位。
        每轮拿到消息后按会话一次性处理；单条消息处理失败最多重试
        ``MESSAGE_MAX_ATTEMPTS`` 次，超限进「失败队列」并告警，不再阻塞同会话后续消息。

        - 不传 ``session_ids``：采集当前监控范围（置顶 + 白名单），并兜底所有已入库未处理会话；
        - 传 ``session_ids``：回补指定会话（新置顶群）；
        - ``mode="window"``：固定回看 ``minutes``（新置顶回补 / 启动额外回补），不推进游标；
        - ``mode="watermark"``：从每个会话的 localId 游标继续，单轮最长回看
          ``WCDA_CATCHUP_MAX_HOURS``；每轮最多处理 ``WCDA_CATCHUP_ROUND_BUDGET`` 条，
          未处理完的留在下一轮；
        - 本轮新建的待办在轮末**合并成一条推送**（同轮合并推送），不再每个会话推一次。
        """
        started = time.monotonic()
        empty_result = {
            "ok": False, "mode": "off", "minutes": 0, "sessions": 0, "inserted": 0,
            "processed": 0, "scanned": 0, "pages": 0, "tasks_created": 0,
            "tasks_updated": 0, "dropped_other": 0, "failed": 0, "dead_lettered": 0,
            "budget_exhausted": False, "skipped": [], "pending": self.db.count_unprocessed(),
            "failed_queue": self.db.count_failed_messages(), "at": now_iso(),
            # 降级 / 可观测（P1-1/P1-2/P1-4/P2-3/P2-5）：ok 只表示“没有会话读取/处理失败”，
            # 抽取饱和、LLM 回退等“降级”单独用 degraded/degraded_reasons 暴露。
            "partial": False, "degraded": False, "degraded_reasons": [],
            "llm_batches": 0, "llm_fallbacks": 0, "last_llm_error": None,
            "extraction_cap_hits": 0, "messages_without_task": 0,
            "read_rows": 0, "duplicate_rows": 0, "read_ratio": 0.0,
            "sessions_failed": [], "sessions_failed_count": 0,
        }
        if self.data_source != "wcda" or not self.wcda.configured:
            return {**empty_result, "reason": "当前数据源不是 wcda"}

        mode = "watermark" if str(mode or "").lower() in ("watermark", "all", "auto") else "window"
        # 只有水位模式才推进 catchup_state；window 模式只扫固定窗口，写水位会让
        # 后续 watermark 轮误以为空档已覆盖（P1：混合调用永久漏采）。
        advance = mode == "watermark"
        now_ts = int(time.time())
        window_seconds = self._collection_window_seconds(minutes, mode)
        explicit = [str(item).strip() for item in (session_ids or []) if str(item).strip()]
        source_unreachable = False
        sessions_error: str | None = None
        if explicit:
            if not self._sessions_cache:
                self._sessions_cache = await self._fetch_sessions_cache()
            known = {str(s.get("username")): str(s.get("name") or "") for s in self._sessions_cache}
            targets = [{"session_id": sid, "group_name": known.get(sid, "")} for sid in explicit]
        else:
            targets, sessions_error = await self._monitored_sessions()
            if sessions_error:
                # 会话列表接口失败 ≠ 真的没有会话：本轮如实标记采集失败（P1-3），
                # 但本地已入库未处理的消息仍然继续兜底处理。
                self.connected = False
                self.last_error = f"读取监控会话失败：{sessions_error}"[:300]
            if not targets and not sessions_error:
                alive = await self._wcda_alive()
                if alive is False:
                    source_unreachable = True
                    self.connected = False
                    self.last_error = "读取不到监控会话：WeChatDataAnalysis 未连接"
            # 兜底：所有「已入库未处理」的会话都纳入本轮处理目标（不必重新拉取）。
            # 即使数据源暂时不可达，也先把本地已入库未处理的消息处理掉。
            known_ids = {str(item["session_id"]) for item in targets}
            for pending_session in self.db.unprocessed_sessions():
                session_id = str(pending_session["session_id"])
                if session_id and session_id not in known_ids:
                    targets.append({
                        "session_id": session_id,
                        "group_name": str(pending_session.get("group_name") or ""),
                        "fallback_only": True,
                    })
            if not targets:
                at = now_iso()
                if sessions_error or source_unreachable:
                    reason = self.last_error or "WeChatDataAnalysis 未连接"
                    failed = {**empty_result, "mode": mode,
                              "minutes": int(round(window_seconds / 60)),
                              "reason": reason, "at": at,
                              "degraded": True,
                              "degraded_reasons": (["sessions_unavailable"]
                                                   if sessions_error else [])}
                    self._last_collection = failed
                    self._last_collect_at = at
                    return failed
                # 数据源可达但当前没有要采集/要兜底的会话：视为一次成功的空轮
                empty_ok = {**empty_result, "ok": True, "mode": mode,
                            "minutes": int(round(window_seconds / 60)), "at": at}
                self._last_collection = empty_ok
                self._last_collect_at = at
                self._last_collect_ok_at = at
                self.connected = True
                self.last_error = None
                return empty_ok

        settings = self.settings
        budget = max(0, int(settings.wcda_catchup_round_budget))
        used_budget = 0
        budget_exhausted = False
        inserted_total = processed_total = sessions_touched = 0
        created_total = updated_total = dropped_total = 0
        scanned_total = pages_total = failures = 0
        read_rows_total = duplicate_rows_total = 0
        llm_batches_total = llm_fallbacks_total = 0
        cap_hits_total = without_task_total = 0
        degraded_reasons: set[str] = set()
        failed_sessions: list[str] = []
        if sessions_error:
            degraded_reasons.add("sessions_unavailable")
        dead_lettered: list[dict] = []
        skipped: list[str] = []
        # 同轮合并推送：本轮内新建的待办先收集，轮末合并成一条推送
        self._round_push = {"tasks": [], "summaries": []}
        collector: dict | None = None
        try:
            for target in targets:
                if budget and used_budget >= budget:
                    budget_exhausted = True
                    break
                session_id = str(target["session_id"])
                group_name = str(target.get("group_name") or "")
                label = group_name or session_id
                fetched: dict = {}
                since_ts = now_ts - window_seconds
                if not target.get("fallback_only"):
                    state = self.db.get_catchup_state(session_id) or {}
                    cursor = int(state.get("last_local_id") or 0) if advance else 0
                    watermark_ts = int(state.get("last_ts") or 0)
                    since_ts = self._collection_since_ts(
                        session_id, mode=mode, window_seconds=window_seconds, now_ts=now_ts)
                    floor_ts = None
                    if cursor > 0:
                        # 游标模式：允许为找「晚到消息」翻到 MAX_HOURS 之前；时间窗只用于无 localId 行
                        floor_ts = now_ts - max(1, int(settings.wcda_catchup_max_hours)) * 3600
                    fetched = await self._fetch_collect_rows(
                        session_id, since_ts, cursor=cursor,
                        watermark_ts=watermark_ts, floor_ts=floor_ts)
                    pages_total += int(fetched.get("pages") or 0)
                    read_rows_total += int(fetched.get("read_rows") or 0)
                    duplicate_rows_total += int(fetched.get("duplicate_rows") or 0)
                    if not fetched.get("ok"):
                        skipped.append(session_id)
                        failed_sessions.append(session_id)
                        failures += 1
                        degraded_reasons.add("partial_failure")
                        logger.warning("采集 %s 读取失败/积压过大，留到下一轮：%s",
                                       label, fetched.get("error"))
                    else:
                        rows = fetched.get("rows") or []
                        scanned_total += len(rows)
                        converted: list[dict] = []
                        for row in rows:
                            try:
                                item = WcdaClient.convert_api_message(
                                    session_id, group_name, row, self.wcda.account)
                            except (TypeError, ValueError):  # 单条脏数据不让整轮失败
                                continue
                            if item:
                                converted.append(item)
                        # existing_message_ids 只跳过已终结（processed=1/2）的消息；
                        # processed=0 的消息要重新处理
                        processed_ids = self.db.existing_message_ids(
                            list({str(item.get("rawid") or "") for item in converted}))
                        fresh = [item for item in converted
                                 if str(item.get("rawid") or "") not in processed_ids]
                        if fresh:
                            try:
                                inserted_total += self.db.add_messages(fresh)
                            except Exception as error:  # noqa: BLE001 - 入库失败保留游标，下轮重试
                                failures += 1
                                skipped.append(session_id)
                                failed_sessions.append(session_id)
                                degraded_reasons.add("partial_failure")
                                logger.warning("采集 %s 入库失败，保留游标下轮重试：%s", label, error)
                                continue
                            self.last_event_at = now_iso()
                        # 拉取成功后即推进游标：消息已经持久化，处理失败交给 DB 的未处理兜底
                        if advance:
                            self.db.set_catchup_state(
                                session_id, max(now_ts, since_ts),
                                fetched.get("local_id") or None,
                                reset_local_id=bool(fetched.get("cursor_reset")))

                remaining = (budget - used_budget) if budget else 0
                pending = self.db.unprocessed_messages(
                    session_id, limit=(remaining + 1) if budget else 0)
                if budget and len(pending) > remaining:
                    budget_exhausted = True
                    pending = pending[:remaining]
                if not pending:
                    continue
                if explicit:
                    self._force_allow_sessions.add(session_id)
                try:
                    outcome = await self._process_pending_messages(session_id, pending)
                finally:
                    if explicit:
                        self._force_allow_sessions.discard(session_id)
                processed_total += outcome["processed"]
                used_budget += outcome["processed"]
                dead_lettered.extend(outcome["dead"])
                llm_batches_total += int(outcome.get("llm_batches") or 0)
                llm_fallbacks_total += int(outcome.get("llm_fallbacks") or 0)
                cap_hits_total += int(outcome.get("cap_hits") or 0)
                without_task_total += int(outcome.get("messages_without_task") or 0)
                degraded_reasons.update(outcome.get("degraded_reasons") or [])
                if outcome.get("failed"):
                    # 读取/处理失败（含整体性故障）一律体现在采集健康状态里：
                    # 本轮 ok=false、partial 按是否有实际进展标记（P1-4/P2-5）
                    failures += 1
                    failed_sessions.append(session_id)
                    degraded_reasons.add(str(outcome.get("failure_kind") or "partial_failure"))
                    logger.warning("采集 %s：处理失败 %s 条（%s）", label, outcome["failed"],
                                   outcome.get("failure_kind") or "failure")
                if outcome["processed"]:
                    sessions_touched += 1
                created_total += outcome["tasks_created"]
                updated_total += outcome["tasks_updated"]
                dropped_total += outcome["dropped_other"]
                if outcome["processed"] and (
                        outcome["tasks_created"] or outcome["tasks_updated"]):
                    logger.info("采集 %s：处理 %s 条消息，新建 %s 项待办、更新 %s 项",
                                label, outcome["processed"], outcome["tasks_created"],
                                outcome["tasks_updated"])
                elif outcome["processed"]:
                    logger.info("采集 %s：处理 %s 条消息，未产生新待办", label, outcome["processed"])
                if budget_exhausted:
                    break
        finally:
            collector = self._round_push
            self._round_push = None

        result = {
            "ok": True,
            "mode": mode,
            "minutes": int(round(window_seconds / 60)),
            "sessions": sessions_touched,
            "inserted": inserted_total,
            "processed": processed_total,
            "scanned": scanned_total,
            "pages": pages_total,
            "tasks_created": created_total,
            "tasks_updated": updated_total,
            "dropped_other": dropped_total,
            "failed": len(dead_lettered),
            "dead_lettered": len(dead_lettered),
            "budget_exhausted": budget_exhausted,
            "skipped": skipped[:20],
            "pending": self.db.count_unprocessed(),
            "failed_queue": self.db.count_failed_messages(),
            # 降级 / 抽取饱和 / 分页读取放大：外部（设置页、脚本、告警）可见
            "partial": False,
            "degraded": bool(degraded_reasons),
            "degraded_reasons": sorted(degraded_reasons),
            "llm_batches": llm_batches_total,
            "llm_fallbacks": llm_fallbacks_total,
            "last_llm_error": self.last_llm_error,
            "extraction_cap_hits": cap_hits_total,
            "messages_without_task": without_task_total,
            "read_rows": read_rows_total,
            "duplicate_rows": duplicate_rows_total,
            "read_ratio": (round(read_rows_total / scanned_total, 2)
                           if read_rows_total and scanned_total else 1.0),
            "sessions_failed": failed_sessions[:20],
            "sessions_failed_count": len(failed_sessions),
            "duration_ms": int((time.monotonic() - started) * 1000),
            "at": now_iso(),
        }
        if source_unreachable:
            # 数据源不可达：本地未处理消息仍然已经处理，但 connected/ok 如实标记失败
            result["ok"] = False
            result["reason"] = self.last_error or "WeChatDataAnalysis 未连接"
            self.connected = False
        elif sessions_error or failures:
            # ok=false 表示本轮有会话读取/处理失败；partial 表示仍有消息被处理（有进展）
            result["ok"] = False
            result["partial"] = bool(processed_total)
            if sessions_error:
                result["reason"] = f"读取监控会话失败：{sessions_error}"[:300]
                self.connected = False
            else:
                result["reason"] = (
                    f"采集部分失败：{failures} 个会话读取/处理失败（详见日志）"
                    if result["partial"]
                    else f"采集失败：{failures} 个会话读取/处理失败（详见日志）")
                self.connected = True
            self.last_error = result["reason"]
        else:
            # 只要采集轮能跑完就说明数据源可达
            self.connected = True
            self.last_error = None
            if not result["degraded"]:
                self._last_collect_ok_at = result["at"]
        self._last_collection = result
        self._last_collect_at = result["at"]
        # 同轮合并推送：本轮所有新建待办只发一条推送
        if collector is not None:
            try:
                await self._flush_round_push(collector)
            except Exception as error:  # noqa: BLE001 - 推送失败不影响采集结果
                logger.warning("同轮合并推送失败：%s", error)
        # 处理失败重试超限：落失败队列并告警（不受免打扰限制）
        if dead_lettered:
            await self._alert_failed_messages(dead_lettered)
        # 静默降级/抽取饱和不再只写日志：按阈值限频推送提醒
        await self._maybe_alert_llm_degraded(result)
        await self._maybe_alert_extraction_cap(result)
        logger.info("采集完成（%s）：会话 %s、扫描 %s、新增消息 %s、处理 %s、"
                    "新建待办 %s、更新 %s、失败入队 %s、分页 %s、待处理 %s、耗时 %sms%s%s",
                    mode, result["sessions"], result["scanned"], result["inserted"],
                    result["processed"], result["tasks_created"], result["tasks_updated"],
                    result["dead_lettered"], result["pages"], result["pending"],
                    result["duration_ms"],
                    "（预算用完，余量下轮继续）" if budget_exhausted else "",
                    (f"（降级：{','.join(result['degraded_reasons'])}）"
                     if result["degraded"] else ""))
        return result

    async def catchup(self, minutes: int | None = None,
                      session_ids: list[str] | None = None,
                      mode: str = "window") -> dict:
        """已废弃：保留给历史脚本/旧调用，等价于 :meth:`collect`。"""
        return await self.collect(minutes=minutes, session_ids=session_ids, mode=mode)

    async def _collect_loop(self) -> None:
        settings = self.settings
        await asyncio.sleep(8)  # 等服务启动完成再首轮采集
        try:
            # 先按 localId 游标采停机/断网期间的空档；配置了启动回补窗口时再额外按窗口采一次
            await self.collect(mode="watermark")
            if settings.wcda_catchup_on_start_minutes > 0:
                await self.collect(minutes=settings.wcda_catchup_on_start_minutes, mode="window")
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - 采集失败不阻塞服务
            self._record_collect_failure(error)
            logger.warning("启动采集失败：%s", error)
        interval = settings.wcda_catchup_seconds
        if interval <= 0:
            return
        while True:
            await asyncio.sleep(interval)
            try:
                await self.collect(mode="watermark")
                await self._maybe_deep_scan()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001
                self._record_collect_failure(error)
                logger.warning("采集失败：%s", error)

    def _deep_scan_due(self) -> bool:
        minutes = int(self.settings.wcda_deep_scan_minutes)
        if minutes <= 0:
            return False
        return time.monotonic() - self._last_deep_scan_at >= minutes * 60

    async def _maybe_deep_scan(self) -> dict | None:
        """晚到消息兜底深扫：按时间窗重扫一批，补上 localId 游标扫描深度之外的旧 ts 消息。

        WCDA 的 /api/chat/messages 按 createTime 排序、且没有 localId 过滤参数，所以
        「写入晚、业务时间旧」的消息可能落在游标快速扫描的深度之外（见
        docs/API.md「采集」）。深扫不推进 localId 游标，只做时间窗重扫 + rawid 去重，
        因此不会重复建待办，也不会把游标推错。
        """
        if not self._deep_scan_due():
            return None
        self._last_deep_scan_at = time.monotonic()
        minutes = max(30, int(self.settings.wcda_deep_scan_lookback_minutes))
        logger.info("开始晚到消息深扫：回看 %s 分钟（不影响 localId 游标）", minutes)
        try:
            result = await self.collect(minutes=minutes, mode="window")
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - 深扫失败不影响主采集轮
            logger.warning("晚到消息深扫失败：%s", error)
            self._last_deep_scan_result = {
                "at": now_iso(), "lookback_minutes": minutes,
                "error": f"{type(error).__name__}: {error}"[:200],
            }
            return None
        logger.info("晚到消息深扫完成：扫描 %s、新增 %s、处理 %s、新建待办 %s",
                    result.get("scanned"), result.get("inserted"),
                    result.get("processed"), result.get("tasks_created"))
        self._last_deep_scan_iso = now_iso()
        self._last_deep_scan_result = {
            "at": self._last_deep_scan_iso,
            "lookback_minutes": minutes,
            "scanned": int(result.get("scanned") or 0),
            "inserted": int(result.get("inserted") or 0),
            "processed": int(result.get("processed") or 0),
            "tasks_created": int(result.get("tasks_created") or 0),
        }
        result["deep_scan"] = True
        return result

    def _record_collect_failure(self, error: Exception, mode: str = "watermark") -> None:
        """把失败轮写进采集快照，设置页「上次采集失败」和排障都能看到原因。"""
        self.connected = False
        self.last_error = f"{type(error).__name__}: {error}"[:300]
        at = now_iso()
        self._last_collection = {
            "ok": False, "mode": mode, "minutes": 0, "sessions": 0, "inserted": 0,
            "processed": 0, "scanned": 0, "pages": 0, "tasks_created": 0,
            "tasks_updated": 0, "dropped_other": 0, "failed": 0, "dead_lettered": 0,
            "budget_exhausted": False, "skipped": [],
            "pending": self.db.count_unprocessed(),
            "failed_queue": self.db.count_failed_messages(), "at": at,
            "partial": False, "degraded": True, "degraded_reasons": ["collect_error"],
            "llm_batches": 0, "llm_fallbacks": 0, "last_llm_error": self.last_llm_error,
            "extraction_cap_hits": 0, "messages_without_task": 0,
            "read_rows": 0, "duplicate_rows": 0, "read_ratio": 0.0,
            "sessions_failed": [], "sessions_failed_count": 0,
            "reason": self.last_error,
        }
        self._last_collect_at = at

    def _collect_enabled(self) -> bool:
        return bool(self.data_source == "wcda" and self.wcda.configured
                    and (self.settings.wcda_catchup_seconds > 0
                         or self.settings.wcda_catchup_on_start_minutes > 0))

    def _collection_metrics(self) -> dict:
        """上一轮采集的降级/抽取饱和/分页读数快照（health 与设置页共用）。"""
        last = self._last_collection or {}
        return {
            "degraded": bool(last.get("degraded")),
            "degraded_reasons": list(last.get("degraded_reasons") or []),
            "partial": bool(last.get("partial")),
            "llm_batches": int(last.get("llm_batches") or 0),
            "llm_fallbacks": int(last.get("llm_fallbacks") or 0),
            "llm_fallback_total": self._llm_fallback_total,
            "llm_fallback_streak": self._llm_fallback_streak,
            "last_llm_error": self.last_llm_error,
            "extraction_cap_hits": int(last.get("extraction_cap_hits") or 0),
            "messages_without_task": int(last.get("messages_without_task") or 0),
            "read_rows": int(last.get("read_rows") or 0),
            "duplicate_rows": int(last.get("duplicate_rows") or 0),
            "read_ratio": last.get("read_ratio") or 0.0,
            "sessions_failed": list(last.get("sessions_failed") or []),
            "llm": {
                "enabled": bool(self.settings.llm_enabled),
                "configured": bool(self.settings.llm_enabled and self.settings.llm_api_key),
                "batch_messages": int(self.settings.llm_batch_messages),
            },
            "deep_scan": {
                "minutes": int(self.settings.wcda_deep_scan_minutes),
                "lookback_minutes": int(self.settings.wcda_deep_scan_lookback_minutes),
                "disabled": int(self.settings.wcda_deep_scan_minutes) <= 0,
                "due": self._deep_scan_due(),
                "last_at": self._last_deep_scan_iso,
                "last": self._last_deep_scan_result,
            },
        }

    def collection_state(self) -> dict:
        """采集游标/参数快照（设置页与排障用；states 最多返回最近 200 个会话）。"""
        settings = self.settings
        states = self.db.catchup_states()
        return {
            "mode": "watermark" if self._collect_enabled() else "off",
            "seconds": settings.wcda_catchup_seconds,
            "lookback_minutes": settings.wcda_catchup_lookback_minutes,
            "backfill_minutes": settings.wcda_catchup_on_start_minutes,
            "max_hours": settings.wcda_catchup_max_hours,
            "page_size": settings.wcda_catchup_page_size,
            "round_budget": settings.wcda_catchup_round_budget,
            "empty_history_hours": settings.wcda_catchup_empty_history_hours,
            "overlap_seconds": settings.wcda_catchup_overlap_seconds,
            "deep_scan_minutes": settings.wcda_deep_scan_minutes,
            "deep_scan_lookback_minutes": settings.wcda_deep_scan_lookback_minutes,
            "max_attempts": settings.message_max_attempts,
            "llm_batch_messages": settings.llm_batch_messages,
            "watermark_sessions": len(states),
            "cursor_sessions": sum(1 for item in states if int(item.get("last_local_id") or 0) > 0),
            "pending_messages": self.db.count_unprocessed(),
            "pending_sessions": len(self.db.unprocessed_sessions()),
            "failed_messages": self.db.count_failed_messages(),
            "failed_sessions": len(self.db.failed_sessions()),
            "last_ok_at": self._last_collect_ok_at,
            "states": states[-200:],
            "last": self._last_collection,
            **self._collection_metrics(),
        }

    def catchup_state(self) -> dict:
        """兼容旧调用：等价于 :meth:`collection_state`。"""
        return self.collection_state()

    # ---------- 失败队列 ----------
    def failed_queue(self, limit: int = 100, offset: int = 0) -> dict:
        """失败队列快照：重试超限的消息（不再自动重试，等人工处理）。"""
        items = self.db.failed_messages(limit=limit, offset=offset)
        return {
            "failed": items,
            "count": self.db.count_failed_messages(),
            "sessions": self.db.failed_sessions(),
            "max_attempts": self.settings.message_max_attempts,
        }

    def retry_failed(self, rawids: list[str] | None = None, limit: int = 500) -> dict:
        """把失败队列消息放回待处理（下一轮采集轮会自动重试）。"""
        requeued = self.db.requeue_failed_messages(rawids, limit=limit)
        if requeued:
            logger.info("失败队列重试：%s 条消息放回待处理", requeued)
        return {"ok": True, "requeued": requeued,
                "remaining": self.db.count_failed_messages()}

    # ---------- 事件账本 ----------
    def _matches_custom(self, message: dict) -> bool:
        whitelist = self.settings.group_whitelist
        if not whitelist:
            return False
        haystack = f"{message.get('session_id', '')} {message.get('group_name', '')}".lower()
        return any(item.lower() in haystack for item in whitelist)

    def _message_allowed(self, message: dict) -> bool:
        settings = self.settings
        # 显式回补指定会话时临时放行（不改变长期白名单配置）
        if str(message.get("session_id") or "") in self._force_allow_sessions:
            return True
        is_group = bool(message.get("is_group", True))
        if not is_group and not settings.include_private:
            return False
        mode = settings.whitelist_mode
        if mode == "all":
            return True
        custom = self._matches_custom(message)
        if mode == "custom":
            # 模式选了自定义但名单为空时，不静默拦截所有消息
            return True if not settings.group_whitelist else custom
        pinned = self.pinned_ready and message.get("session_id") in self.pinned_sessions
        if mode == "pinned":
            if not self.pinned_ready:
                if not self._pinned_warned:
                    logger.warning("置顶白名单尚未同步，暂不拦截消息")
                    self._pinned_warned = True
                return True
            return pinned
        if mode == "pinned_custom":
            if not self.pinned_ready and not settings.group_whitelist:
                return True
            return pinned or custom
        return True

    # ---------- 失败重试 / 失败队列 ----------
    async def _process_pending_messages(self, session_id: str,
                                        pending: list[dict]) -> dict:
        """处理单个会话的待处理消息，失败时按条隔离、按次重试，超限落失败队列。

        设计要点（不阻塞同会话后续消息）：
        1. 整批处理成功 → 全部标记 processed=1；
        2. 整批失败 → 逐条隔离重试，能单独处理成功的消息立即标记已处理，只有真正
           失败的那条累积 attempts；
        3. 待处理超过 ``_ISOLATE_LIMIT`` 条时，先隔离探测前 N 条：若探测样本**全部**
           失败，更像 LLM/网络等整体性故障，本轮只记 last_error、不计 attempts，
           避免一次短时故障把整批积压打进失败队列；只要样本里有成功的，就按正常
           毒消息逻辑计 attempts（多轮后大积压会被逐批排空）；
        4. attempts 达到 ``MESSAGE_MAX_ATTEMPTS`` → 置 processed=2（失败队列）并返回
           告警明细，下一轮不再自动重试，同会话后续消息继续正常处理。

        返回 ``{"processed", "dead", "failed", "tasks_created", "tasks_updated",
        "dropped_other"}``；``failed`` 是本轮失败的消息条数（用于采集健康状态 last_error）。
        """
        max_attempts = max(1, int(self.settings.message_max_attempts))
        outcome = {"processed": 0, "dead": [], "tasks_created": 0,
                   "tasks_updated": 0, "dropped_other": 0, "failed": 0,
                   # 降级/抽取饱和（P1-1/P1-2/P1-4）：由 process_batch 聚合上来
                   "llm_batches": 0, "llm_fallbacks": 0, "cap_hits": 0,
                   "messages_without_task": 0, "degraded_reasons": [],
                   "failure_kind": None}
        by_id = {str(item.get("rawid") or ""): item for item in pending}
        try:
            stats = await self.process_batch(pending)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - 单条毒消息隔离，不拖垮整轮
            if len(pending) <= self._ISOLATE_LIMIT:
                logger.warning("采集处理失败（%s，%s 条），逐条隔离重试：%s",
                               session_id, len(pending), error)
                failed_items = await self._isolate_messages(pending, outcome)
                self._record_failure_attempts(failed_items, by_id, max_attempts, outcome)
                outcome["failed"] = len(failed_items)
                outcome["failure_kind"] = "partial_failure"
                self._global_failure_streaks.pop(session_id, None)
                return outcome
            # 大积压：先隔离探测前 N 条，区分「整体性故障」与「个别毒消息」
            probe = pending[: self._ISOLATE_LIMIT]
            logger.warning("采集处理失败（%s，%s 条，先隔离探测 %s 条）：%s",
                           session_id, len(pending), len(probe), error)
            failed_items = await self._isolate_messages(probe, outcome)
            if failed_items and outcome["processed"] == 0:
                # 探测样本全部失败 → 更像整体性故障：不计 attempts，只记 last_error
                self.db.record_message_errors(
                    [str(item.get("rawid") or "") for item in pending],
                    f"{type(error).__name__}: {error}"[:300])
                streak = self._global_failure_streaks.get(session_id, 0) + 1
                self._global_failure_streaks[session_id] = streak
                outcome["failed"] = len(pending)
                outcome["failure_kind"] = "sustained_failure"
                if streak == 3 or streak % 10 == 0:
                    logger.warning("采集持续处理失败（%s，连续 %s 轮），升级告警：%s",
                                   session_id, streak, error)
                    await self._alert_sustained_failure(session_id, streak, len(pending), error)
                else:
                    logger.warning("采集处理整体性失败（%s），本轮不计重试次数，保留待处理：%s",
                                   session_id, error)
                return outcome
            self._record_failure_attempts(failed_items, by_id, max_attempts, outcome)
            outcome["failed"] = len(failed_items)
            outcome["failure_kind"] = "partial_failure"
            if outcome["processed"]:
                self._global_failure_streaks.pop(session_id, None)
            return outcome

        # 整批成功（含被过滤/判为他人）→ 标记已处理
        self._global_failure_streaks.pop(session_id, None)
        self.db.mark_messages_processed([str(item.get("rawid") or "") for item in pending])
        outcome["processed"] = len(pending)
        outcome["tasks_created"] = int(stats.get("tasks_created", 0))
        outcome["tasks_updated"] = int(stats.get("tasks_updated", 0))
        outcome["dropped_other"] = int(stats.get("dropped_other", 0))
        outcome["llm_batches"] = int(stats.get("llm_batches") or 0)
        outcome["llm_fallbacks"] = int(stats.get("llm_fallbacks") or 0)
        outcome["cap_hits"] = int(stats.get("cap_hits") or 0)
        outcome["messages_without_task"] = int(stats.get("messages_without_task") or 0)
        outcome["degraded_reasons"] = list(stats.get("degraded_reasons") or [])
        return outcome

    async def _isolate_messages(self, messages: list[dict],
                                outcome: dict) -> list[tuple[dict, Exception]]:
        """逐条重试一批消息：成功的立即标记 processed=1，返回真正失败的消息+异常。"""
        failed: list[tuple[dict, Exception]] = []
        for message in messages:
            try:
                single = await self.process_batch([message])
            except asyncio.CancelledError:
                raise
            except Exception as single_error:  # noqa: BLE001
                failed.append((message, single_error))
                continue
            outcome["processed"] += 1
            outcome["tasks_created"] += int(single.get("tasks_created", 0))
            outcome["tasks_updated"] += int(single.get("tasks_updated", 0))
            outcome["dropped_other"] += int(single.get("dropped_other", 0))
            outcome["llm_batches"] += int(single.get("llm_batches") or 0)
            outcome["llm_fallbacks"] += int(single.get("llm_fallbacks") or 0)
            outcome["cap_hits"] += int(single.get("cap_hits") or 0)
            outcome["messages_without_task"] += int(single.get("messages_without_task") or 0)
            outcome["degraded_reasons"].extend(single.get("degraded_reasons") or [])
            self.db.mark_messages_processed([str(message.get("rawid") or "")])
        return failed

    def _record_failure_attempts(self, failed_items: list[tuple[dict, Exception]],
                                 by_id: dict[str, dict], max_attempts: int,
                                 outcome: dict) -> None:
        """为隔离后仍失败的消息累计 attempts，并把超限的放入失败队列（告警明细）。"""
        for message, failure in failed_items:
            rawid = str(message.get("rawid") or "")
            if not rawid:
                continue
            detail = self.db.record_message_failures(
                [rawid], f"{type(failure).__name__}: {failure}"[:300], max_attempts)
            outcome["dead"].extend(self._dead_letter_items(
                detail.get("dead") or [], by_id, failure))

    @staticmethod
    def _dead_letter_items(rawids: list[str], by_id: dict[str, dict],
                           error: Exception) -> list[dict]:
        """组装告警明细：哪条消息、哪个群、什么错误、重试了几次。"""
        items: list[dict] = []
        for rawid in rawids:
            message = by_id.get(rawid) or {}
            items.append({
                "rawid": rawid,
                "group_name": str(message.get("group_name") or ""),
                "sender": str(message.get("sender") or ""),
                "content": str(message.get("content") or ""),
                "error": f"{type(error).__name__}: {error}"[:200],
            })
        return items

    async def _alert_failed_messages(self, items: list[dict]) -> None:
        """失败队列告警：一条汇总推送（不受免打扰限制），并在日志里写明原因。"""
        if not items:
            return
        title = f"微信待办 · {len(items)} 条消息处理失败"
        lines: list[str] = []
        for item in items[:5]:
            meta = " · ".join(part for part in (
                clip(item.get("group_name") or "", 20),
                clip(item.get("sender") or "", 20),
            ) if part)
            text = clip(item.get("content") or "", 60) or item.get("rawid") or ""
            lines.append(f"• {text}" + (f"（{meta}）" if meta else ""))
        if len(items) > 5:
            lines.append(f"…共 {len(items)} 条")
        lines.append(f"已重试 {self.settings.message_max_attempts} 次仍失败，已移入失败队列，"
                     f"不影响同会话后续消息；可在设置页「运行状态 → 采集」查看并重试。")
        body = "\n".join(lines)[:480]
        logger.error("消息处理失败已进入失败队列（%s 条）：%s", len(items),
                     "；".join(f"{clip(item.get('content') or '', 30)} <- {item.get('error')}"
                               for item in items[:5]))
        try:
            await self._notify(title, body, priority="high", bypass_quiet=True)
        except Exception as error:  # noqa: BLE001 - 告警失败不影响采集/入库
            logger.warning("失败队列告警推送失败：%s", error)

    async def _alert_sustained_failure(self, session_id: str, streak: int,
                                       pending: int, error: Exception) -> None:
        """连续多轮整体性处理失败时的升级告警。

        大积压 + 探测样本全部失败时按「整体性故障」处理（不消耗重试次数、不进失败队列），
        但若连续多轮仍不恢复，可能是一次长期故障，也可能是一批每条都失败的畸形消息：
        推送一条告警让用户尽早介入（检查数据源/日志，或在修复后重试失败队列）。
        """
        group = str(self.pinned_names.get(session_id) or session_id)
        body = (f"采集连续 {streak} 轮处理失败（会话 {clip(group, 30)}，待处理 {pending} 条）。\n"
                f"可能为整体性故障（大模型/网络/数据源），也可能是这批消息本身有问题。\n"
                f"未消耗重试次数、未进失败队列，恢复后会自动继续；请检查日志与数据源。\n"
                f"最近错误：{type(error).__name__}: {clip(str(error), 120)}")[:480]
        logger.error("采集连续 %s 轮整体性失败（%s，待处理 %s 条）：%s",
                     streak, session_id, pending, error)
        try:
            await self._notify("微信待办 · 采集持续失败", body, priority="high",
                               bypass_quiet=True)
        except Exception as alert_error:  # noqa: BLE001 - 告警失败不影响采集/入库
            logger.warning("采集持续失败告警推送失败：%s", alert_error)

    # ---------- LLM 降级 / 抽取饱和 告警（P1-1/P1-2） ----------
    def _note_llm_success(self) -> None:
        """任意一批 LLM 调用成功即视为恢复，重置“连续回退”计数与告警阈值。"""
        if self._llm_fallback_streak:
            self._llm_alert_next = self._LLM_ALERT_FIRST
        self._llm_fallback_streak = 0

    def _note_llm_fallback(self, error: Exception) -> None:
        """记录一次 LLM 回退到规则模式；状态会在 collect 结果/health 里暴露。"""
        detail = getattr(error, "detail", None) or f"{type(error).__name__}: {error}"
        self.last_llm_error = str(detail)[:300]
        self._llm_fallback_total += 1
        self._llm_fallback_streak += 1
        logger.warning("LLM 调用失败，已回退规则模式（连续第 %s 次）：%s",
                       self._llm_fallback_streak, self.last_llm_error)

    def _llm_alert_due(self) -> bool:
        if self._llm_fallback_streak < self._llm_alert_next:
            return False
        return time.monotonic() - self._last_llm_alert_at >= self._LLM_ALERT_INTERVAL

    async def _maybe_alert_llm_degraded(self, result: dict) -> None:
        """LLM 持续不可用/上下文溢出时推送一次告警（限频，避免每批一条）。"""
        if not result.get("llm_fallbacks") or not self._llm_alert_due():
            return
        self._last_llm_alert_at = time.monotonic()
        self._llm_alert_next = self._llm_fallback_streak + self._LLM_ALERT_EVERY
        body = (f"大模型调用连续失败 {self._llm_fallback_streak} 次，采集已自动回退本地规则模式。\n"
                f"规则模式对模糊待办的召回明显低于大模型，待办可能变少/不准；"
                f"消息不会丢，恢复后会自动继续用大模型。\n"
                f"最近错误：{clip(self.last_llm_error or '', 160)}\n"
                f"可在设置页「运行状态 → 采集」查看降级状态。")[:480]
        logger.error("LLM 持续失败已回退规则模式（连续 %s 次）：%s",
                     self._llm_fallback_streak, self.last_llm_error)
        try:
            await self._notify("微信待办 · 大模型不可用，已降级", body, priority="high",
                               bypass_quiet=True)
        except Exception as alert_error:  # noqa: BLE001 - 告警失败不影响采集/入库
            logger.warning("LLM 降级告警推送失败：%s", alert_error)

    async def _maybe_alert_extraction_cap(self, result: dict) -> None:
        """抽取命中上限且大量消息没生成待办时提醒“可能漏建”（限频）。

        上限（LLM 每批 8 事项/12 任务、规则每批 12 条）是设计容量；分片归并后仍命中时，
        说明单批消息量依旧过大，需要让用户知道“这轮可能少建了待办”，而不是静默消费。
        """
        hits = int(result.get("extraction_cap_hits") or 0)
        without = int(result.get("messages_without_task") or 0)
        if hits <= 0 or (without < 10 and hits < 3):
            return
        if time.monotonic() - self._last_cap_alert_at < self._CAP_ALERT_INTERVAL:
            return
        self._last_cap_alert_at = time.monotonic()
        body = (f"本轮有 {without} 条消息处理了但没有生成待办，"
                f"其中 {hits} 批触发了抽取上限（每批最多 8 事项/12 任务）。\n"
                f"如果是消息洪峰，建议调小 LLM_BATCH_MESSAGES（当前 "
                f"{int(self.settings.llm_batch_messages)} 条/批）让每批更聚焦；"
                f"若确认都是闲聊可忽略本提醒。\n"
                f"也可在设置页「运行状态 → 采集」查看“抽取上限/未生成待办”计数。")[:480]
        logger.warning("抽取命中上限，可能有待办漏建：cap_hits=%s messages_without_task=%s",
                       hits, without)
        try:
            await self._notify("微信待办 · 有待办可能没提取全", body, priority="normal")
        except Exception as alert_error:  # noqa: BLE001 - 告警失败不影响采集/入库
            logger.warning("抽取上限提醒推送失败：%s", alert_error)

    async def _identity_for(self, session_id: str, group_name: str,
                            is_group: bool) -> IdentityContext:
        """取本会话的身份上下文；任何异常都退回手动别名，不影响原流程。"""
        try:
            return await self.identity.for_session(session_id, group_name, is_group=is_group)
        except Exception as error:  # noqa: BLE001 - 身份识别是辅助能力
            logger.warning("身份识别失败，按手动别名继续：%s", error)
            return self.identity.plain_context(session_id, group_name, is_group)

    def _note_judgement(self, verdict, kept: bool) -> None:
        if kept:
            if verdict.kind == "me":
                self._judge_stats["kept_me"] += 1
            elif verdict.kind == "all":
                self._judge_stats["kept_all"] += 1
            else:
                self._judge_stats["kept_none"] += 1
        else:
            self._judge_stats["dropped_other"] += 1

    def _log_dropped_message(self, message: dict, verdict) -> None:
        item = {
            "at": now_iso(),
            "group": message.get("group_name") or "私聊",
            "sender": message.get("sender") or "",
            "targets": list(verdict.targets),
            "reason": verdict.reason,
            "content": clip(message.get("content") or "", 120),
        }
        self._recent_drops.append(item)
        logger.info("丢弃指向他人的消息：群=%s 发送者=%s 判定=%s 内容=%s",
                    item["group"], item["sender"] or "-", verdict.reason, item["content"])

    def _filter_messages(self, messages: list[dict],
                         identity: IdentityContext | None = None) -> tuple[list[dict], int]:
        """白名单/系统消息/噪音过滤；并丢弃「明确指向他人」的 @/点名消息。

        返回 (保留的消息, 被身份判定丢弃的条数)。没有被 @/点名的消息行为不变。
        """
        settings = self.settings
        # 规则过滤的“@我”识别也带上群昵称/自动别名，避免把 @ 我的短消息当闲聊丢掉
        me_names = list(dict.fromkeys(
            list(identity.aliases if identity else []) + list(settings.me_names or [])
        ))
        filtered = []
        dropped_other = 0
        for message in messages:
            if not self._message_allowed(message):
                continue
            render_type = (message.get("render_type") or "").lower()
            content = message.get("content", "")
            if render_type in ("system", "revoke", "recall"):
                continue
            if (not settings.llm_enabled and render_type in ("link", "app", "mini_program")
                    and len(content.strip()) < 30
                    and not has_action_hint(content)
                    and not any(word in content for word in EVENT_HINTS)):
                # 规则模式下的短链接卡片：只有标题里带“报名/统计/活动”等行动信息才保留
                continue
            # 身份判定：只有明确指向他人时才丢弃；未指定对象/认不出来 → 保持原逻辑
            verdict = classify_message(
                content,
                at_usernames=message.get("at_usernames"),
                at_names=message.get("at_names"),
                quote_username=message.get("quote_username") or "",
                identity=identity,
                fuzzy=settings.identity_fuzzy,
            )
            message["_verdict"] = verdict
            if settings.identity_filter and verdict.kind == "other":
                dropped_other += 1
                self._note_judgement(verdict, kept=False)
                self._log_dropped_message(message, verdict)
                continue
            if settings.llm_enabled:
                # LLM 模式只做轻量降噪，是否成待办交给模型判断
                if not should_send_to_llm(content, me_names):
                    continue
            elif not is_worth_summarizing(content, me_names):
                continue
            self._note_judgement(verdict, kept=True)
            filtered.append(message)
        return filtered, dropped_other

    def _open_events_payload(self, session_id: str) -> list[dict]:
        # 不按条数截断：事项不再自动关闭，该会话的所有事项都保留为归并目标，
        # 否则「再次聊到的同一件事」会被新事项挤出候选。
        # 但已完成 / 已忽略的阶段（事件里没有待办任务）不参与归并：
        # 否则模型会把新消息塞进已经做完的事项里。
        events = self.db.open_events(session_id)
        payload: list[dict] = []
        for event in events:
            tasks = [
                task for task in self.db.event_tasks(event["id"])
                if task.get("status") in ("pending", "snoozed")
            ]
            if not tasks:
                continue
            event["tasks"] = tasks
            payload.append(event)
        return payload

    def _llm_batches(self, messages: list[dict]) -> list[list[dict]]:
        """按 LLM_BATCH_MESSAGES 把一轮消息切成多个归并批次（0 = 不切批）。"""
        size = int(getattr(self.settings, "llm_batch_messages", 0) or 0)
        if size <= 0 or len(messages) <= size:
            return [messages]
        return [messages[index:index + size] for index in range(0, len(messages), size)]

    async def _process_session_batch(self, session_id: str, messages: list[dict]) -> dict:
        group_name = (messages[-1].get("group_name") if messages else "") or ""
        is_group = bool(messages[-1].get("is_group", True)) if messages else True
        identity = await self._identity_for(session_id, group_name, is_group)
        filtered, dropped_other = self._filter_messages(messages, identity)
        if not filtered:
            return {"messages": len(messages), "kept": 0, "tasks_created": 0, "tasks_updated": 0,
                    "dropped_other": dropped_other, "llm_batches": 0, "llm_fallbacks": 0,
                    "cap_hits": 0, "messages_without_task": 0, "degraded_reasons": [],
                    "covered_msg_ids": []}
        group_name = group_name or (filtered[-1].get("group_name") or "")
        last_ts = max(int(message.get("ts") or 0) for message in filtered)
        now = now_local()

        if self.settings.llm_enabled:
            stats = await self._llm_reconcile_batches(
                session_id, group_name, filtered, last_ts, now, identity)
            stats["dropped_other"] = dropped_other
            return stats

        stats = await self._fallback_event(session_id, group_name, filtered, last_ts, now, identity)
        stats["dropped_other"] = dropped_other
        return stats

    async def _llm_reconcile_batches(self, session_id: str, group_name: str,
                                     messages: list[dict], last_ts: int, now,
                                     identity: IdentityContext | None) -> dict:
        """把一轮消息分片多次归并（P1-1），并统计回退/上限命中（P1-2）。

        单次归并 prompt 明确「最多 8 个事项、总共最多 12 个任务」，洪峰下整批一次归并
        会静默饱和；按 ``LLM_BATCH_MESSAGES`` 分片后每片独立归并、逐片更新事项账本，
        片间仍可通过新写入的 open events 归并。某一片 LLM 失败时只回退该片到规则模式，
        并把 ``llm_fallbacks``/``last_llm_error``/``degraded_reasons`` 暴露给采集结果。
        """
        totals = {
            "messages": len(messages), "kept": len(messages),
            "tasks_created": 0, "tasks_updated": 0, "events": 0,
            "mode": "llm", "summary": "",
            "llm_batches": 0, "llm_fallbacks": 0, "cap_hits": 0,
            "messages_without_task": 0, "covered_msg_ids": [], "degraded_reasons": [],
            "supplemented": 0, "suppressed": 0,
        }
        covered: set[str] = set()
        summaries: list[str] = []
        degraded: list[str] = []
        for batch in self._llm_batches(messages):
            open_events = self._open_events_payload(session_id)
            try:
                result = await llm_reconcile(batch, open_events, self.settings, now,
                                             identity=identity)
            except LLMUnavailable as error:
                self._note_llm_fallback(error)
                degraded.append("llm_fallback")
                totals["llm_fallbacks"] += 1
                result = None
            if result is None:
                # 该片回退规则模式；其余片仍然继续尝试大模型
                stats = await self._fallback_event(session_id, group_name, batch, last_ts,
                                                   now, identity)
            else:
                self._note_llm_success()
                totals["llm_batches"] += 1
                if getattr(result, "cap_hit", False):
                    totals["cap_hits"] += 1
                merge_stats = await self._merge_into_manual_tasks(batch, result, now)
                stats = await self._apply_reconcile(session_id, group_name, batch, last_ts, result)
                stats.update(merge_stats)
            totals["tasks_created"] += int(stats.get("tasks_created") or 0)
            totals["tasks_updated"] += int(stats.get("tasks_updated") or 0)
            totals["events"] += int(stats.get("events") or 0)
            totals["supplemented"] += int(stats.get("supplemented") or 0)
            totals["suppressed"] += int(stats.get("suppressed") or 0)
            covered.update(str(mid) for mid in (stats.get("covered_msg_ids") or []) if mid)
            if stats.get("summary"):
                summaries.append(str(stats["summary"]))
            degraded.extend(stats.get("degraded_reasons") or [])
        totals["mode"] = "llm" if totals["llm_batches"] else "rule"
        totals["covered_msg_ids"] = sorted(covered)
        totals["messages_without_task"] = max(0, len(messages) - len(covered))
        totals["summary"] = "；".join(dict.fromkeys(summaries))[:400]
        if totals["cap_hits"]:
            degraded.append("extraction_cap")
            logger.warning("LLM 归并命中抽取上限 %s 批（消息 %s 条，未生成待办 %s 条）",
                           totals["cap_hits"], len(messages), totals["messages_without_task"])
        totals["degraded_reasons"] = list(dict.fromkeys(degraded))
        return totals

    @staticmethod
    def _match_reasons(messages: list[dict]) -> dict[str, str]:
        """原始消息 id → 判定依据（用于任务详情展示）。"""
        reasons: dict[str, str] = {}
        for message in messages:
            verdict = message.get("_verdict")
            if verdict is None or verdict.kind not in ("me", "all"):
                continue
            rawid = str(message.get("rawid") or "")
            if rawid:
                reasons[rawid] = verdict.reason
        return reasons

    async def _apply_reconcile(self, session_id: str, group_name: str, messages: list[dict],
                                last_ts: int, result: ReconcileResult) -> dict:
        created: list[dict] = []
        updated = 0
        events_touched = 0
        reasons = self._match_reasons(messages)
        for item in result.items:
            event = self.db.get_event(item.event_id) if item.event_id else None
            if event and event.get("status") == "open" and event.get("session_id") == session_id:
                self.db.update_event(event["id"], {
                    "title": item.event_title or event.get("title") or "",
                    "summary": item.event_summary or event.get("summary") or "",
                    "last_msg_ts": last_ts,
                })
            else:
                event = self.db.create_event(
                    session_id=session_id, group_name=group_name,
                    title=item.event_title, summary=item.event_summary, last_msg_ts=last_ts,
                )
            events_touched += 1

            for task in item.tasks:
                source_ids = [msg_id for msg_id in task.source_msg_ids if msg_id]
                match_reason = next((reasons[msg_id] for msg_id in source_ids if msg_id in reasons), "")
                if task.task_id:
                    existing = self.db.get_task(task.task_id)
                    if existing and existing.get("event_id") == event["id"]:
                        fields = {
                            "title": task.title or existing.get("title") or "",
                            "notes": task.notes or existing.get("notes") or "",
                            "priority": task.priority or existing.get("priority") or "normal",
                        }
                        if task.due_at:
                            fields["due_at"] = task.due_at
                        self.db.update_task(task.task_id, fields)
                        self.db.merge_task_sources(task.task_id, source_ids)
                        updated += 1
                        continue

                extracted = ExtractedTask(
                    title=task.title, notes=task.notes, due_at=task.due_at,
                    priority=task.priority, group_name=group_name,
                    source_msg_ids=source_ids,
                )
                row, is_new = self.db.create_task(
                    title=extracted.title, notes=extracted.notes, due_at=extracted.due_at,
                    priority=extracted.priority, group_name=group_name,
                    source_msg_ids=source_ids, dedupe_key=task_dedupe_key(extracted),
                    event_id=event["id"], match_reason=match_reason,
                )
                if is_new:
                    created.append(row)

        if created:
            logger.info("新增 %s 项待办，更新 %s 项（模式 llm，事项 %s）",
                        len(created), updated, events_touched)
            await self.push_new_tasks(created, result.summary)
        covered_ids = {
            str(msg_id) for item in result.items for task in item.tasks
            for msg_id in (task.source_msg_ids or []) if msg_id
        }
        return {
            "messages": len(messages), "kept": len(messages),
            "tasks_created": len(created), "tasks_updated": updated,
            "events": events_touched, "mode": "llm", "summary": result.summary,
            "covered_msg_ids": sorted(covered_ids),
        }

    # ---------- 手动待办：新建去重 / 自动补充 / 已忽略去重 ----------
    async def _merge_into_manual_tasks(self, messages: list[dict], result: ReconcileResult,
                                       now) -> dict:
        """模型总结出新待办后，与手动待办比对：

        - 未完成的手动待办：几乎相同 → 补充（split 时剩余部分另建）；
        - 最近 MANUAL_SUPPRESS_DAYS 天内被忽略的手动待办：同一件事 → 丢弃新待办（suppress）；
        - 都不匹配 → 照常新建。

        底线（与用户约定）：手动待办的标题、已有备注不改；截止时间默认只补空，
        但模型明确识别到改期/新的截止时间（supplement_due 非空）时覆盖已有时间；
        并追加来源消息便于追溯；已忽略的待办只用于抑制重复，本身不被改写。
        返回 {"supplemented": n, "suppressed": m}。
        """
        stats = {"supplemented": 0, "suppressed": 0}
        candidates: list[tuple[int, int, ReconcileTask]] = []
        for item_index, item in enumerate(result.items):
            for task_index, task in enumerate(item.tasks):
                if not task.task_id:  # 只判定「新建」的待办；更新已有任务不动
                    candidates.append((item_index, task_index, task))
        if not candidates:
            return stats
        manual_tasks = self.db.list_manual_pending(limit=20)
        suppress_days = max(0, int(getattr(self.settings, "manual_suppress_days", 0) or 0))
        ignored_tasks: list[dict] = []
        if suppress_days > 0:
            cutoff = to_iso(now - timedelta(days=suppress_days))
            ignored_tasks = self.db.list_manual_ignored(cutoff, limit=20)
        if not manual_tasks and not ignored_tasks:
            return stats
        decisions = await llm_match_manual(
            [task for _item, _task, task in candidates], manual_tasks, messages,
            self.settings, now, ignored_tasks=ignored_tasks,
        )
        if not decisions:
            return stats

        manual_by_id = {str(task.get("id")): task for task in manual_tasks}
        ignored_by_id = {str(task.get("id")): task for task in ignored_tasks}
        dropped: set[tuple[int, int]] = set()
        supplemented_ids: set[str] = set()
        suppressed = 0
        for decision in decisions:
            if not 0 <= decision.index < len(candidates):
                continue
            item_index, task_index, task = candidates[decision.index]
            if decision.relation == "suppress":
                ignored = ignored_by_id.get(decision.manual_task_id or "")
                if ignored is None:
                    continue
                dropped.add((item_index, task_index))
                suppressed += 1
                logger.info("已忽略去重：跳过新建「%s」（对应已忽略待办「%s」，窗口 %s 天）",
                            clip(task.title or "", 30),
                            clip(ignored.get("title") or "", 30), suppress_days)
                continue
            manual = manual_by_id.get(decision.manual_task_id or "")
            if decision.relation in ("same", "split") and manual is None:
                continue
            if decision.relation == "same":
                self._supplement_manual(manual, task, decision, include_candidate=True)
                dropped.add((item_index, task_index))
                supplemented_ids.add(manual["id"])
            elif decision.relation == "split":
                self._supplement_manual(manual, task, decision, include_candidate=False)
                remainder = decision.remainder
                if remainder is not None:
                    # 剩余部分另建一条新待办（来源消息保留，便于详情页追溯）
                    task.title = remainder.title
                    task.notes = remainder.notes
                    task.due_at = remainder.due_at
                    task.priority = remainder.priority
                supplemented_ids.add(manual["id"])

        if dropped:
            kept_items = []
            for item_index, item in enumerate(result.items):
                item.tasks = [task for task_index, task in enumerate(item.tasks)
                              if (item_index, task_index) not in dropped]
                # 新事项的任务全被并进手动待办 / 抑制掉时，不再建一条空事项
                if item.tasks or item.event_id:
                    kept_items.append(item)
            result.items = kept_items

        if supplemented_ids:
            logger.info(
                "手动待办自动补充 %s 条（本轮新建待办 %s 条参与判定，并入后跳过新建 %s 条）",
                len(supplemented_ids), len(candidates), len(dropped),
            )
        stats["supplemented"] = len(supplemented_ids)
        stats["suppressed"] = suppressed
        return stats

    def _supplement_manual(self, manual: dict, candidate: ReconcileTask, decision,
                           *, include_candidate: bool) -> None:
        """把细节补进手动待办；include_candidate=True 时用新待办的旧字段兜底。

        截止时间：模型明确给出 supplement_due（识别到改期/新时间）时允许覆盖手动已有时间；
        只有兜底的 candidate.due_at 时仍只补空，避免“只是再次提到”就改掉用户填的时间。
        """
        notes = decision.supplement_notes
        explicit_due = decision.supplement_due
        due_at = explicit_due
        if include_candidate:
            notes = notes or clip(candidate.notes or "", 400)
            if not due_at:
                due_at = candidate.due_at
        _task, changed = self.db.supplement_task(
            manual["id"],
            due_at=due_at,
            notes=notes,
            source_msg_ids=list(candidate.source_msg_ids or []),
            match_reason="自动补充（来自新消息归并）",
            overwrite_due=bool(explicit_due),
        )
        if changed:
            logger.info("手动待办已补充「%s」：%s",
                        clip(manual.get("title") or "", 30), ",".join(changed))

    async def _fallback_event(self, session_id: str, group_name: str, messages: list[dict],
                              last_ts: int, now,
                              identity: IdentityContext | None = None) -> dict:
        """规则模式抽取并落库（LLM 不可用 / 未配置时）。

        P2-2：规则模式没有 LLM 的归并能力，同一事项换个说法在多条消息/多轮里会重复建
        待办。这里在落库前用 ``titles_similar`` 与**该会话仍打开事项里的待办**比对：
        相似则把来源消息并入已有待办、只补空白字段；事项也优先复用标题相似的事项，
        不再每轮新建一个重复事项。
        """
        names = list(dict.fromkeys(
            list(identity.aliases if identity else []) + list(self.settings.me_names or [])
        ))
        result = rule_extract(messages, self.settings, now, me_names=names,
                              max_tasks=self._RULE_MAX_TASKS)
        cap_hits = 1 if getattr(result, "cap_hit", False) else 0
        if not result.tasks:
            return {"messages": len(messages), "kept": len(messages),
                    "tasks_created": 0, "tasks_updated": 0, "mode": "rule",
                    "cap_hits": cap_hits, "covered_msg_ids": [],
                    "messages_without_task": len(messages)}
        open_events = self._open_events_payload(session_id)
        existing: list[tuple[dict, dict]] = [
            (event, task) for event in open_events for task in (event.get("tasks") or [])
        ]
        reasons = self._match_reasons(messages)
        created: list[dict] = []
        covered: set[str] = set()
        event: dict | None = None
        for task in result.tasks:
            source_ids = [msg_id for msg_id in task.source_msg_ids if msg_id]
            covered.update(source_ids)
            match_reason = next((reasons[msg_id] for msg_id in source_ids if msg_id in reasons), "")

            duplicate = next(((ev, row) for ev, row in existing
                              if titles_same_topic(task.title, row.get("title") or "",
                                                   task.due_at, row.get("due_at"))), None)
            if duplicate is not None:
                dup_event, dup_task = duplicate
                fields = {}
                if not dup_task.get("notes"):
                    fields["notes"] = clip(task.notes or "", 400)
                if not dup_task.get("due_at") and task.due_at:
                    fields["due_at"] = task.due_at
                if fields:
                    self.db.update_task(dup_task["id"], fields)
                self.db.merge_task_sources(dup_task["id"], source_ids)
                logger.info("规则模式去重：新消息并入已有待办「%s」（不重复建）",
                            clip(dup_task.get("title") or "", 30))
                continue

            if event is None:
                event = next((ev for ev in open_events
                              if titles_same_topic(task.title, ev.get("title") or "")), None)
                if event is None:
                    title = task.title or "微信事项"
                    event = self.db.create_event(
                        session_id=session_id, group_name=group_name,
                        title=title, summary=result.summary, last_msg_ts=last_ts,
                    )
                    open_events.append({**event, "tasks": []})
            row, is_new = self.db.create_task(
                title=task.title, notes=task.notes, due_at=task.due_at,
                priority=task.priority, group_name=group_name,
                source_msg_ids=source_ids,
                dedupe_key=task_dedupe_key(task), event_id=event["id"],
                match_reason=match_reason,
            )
            if is_new:
                created.append(row)
                existing.append((event, row))
        if created:
            logger.info("新增 %s 项待办（规则模式）", len(created))
            await self.push_new_tasks(created, result.summary)
        return {"messages": len(messages), "kept": len(messages),
                "tasks_created": len(created), "tasks_updated": 0,
                "mode": "rule", "summary": result.summary,
                "cap_hits": cap_hits, "covered_msg_ids": sorted(covered),
                "messages_without_task": max(0, len(messages) - len(covered))}

    async def process_batch(self, messages: list[dict]) -> dict:
        """兼容入口：把消息按会话分组后分别走事件账本。"""
        groups: dict[str, list[dict]] = {}
        for message in messages:
            groups.setdefault(str(message.get("session_id") or ""), []).append(message)
        total_created = total_updated = total_kept = total_dropped = total_supplemented = 0
        total_suppressed = 0
        total_batches = total_fallbacks = total_cap = total_without = 0
        degraded_reasons: list[str] = []
        last_mode = "rule"
        last_summary = ""
        for session_id, session_messages in groups.items():
            if not session_id:
                continue
            stats = await self._process_session_batch(session_id, session_messages)
            total_created += stats.get("tasks_created", 0)
            total_updated += stats.get("tasks_updated", 0)
            total_kept += stats.get("kept", 0)
            total_dropped += stats.get("dropped_other", 0)
            total_supplemented += stats.get("supplemented", 0)
            total_suppressed += stats.get("suppressed", 0)
            total_batches += int(stats.get("llm_batches") or 0)
            total_fallbacks += int(stats.get("llm_fallbacks") or 0)
            total_cap += int(stats.get("cap_hits") or 0)
            total_without += int(stats.get("messages_without_task") or 0)
            degraded_reasons.extend(stats.get("degraded_reasons") or [])
            last_mode = stats.get("mode", last_mode)
            last_summary = stats.get("summary", last_summary)
        return {"messages": len(messages), "kept": total_kept,
                "tasks_created": total_created, "tasks_updated": total_updated,
                "supplemented": total_supplemented, "suppressed": total_suppressed,
                "dropped_other": total_dropped,
                "llm_batches": total_batches, "llm_fallbacks": total_fallbacks,
                "cap_hits": total_cap, "messages_without_task": total_without,
                "degraded_reasons": list(dict.fromkeys(degraded_reasons)),
                "mode": last_mode, "summary": last_summary}

    # ---------- 推送 ----------
    @property
    def app_url(self) -> str:
        return f"{self.settings.base_url}/?token={quote(self.settings.app_token)}"

    async def _notify(self, title: str, body: str, priority: str = "normal",
                      *, bypass_quiet: bool = False) -> list[dict]:
        if not bypass_quiet and in_quiet_hours(
                now_local(), self.settings.quiet_start, self.settings.quiet_end):
            self.db.log_push("quiet", "-", True, f"免打扰时段跳过：{title}")
            logger.info("免打扰时段，跳过推送：%s", title)
            return []
        if not self.pusher.configured:
            self.db.log_push("skipped", "-", True, "未配置推送通道")
            return []
        results = await self.pusher.send(title, body, self.app_url, priority)
        for result in results:
            self.db.log_push("push", result["channel"], result["ok"], result.get("detail", ""))
        return results

    async def push_new_tasks(self, tasks: list[dict], summary: str) -> None:
        """推送新建待办；采集轮内先收集，轮末合并成一条（同轮合并推送）。"""
        if not tasks:
            return
        collector = self._round_push
        if collector is not None:
            collector["tasks"].extend(tasks)
            if summary:
                collector["summaries"].append(str(summary))
            return
        await self._send_new_tasks_push(tasks, summary)

    async def _flush_round_push(self, collector: dict) -> None:
        """把采集轮内收集到的待办合并成一条推送（标题 + 数量 + 摘要 + 前几条明细）。"""
        tasks = collector.get("tasks") or []
        if not tasks:
            return
        # 摘要可能来自多个会话：去重后拼接，超长截断，避免推送正文爆掉
        summaries = list(dict.fromkeys(
            str(item).strip() for item in (collector.get("summaries") or []) if str(item).strip()))
        summary = "；".join(summaries)[:200]
        await self._send_new_tasks_push(tasks, summary, limit=6)

    async def _send_new_tasks_push(self, tasks: list[dict], summary: str,
                                  limit: int = 4) -> None:
        lines = []
        for task in tasks[:limit]:
            meta = task.get("group_name") or ""
            due = human_due(task.get("due_at"))
            extra = " · ".join(part for part in (meta, due) if part)
            lines.append(f"• {task['title']}" + (f"（{extra}）" if extra else ""))
        if len(tasks) > limit:
            lines.append(f"…共 {len(tasks)} 项")
        body = "\n".join(lines)
        if summary:
            body = f"{summary}\n{body}"
        title = f"微信待办 +{len(tasks)}"
        priority = "high" if any(task.get("priority") == "high" for task in tasks) else "normal"
        await self._notify(title, body, priority)

    # ---------- 定时 ----------
    async def _daily_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(30)
                now = now_local()
                today = now.strftime("%Y-%m-%d")
                clock = now.strftime("%H:%M")
                reactivated = self.db.reactivate_snoozed()
                if reactivated:
                    logger.info("%s 项稍后提醒的任务已恢复", reactivated)
                if (self.settings.digest_enabled and clock == self.settings.digest_time
                        and self._last_digest_date != today):
                    self._last_digest_date = today
                    await self.push_digest()
                if self._last_cleanup_date != today:
                    self._last_cleanup_date = today
                    self.db.cleanup(self.settings.retention_days)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning("定时任务异常：%s", error)

    async def push_digest(self) -> None:
        tasks = self.db.list_tasks("pending")
        now = now_local()
        due_today = []
        for task in tasks:
            if not task.get("due_at"):
                continue
            try:
                due = datetime.fromisoformat(task["due_at"])
            except ValueError:
                continue
            if due.date() <= now.date():
                due_today.append(task)
        if not due_today:
            return
        lines = [f"• {task['title']}（{human_due(task['due_at'], now)}）" for task in due_today[:6]]
        await self._notify(f"今日待办 {len(due_today)} 项", "\n".join(lines))

    async def test_push(self) -> dict:
        if not self.pusher.configured:
            return {"ok": False, "results": [], "reason": "未配置 Bark / ntfy"}
        results = await self.pusher.send("微信待办 · 测试", "推送通道正常，待办会推到这里。", self.app_url)
        return {"ok": any(result.get("ok") for result in results), "results": results}

    # ---------- 演示 ----------
    async def seed_demo(self) -> dict:
        messages = demo_messages()
        for message in messages:
            self.db.add_message(
                rawid=message["rawid"], session_id=message["session_id"],
                group_name=message["group_name"], sender=message["sender"],
                content=message["content"], ts=message["ts"], processed=1, payload=message,
            )
        stats = await self.process_batch(messages)
        stats["tasks_created"] = max(stats.get("tasks_created", 0), 0)
        return stats

    # ---------- 查询 ----------
    def health(self) -> dict:
        counts = self.db.counts()
        source = self.source
        # connected 由采集轮驱动：None=首轮采集尚未完成，True/False=最近一轮结果
        source_connected = False if source is None else self.connected
        collection_enabled = self._collect_enabled()
        pending_messages = self.db.count_unprocessed()
        pending_sessions = len(self.db.unprocessed_sessions())
        failed_messages = self.db.count_failed_messages()
        failed_sessions = len(self.db.failed_sessions())
        states = self.db.catchup_states()
        collection = {
            "enabled": collection_enabled,
            "mode": "watermark" if collection_enabled else "off",
            "seconds": self.settings.wcda_catchup_seconds,
            "lookback_minutes": self.settings.wcda_catchup_lookback_minutes,
            "backfill_minutes": self.settings.wcda_catchup_on_start_minutes,
            "max_hours": self.settings.wcda_catchup_max_hours,
            "page_size": self.settings.wcda_catchup_page_size,
            "round_budget": self.settings.wcda_catchup_round_budget,
            "empty_history_hours": self.settings.wcda_catchup_empty_history_hours,
            "overlap_seconds": self.settings.wcda_catchup_overlap_seconds,
            "deep_scan_minutes": self.settings.wcda_deep_scan_minutes,
            "deep_scan_lookback_minutes": self.settings.wcda_deep_scan_lookback_minutes,
            "max_attempts": self.settings.message_max_attempts,
            "llm_batch_messages": self.settings.llm_batch_messages,
            "watermarks": len(states),
            # 已建立 localId 游标的会话数（localId 增量是本轮采集的主依据）
            "cursor_sessions": sum(1 for item in states if int(item.get("last_local_id") or 0) > 0),
            "pending_messages": pending_messages,
            "pending_sessions": pending_sessions,
            # 失败队列：重试超限、等待人工处理的消息 / 会话
            "failed_messages": failed_messages,
            "failed_sessions": failed_sessions,
            "last_ok_at": self._last_collect_ok_at,
            "last": self._last_collection,
            # 降级/抽取饱和/分页读取放大（P1-1/P1-2/P1-4/P2-3/P2-4）
            **self._collection_metrics(),
        }
        return {
            "ok": True,
            "data_source": self.data_source,
            "source": {
                "type": self.data_source,
                "configured": bool(source and source.configured),
                "connected": source_connected,
                "last_event_at": self.last_event_at,
                "last_error": self.last_error,
                "last_collect_at": self._last_collect_at if self.data_source == "wcda" else None,
            },
            "wcda": {
                "configured": self.wcda.configured,
                "base": self.settings.wcda_base,
                "connected": source_connected if self.data_source == "wcda" else False,
                "last_error": self.last_error if self.data_source == "wcda" else None,
            },
            "llm": {"configured": self.settings.llm_enabled},
            "push": {"bark": self.pusher.has_bark, "ntfy": self.pusher.has_ntfy},
            "filters": {
                "group_whitelist": self.settings.group_whitelist,
                "include_private": self.settings.include_private,
            },
            "whitelist": {
                "mode": self.settings.whitelist_mode,
                "custom_count": len(self.settings.group_whitelist),
                "pinned_count": len(self.pinned_sessions),
                "pinned_ready": self.pinned_ready,
            },
            "events": {
                "open": self.db.count_open_events(),
                # 已入库未处理（采集兜底队列）：用来判断有没有丢单/积压
                "pending_sessions": pending_sessions,
                "pending_messages": pending_messages,
                # 失败队列（重试超限）
                "failed_sessions": failed_sessions,
                "failed_messages": failed_messages,
            },
            "identity": {
                "account": self.identity.account,
                "account_source": self.identity.account_source,
                "filter_enabled": bool(self.settings.identity_filter),
                "fuzzy_enabled": bool(self.settings.identity_fuzzy),
                "groups_cached": len(self.identity.snapshot()["groups"]),
                "judgments": {**self._judge_stats, "since": self._judge_since},
            },
            # 主字段：采集（旧字段 catchup 保留为兼容别名，deprecated）
            "collection": collection,
            "catchup": collection,
            "tasks": {"pending": counts["today"] + counts["week"] + counts["later"]},
            "version": self.settings.version,
        }

    # ---------- LLM 设置 ----------
    @staticmethod
    def _mask_key(key: str) -> str:
        if not key:
            return ""
        if len(key) <= 8:
            return "已保存"
        return f"{key[:3]}…{key[-4:]}"

    def llm_settings(self) -> dict:
        s = self.settings
        return {
            "configured": bool(s.llm_enabled and s.llm_api_key),
            "enabled": bool(s.llm_enabled),
            "base_url": s.llm_base_url,
            "model": s.llm_model,
            "timeout": s.llm_timeout,
            "key_saved": bool(s.llm_api_key),
            "key_hint": self._mask_key(s.llm_api_key),
        }

    def update_llm_settings(self, *, api_key: str | None = None,
                            base_url: str | None = None,
                            model: str | None = None,
                            enabled: bool | None = None) -> dict:
        """更新 LLM 配置：内存立即生效，同时写回 .env 持久化。"""
        s = self.settings
        values: dict[str, str] = {}

        if base_url is not None:
            base = base_url.strip().rstrip("/")
            if base and not base.startswith(("http://", "https://")):
                raise ValueError("Base URL 必须以 http:// 或 https:// 开头")
            if base:
                values["LLM_BASE_URL"] = base
        if model is not None:
            name = model.strip()
            if not name:
                raise ValueError("模型名不能为空")
            values["LLM_MODEL"] = name
        if api_key is not None:
            values["LLM_API_KEY"] = api_key.strip()

        if "LLM_BASE_URL" in values:
            s.llm_base_url = values["LLM_BASE_URL"]
        if "LLM_MODEL" in values:
            s.llm_model = values["LLM_MODEL"]
        if "LLM_API_KEY" in values:
            s.llm_api_key = values["LLM_API_KEY"]
        if enabled is not None:
            s.llm_enabled = bool(enabled)
        # 没有 Key 就不可能启用；刚填入 Key 时默认启用
        if not s.llm_api_key:
            s.llm_enabled = False
        elif "LLM_API_KEY" in values and enabled is None:
            s.llm_enabled = True
        values["LLM_ENABLED"] = "1" if s.llm_enabled else "0"

        written = write_env_values(s.env_file, values)
        result = self.llm_settings()
        result["persisted"] = written
        return result

    async def test_llm(self) -> dict:
        s = self.settings
        if not s.llm_api_key:
            return {"ok": False, "error": "还没有填写 API Key"}
        try:
            from openai import AsyncOpenAI

            client = AsyncOpenAI(api_key=s.llm_api_key, base_url=s.llm_base_url,
                                 timeout=min(20, max(5, s.llm_timeout)))
            response = await client.chat.completions.create(
                model=s.llm_model,
                messages=[{"role": "user", "content": "请只回复两个英文字母：OK"}],
                max_tokens=32,
                temperature=0,
            )
            reply = (response.choices[0].message.content or "").strip()
            return {"ok": True, "model": s.llm_model, "reply": reply[:80]}
        except Exception as error:
            return {"ok": False, "error": f"{type(error).__name__}: {error}"[:300]}

    # ---------- 推送设置 ----------
    @staticmethod
    def _mask_url(url: str) -> str:
        if not url:
            return ""
        try:
            from urllib.parse import urlparse

            host = urlparse(url).netloc or "已保存"
            return f"{host}/…{url[-4:]}"
        except Exception:
            return "已保存"

    def push_settings(self) -> dict:
        s = self.settings
        return {
            "configured": self.pusher.configured,
            "bark": self.pusher.has_bark,
            "bark_saved": bool(s.bark_url),
            "bark_hint": self._mask_url(s.bark_url),
            "ntfy": self.pusher.has_ntfy,
            "ntfy_server": s.ntfy_server,
            "ntfy_topic": s.ntfy_topic,
            "ntfy_token_saved": bool(s.ntfy_token),
        }

    def update_push_settings(self, *, bark_url: str | None = None,
                             ntfy_server: str | None = None,
                             ntfy_topic: str | None = None,
                             ntfy_token: str | None = None) -> dict:
        s = self.settings
        values: dict[str, str] = {}

        if bark_url is not None:
            url = bark_url.strip()
            if url and not url.startswith(("http://", "https://")):
                raise ValueError("Bark 地址必须以 http:// 或 https:// 开头")
            values["PUSH_BARK_URL"] = url.rstrip("/")
        if ntfy_server is not None:
            server = ntfy_server.strip().rstrip("/")
            if server and not server.startswith(("http://", "https://")):
                raise ValueError("ntfy 服务器必须以 http:// 或 https:// 开头")
            values["NTFY_SERVER"] = server or "https://ntfy.sh"
        if ntfy_topic is not None:
            topic = ntfy_topic.strip()
            if " " in topic:
                raise ValueError("ntfy 主题不能包含空格")
            values["NTFY_TOPIC"] = topic
        if ntfy_token is not None:
            values["NTFY_TOKEN"] = ntfy_token.strip()

        for key, value in values.items():
            if key == "PUSH_BARK_URL":
                s.bark_url = value
            elif key == "NTFY_SERVER":
                s.ntfy_server = value
            elif key == "NTFY_TOPIC":
                s.ntfy_topic = value
            elif key == "NTFY_TOKEN":
                s.ntfy_token = value
        self.pusher = Pusher(s)
        written = write_env_values(s.env_file, values)
        result = self.push_settings()
        result["persisted"] = written
        return result

    # ---------- 过滤 / 白名单 ----------
    def filter_settings(self) -> dict:
        s = self.settings
        return {
            "include_private": s.include_private,
            "whitelist_mode": s.whitelist_mode,
            "group_whitelist": s.group_whitelist,
            "pinned_count": len(self.pinned_sessions),
            "pinned_ready": self.pinned_ready,
            "data_source": self.data_source,
        }

    def update_filter_settings(self, *, include_private: bool | None = None,
                               whitelist_mode: str | None = None,
                               group_whitelist: list[str] | None = None) -> dict:
        s = self.settings
        values: dict[str, str] = {}

        if include_private is not None:
            s.include_private = bool(include_private)
            values["INCLUDE_PRIVATE"] = "1" if s.include_private else "0"
        if whitelist_mode is not None:
            mode = whitelist_mode.strip().lower()
            if mode not in ("all", "pinned", "custom", "pinned_custom"):
                raise ValueError("whitelist_mode 只能是 all / pinned / custom / pinned_custom")
            s.whitelist_mode = mode
            values["WHITELIST_MODE"] = mode
        if group_whitelist is not None:
            items = [str(item).strip() for item in group_whitelist if str(item).strip()]
            s.group_whitelist = items
            values["GROUP_WHITELIST"] = ",".join(items)

        written = write_env_values(s.env_file, values)
        if self._needs_pinned():
            try:
                asyncio.get_running_loop().create_task(self.refresh_pinned())
            except RuntimeError:
                pass
        result = self.filter_settings()
        result["persisted"] = written
        return result

    # ---------- 身份与判定 ----------
    def identity_settings(self) -> dict:
        data = self.identity.snapshot()
        data["judgments"] = {**self._judge_stats, "since": self._judge_since}
        data["last_drops"] = list(self._recent_drops)
        return data

    async def update_identity_settings(self, *, me_names: list[str] | None = None,
                                       me_wxid: str | None = None,
                                       identity_filter: bool | None = None,
                                       identity_fuzzy: bool | None = None) -> dict:
        """更新身份识别配置：内存立即生效，同时写回 .env 持久化。"""
        s = self.settings
        values: dict[str, str] = {}
        if me_names is not None:
            items = [str(item).strip() for item in me_names if str(item).strip()]
            s.me_names = items
            values["ME_NAMES"] = ",".join(items)
        if me_wxid is not None:
            wxid = str(me_wxid).strip()
            changed = wxid != s.me_wxid
            s.me_wxid = wxid
            values["ME_WXID"] = wxid
            if changed:
                # 账号变了：重新识别（顺手清空旧缓存）
                await self.identity.refresh()
        if identity_filter is not None:
            s.identity_filter = bool(identity_filter)
            values["IDENTITY_FILTER"] = "1" if s.identity_filter else "0"
        if identity_fuzzy is not None:
            s.identity_fuzzy = bool(identity_fuzzy)
            values["IDENTITY_FUZZY"] = "1" if s.identity_fuzzy else "0"
        # 手动别名变化后，已缓存群上下文里的 aliases 也要跟着变（不重新拉群成员）
        self.identity.refresh_manual()
        written = write_env_values(s.env_file, values)
        result = self.identity_settings()
        result["persisted"] = written
        return result

    async def refresh_identity(self, session_ids: list[str] | None = None) -> dict:
        """刷新身份：不传 session_ids 时，重拉“正在监控的群”（置顶/已缓存群）。"""
        if session_ids:
            targets = [str(item).strip() for item in session_ids if str(item).strip()]
        else:
            targets = [group["session_id"] for group in self.identity.snapshot()["groups"]
                       if group.get("session_id")]
            targets.extend(sorted(self.pinned_sessions))
            if not targets and self.data_source == "wcda":
                try:
                    await self.refresh_pinned()
                except Exception as error:  # noqa: BLE001 - 刷新失败只影响展示
                    logger.warning("刷新置顶会话失败：%s", error)
                targets.extend(sorted(self.pinned_sessions))
            targets = list(dict.fromkeys(targets))[:20]
        # 让设置页的群表格显示群名（而不是 123@chatroom）
        for session_id in targets:
            name = self.pinned_names.get(session_id)
            if name:
                self.identity.remember_group(session_id, name)
        result = await self.identity.refresh(targets or None)
        result["ok"] = True
        return result

    async def check_identity(self, text: str, session_id: str = "") -> dict:
        """判定测试：有群就用该群缓存/按需拉取群成员；没有群就用全局别名。"""
        session = str(session_id or "").strip()
        if session:
            group_name = self.identity.group_name_for(session)
            context = await self.identity.for_session(session, group_name)
        else:
            await self.identity.ensure_global()
            context = self.identity.plain_context()
        verdict = classify_message(text, identity=context, fuzzy=self.settings.identity_fuzzy)
        return {
            "ok": True,
            "text": str(text or ""),
            "verdict": verdict.to_dict(),
            "identity": self.identity.describe_context(context),
        }

    def identity_judgments(self) -> dict:
        return {
            "judgments": {**self._judge_stats, "since": self._judge_since},
            "last_drops": list(self._recent_drops),
        }

    def task_detail(self, task_id: str) -> dict | None:
        task = self.db.get_task(task_id)
        if task is None:
            return None
        sources = self.db.messages_by_ids(task.get("source_msg_ids") or [])
        detail = {"task": task, "sources": sources}
        event = self.db.get_event(task.get("event_id") or "")
        if event:
            detail["event"] = event
        return detail
