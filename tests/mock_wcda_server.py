"""本地 mock：模拟 WeChatDataAnalysis 的本地只读 API。

采集链路（wcda 唯一采集方法）需要：
- ``GET /api/health``             健康检查
- ``GET /api/chat/sessions``      会话列表（username/name/isGroup/isTop）
- ``GET /api/chat/messages``      按会话分页读取消息（limit/offset/order/hasMore）

同时保留旧的 ``GET /api/chat/realtime/messages`` SSE（历史脚本/对照用；当前 wcda 已不订阅）。

    python tests/mock_wcda_server.py 10399

然后在另一个终端：
    $env:DATA_SOURCE="wcda"; $env:WCDA_BASE="http://127.0.0.1:10399"
    $env:WCDA_CATCHUP_SECONDS="10"; $env:DEMO_MODE="1"
    python run.py --port 8790
"""
from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

_LOCK = threading.RLock()
_START = int(time.time())

_SESSIONS = [
    {"username": "123@chatroom", "name": "项目群", "isGroup": True, "isTop": True},
    {"username": "456@chatroom", "name": "课程群", "isGroup": True, "isTop": True},
]


def _build_messages() -> dict[str, list[dict]]:
    now = _START
    samples = [
        ("123@chatroom", "项目群", "张三", "明天下午3点开会，记得带上周的材料", now - 900),
        ("123@chatroom", "项目群", "李四", "收到", now - 840),
        ("123@chatroom", "项目群", "王五", "周五前填写破冰人数统计表，记得提交", now - 600),
        ("456@chatroom", "课程群", "助教", "大家记得10月28日之前交作业，提交到学习通", now - 480),
        ("456@chatroom", "课程群", "李四", "哈哈哈哈这也行", now - 300),
        ("456@chatroom", "课程群", "助教", "明早8点去上课，别迟到", now - 120),
    ]
    messages: dict[str, list[dict]] = {}
    for index, (username, _group, sender, content, ts) in enumerate(samples, start=1):
        messages.setdefault(username, []).append({
            "id": f"mock:Msg_{username}:{index}",
            "localId": index,
            "serverId": 9000 + index,
            "serverIdStr": str(9000 + index),
            "createTime": ts,
            "senderUsername": f"wxid_{sender}",
            "senderDisplayName": sender,
            "from": sender,
            "isSent": False,
            "type": 1,
            "renderType": "text",
            "content": content,
            "atUsernames": [],
            "atUsers": [],
            "quoteUsername": "",
        })
    return messages


_MESSAGES = _build_messages()
_SSE_MESSAGES = [
    {
        "type": "message",
        "account": "wxid_mock",
        "username": username,
        "conversationName": _SESSIONS[0 if username.startswith("123") else 1]["name"],
        "received": True,
        "cursor": item["localId"],
        "ts": item["createTime"] * 1000,
        "message": item,
    }
    for username, rows in _MESSAGES.items()
    for item in rows
]


def _send_json(handler: BaseHTTPRequestHandler, payload: dict, status: int = 200) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # 静默
        pass

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/api/health":
            _send_json(self, {"status": "healthy", "service": "mock-wcda"})
            return
        if path == "/api/current-account":
            _send_json(self, {"data": {"current_account": "wxid_mock"}})
            return
        if path == "/api/chat/contacts/profile":
            _send_json(self, {"contact": {"username": "wxid_mock", "displayName": "Mock 我"}})
            return
        if path == "/api/chat/contacts/group_members":
            _send_json(self, {"members": [], "hasMore": False})
            return
        if path == "/api/chat/sessions":
            try:
                limit = int((query.get("limit") or ["500"])[0] or 500)
            except ValueError:
                limit = 500
            _send_json(self, {"sessions": _SESSIONS[:max(1, limit)]})
            return
        if path == "/api/chat/messages":
            self._serve_messages(query)
            return
        if path == "/api/chat/realtime/messages":
            self._serve_legacy_sse()
            return
        _send_json(self, {"error": "not found", "path": path}, status=404)

    def _serve_messages(self, query: dict) -> None:
        username = (query.get("username") or [""])[0]
        if not username:
            _send_json(self, {"error": "username required"}, status=400)
            return
        try:
            limit = max(1, min(int((query.get("limit") or ["500"])[0] or 500), 1000))
            offset = max(0, int((query.get("offset") or ["0"])[0] or 0))
        except ValueError:
            _send_json(self, {"error": "limit/offset must be int"}, status=400)
            return
        order = (query.get("order") or ["desc"])[0]
        with _LOCK:
            rows = sorted(_MESSAGES.get(username, []),
                          key=lambda item: int(item.get("createTime") or 0),
                          reverse=order != "asc")
            page = rows[offset:offset + limit]
            _send_json(self, {
                "messages": page,
                "hasMore": offset + len(page) < len(rows),
                "total": len(rows),
                "nextOffset": offset + len(page),
            })

    def _serve_legacy_sse(self) -> None:
        """旧 SSE（wcda 已不订阅；仅为历史脚本/对照保留）。"""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        ready = {
            "type": "ready", "account": "wxid_mock", "username": None,
            "mode": "incoming", "cursor": None, "conversationCount": len(_SESSIONS),
            "intervalMs": 500, "ts": int(time.time()) * 1000,
        }
        try:
            for event in [ready, *_SSE_MESSAGES]:
                self.wfile.write(f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8"))
                self.wfile.flush()
                time.sleep(0.2)
            for _ in range(120):
                self.wfile.write(b": ping\n\n")
                self.wfile.flush()
                time.sleep(1)
        except (BrokenPipeError, ConnectionResetError):
            return


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 10399
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"mock WCDA listening on http://127.0.0.1:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
