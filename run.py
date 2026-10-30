"""开发态启动入口：``python run.py [--demo] [--host 0.0.0.0] [--port 8765]``

只做「开发态引导」：参数解析、banner、uvicorn 启动都在 ``app/cli.py``
（打包后的 exe 复用同一份逻辑，见 launcher.pyw --serve）。
路径全部由 ``app/paths.py`` 解析，本文件不推导任何目录。
"""
from __future__ import annotations


def main() -> None:
    from app.cli import run_server

    run_server()


if __name__ == "__main__":
    main()
