"""核心编排：监听 → 批次 → 过滤 → 提取 → 入库 → 推送 → 定时摘要。"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from datetime import datetime
from urllib.parse import quote

from .config import Settings
from .db import Database
from .envfile import write_env_values
from .extract import (ExtractedTask, ReconcileResult, ReconcileTask, extract_tasks,
                      llm_match_manual, llm_reconcile, rule_extract, task_dedupe_key)
from .filters import EVENT_HINTS, has_action_hint, is_worth_summarizing, should_send_to_llm
from .identity import IdentityContext, IdentityResolver
from .mentions import classify_message
from .push import Pusher
from .utils import clip, human_due, in_quiet_hours, now_iso, now_local
from .wcda import WcdaClient
from .weflow import WeFlowClient

logger = logging.getLogger(__name__)


class AppService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.db = Database(settings.db_path)
        self.pusher = Pusher(settings)
        self.weflow = WeFlowClient(settings)
        self.wcda = WcdaClient(settings)
        self.data_source = settings.data_source
        if self.data_source == "wcda":
            self.source = self.wcda
        elif self.data_source == "weflow":
            self.source = self.weflow
        else:  # demo / 其它：不连接真实数据源
            self.source = None
        self.session_buffers: dict[str, list[dict]] = {}
        self.session_first_at: dict[str, float] = {}
        self.session_last_at: dict[str, float] = {}
        self.session_group: dict[str, str] = {}
        # @/点名 身份判定（只过滤“明确指向他人”的消息，详见 app/mentions.py）
        self.identity = IdentityResolver(settings, self.wcda if self.data_source == "wcda" else None)
        self._judge_stats = {"kept_me": 0, "kept_all": 0, "kept_none": 0, "dropped_other": 0}
        self._judge_since = now_iso()
        self._recent_drops: deque[dict] = deque(maxlen=20)
        self.connected = False
        self.last_event_at: str | None = None
        self.last_error: str | None = None
        self._sessions_cache: list[dict] = []
        self._last_catchup: dict | None = None
        self._force_allow_sessions: set[str] = set()
        # wcda 单会话实时流（workaround：WCDA 全量 SSE 存在不推送的 bug）
        self._stream_tasks: dict[str, asyncio.Task] = {}
        self._stream_group_names: dict[str, str] = {}
        self._last_digest_date: str | None = None
        self._last_cleanup_date: str | None = None
        self._workers: list[asyncio.Task] = []
        self.started_at = now_iso()
        self.started_ts = time.time()
        # 置顶白名单（来自 WeChatDataAnalysis 的会话列表）
        self.pinned_sessions: set[str] = set()
        self.pinned_names: dict[str, str] = {}
        self.pinned_ready = False
        self._pinned_warned = False

    # ---------- lifecycle ----------
    async def start(self) -> None:
        self._workers = [
            asyncio.create_task(self._event_loop(), name="event-worker"),
            asyncio.create_task(self._daily_loop(), name="daily"),
        ]
        if self.data_source == "wcda" and self.wcda.configured:
            # WCDA 的“全量”实时流实测经常不推送，改为按会话逐个订阅（单会话 SSE 是正常的）
            self._workers.append(asyncio.create_task(self._wcda_stream_manager(), name="wcda-streams"))
        else:
            self._workers.append(asyncio.create_task(self._sse_loop(), name=f"{self.data_source}-sse"))
        if self.data_source == "weflow" and self.settings.weflow_configured and self.settings.catchup_minutes > 0 \
                and not self.settings.only_new_messages:
            self._workers.append(asyncio.create_task(self._catchup(), name="catchup"))
        # wcda 补采兜底（单会话实时流异常/漏消息时兜底；也用于新监控群的历史回补）
        if (self.data_source == "wcda" and self.wcda.configured
                and (self.settings.wcda_catchup_seconds > 0
                     or self.settings.wcda_catchup_on_start_minutes > 0)):
            self._workers.append(asyncio.create_task(self._catchup_loop(), name="wcda-catchup"))
        if self._needs_pinned() or self.data_source == "wcda":
            self._workers.append(asyncio.create_task(self._pinned_loop(), name="pinned-refresh"))
        logger.info("服务已启动：%s（数据源 %s，仅新消息=%s，白名单=%s）",
                    self.settings.base_url, self.data_source,
                    self.settings.only_new_messages, self.settings.whitelist_mode)

    async def stop(self) -> None:
        for worker in self._workers:
            worker.cancel()
        for task in list(self._stream_tasks.values()):
            task.cancel()
        await asyncio.gather(*self._workers, *self._stream_tasks.values(), return_exceptions=True)
        self._stream_tasks.clear()
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
                            await self.catchup(minutes=minutes, session_ids=new_sessions)
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
        """刷新置顶会话；返回这次新增置顶的会话（用于自动补采）。"""
        sessions = await self.wcda.fetch_sessions()
        if not sessions:
            return []
        added = self._apply_pinned_sessions(sessions)
        logger.info("已同步置顶会话 %s 个", len(self.pinned_sessions))
        return added

    def _handle_stream_message(self, message: dict) -> None:
        """实时流收到一条消息：去重入库 + 进缓冲，顺手记录连接状态。"""
        if message.get("event") == "stream.ready":
            self.connected = True
            self.last_error = None
            self.last_event_at = now_iso()
            return
        if message.get("event") == "message.revoke" or not message.get("rawid"):
            return
        # 只处理服务启动之后“新收到”的消息，避免回溯历史
        if not self._is_new_message(message):
            return
        self.connected = True
        self.last_error = None
        self.last_event_at = now_iso()
        # 不在白名单里的会话直接丢弃，不占缓冲、不写库
        if not self._message_allowed(message):
            return
        inserted = self.db.add_message(
            rawid=message["rawid"], session_id=message["session_id"],
            group_name=message["group_name"], sender=message["sender"],
            content=message["content"], ts=message["ts"],
        )
        if inserted:
            self._buffer_message(message)

    async def _sse_loop(self) -> None:
        backoff = 5
        while True:
            if self.source is None or not self.source.configured:
                await asyncio.sleep(30)
                continue
            try:
                async for message in self.source.stream_messages():
                    self._handle_stream_message(message)
                self.connected = False
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.connected = False
                self.last_error = f"{type(error).__name__}: {error}"[:300]
                logger.warning("%s 实时流断开：%s（%s 秒后重连）", self.data_source, error, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            backoff = 5
            await asyncio.sleep(3)

    # ---------- wcda 单会话实时流（绕开 WCDA 全量 SSE 的 bug） ----------
    async def _wcda_stream_manager(self) -> None:
        """按“当前监控会话”维护一组单会话 SSE 连接：置顶变化后自动增删连接。"""
        await asyncio.sleep(3)
        interval = min(60, max(10, int(self.settings.pinned_refresh_seconds)))
        while True:
            try:
                targets = await self._monitored_sessions()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001
                logger.warning("实时流订阅：刷新监控会话失败：%s", error)
                targets = []
            desired = {
                str(item.get("session_id")): str(item.get("group_name") or "")
                for item in targets if item.get("session_id")
            }
            # 取消已不在监控范围的连接
            for session_id in list(self._stream_tasks):
                if session_id not in desired:
                    task = self._stream_tasks.pop(session_id, None)
                    self._stream_group_names.pop(session_id, None)
                    if task is not None:
                        task.cancel()
            # 给新会话建立连接
            for session_id, group_name in desired.items():
                task = self._stream_tasks.get(session_id)
                if task is not None and not task.done():
                    continue
                self._stream_group_names[session_id] = group_name
                self._stream_tasks[session_id] = asyncio.create_task(
                    self._wcda_session_stream(session_id, group_name),
                    name=f"wcda-stream-{session_id[:16]}",
                )
            await asyncio.sleep(interval)

    async def _wcda_session_stream(self, session_id: str, group_name: str) -> None:
        backoff = 3
        label = group_name or session_id
        while True:
            try:
                async for message in self.wcda.stream_messages(username=session_id):
                    self._handle_stream_message(message)
                backoff = 3
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001
                self.connected = False
                self.last_error = f"{type(error).__name__}: {error}"[:300]
                logger.warning("单会话实时流断开（%s）：%s（%s 秒后重连）", label, error, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            await asyncio.sleep(2)

    async def _catchup(self) -> None:
        if self.settings.only_new_messages:
            return
        try:
            await asyncio.sleep(3)
            messages = await self.weflow.fetch_recent(self.settings.catchup_minutes)
            if not messages:
                return
            inserted = []
            for message in messages:
                if self.db.add_message(
                    rawid=message["rawid"], session_id=message["session_id"],
                    group_name=message["group_name"], sender=message["sender"],
                    content=message["content"], ts=message["ts"],
                ):
                    inserted.append(message)
            if inserted:
                logger.info("补采到 %s 条消息，立即归纳", len(inserted))
                await self.process_batch(inserted)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning("补采失败：%s", error)

    # ---------- wcda 补采兜底 ----------
    async def _monitored_sessions(self) -> list[dict]:
        """当前监控范围内的会话（置顶 + 自定义名单），顺带同步置顶变化。"""
        sessions = await self.wcda.fetch_sessions(limit=500)
        if not sessions:
            return []
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
        return targets

    # ---------- wcda 补采兜底 ----------
    def _catchup_window_seconds(self, minutes: int | None, mode: str) -> int:
        """本轮补采的回看上限（秒）。

        - ``window``：固定回看 ``minutes``（缺省 WCDA_CATCHUP_LOOKBACK_MINUTES），与旧行为一致；
        - ``watermark``：按会话水位补，上限 ``WCDA_CATCHUP_MAX_HOURS``（可传 minutes 覆盖）。
        """
        settings = self.settings
        if mode == "watermark":
            if minutes is None:
                return max(1, int(settings.wcda_catchup_max_hours)) * 3600
            return max(60, min(int(minutes) * 60, 7 * 24 * 3600))
        window_minutes = int(minutes if minutes is not None else settings.wcda_catchup_lookback_minutes)
        return max(1, min(window_minutes, 7 * 24 * 60)) * 60

    def _catchup_since_ts(self, session_id: str, *, mode: str,
                          window_seconds: int, now_ts: int) -> int:
        """本轮该会话的起始时间（unix 秒）。

        window 模式固定回看；watermark 模式从该会话上次补采水位继续：
        - 水位缺失时用「本进程启动前已入库」的最新消息时间（首次接管），不用启动后实时流
          刚写入的新消息，否则会把离线积压误判成已覆盖；
        - 启动前也没有该会话任何历史时，自动回看 ``WCDA_CATCHUP_EMPTY_HISTORY_HOURS``（默认 1 天）；
        - 任何时候都至少回看 lookback，兜住实时流漏掉的消息；
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
    def _catchup_row_ts(row: dict) -> int:
        """容错读取 WCDA 消息时间戳（非数字按 0 处理，不让整轮补采失败）。"""
        try:
            return int(row.get("createTime") or 0)
        except (TypeError, ValueError):
            return 0

    async def _fetch_catchup_rows(self, session_id: str, since_ts: int) -> dict:
        """读取该会话 since_ts 之后的消息（分页从最新往前回溯）。

        返回 ``{"ok", "rows", "complete", "pages", "error"}``：
        - ``ok=False`` 表示读取失败或积压超过单轮上限，调用方应跳过该会话、保留水位；
        - ``rows`` 已按时间升序排列。
        """
        settings = self.settings
        fetcher = getattr(self.wcda, "fetch_messages_page", None)
        if not callable(fetcher):
            # 兼容旧客户端 / 测试桩：退化为单次读取最近消息
            reader = getattr(self.wcda, "fetch_recent_messages", None)
            batch = await reader(session_id, limit=settings.wcda_catchup_limit) if callable(reader) else []
            rows = [row for row in batch if self._catchup_row_ts(row) >= since_ts]
            rows.sort(key=self._catchup_row_ts)
            return {"ok": True, "rows": rows, "complete": True, "pages": 1}

        page_size = max(50, min(int(settings.wcda_catchup_page_size), 1000))
        max_rows = max(1000, int(settings.wcda_catchup_max_messages))
        rows: list[dict] = []
        seen: set[str] = set()
        offset = 0
        pages = 0
        while True:
            page = await fetcher(session_id, limit=page_size, offset=offset, order="desc")
            pages += 1
            if not page.get("ok"):
                return {"ok": False, "rows": rows, "complete": False, "pages": pages,
                        "error": page.get("error") or "fetch_failed"}
            batch = [row for row in (page.get("messages") or []) if isinstance(row, dict)]
            if not batch:
                break
            for row in batch:
                # 分页期间若有新消息插入，offset 可能使相邻页重复；按 rawid 去重
                key = str(row.get("id") or row.get("localId") or "")
                if key:
                    if key in seen:
                        continue
                    seen.add(key)
                if self._catchup_row_ts(row) >= since_ts:
                    rows.append(row)
            offset += len(batch)
            oldest = min(self._catchup_row_ts(row) for row in batch)
            if oldest < since_ts:
                break
            if len(rows) >= max_rows:
                return {"ok": False, "rows": rows, "complete": False, "pages": pages,
                        "error": f"backlog_over_limit:{len(rows)}"}
            if pages >= 400:
                return {"ok": False, "rows": rows, "complete": False, "pages": pages,
                        "error": "too_many_pages"}
            # WCDA 正常会返回 hasMore；短页且明确没有更多时才认为历史到底
            if len(batch) < page_size and not page.get("has_more"):
                break
        rows.sort(key=self._catchup_row_ts)
        return {"ok": True, "rows": rows, "complete": True, "pages": pages}

    def _catchup_chunks(self, messages: list[dict]) -> list[list[dict]]:
        """按条数/字符数切批（时间升序）；批大小补采可单独配置，字符上限与实时账本一致。"""
        settings = self.settings
        max_messages = max(5, int(settings.wcda_catchup_batch_messages))
        max_chars = max(500, int(settings.event_max_chars))
        chunks: list[list[dict]] = []
        current: list[dict] = []
        chars = 0
        for message in messages:
            size = len(str(message.get("content") or ""))
            if current and (len(current) >= max_messages or chars + size > max_chars):
                chunks.append(current)
                current = []
                chars = 0
            current.append(message)
            chars += size
        if current:
            chunks.append(current)
        return chunks

    async def catchup(self, minutes: int | None = None,
                      session_ids: list[str] | None = None,
                      mode: str = "window") -> dict:
        """按会话补采最近消息（仅由后台自动轮 / 新置顶回补调用）。

        - 不传 ``session_ids``：只补当前监控范围（置顶 + 白名单）；
        - 传 ``session_ids``：内部回补指定会话（新置顶群）；
        - ``mode="window"``：固定回看 ``minutes``（新置顶回补 / 启动额外回补）；
        - ``mode="watermark"``：从每个会话上次补采水位继续，长时间离线后补齐全部未采集消息；
          单轮最长回看 ``WCDA_CATCHUP_MAX_HOURS``，每轮最多处理
          ``WCDA_CATCHUP_ROUND_BUDGET`` 条，未处理完的留在下一轮（水位按批推进）。
        """
        if self.data_source != "wcda" or not self.wcda.configured:
            return {"ok": False, "reason": "当前数据源不是 wcda", "inserted": 0, "sessions": 0,
                    "minutes": 0, "tasks_created": 0, "tasks_updated": 0, "dropped_other": 0}
        mode = "watermark" if str(mode or "").lower() in ("watermark", "all", "auto") else "window"
        # 只有水位模式才推进 catchup_state；window 模式只扫固定窗口，写水位会让
        # 后续 watermark 轮误以为空档已覆盖（P1：混合调用永久漏采）。
        advance = mode == "watermark"
        now_ts = int(time.time())
        window_seconds = self._catchup_window_seconds(minutes, mode)
        explicit = [str(item).strip() for item in (session_ids or []) if str(item).strip()]
        if explicit:
            if not self._sessions_cache:
                sessions = await self.wcda.fetch_sessions(limit=500)
                self._sessions_cache = sessions
            known = {str(s.get("username")): str(s.get("name") or "") for s in self._sessions_cache}
            targets = [{"session_id": sid, "group_name": known.get(sid, "")} for sid in explicit]
        else:
            targets = await self._monitored_sessions()

        budget = max(0, int(self.settings.wcda_catchup_round_budget))
        used_budget = 0
        budget_exhausted = False
        inserted_total = sessions_touched = created_total = updated_total = dropped_total = 0
        scanned_total = pages_total = 0
        skipped: list[str] = []
        for target in targets[:60]:
            if budget and used_budget >= budget:
                budget_exhausted = True
                break
            session_id = target["session_id"]
            since_ts = self._catchup_since_ts(session_id, mode=mode,
                                              window_seconds=window_seconds, now_ts=now_ts)
            fetched = await self._fetch_catchup_rows(session_id, since_ts)
            pages_total += int(fetched.get("pages") or 0)
            if not fetched.get("ok"):
                skipped.append(session_id)
                logger.warning("补采 %s 读取失败/积压过大，留到下一轮：%s",
                               target["group_name"] or session_id, fetched.get("error"))
                continue
            rows = fetched.get("rows") or []
            scanned_total += len(rows)
            if not rows:
                # 本轮扫到 now，前移水位，避免下一轮重复回溯（仅水位模式）
                if advance:
                    self.db.set_catchup_state(session_id, max(now_ts, since_ts))
                continue
            converted: list[dict] = []
            for row in rows:
                try:
                    item = WcdaClient.convert_api_message(
                        session_id, target["group_name"], row, self.wcda.account)
                except (TypeError, ValueError):  # 单条脏数据不让整轮失败
                    continue
                if item:
                    converted.append(item)
            if not converted:
                if advance:
                    self.db.set_catchup_state(session_id, max(now_ts, since_ts))
                continue
            existing = set() if explicit else self.db.existing_message_ids(
                list({str(item.get("rawid") or "") for item in converted}))
            processable = [
                item for item in converted
                if explicit or str(item.get("rawid") or "") not in existing
            ]
            if not processable:
                if advance:
                    self.db.set_catchup_state(session_id, max(now_ts, since_ts))
                continue

            inserted = 0
            touched = False
            processed_in_session = 0
            session_created = 0
            for chunk in self._catchup_chunks(processable):
                if budget and used_budget >= budget:
                    budget_exhausted = True
                    break
                if explicit:
                    self._force_allow_sessions.add(session_id)
                try:
                    stats = await self.process_batch(chunk)
                except asyncio.CancelledError:
                    raise
                except Exception as error:  # noqa: BLE001 - 单批失败不拖垮其余会话
                    logger.warning("补采归纳失败（%s），留到下一轮：%s",
                                   target["group_name"] or session_id, error)
                    break
                finally:
                    if explicit:
                        self._force_allow_sessions.discard(session_id)
                touched = True
                session_created += stats.get("tasks_created", 0)
                created_total += stats.get("tasks_created", 0)
                updated_total += stats.get("tasks_updated", 0)
                dropped_total += stats.get("dropped_other", 0)
                # 先归纳、再落库：归纳失败时消息不会入库，下一轮还能重新处理
                for item in chunk:
                    if self.db.add_message(
                        rawid=str(item.get("rawid") or ""), session_id=session_id,
                        group_name=str(item.get("group_name") or target["group_name"]),
                        sender=str(item.get("sender") or ""),
                        content=str(item.get("content") or ""),
                        ts=int(item.get("ts") or 0),
                    ):
                        inserted += 1
                processed_in_session += len(chunk)
                used_budget += len(chunk)
                newest_ts = max(int(item.get("ts") or 0) for item in chunk)
                if advance:
                    self.db.set_catchup_state(session_id, newest_ts)
            if touched:
                sessions_touched += 1
                inserted_total += inserted
                logger.info("补采 %s：%s 条消息，新增 %s 项待办",
                            target["group_name"] or session_id, processed_in_session,
                            session_created)
                if advance and processed_in_session >= len(processable) and not budget_exhausted:
                    self.db.set_catchup_state(session_id, now_ts)
            if budget_exhausted:
                break

        result = {
            "ok": True,
            "mode": mode,
            "minutes": int(round(window_seconds / 60)),
            "sessions": sessions_touched,
            "inserted": inserted_total,
            "scanned": scanned_total,
            "pages": pages_total,
            "tasks_created": created_total,
            "tasks_updated": updated_total,
            "dropped_other": dropped_total,
            "budget_exhausted": budget_exhausted,
            "skipped": skipped[:20],
            "at": now_iso(),
        }
        self._last_catchup = result
        if inserted_total:
            logger.info("补采完成（%s）：%s 个会话、%s 条消息、新增 %s 项待办",
                        mode, sessions_touched, inserted_total, created_total)
        return result

    async def _catchup_loop(self) -> None:
        settings = self.settings
        await asyncio.sleep(8)  # 等实时流先建立
        try:
            # 先按会话水位补停机/断网期间的空档；配置了启动回补窗口时再额外按窗口补一次
            await self.catchup(mode="watermark")
            if settings.wcda_catchup_on_start_minutes > 0:
                await self.catchup(minutes=settings.wcda_catchup_on_start_minutes, mode="window")
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - 补采失败不影响实时流
            logger.warning("启动补采失败：%s", error)
        interval = settings.wcda_catchup_seconds
        if interval <= 0:
            return
        while True:
            await asyncio.sleep(interval)
            try:
                await self.catchup(mode="watermark")
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001
                logger.warning("补采失败：%s", error)

    def _catchup_enabled(self) -> bool:
        return bool(self.data_source == "wcda" and self.wcda.configured
                    and (self.settings.wcda_catchup_seconds > 0
                         or self.settings.wcda_catchup_on_start_minutes > 0))

    def catchup_state(self) -> dict:
        """补采水位/参数快照（设置页与排障用；states 最多返回最近 200 个会话）。"""
        settings = self.settings
        states = self.db.catchup_states()
        return {
            "mode": "watermark" if self._catchup_enabled() else "off",
            "max_hours": settings.wcda_catchup_max_hours,
            "page_size": settings.wcda_catchup_page_size,
            "round_budget": settings.wcda_catchup_round_budget,
            "empty_history_hours": settings.wcda_catchup_empty_history_hours,
            "watermark_sessions": len(states),
            "states": states[-200:],
            "last": self._last_catchup,
        }

    # ---------- 事件账本 ----------
    def _buffer_message(self, message: dict) -> None:
        session_id = str(message.get("session_id") or "")
        if not session_id:
            return
        now = time.monotonic()
        self.session_buffers.setdefault(session_id, []).append(message)
        self.session_first_at.setdefault(session_id, now)
        self.session_last_at[session_id] = now
        self.session_group[session_id] = message.get("group_name", "")

    async def _event_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(1)
                now = time.monotonic()
                ready: list[str] = []
                for session_id, messages in list(self.session_buffers.items()):
                    if not messages:
                        continue
                    idle = now - self.session_last_at.get(session_id, now)
                    age = now - self.session_first_at.get(session_id, now)
                    chars = sum(len(str(m.get("content") or "")) for m in messages)
                    if (idle >= self.settings.event_idle_seconds
                            or age >= self.settings.event_max_wait_seconds
                            or len(messages) >= self.settings.event_max_messages
                            or chars >= self.settings.event_max_chars):
                        ready.append(session_id)
                for session_id in ready:
                    messages = self.session_buffers.pop(session_id, [])
                    self.session_first_at.pop(session_id, None)
                    self.session_last_at.pop(session_id, None)
                    if messages:
                        await self._process_session_batch(session_id, messages)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.exception("事件批次处理失败：%s", error)
                await asyncio.sleep(2)

    def _is_new_message(self, message: dict) -> bool:
        if not self.settings.only_new_messages:
            return True
        try:
            ts = int(message.get("ts") or 0)
        except (TypeError, ValueError):
            ts = 0
        return not ts or ts >= self.started_ts - 120

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

    async def _process_session_batch(self, session_id: str, messages: list[dict]) -> dict:
        group_name = self.session_group.get(session_id) or (
            messages[-1].get("group_name") if messages else "") or ""
        is_group = bool(messages[-1].get("is_group", True)) if messages else True
        identity = await self._identity_for(session_id, group_name, is_group)
        filtered, dropped_other = self._filter_messages(messages, identity)
        if not filtered:
            return {"messages": len(messages), "kept": 0, "tasks_created": 0, "tasks_updated": 0,
                    "dropped_other": dropped_other}
        group_name = group_name or (filtered[-1].get("group_name") or "")
        last_ts = max(int(message.get("ts") or 0) for message in filtered)
        now = now_local()

        if self.settings.llm_enabled:
            open_events = self._open_events_payload(session_id)
            result = await llm_reconcile(filtered, open_events, self.settings, now, identity=identity)
            if result is not None:
                supplemented = await self._merge_into_manual_tasks(filtered, result, now)
                stats = await self._apply_reconcile(session_id, group_name, filtered, last_ts, result)
                stats["supplemented"] = supplemented
                stats["dropped_other"] = dropped_other
                return stats

        stats = await self._fallback_event(session_id, group_name, filtered, last_ts, now, identity)
        stats["dropped_other"] = dropped_other
        return stats

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
        return {
            "messages": len(messages), "kept": len(messages),
            "tasks_created": len(created), "tasks_updated": updated,
            "events": events_touched, "mode": "llm", "summary": result.summary,
        }

    # ---------- 手动待办：新建去重 / 自动补充 ----------
    async def _merge_into_manual_tasks(self, messages: list[dict], result: ReconcileResult,
                                       now) -> int:
        """模型总结出新待办后，与手动待办比对：几乎相同 → 补充；部分相同 → 补充 + 拆分。

        底线（与用户约定）：手动待办的标题、已有截止时间、已有备注一律不改；
        只补它目前为空的字段，并追加来源消息便于追溯。
        返回本次补充到的手动待办条数。
        """
        candidates: list[tuple[int, int, ReconcileTask]] = []
        for item_index, item in enumerate(result.items):
            for task_index, task in enumerate(item.tasks):
                if not task.task_id:  # 只判定「新建」的待办；更新已有任务不动
                    candidates.append((item_index, task_index, task))
        if not candidates:
            return 0
        manual_tasks = self.db.list_manual_pending(limit=20)
        if not manual_tasks:
            return 0
        decisions = await llm_match_manual(
            [task for _item, _task, task in candidates], manual_tasks, messages,
            self.settings, now,
        )
        if not decisions:
            return 0

        manual_by_id = {str(task.get("id")): task for task in manual_tasks}
        dropped: set[tuple[int, int]] = set()
        supplemented_ids: set[str] = set()
        for decision in decisions:
            if not 0 <= decision.index < len(candidates):
                continue
            item_index, task_index, task = candidates[decision.index]
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
                # 新事项的任务全被并进手动待办时，不再建一条空事项
                if item.tasks or item.event_id:
                    kept_items.append(item)
            result.items = kept_items

        if supplemented_ids:
            logger.info(
                "手动待办自动补充 %s 条（本轮新建待办 %s 条参与判定，并入后跳过新建 %s 条）",
                len(supplemented_ids), len(candidates), len(dropped),
            )
        return len(supplemented_ids)

    def _supplement_manual(self, manual: dict, candidate: ReconcileTask, decision,
                           *, include_candidate: bool) -> None:
        """把细节补进手动待办；include_candidate=True 时用新待办的旧字段兜底。"""
        notes = decision.supplement_notes
        due_at = decision.supplement_due
        if include_candidate:
            notes = notes or clip(candidate.notes or "", 400)
            due_at = due_at or candidate.due_at
        _task, changed = self.db.supplement_task(
            manual["id"],
            due_at=due_at,
            notes=notes,
            source_msg_ids=list(candidate.source_msg_ids or []),
            match_reason="自动补充（来自新消息归并）",
        )
        if changed:
            logger.info("手动待办已补充「%s」：%s",
                        clip(manual.get("title") or "", 30), ",".join(changed))

    async def _fallback_event(self, session_id: str, group_name: str, messages: list[dict],
                              last_ts: int, now,
                              identity: IdentityContext | None = None) -> dict:
        names = list(dict.fromkeys(
            list(identity.aliases if identity else []) + list(self.settings.me_names or [])
        ))
        result = rule_extract(messages, self.settings, now, me_names=names)
        if not result.tasks:
            return {"messages": len(messages), "kept": len(messages),
                    "tasks_created": 0, "tasks_updated": 0, "mode": "rule"}
        title = result.tasks[0].title or "微信事项"
        event = self.db.create_event(
            session_id=session_id, group_name=group_name,
            title=title, summary=result.summary, last_msg_ts=last_ts,
        )
        reasons = self._match_reasons(messages)
        created: list[dict] = []
        for task in result.tasks:
            source_ids = [msg_id for msg_id in task.source_msg_ids if msg_id]
            match_reason = next((reasons[msg_id] for msg_id in source_ids if msg_id in reasons), "")
            row, is_new = self.db.create_task(
                title=task.title, notes=task.notes, due_at=task.due_at,
                priority=task.priority, group_name=group_name,
                source_msg_ids=source_ids,
                dedupe_key=task_dedupe_key(task), event_id=event["id"],
                match_reason=match_reason,
            )
            if is_new:
                created.append(row)
        if created:
            logger.info("新增 %s 项待办（规则模式）", len(created))
            await self.push_new_tasks(created, result.summary)
        return {"messages": len(messages), "kept": len(messages),
                "tasks_created": len(created), "tasks_updated": 0,
                "mode": "rule", "summary": result.summary}

    async def process_batch(self, messages: list[dict]) -> dict:
        """兼容入口：把消息按会话分组后分别走事件账本。"""
        groups: dict[str, list[dict]] = {}
        for message in messages:
            groups.setdefault(str(message.get("session_id") or ""), []).append(message)
        total_created = total_updated = total_kept = total_dropped = total_supplemented = 0
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
            last_mode = stats.get("mode", last_mode)
            last_summary = stats.get("summary", last_summary)
        return {"messages": len(messages), "kept": total_kept,
                "tasks_created": total_created, "tasks_updated": total_updated,
                "supplemented": total_supplemented,
                "dropped_other": total_dropped,
                "mode": last_mode, "summary": last_summary}

    # ---------- 推送 ----------
    @property
    def app_url(self) -> str:
        return f"{self.settings.base_url}/?token={quote(self.settings.app_token)}"

    async def _notify(self, title: str, body: str, priority: str = "normal") -> list[dict]:
        if in_quiet_hours(now_local(), self.settings.quiet_start, self.settings.quiet_end):
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
        lines = []
        for task in tasks[:4]:
            meta = task.get("group_name") or ""
            due = human_due(task.get("due_at"))
            extra = " · ".join(part for part in (meta, due) if part)
            lines.append(f"• {task['title']}" + (f"（{extra}）" if extra else ""))
        if len(tasks) > 4:
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
        messages = await self.weflow.demo_messages()
        for message in messages:
            self.db.add_message(
                rawid=message["rawid"], session_id=message["session_id"],
                group_name=message["group_name"], sender=message["sender"],
                content=message["content"], ts=message["ts"],
            )
        stats = await self.process_batch(messages)
        stats["tasks_created"] = max(stats.get("tasks_created", 0), 0)
        return stats

    # ---------- 查询 ----------
    def health(self) -> dict:
        counts = self.db.counts()
        source = self.source
        source_connected = self.connected and source is not None
        return {
            "ok": True,
            "data_source": self.data_source,
            "source": {
                "type": self.data_source,
                "configured": bool(source and source.configured),
                "connected": source_connected,
                "last_event_at": self.last_event_at,
                "last_error": self.last_error,
                "stream_mode": "per-session" if self.data_source == "wcda" else "single",
                "streams": len(self._stream_tasks),
            },
            # 兼容旧前端字段：始终返回，但只有 weflow 数据源时才有意义
            "weflow": {
                "configured": self.weflow.configured if self.data_source == "weflow" else False,
                "connected": source_connected if self.data_source == "weflow" else False,
                "last_event_at": self.last_event_at,
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
            "only_new_messages": self.settings.only_new_messages,
            "events": {
                "open": self.db.count_open_events(),
                "buffered_sessions": sum(1 for msgs in self.session_buffers.values() if msgs),
                "buffered_messages": sum(len(msgs) for msgs in self.session_buffers.values()),
            },
            "identity": {
                "account": self.identity.account,
                "account_source": self.identity.account_source,
                "filter_enabled": bool(self.settings.identity_filter),
                "fuzzy_enabled": bool(self.settings.identity_fuzzy),
                "groups_cached": len(self.identity.snapshot()["groups"]),
                "judgments": {**self._judge_stats, "since": self._judge_since},
            },
            "catchup": {
                "enabled": self._catchup_enabled(),
                "seconds": self.settings.wcda_catchup_seconds,
                "lookback_minutes": self.settings.wcda_catchup_lookback_minutes,
                "backfill_minutes": self.settings.wcda_catchup_on_start_minutes,
                "mode": "watermark" if self._catchup_enabled() else "off",
                "max_hours": self.settings.wcda_catchup_max_hours,
                "page_size": self.settings.wcda_catchup_page_size,
                "round_budget": self.settings.wcda_catchup_round_budget,
                "empty_history_hours": self.settings.wcda_catchup_empty_history_hours,
                "watermarks": len(self.db.catchup_states()),
                "last": self._last_catchup,
            },
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
            "only_new_messages": s.only_new_messages,
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
