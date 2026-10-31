"""统一命令行入口：``serve`` / ``tunnel``。

开发态由 ``run.py`` 调用；打包成 exe 后由 ``launcher.pyw`` 以
``WeChatTaskHub.exe --serve`` / ``--tunnel`` 自拉起（不再依赖 pythonw.exe /
run.py），因此这里只做「参数解析 + 启动」，不含任何 UI。
"""
from __future__ import annotations

import argparse
import os
import sys


def _ensure_stdio() -> None:
    """无控制台（PyInstaller --noconsole）时把 stdout/stderr 落到 data/*.log。

    uvicorn 的日志默认写 stderr；Windows GUI 子系统进程没有控制台时
    ``sys.stdout`` / ``sys.stderr`` 为 None，必须提前替换成文件句柄，
    否则启动阶段的日志会直接丢失。
    """
    from .paths import DATA_DIR, ensure_runtime_dirs

    ensure_runtime_dirs()
    for attr, filename in (("stdout", "service.out.log"), ("stderr", "service.err.log")):
        if getattr(sys, attr, None) is not None:
            continue
        try:
            stream = open(DATA_DIR / filename, "a", encoding="utf-8", buffering=1)
        except OSError:
            continue
        setattr(sys, attr, stream)


def _print_banner(settings, lan_ip: str) -> None:
    source_label = {
        "wcda": f"WeChatDataAnalysis（{settings.wcda_base}）",
        "demo": "演示（不连接数据源）",
    }.get(settings.data_source, settings.data_source)
    print("=" * 62)
    print("  微信待办  WeChat Task Hub")
    print(f"  数据源   : {source_label}")
    print(f"  本机访问 : http://127.0.0.1:{settings.port}/?token={settings.app_token}")
    print(f"  手机访问 : http://{lan_ip}:{settings.port}/?token={settings.app_token}")
    if settings.demo_mode:
        print("  演示模式 : 已开启（可用 POST /api/demo/seed 生成示例待办）")
    if settings.data_source == "wcda":
        print("  提示     : 请保持 WeChatDataAnalysis 桌面端运行，并已登录微信")
    if not settings.llm_api_key:
        print("  提示     : 未配置 LLM_API_KEY，使用本地规则提取待办")
    if not (settings.bark_url or settings.ntfy_topic):
        print("  提示     : 未配置 Bark / ntfy，待办只入库不推送")
    print("=" * 62, flush=True)


def run_server(argv: list[str] | None = None) -> None:
    """前台运行 uvicorn（阻塞）。argv 只支持 --host / --port / --demo。"""
    parser = argparse.ArgumentParser(
        prog="WeChatTaskHub --serve",
        description="微信待办服务端（uvicorn）",
        add_help=False,
    )
    parser.add_argument("--host", default=None, help="监听地址，默认取 .env")
    parser.add_argument("--port", type=int, default=None, help="端口，默认 8765")
    parser.add_argument("--demo", action="store_true", help="演示模式：不连微信数据源")
    args, _unknown = parser.parse_known_args(list(sys.argv[1:] if argv is None else argv))

    if args.demo:
        os.environ["DEMO_MODE"] = "1"
    if args.host:
        os.environ["HOST"] = args.host
    if args.port:
        os.environ["PORT"] = str(args.port)

    # 先修正 stdio，再导入 main（main 在导入期就配置 logging）。
    _ensure_stdio()

    from .config import Settings, detect_lan_ip
    from .main import app
    import uvicorn

    settings = Settings.load()
    _print_banner(settings, detect_lan_ip())
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")


def run_tunnel(argv: list[str] | None = None) -> int:
    """运行隧道守护（tools/tunnel.py），参数原样透传（--check/--stop/--once）。"""
    from .paths import APP_DIR, FROZEN

    if not FROZEN and str(APP_DIR) not in sys.path:
        # 开发态 `python -m app.cli` 从其它 cwd 启动时，保证 tools 包可导入
        sys.path.insert(0, str(APP_DIR))
    from tools.tunnel import main as tunnel_main

    return int(tunnel_main(list(sys.argv[1:] if argv is None else argv)))


def main(argv: list[str] | None = None) -> int:
    """子命令分发：``serve`` / ``tunnel`` / ``version``（开发调试用）。"""
    args = list(sys.argv[1:] if argv is None else argv)
    command = args[0].lower() if args else "serve"
    if command == "serve":
        run_server(args[1:])
        return 0
    if command == "tunnel":
        return run_tunnel(args[1:])
    if command in ("version", "--version", "-v"):
        from .version import __version__

        print(__version__)
        return 0
    print(f"未知子命令：{command}（可用：serve / tunnel / version）", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
