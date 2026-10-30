"""WeChatDataAnalysis 本地 API 客户端。

WeChatDataAnalysis（LifeArchiveProject）桌面端启动后会在本机提供 FastAPI 服务：
- 默认地址：http://127.0.0.1:10392
- 健康检查：GET /api/health
- 实时消息：GET /api/chat/realtime/messages（SSE，原生 WCDB 实时读取）

SSE data 事件示例：
    {"type":"message","account":"wxid_x","username":"123@chatroom",
     "conversationName":"项目群","received":true,"cursor":123,
     "message":{"localId":123,"serverId":..., "createTime":1760000000,
                "senderUsername":"wxid_a","from":"张三","isSent":false,
                "content":"明天下午3点开会","type":1,...}}
"""
from __future__ import annotations

import json
import logging
import time
from typing import AsyncIterator

import httpx

from .config import Settings

logger = logging.getLogger(__name__)


class WcdaClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.base = settings.wcda_base.rstrip("/")
        self.interval_ms = max(100, settings.wcda_interval_ms)
        self.account = settings.wcda_account.strip()

    @property
    def configured(self) -> bool:
        return bool(self.base)

    async def check_health(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.get(f"{self.base}/api/health")
                return response.status_code == 200
        except Exception:
            return False

    async def fetch_sessions(self, limit: int = 500) -> list[dict]:
        """获取会话列表（含 isTop / isGroup），用于置顶白名单。"""
        if not self.configured:
            return []
        params: dict = {"limit": max(1, min(limit, 2000))}
        if self.account:
            params["account"] = self.account
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(f"{self.base}/api/chat/sessions",
                                            params=params,
                                            headers={"Accept": "application/json"})
                response.raise_for_status()
                payload = response.json()
                return [s for s in (payload.get("sessions") or []) if isinstance(s, dict)]
        except Exception as error:
            logger.warning("读取 WeChatDataAnalysis 会话列表失败：%s", error)
            return []

    # ---------- 身份识别相关（见 app/identity.py） ----------
    async def fetch_current_account(self) -> str:
        """当前登录微信的 wxid；自动识别失败时返回空串（由 ME_WXID 兜底）。"""
        if not self.configured:
            return ""
        params: dict = {}
        if self.account:
            params["account"] = self.account
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(f"{self.base}/api/current-account",
                                            params=params or None,
                                            headers={"Accept": "application/json"})
                response.raise_for_status()
                payload = response.json()
        except Exception as error:
            logger.warning("读取当前微信账号失败：%s", error)
            return ""
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            data = payload if isinstance(payload, dict) else {}
        return str(data.get("current_account") or data.get("account") or "").strip()

    async def fetch_contact_profile(self, username: str) -> dict:
        """通讯录里的单个联系人资料（我自己的昵称/备注/微信号从这取）。"""
        name = str(username or "").strip()
        if not self.configured or not name:
            return {}
        params: dict = {"username": name}
        if self.account:
            params["account"] = self.account
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(f"{self.base}/api/chat/contacts/profile",
                                            params=params,
                                            headers={"Accept": "application/json"})
                response.raise_for_status()
                payload = response.json()
        except Exception as error:
            logger.warning("读取联系人资料失败（%s）：%s", name, error)
            return {}
        contact = payload.get("contact") if isinstance(payload, dict) else None
        return contact if isinstance(contact, dict) else {}

    async def fetch_group_members(self, username: str, limit: int = 300) -> list[dict]:
        """分页读取群成员；成员里的 displayName 就是该群的群昵称。"""
        room = str(username or "").strip()
        if not self.configured or not room:
            return []
        members: list[dict] = []
        offset = 0
        page_size = 100
        async with httpx.AsyncClient(timeout=15) as client:
            for _ in range(10):  # 最多 10 页，防止接口异常时死循环
                if len(members) >= limit:
                    break
                params: dict = {
                    "username": room,
                    "limit": min(page_size, limit - len(members)),
                    "offset": offset,
                }
                if self.account:
                    params["account"] = self.account
                try:
                    response = await client.get(f"{self.base}/api/chat/contacts/group_members",
                                                params=params,
                                                headers={"Accept": "application/json"})
                    response.raise_for_status()
                    payload = response.json()
                except Exception as error:
                    logger.warning("读取群成员失败（%s）：%s", room, error)
                    break
                batch = [item for item in (payload.get("members") or []) if isinstance(item, dict)]
                if not batch:
                    break
                members.extend(batch)
                if not payload.get("hasMore"):
                    break
                next_offset = payload.get("nextOffset")
                if isinstance(next_offset, (int, float)) and int(next_offset) > offset:
                    offset = int(next_offset)
                else:
                    offset += len(batch)
        return members[:limit]

    async def stream_messages(self, username: str = "",
                              since_local_id: int = 0) -> AsyncIterator[dict]:
        """连接 WeChatDataAnalysis 的实时消息 SSE，逐条产出标准消息 dict。

        传 ``username`` 时只订阅该会话（单会话模式）。实测 WCDA 的“全量”实时流
        经常不推送消息，而单会话模式正常，所以 wcda 数据源默认按会话逐个订阅。
        """
        if not self.configured:
            raise RuntimeError("WeChatDataAnalysis 未配置")
        url = f"{self.base}/api/chat/realtime/messages"
        params: dict = {
            "interval_ms": self.interval_ms,
            "include_sent": 1 if self.settings.wcda_include_sent else 0,
        }
        if self.account:
            params["account"] = self.account
        conversation = str(username or "").strip()
        if conversation:
            params["username"] = conversation
            if int(since_local_id or 0) > 0:
                params["since_local_id"] = max(1, int(since_local_id))
        if self.settings.wcda_replay_username and self.settings.wcda_allow_replay:
            params["username"] = self.settings.wcda_replay_username
            params["since_local_id"] = max(1, self.settings.wcda_replay_since_id)
        timeout = httpx.Timeout(connect=10, read=None, write=10, pool=10)
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("GET", url, params=params,
                                     headers={"Accept": "text/event-stream"}) as response:
                if response.status_code != 200:
                    body = (await response.aread()).decode("utf-8", "ignore")[:300]
                    raise RuntimeError(f"WeChatDataAnalysis 返回 {response.status_code}: {body}")
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if not raw:
                        continue
                    try:
                        payload = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    event_type = payload.get("type")
                    if event_type == "ready":
                        # 流已建立（即使暂时没有新消息），让上层标记为已连接
                        yield {
                            "event": "stream.ready",
                            "rawid": "",
                            "account": payload.get("account", ""),
                            "conversation_count": payload.get("conversationCount", 0),
                        }
                        continue
                    if event_type != "message":
                        continue
                    message = self.convert_message(payload)
                    if message:
                        yield message

    @staticmethod
    def convert_message(payload: dict) -> dict | None:
        message = payload.get("message") or {}
        if not isinstance(message, dict):
            return None
        if message.get("isSent"):
            return None
        # 系统消息 / 撤回 / 入群提示等不参与归纳
        msg_type = message.get("type")
        render_type = str(message.get("renderType") or "").strip().lower()
        try:
            msg_type_int = int(msg_type or 0)
        except (TypeError, ValueError):
            msg_type_int = 0
        if msg_type_int in (10000, 10002):
            return None
        if render_type in ("system", "revoke", "recall"):
            return None
        content = str(message.get("content") or "").strip()
        # WCDA 未渲染的系统模板占位符，例如 "$username$"邀请"$names$"加入了群聊
        if "$username$" in content or "$names$" in content:
            return None
        conversation_id = str(payload.get("username") or "")
        account = str(payload.get("account") or "")
        local_id = message.get("localId")
        rawid = str(message.get("id") or message.get("serverIdStr")
                    or message.get("serverId") or f"{account}:{conversation_id}:{local_id}")
        if not rawid or rawid == "0":
            return None
        ts = int(message.get("createTime") or 0)
        if ts > 10**12:  # 毫秒转秒
            ts //= 1000
        if ts <= 0:
            ts = int(payload.get("ts") or time.time() * 1000) // 1000
        sender = str(
            message.get("senderDisplayName")
            or message.get("senderName")
            or message.get("from")
            or message.get("senderUsername")
            or ""
        )
        group_name = str(payload.get("conversationName")
                         or message.get("conversationName") or "")
        # 微信原始 @ 名单（wxid 级，含 notify@all）；比解析正文里的 "@昵称" 更可靠
        at_usernames = [
            str(item).strip()
            for item in (message.get("atUsernames") or [])
            if str(item).strip()
        ]
        at_names = []
        for entry in message.get("atUsers") or []:
            if isinstance(entry, dict):
                display = str(entry.get("displayName") or "").strip()
                if display:
                    at_names.append(display)
            elif isinstance(entry, str) and entry.strip():
                at_names.append(entry.strip())
        return {
            "rawid": rawid,
            "session_id": conversation_id,
            "group_name": group_name,
            "sender": sender,
            "content": content,
            "ts": ts,
            "is_group": conversation_id.endswith("@chatroom"),
            "event": "message.new",
            "msg_type": msg_type_int,
            "render_type": render_type,
            "at_usernames": at_usernames,
            "at_names": at_names,
            "quote_username": str(message.get("quoteUsername") or "").strip(),
            "link_title": str(message.get("title") or "").strip(),
            "link_url": str(message.get("url") or "").strip(),
            "link_type": str(message.get("linkType") or "").strip(),
        }

    @staticmethod
    def convert_api_message(session_id: str, group_name: str, message: dict,
                            account: str = "") -> dict | None:
        """把 /api/chat/messages 里的消息转成内部标准 dict（补采用）。"""
        if not isinstance(message, dict):
            return None
        payload = {
            "type": "message",
            "account": account,
            "username": session_id,
            "conversationName": group_name,
            "ts": int(message.get("createTime") or 0) * 1000,
            "message": message,
        }
        return WcdaClient.convert_message(payload)

    async def fetch_recent_messages(self, username: str, limit: int = 300) -> list[dict]:
        """读取某个会话最近的消息（补采兜底用）。"""
        session = str(username or "").strip()
        if not self.configured or not session:
            return []
        params: dict = {"username": session, "limit": max(1, min(limit, 500))}
        if self.account:
            params["account"] = self.account
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.get(f"{self.base}/api/chat/messages",
                                            params=params,
                                            headers={"Accept": "application/json"})
                response.raise_for_status()
                payload = response.json()
        except Exception as error:
            logger.warning("补采读取会话消息失败（%s）：%s", session, error)
            return []
        rows = payload.get("messages") if isinstance(payload, dict) else None
        return [item for item in (rows or []) if isinstance(item, dict)]

    async def fetch_messages_page(self, username: str, *, limit: int = 500,
                                  offset: int = 0, order: str = "desc") -> dict:
        """分页读取会话历史消息（补采增强用）。

        WCDA 的 ``offset`` 表示「从最新一条往前跳过多少条」，``order`` 只决定当前页内
        的排序方向；因此从 offset=0 开始逐页加大 offset 即可稳定回溯到更早的消息。
        返回 ``{"ok", "messages", "has_more", "error"}``，失败时不抛异常，交给调用方
        决定是否跳过该会话（避免一条会话失败拖垮整轮补采）。
        """
        session = str(username or "").strip()
        if not self.configured or not session:
            return {"ok": False, "messages": [], "has_more": False, "error": "not_configured"}
        safe_limit = max(1, min(int(limit or 500), 1000))
        params: dict = {
            "username": session,
            "limit": safe_limit,
            "offset": max(0, int(offset or 0)),
            "order": order if order in ("asc", "desc") else "desc",
        }
        if self.account:
            params["account"] = self.account
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(f"{self.base}/api/chat/messages",
                                            params=params,
                                            headers={"Accept": "application/json"})
                response.raise_for_status()
                payload = response.json()
        except Exception as error:
            logger.warning("补采分页读取失败（%s offset=%s）：%s", session, offset, error)
            return {"ok": False, "messages": [], "has_more": False, "error": str(error)[:200]}
        rows = payload.get("messages") if isinstance(payload, dict) else None
        return {
            "ok": True,
            "messages": [item for item in (rows or []) if isinstance(item, dict)],
            "has_more": bool(payload.get("hasMore")) if isinstance(payload, dict) else False,
        }
