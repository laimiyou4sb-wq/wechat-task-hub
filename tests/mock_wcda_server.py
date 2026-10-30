"""本地 mock：模拟 WeChatDataAnalysis 的 /api/health 与 /api/chat/realtime/messages SSE。

仅用于联调/测试，不接触真实微信数据。

    python tests/mock_wcda_server.py 10399
"""
from __future__ import annotations

import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _message(local_id: int, server_id: int, username: str, group: str,
             sender: str, content: str, ts: int) -> dict:
    return {
        "type": "message",
        "account": "wxid_mock",
        "username": username,
        "conversationName": group,
        "received": True,
        "cursor": local_id,
        "ts": ts * 1000,
        "message": {
            "id": f"mock:Msg_{username}:{local_id}",
            "localId": local_id,
            "serverId": server_id,
            "serverIdStr": str(server_id),
            "createTime": ts,
            "senderUsername": f"wxid_{sender}",
            "from": sender,
            "isSent": False,
            "type": 1,
            "content": content,
        },
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # 静默
        pass

    def do_GET(self):
        if self.path.startswith("/api/health"):
            body = json.dumps({"status": "healthy", "service": "mock-wcda"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if not self.path.startswith("/api/chat/realtime/messages"):
            self.send_error(404)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        now = int(time.time())
        ready = {
            "type": "ready", "account": "wxid_mock", "username": None,
            "mode": "incoming", "cursor": None, "conversationCount": 2,
            "intervalMs": 500, "ts": now * 1000,
        }
        events = [
            ready,
            _message(101, 9001, "123@chatroom", "项目群", "张三",
                     "明天下午3点开会，记得带上周的材料", now),
            _message(102, 9002, "456@chatroom", "课程群", "助教",
                     "大家记得10月28日之前交作业，提交到学习通", now + 1),
            _message(103, 9003, "456@chatroom", "课程群", "李四",
                     "哈哈哈哈", now + 2),
        ]
        try:
            for event in events:
                self.wfile.write(f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8"))
                self.wfile.flush()
                time.sleep(0.3)
            # 保持连接，发送心跳
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
