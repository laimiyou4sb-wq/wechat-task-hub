"""FastAPI 应用入口。"""
from __future__ import annotations

import logging
from collections import OrderedDict
from contextlib import asynccontextmanager

import anyio
from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

try:  # 运行环境已安装 brotli；缺失时中间件自动退化，不影响服务启动
    import brotli
except ImportError:  # pragma: no cover - 仅作兜底
    brotli = None  # type: ignore[assignment]

from .api import router
from .config import Settings
from .mobile import router as mobile_router
from .paths import WEB_DIR, ensure_runtime_dirs
from .service import AppService
from .version import __version__

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Brotli 中间件（纯 ASGI，位于 GZipMiddleware 外层）
#
# 浏览器同时支持 br 与 gzip 时优先 br；br 未命中（未声明、q 值更低、非文本、
# 太小、已编码）时把响应原样交给内层 GZipMiddleware，由它决定是否 gzip。
# 压缩质量 11 对静态文本比 gzip -9 再省约 16%，压缩结果按 ETag 缓存，避免
# 每次请求重复消耗 CPU。
# ---------------------------------------------------------------------------
_COMPRESSIBLE_TYPES = frozenset({
    "text/html",
    "text/css",
    "text/javascript",
    "application/javascript",
    "application/x-javascript",
    "application/json",
    "application/manifest+json",
    "image/svg+xml",
})

_BROTLI_CACHE_LIMIT = 64
_BROTLI_CACHE: "OrderedDict[tuple, bytes]" = OrderedDict()


def _parse_accept_encoding(value: str) -> dict[str, float]:
    """把 Accept-Encoding 解析为 {编码: q 值}；未知参数忽略，非法 q 按 1 处理。"""
    accepted: dict[str, float] = {}
    for item in value.split(","):
        token, _, params = item.strip().partition(";")
        token = token.strip().lower()
        if not token:
            continue
        if token == "x-gzip":
            token = "gzip"
        quality = 1.0
        for param in params.split(";"):
            name, _, raw = param.partition("=")
            if name.strip().lower() == "q":
                try:
                    quality = float(raw.strip())
                except ValueError:
                    quality = 1.0
        accepted[token] = max(accepted.get(token, 0.0), quality)
    return accepted


def _prefers_brotli(accept_encoding: str) -> bool:
    """客户端显式接受 br，且 br 的 q 值不低于 gzip 时返回 True（相等时 br 优先）。"""
    if not accept_encoding or brotli is None:
        return False
    accepted = _parse_accept_encoding(accept_encoding)
    br_quality = accepted.get("br", 0.0)
    if br_quality <= 0:
        return False
    return br_quality >= accepted.get("gzip", 0.0)


def _is_compressible_type(content_type: str) -> bool:
    return content_type.split(";")[0].strip().lower() in _COMPRESSIBLE_TYPES


def _brotli_compress_sync(body: bytes, quality: int) -> bytes:
    return brotli.compress(body, mode=brotli.MODE_TEXT, quality=quality)


class _BrotliResponder:
    """缓冲候选响应体，压缩成功后写回 content-encoding / content-length。"""

    def __init__(self, app: ASGIApp, minimum_size: int = 500, quality: int = 11) -> None:
        self.app = app
        self.minimum_size = minimum_size
        self.quality = quality
        self.send: Send = self._unattached_send
        self.scope: Scope = {}
        self.initial_message: Message = {}
        self.chunks: list[bytes] = []
        self.compressible = False
        self.started = False
        self.etag: str | None = None
        self.content_type = ""

    @staticmethod
    async def _unattached_send(message: Message) -> None:  # pragma: no cover - 防御性
        raise RuntimeError("send 尚未绑定")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self.scope = scope
        self.send = send
        await self.app(scope, receive, self._send_with_compression)

    async def _send_with_compression(self, message: Message) -> None:
        kind = message["type"]
        if kind == "http.response.start":
            await self._on_start(message)
        elif kind == "http.response.body":
            await self._on_body(message)
        elif kind == "http.response.pathsend":
            # uvicorn(h11) 目前不发该消息；若将来启用 pathsend，原样转发（不压缩）
            if not self.started:
                await self.send(self.initial_message)
                self.started = True
            await self.send(message)
        else:  # pragma: no cover - 其他扩展消息（如 trailers）直接透传
            if self.initial_message:
                await self.send(self.initial_message)
                self.initial_message = {}
            await self.send(message)

    async def _on_start(self, message: Message) -> None:
        self.initial_message = message
        headers = Headers(raw=message["headers"])
        self.etag = headers.get("etag")
        self.content_type = headers.get("content-type", "")
        status = message.get("status", 200)
        self.compressible = (
            status == 200
            and "content-encoding" not in headers
            and _is_compressible_type(self.content_type)
        )
        content_length = headers.get("content-length")
        if self.compressible and content_length is not None:
            try:
                if int(content_length) < self.minimum_size:
                    self.compressible = False
            except ValueError:
                pass
        if not self.compressible:
            await self.send(message)
            self.started = True

    async def _on_body(self, message: Message) -> None:
        if not self.compressible:
            await self.send(message)
            return
        body = message.get("body", b"")
        if body:
            self.chunks.append(body)
        if message.get("more_body", False):
            return
        await self._finish()

    async def _finish(self) -> None:
        body = b"".join(self.chunks)
        self.chunks.clear()
        headers = MutableHeaders(raw=self.initial_message["headers"])
        vary = headers.get("vary", "")
        if "accept-encoding" not in vary.lower():
            headers["Vary"] = f"{vary}, Accept-Encoding" if vary else "Accept-Encoding"
        compressed = None
        if len(body) >= self.minimum_size:
            compressed = await self._compress(body)
        if compressed is not None and len(compressed) < len(body):
            headers["Content-Encoding"] = "br"
            headers["Content-Length"] = str(len(compressed))
            await self.send(self.initial_message)
            await self.send({"type": "http.response.body", "body": compressed, "more_body": False})
        else:  # 不压缩（太小或压缩后反而更大）：保持原始响应头与响应体
            await self.send(self.initial_message)
            await self.send({"type": "http.response.body", "body": body, "more_body": False})
        self.started = True

    async def _compress(self, body: bytes) -> bytes | None:
        cache_key: tuple | None = None
        if self.etag:
            cache_key = (self.scope.get("path", ""), self.etag, self.content_type, len(body))
            cached = _BROTLI_CACHE.get(cache_key)
            if cached is not None:
                _BROTLI_CACHE.move_to_end(cache_key)
                return cached
        try:
            # 质量 11 压缩几十 KB 需数十毫秒，放到工作线程避免阻塞事件循环
            compressed = await anyio.to_thread.run_sync(
                _brotli_compress_sync, body, self.quality
            )
        except Exception:  # pragma: no cover - 压缩失败时退化为未压缩响应
            logger.exception("Brotli 压缩失败，本次响应回退为未压缩")
            return None
        if cache_key is not None and len(compressed) < len(body):
            _BROTLI_CACHE[cache_key] = compressed
            while len(_BROTLI_CACHE) > _BROTLI_CACHE_LIMIT:
                _BROTLI_CACHE.popitem(last=False)
        return compressed


class BrotliMiddleware:
    """br 优先、gzip 回退的压缩中间件。

    置于 GZipMiddleware 外层：命中 br 时把下游请求头的 Accept-Encoding 收窄为
    ``br``，内层 GZip 因此不会再次压缩（避免 ``content-encoding: gzip, br``）。
    """

    def __init__(self, app: ASGIApp, minimum_size: int = 500, quality: int = 11) -> None:
        self.app = app
        self.minimum_size = minimum_size
        self.quality = quality

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not _prefers_brotli(Headers(scope=scope).get("accept-encoding", "")):
            await self.app(scope, receive, send)
            return
        scope = dict(scope)
        scope["headers"] = [
            (name, value)
            for name, value in scope["headers"]
            if name.lower() != b"accept-encoding"
        ] + [(b"accept-encoding", b"br")]
        responder = _BrotliResponder(self.app, self.minimum_size, self.quality)
        await responder(scope, receive, send)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings.load()
    ensure_runtime_dirs()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    service = AppService(settings)
    app.state.service = service
    await service.start()
    try:
        yield
    finally:
        await service.stop()


def create_app() -> FastAPI:
    """组装应用：API 路由 → 静态站点（/），顺序即匹配优先级。"""
    application = FastAPI(title="微信待办", version=__version__, lifespan=lifespan)
    # 压缩 HTML/CSS/JS/JSON：手机端走局域网时首屏体积明显下降（>=500 字节才压缩）。
    # 先加 GZip（内层）再加 Brotli（外层）：br 命中后内层不会再 gzip 二次压缩。
    application.add_middleware(GZipMiddleware, minimum_size=500)
    application.add_middleware(BrotliMiddleware, minimum_size=500, quality=11)
    application.include_router(router)
    application.include_router(mobile_router)
    if WEB_DIR.exists():
        application.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
    return application


app = create_app()
