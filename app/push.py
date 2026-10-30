"""推送到手机：Bark（iOS）与 ntfy（Android/任意）。"""
from __future__ import annotations

import logging
from urllib.parse import quote

import httpx

from .config import Settings

logger = logging.getLogger(__name__)


class Pusher:
    def __init__(self, settings: Settings):
        self.bark = settings.bark_url.rstrip("/")
        self.ntfy_server = settings.ntfy_server.rstrip("/")
        self.ntfy_topic = settings.ntfy_topic
        self.ntfy_token = settings.ntfy_token

    @property
    def configured(self) -> bool:
        return bool(self.bark or (self.ntfy_topic and self.ntfy_server))

    @property
    def has_bark(self) -> bool:
        return bool(self.bark)

    @property
    def has_ntfy(self) -> bool:
        return bool(self.ntfy_topic and self.ntfy_server)

    async def send(self, title: str, body: str, url: str | None = None,
                   priority: str = "normal") -> list[dict]:
        results: list[dict] = []
        if not self.configured:
            return results
        async with httpx.AsyncClient(timeout=15) as client:
            if self.has_bark:
                results.append(await self._send_bark(client, title, body, url, priority))
            if self.has_ntfy:
                results.append(await self._send_ntfy(client, title, body, url, priority))
        return results

    async def _send_bark(self, client: httpx.AsyncClient, title: str, body: str,
                         url: str | None, priority: str) -> dict:
        try:
            endpoint = f"{self.bark}/{quote(title[:80], safe='')}/{quote(body[:500], safe='')}"
            params = {"group": "微信待办", "level": "timeSensitive" if priority == "high" else "active"}
            if url:
                params["url"] = url
            response = await client.get(endpoint, params=params)
            ok = response.status_code == 200
            return {"channel": "bark", "ok": ok, "detail": response.text[:200]}
        except Exception as error:
            logger.warning("Bark 推送失败: %s", error)
            return {"channel": "bark", "ok": False, "detail": str(error)[:200]}

    async def _send_ntfy(self, client: httpx.AsyncClient, title: str, body: str,
                         url: str | None, priority: str) -> dict:
        try:
            payload = {
                "topic": self.ntfy_topic,
                "title": title,
                "message": body,
                "priority": 4 if priority == "high" else 3,
                "tags": ["memo"],
            }
            if url:
                payload["click"] = url
                payload["actions"] = [{"action": "view", "label": "打开待办", "url": url}]
            headers = {}
            if self.ntfy_token:
                headers["Authorization"] = f"Bearer {self.ntfy_token}"
            response = await client.post(f"{self.ntfy_server}/", json=payload, headers=headers)
            ok = response.status_code in (200, 201, 202)
            return {"channel": "ntfy", "ok": ok, "detail": response.text[:200]}
        except Exception as error:
            logger.warning("ntfy 推送失败: %s", error)
            return {"channel": "ntfy", "ok": False, "detail": str(error)[:200]}
