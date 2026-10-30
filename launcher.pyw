"""微信待办 · 快捷启动器（pythonw 运行，无控制台窗口）

双击桌面快捷方式时：
1. 如果服务已经在运行 → 直接打开任务表网页；
2. 如果 WeChatDataAnalysis 没运行 → 尝试启动它；
3. 后台启动本项目的服务（隐藏窗口）；
4. 等健康检查通过后自动打开浏览器。

也支持 --stop 停止服务（桌面「停止微信待办」快捷方式）。

打包成 exe 后本文件同时是 PyInstaller 入口：
    WeChatTaskHub.exe            启动器（默认行为）
    WeChatTaskHub.exe --serve    真正跑 uvicorn（无窗口子进程）
    WeChatTaskHub.exe --tunnel   隧道守护（参数透传：--check / --stop / --once）
    WeChatTaskHub.exe --stop     停止服务 + 隧道
所有路径都来自 app/paths.py，不再用 __file__ 推导。
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path

from app.config import ensure_env_file
from app.paths import APP_DIR, DATA_DIR, ENV_FILE, FROZEN, TOOLS_DIR, ensure_runtime_dirs

ROOT = APP_DIR
ensure_runtime_dirs()

# 仅「双击启动」路径自动补 .env；--serve / --tunnel / --stop / --version 等
# 管理/子进程命令不产生写文件副作用（打包后的首次运行就是双击 exe）。
_MANAGEMENT_MODE = any(
    flag in sys.argv[1:] for flag in ("--serve", "--tunnel", "--stop", "--version")
)
if not _MANAGEMENT_MODE:
    ensure_env_file()

LOG_FILE = DATA_DIR / "launcher.log"
PID_FILE = DATA_DIR / "server.pid"
TUNNEL_PY = TOOLS_DIR / "tunnel.py"
TUNNEL_STATE = DATA_DIR / "tunnel.json"
TUNNEL_PID = DATA_DIR / "tunnel.pid"


def log(message: str) -> None:
    try:
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")
    except Exception:
        pass


def read_env_file() -> dict[str, str]:
    values: dict[str, str] = {}
    env_path = ENV_FILE
    if not env_path.exists():
        return values
    try:
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    except Exception as error:
        log(f"读取 .env 失败: {error}")
    return values


ENV = read_env_file()


def setting(key: str, default: str = "") -> str:
    return os.environ.get(key) or ENV.get(key) or default


PORT = int(setting("PORT", "8765") or 8765)
TOKEN = setting("APP_TOKEN", "")
BASE_URL = f"http://127.0.0.1:{PORT}"
WCDA_BASE = setting("WCDA_BASE", "http://127.0.0.1:10392").rstrip("/")
NO_BROWSER = bool(os.environ.get("WECHAT_TASK_NO_BROWSER"))
NO_DIALOG = bool(os.environ.get("WECHAT_TASK_NO_DIALOG"))

# 公网隧道（ngrok 固定域名）。TUNNEL_ENABLED=0 时完全跳过，不影响局域网直连。
TUNNEL_ENABLED = setting("TUNNEL_ENABLED", "0").strip().lower() in ("1", "true", "yes", "on")
TUNNEL_PROVIDER = setting("TUNNEL_PROVIDER", "ngrok").strip() or "ngrok"
try:
    TUNNEL_WAIT = max(10, int(setting("TUNNEL_START_TIMEOUT", "45") or 45))
except ValueError:
    TUNNEL_WAIT = 45


def http_ok(url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return 200 <= getattr(response, "status", 200) < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


def service_healthy() -> bool:
    return http_ok(f"{BASE_URL}/api/health")


def show_message(text: str, title: str = "微信待办", error: bool = False) -> None:
    if NO_DIALOG:
        log(f"[dialog] {title}: {text}")
        return
    try:
        flags = 0x10 if error else 0x40
        ctypes.windll.user32.MessageBoxW(0, text, title, flags)
    except Exception:
        pass


def pythonw_path() -> str:
    executable = Path(sys.executable)
    candidate = executable.with_name("pythonw.exe")
    if candidate.exists():
        return str(candidate)
    return str(executable)


def creation_flags() -> int:
    # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP：启动后不随快捷方式退出
    return 0x00000008 | 0x00000200


def self_exe() -> str:
    """当前可执行文件：冻结态就是 WeChatTaskHub.exe。"""
    return sys.executable if FROZEN else pythonw_path()


def service_command() -> list[str]:
    """服务子进程命令：冻结态靠 exe --serve 自拉起，开发态 pythonw run.py。"""
    if FROZEN:
        return [sys.executable, "--serve"]
    return [pythonw_path(), str(APP_DIR / "run.py")]


def tunnel_available() -> bool:
    # 冻结态隧道逻辑已打进 exe（--tunnel），不依赖外置 tools/tunnel.py
    return FROZEN or TUNNEL_PY.is_file()


def tunnel_command(args: list[str]) -> list[str]:
    """隧道子命令：冻结态 = exe --tunnel <args>；开发态 = pythonw tools/tunnel.py <args>。"""
    if FROZEN:
        return [sys.executable, "--tunnel", *args]
    return [pythonw_path(), str(TUNNEL_PY), *args]


def tunnel_hint(action: str = "--check") -> str:
    if FROZEN:
        return f"{Path(sys.executable).name} --tunnel {action}"
    return f"python tools\\tunnel.py {action}"


def clean_child_env() -> dict[str, str]:
    """去掉开发环境可能继承的 Electron 变量，否则 GUI 应用会被当成 Node 运行。"""
    child_env = os.environ.copy()
    for key in ("ELECTRON_RUN_AS_NODE", "ELECTRON_NO_ATTACH_CONSOLE", "NODE_OPTIONS"):
        child_env.pop(key, None)
    return child_env


def find_wcda_exe() -> Path | None:
    candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "WeChatDataAnalysis" / "WeChatDataAnalysis.exe",
        Path(os.environ.get("ProgramFiles", "")) / "WeChatDataAnalysis" / "WeChatDataAnalysis.exe",
        Path(os.environ.get("ProgramFiles(x86)", "")) / "WeChatDataAnalysis" / "WeChatDataAnalysis.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def ensure_wcda() -> bool:
    if not WCDA_BASE:
        return True
    if http_ok(f"{WCDA_BASE}/api/health"):
        return True
    exe = find_wcda_exe()
    if exe is None:
        log("WeChatDataAnalysis 未运行，且未找到安装程序")
        return False
    try:
        log(f"启动 WeChatDataAnalysis: {exe}")
        process = subprocess.Popen([str(exe)], cwd=str(exe.parent), creationflags=creation_flags(),
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   env=clean_child_env())
    except Exception as error:
        log(f"启动 WeChatDataAnalysis 失败: {error}")
        return False
    deadline = time.time() + 20
    while time.time() < deadline:
        if http_ok(f"{WCDA_BASE}/api/health"):
            log("WeChatDataAnalysis 已就绪")
            return True
        if process.poll() is not None and time.time() > deadline - 15:
            log(f"WeChatDataAnalysis 进程已退出（code={process.returncode}），跳过等待")
            return False
        time.sleep(1)
    log("WeChatDataAnalysis 20 秒内未就绪，继续启动服务（会自动重连）")
    return False


def start_service() -> bool:
    if service_healthy():
        return True
    log("启动微信待办服务")
    stdout_path = DATA_DIR / "service.out.log"
    stderr_path = DATA_DIR / "service.err.log"
    try:
        with stdout_path.open("ab") as stdout, stderr_path.open("ab") as stderr:
            process = subprocess.Popen(
                service_command(),
                cwd=str(ROOT),
                stdout=stdout,
                stderr=stderr,
                creationflags=creation_flags(),
                env=clean_child_env(),
            )
        PID_FILE.write_text(str(process.pid), encoding="utf-8")
        log(f"服务进程 PID={process.pid}")
    except Exception as error:
        log(f"启动服务失败: {error}")
        show_message(f"启动失败：{error}\n\n日志：{LOG_FILE}", error=True)
        return False

    deadline = time.time() + 45
    while time.time() < deadline:
        if service_healthy():
            return True
        time.sleep(0.8)
    show_message(f"服务启动超时，请查看日志：{LOG_FILE}", error=True)
    return False


def open_app() -> None:
    url = f"{BASE_URL}/?token={TOKEN}" if TOKEN else f"{BASE_URL}/"
    if NO_BROWSER:
        log(f"（测试模式）跳过打开浏览器：{url}")
        return
    try:
        webbrowser.open(url)
    except Exception as error:
        log(f"打开浏览器失败: {error}")
        show_message(f"服务已启动，请手动打开：\n{url}")


def pid_is_ours(pid: str) -> bool:
    """pid 是否为本程序的进程。

    开发态镜像名是 python.exe / pythonw.exe；打包后是 WeChatTaskHub.exe，
    所以不能只认 "python"，否则 --stop / 服务复用 / 隧道复用全部失效。
    """
    try:
        output = subprocess.check_output(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            stderr=subprocess.DEVNULL,
        ).decode("utf-8", "ignore").lower()
    except Exception:
        return False
    if not output.strip():
        return False
    if "python" in output:
        return True
    return FROZEN and Path(sys.executable).stem.lower() in output


def stop_service() -> None:
    # 先停隧道（ngrok/cloudflared 子进程），再停服务本体
    stop_tunnel()
    pid = ""
    try:
        pid = PID_FILE.read_text(encoding="utf-8").strip()
    except Exception:
        pid = ""
    if pid and not pid_is_ours(pid):
        pid = ""
    if not pid:
        candidate = find_pid_by_port()
        if candidate and pid_is_ours(candidate):
            pid = candidate
    if not pid:
        show_message("微信待办服务当前没有运行。")
        return
    try:
        subprocess.run(["taskkill", "/PID", pid, "/F", "/T"],
                       creationflags=creation_flags(), check=False)
        log(f"已停止服务 PID={pid}")
        show_message("微信待办服务已停止（隧道已一并停止）。")
    except Exception as error:
        show_message(f"停止失败：{error}", error=True)
    try:
        PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def find_pid_by_port() -> str:
    try:
        output = subprocess.check_output(["netstat", "-ano"], stderr=subprocess.DEVNULL)
        text = output.decode("utf-8", "ignore")
        needle = f":{PORT}"
        for line in text.splitlines():
            if needle in line and "LISTENING" in line.upper():
                parts = line.split()
                if parts and parts[-1].isdigit():
                    return parts[-1]
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# 公网隧道（tools/tunnel.py，ngrok 固定域名）
#
# 说明：旧的 Tailscale 方案已弃用，这里不再依赖 Tailscale；远程地址由
# tools/tunnel.py 维护并写入 data/tunnel.json（服务端 pairing 会读它）。
# TUNNEL_ENABLED=0（默认）时以下逻辑全部跳过，行为与旧版一致。
# ---------------------------------------------------------------------------
def read_json_file(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def tunnel_pid_alive() -> bool:
    info = read_json_file(TUNNEL_PID)
    pid = str(info.get("supervisor_pid") or "").strip()
    return bool(pid) and pid_is_ours(pid)


def tunnel_state_url(max_age: float = 150.0) -> str:
    """读 data/tunnel.json；只在「最近刷新过」时返回地址（心跳会不断刷新 updated_at）。"""
    info = read_json_file(TUNNEL_STATE)
    url = str(info.get("url") or "").strip().rstrip("/")
    if not url.startswith("https://"):
        return ""
    try:
        updated = datetime.strptime(str(info.get("updated_at") or ""), "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return ""
    if (datetime.now() - updated).total_seconds() > max_age:
        return ""
    return url


def tunnel_public_ok(url: str, timeout: float = 8.0) -> bool:
    headers = {
        "Accept": "application/json",
        "User-Agent": "wxtask-launcher/1.0",
        "ngrok-skip-browser-warning": "1",
        "Cache-Control": "no-store",
    }
    try:
        request = urllib.request.Request(f"{url}/api/health", headers=headers)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if not 200 <= getattr(response, "status", 200) < 300:
                return False
            payload = json.loads(response.read().decode("utf-8", "replace"))
            return isinstance(payload, dict) and payload.get("ok") is True
    except Exception:
        return False


def tunnel_ready() -> bool:
    if not TUNNEL_ENABLED:
        return True
    if not tunnel_pid_alive():
        return False
    url = tunnel_state_url()
    if not url:
        return False
    return tunnel_public_ok(url)


def run_tunnel_command(args: list[str], timeout: float = 45.0) -> int:
    """调用隧道子命令（--check / --stop），返回退出码；失败返回 -1。"""
    child_env = clean_child_env()
    child_env["PYTHONIOENCODING"] = "utf-8"  # 让子进程输出与下面 text=True/encoding=utf-8 一致
    try:
        result = subprocess.run(
            tunnel_command(args),
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            creationflags=creation_flags(),
            env=child_env,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        output = ((result.stdout or "") + (result.stderr or "")).strip()
        log(f"tunnel {' '.join(args)} 退出码={result.returncode} 输出={output[:400]}")
        return result.returncode
    except Exception as error:
        log(f"调用隧道命令 {' '.join(args)} 失败: {error!r}")
        return -1


def stop_tunnel() -> None:
    """停止隧道守护进程及其 ngrok/cloudflared 子进程（pid 在 data/tunnel.pid）。"""
    if not tunnel_available():
        return
    run_tunnel_command(["--stop"])


def ensure_tunnel() -> bool:
    """确保公网隧道在线：已在线则复用，否则先清理旧进程再拉起新的。"""
    if not TUNNEL_ENABLED:
        log("隧道未启用（TUNNEL_ENABLED=0），跳过远程隧道（局域网直连不受影响）")
        return True
    if not tunnel_available():
        log(f"找不到隧道脚本：{TUNNEL_PY}")
        return False
    if tunnel_ready():
        log(f"隧道已在线，复用：{tunnel_state_url()}")
        return True

    log(f"隧道未在线，准备启动（provider={TUNNEL_PROVIDER}，最多等 {TUNNEL_WAIT} 秒）")
    # 先停掉可能残留的旧守护进程/子进程，避免 4040 端口或固定域名冲突
    if TUNNEL_PID.exists():
        run_tunnel_command(["--stop"])
    try:
        out_path = DATA_DIR / "tunnel-launcher.out.log"
        with out_path.open("ab") as out:
            process = subprocess.Popen(
                tunnel_command([]),
                cwd=str(ROOT),
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=out,
                creationflags=creation_flags(),
                env=clean_child_env(),
            )
        log(f"隧道守护进程 PID={process.pid}")
    except Exception as error:
        log(f"启动隧道失败: {error!r}")
        return False

    deadline = time.time() + TUNNEL_WAIT
    while time.time() < deadline:
        if process.poll() is not None:
            log(f"隧道进程提前退出（code={process.returncode}），详见 data/tunnel.log")
            return False
        url = tunnel_state_url()
        if url and tunnel_public_ok(url):
            log(f"隧道就绪：{url}")
            return True
        time.sleep(1.5)
    log(f"隧道 {TUNNEL_WAIT} 秒内未就绪，先打开网页（局域网可用）；详见 data/tunnel.log")
    return False


def main() -> None:
    argv = sys.argv[1:]
    # 打包后单 exe 子命令：服务/隧道子进程靠 exe 自拉起（开发态则由 pythonw 拉起）
    if "--serve" in argv:
        from app.cli import run_server

        run_server(argv[argv.index("--serve") + 1:])
        return
    if "--tunnel" in argv:
        from app.cli import run_tunnel

        raise SystemExit(run_tunnel(argv[argv.index("--tunnel") + 1:]))
    if "--version" in argv:
        from app.version import __version__

        print(__version__)
        return
    if "--stop" in argv:
        stop_tunnel()
        stop_service()
        return
    log("快捷方式启动")
    if service_healthy():
        log("服务已在运行，直接打开网页")
        ensure_tunnel()
        open_app()
        return
    wcda_ready = ensure_wcda()
    if not wcda_ready and WCDA_BASE:
        wcda_ready = http_ok(f"{WCDA_BASE}/api/health")
    if not start_service():
        return
    tunnel_ok = ensure_tunnel()
    if TUNNEL_ENABLED and not tunnel_ok:
        show_message(
            "任务表已经打开，但公网隧道还没就绪（局域网访问不受影响）。\n\n"
            "常见原因：还没配置 ngrok authtoken、电脑断网、ngrok 免费额度用完。\n"
            f"排查：{tunnel_hint()}\n日志：{DATA_DIR / 'tunnel.log'}",
        )
    open_app()
    if not wcda_ready:
        show_message(
            "任务表已经打开，但 WeChatDataAnalysis 还没就绪。\n\n"
            "请手动打开 WeChatDataAnalysis 并登录微信；\n"
            "本服务会自动重连，连接后开始接收新消息。",
        )


if __name__ == "__main__":
    # 子进程模式（--serve / --tunnel）没有 UI，异常只落日志、不弹窗（弹窗会卡住子进程）
    _CHILD_MODE = "--serve" in sys.argv or "--tunnel" in sys.argv
    try:
        main()
    except SystemExit:
        raise
    except Exception as error:  # 防止 pythonw / 无控制台 exe 静默崩溃
        log(f"未捕获异常: {error!r}")
        if not _CHILD_MODE:
            show_message(f"启动器异常：{error}\n\n日志：{LOG_FILE}", error=True)
        else:
            sys.exit(1)
