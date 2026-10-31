"""WeChatDataAnalysis 本地 API 客户端。

WeChatDataAnalysis（LifeArchiveProject）桌面端启动后会在本机提供 FastAPI 服务：
- 默认地址：http://127.0.0.1:10392
- 健康检查：GET /api/health
- 会话列表：GET /api/chat/sessions
- 消息采集：GET /api/chat/messages（按会话分页读取，采集唯一入口）

注意：本项目只有这一条数据源/采集路径。后台采集轮按「每会话 localId 游标 + 时间水位」
分页读取（见 app/service.py::collect），不再订阅任何实时 SSE。
"""
from __future__ import annotations

import logging
import time

import httpx

from .config import Settings

logger = logging.getLogger(__name__)


class WcdaClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.base = settings.wcda_base.rstrip("/")
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
        """把 /api/chat/messages 里的消息转成内部标准 dict（采集用）。"""
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
        """读取某个会话最近的消息（采集用）。"""
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
            logger.warning("采集读取会话消息失败（%s）：%s", session, error)
            return []
        rows = payload.get("messages") if isinstance(payload, dict) else None
        return [item for item in (rows or []) if isinstance(item, dict)]

    async def fetch_messages_page(self, username: str, *, limit: int = 500,
                                  offset: int = 0, order: str = "desc") -> dict:
        """分页读取会话历史消息（采集用）。

        WCDA 的 ``offset`` 表示「从最新一条往前跳过多少条」，``order`` 只决定当前页内
        的排序方向；因此从 offset=0 开始逐页加大 offset 即可稳定回溯到更早的消息。
        返回 ``{"ok", "messages", "has_more", "error"}``，失败时不抛异常，交给调用方
        决定是否跳过该会话（避免一条会话失败拖垮整轮采集）。
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
            logger.warning("采集分页读取失败（%s offset=%s）：%s", session, offset, error)
            return {"ok": False, "messages": [], "has_more": False, "error": str(error)[:200]}
        rows = payload.get("messages") if isinstance(payload, dict) else None
        return {
            "ok": True,
            "messages": [item for item in (rows or []) if isinstance(item, dict)],
            "has_more": bool(payload.get("hasMore")) if isinstance(payload, dict) else False,
        }
