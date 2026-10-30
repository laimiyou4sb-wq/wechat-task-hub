"""WeFlow 本地 HTTP API / SSE 客户端。

WeFlow 设置为：设置 → API 服务 → 启动，并打开「主动推送」。
文档：GET /api/v1/push/messages（text/event-stream）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import AsyncIterator

import httpx

from .config import Settings

logger = logging.getLogger(__name__)


class WeFlowClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.base = settings.weflow_base.rstrip("/")
        self.token = settings.weflow_token
        self._sessions_cache: tuple[float, list[dict]] | None = None

    @property
    def configured(self) -> bool:
        return bool(self.base and self.token)

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}

    async def check_health(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.get(f"{self.base}/health")
                return response.status_code == 200
        except Exception:
            return False

    async def resolve_sessions(self, force: bool = False) -> list[dict]:
        now = time.time()
        if not force and self._sessions_cache and now - self._sessions_cache[0] < 300:
            return self._sessions_cache[1]
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(f"{self.base}/api/v1/sessions",
                                            params={"limit": 500}, headers=self._headers())
                response.raise_for_status()
                payload = response.json()
                sessions = payload.get("sessions") or []
                self._sessions_cache = (now, sessions)
                return sessions
        except Exception as error:
            logger.warning("读取 WeFlow 会话列表失败: %s", error)
            return []

    def _session_allowed(self, session: dict) -> bool:
        username = str(session.get("username") or "")
        display = str(session.get("displayName") or "")
        is_group = username.endswith("@chatroom") or session.get("type") == 2
        if not is_group and not self.settings.include_private:
            return False
        whitelist = self.settings.group_whitelist
        if not whitelist:
            return is_group
        haystack = f"{username} {display}".lower()
        return any(item.lower() in haystack for item in whitelist)

    async def fetch_recent(self, minutes: int) -> list[dict]:
        """按会话补采最近 N 分钟消息，用于服务重启/电脑休眠后的补漏。"""
        if not self.configured or minutes <= 0:
            return []
        sessions = [session for session in await self.resolve_sessions() if self._session_allowed(session)]
        start = int(time.time()) - minutes * 60
        messages: list[dict] = []
        async with httpx.AsyncClient(timeout=30) as client:
            for session in sessions[:50]:
                talker = session.get("username")
                try:
                    response = await client.get(
                        f"{self.base}/api/v1/messages",
                        params={"talker": talker, "start": start, "limit": 200},
                        headers=self._headers(),
                    )
                    response.raise_for_status()
                    payload = response.json()
                except Exception as error:
                    logger.warning("补采 %s 失败: %s", talker, error)
                    continue
                for item in payload.get("messages") or []:
                    message = self._convert_api_message(item, session, talker)
                    if message:
                        messages.append(message)
        messages.sort(key=lambda item: item["ts"])
        return messages

    def _convert_api_message(self, item: dict, session: dict, talker: str) -> dict | None:
        content = (item.get("parsedContent") or item.get("content") or "").strip()
        rawid = str(item.get("serverId") or item.get("localId") or "")
        if not rawid:
            return None
        return {
            "rawid": rawid,
            "session_id": talker,
            "group_name": str(session.get("displayName") or ""),
            "sender": str(item.get("senderUsername") or ""),
            "content": content,
            "ts": int(item.get("createTime") or 0),
            "is_group": talker.endswith("@chatroom"),
            "event": "message.new",
            "at_usernames": [],
            "at_names": [],
            "quote_username": "",
        }

    @staticmethod
    def _convert_stream_event(payload: dict) -> dict | None:
        event = str(payload.get("event") or "message.new")
        session_id = str(payload.get("sessionId") or "")
        group_name = str(payload.get("groupName") or "")
        return {
            "rawid": str(payload.get("rawid") or ""),
            "session_id": session_id,
            "group_name": group_name,
            "sender": str(payload.get("sourceName") or ""),
            "content": str(payload.get("content") or ""),
            "ts": int(payload.get("timestamp") or time.time()),
            "is_group": bool(group_name) or session_id.endswith("@chatroom"),
            "event": event,
            "at_usernames": [],
            "at_names": [],
            "quote_username": "",
        }

    async def stream_messages(self) -> AsyncIterator[dict]:
        """连接 WeFlow SSE，逐条产出消息；断开时抛异常由上层重连。"""
        if not self.configured:
            raise RuntimeError("WeFlow 未配置")
        url = f"{self.base}/api/v1/push/messages"
        timeout = httpx.Timeout(connect=10, read=None, write=10, pool=10)
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("GET", url, params={"access_token": self.token},
                                     headers={"Accept": "text/event-stream"}) as response:
                response.raise_for_status()
                data_lines: list[str] = []
                async for line in response.aiter_lines():
                    if line == "":
                        if data_lines:
                            raw = "\n".join(data_lines)
                            data_lines = []
                            try:
                                payload = json.loads(raw)
                            except json.JSONDecodeError:
                                continue
                            message = self._convert_stream_event(payload)
                            if message and message["rawid"]:
                                yield message
                        continue
                    if line.startswith(":"):
                        continue
                    if line.startswith("data:"):
                        data_lines.append(line[5:].lstrip())

    async def demo_messages(self) -> list[dict]:
        """演示用虚构消息（不访问网络）。"""
        now = int(time.time())
        samples = [
            ("项目群", "李老师", "【通知】本周五18:00前提交报名表，逾期不候，表格在群文件。", now - 300),
            ("项目群", "王同学", "明天下午3点开组会，记得带上上周的材料。", now - 240),
            ("项目群", "张三", "哈哈哈哈这也行", now - 200),
            ("课程群", "助教", "大家记得10月28日之前交作业，提交到学习通。", now - 160),
            ("课程群", "李四", "收到", now - 120),
            ("生活群", "快递驿站", "今晚8点前把取件码发我，不然要退回。", now - 80),
        ]
        messages = []
        for index, (group, sender, content, ts) in enumerate(samples):
            messages.append({
                "rawid": f"demo-{now}-{index}",
                "session_id": f"demo-{index}@chatroom",
                "group_name": group,
                "sender": sender,
                "content": content,
                "ts": ts,
                "is_group": True,
                "event": "message.new",
            })
        return messages
