#!/usr/bin/env python3
"""微信待办 · 公网隧道守护进程（ngrok 固定域名 / cloudflared 应急）

做什么：
  把本机 ``http://127.0.0.1:<PORT>`` 通过 ngrok（免费固定 dev domain）暴露成
  ``https://<名字>.ngrok-free.app``，并把当前公网地址写入 ``data/tunnel.json``：

      {"url": "https://xxx.ngrok-free.app", "kind": "ngrok", "updated_at": "2026-10-26T19:30:00"}

  服务端 ``GET /api/app/pairing`` 会读这个文件，把远程地址放进配对码，手机 App
  在外网时自动切到它；本机服务本身仍然只听 127.0.0.1 / 0.0.0.0:8765。

用法：
  python tools/tunnel.py              # 守护模式（launcher.pyw 用 pythonw 无窗口拉起）
  python tools/tunnel.py --once       # 前台调试：日志同时打印到控制台，Ctrl+C 退出
  python tools/tunnel.py --check      # 只打印状态；健康返回 0，不健康返回 1
  python tools/tunnel.py --stop       # 停止守护进程 + 子进程，清理 pid / tunnel.json

依赖（全部为标准库）：
  - 主路径：``tools/ngrok/ngrok.exe``（由 ``tools/ngrok_setup.ps1`` 下载并写 authtoken）
  - 应急：``tools/cloudflared.exe``（免注册快速隧道，地址每次启动都变，仅临时用）

相关环境变量（写在项目根目录 .env，见 .env.example）：
  TUNNEL_ENABLED=1              总开关；0 = 不启用远程隧道（局域网直连不受影响）
  TUNNEL_PROVIDER=ngrok         ngrok（推荐）| cloudflared（应急）
  NGROK_AUTHTOKEN=              可选；更推荐由 tools/ngrok_setup.ps1 写进 ngrok.yml
  NGROK_DOMAIN=                 免费 dev domain，如 xxx.ngrok-free.app；留空 = ngrok 自动分配
  NGROK_BIN=                    可选；ngrok.exe 路径，默认 tools/ngrok/ngrok.exe
  NGROK_CONFIG=                 可选；ngrok.yml 路径，默认 %LOCALAPPDATA%\\ngrok\\ngrok.yml
  CLOUDFLARED_BIN=              可选；cloudflared.exe 路径，默认 tools/cloudflared.exe
  TUNNEL_HEALTH_INTERVAL=60     心跳间隔（秒）
  TUNNEL_MAX_FAILURES=3         连续失败多少次后重启隧道
"""
from __future__ import annotations

import argparse
import atexit
import ctypes
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

# 开发态直接 `python tools\tunnel.py` 时，把项目根放进 sys.path；冻结态由 exe 自带。
_DEV_ROOT = Path(__file__).resolve().parent.parent
if not getattr(sys, "frozen", False) and str(_DEV_ROOT) not in sys.path:
    sys.path.insert(0, str(_DEV_ROOT))

from app.paths import APP_DIR, DATA_DIR, ENV_FILE, TOOLS_DIR  # noqa: E402

ROOT = APP_DIR
LOG_FILE = DATA_DIR / "tunnel.log"
STATE_FILE = DATA_DIR / "tunnel.json"
PID_FILE = DATA_DIR / "tunnel.pid"

DEFAULT_NGROK_BIN = TOOLS_DIR / "ngrok" / "ngrok.exe"
DEFAULT_CLOUDFLARED_BIN = TOOLS_DIR / "cloudflared.exe"
NGROK_API_URL = "http://127.0.0.1:4040/api/tunnels"
NGROK_DASHBOARD_DOMAINS = "https://dashboard.ngrok.com/domains"
USER_AGENT = "wxtask-tunnel/1.0 (wechat-task-hub)"
CREATE_NO_WINDOW = 0x08000000

_CONSOLE = False  # 是否同时输出到控制台（pythonw 下为 False）
_ATEXIT_CHILD = None


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
def _read_env_file(path: Path = ENV_FILE) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        for raw in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return values


_ENV = _read_env_file()


def setting(key: str, default: str = "") -> str:
    return (os.environ.get(key) or _ENV.get(key) or default).strip()


def setting_int(key: str, default: int, minimum: int | None = None) -> int:
    try:
        value = int(setting(key, str(default)))
    except ValueError:
        value = default
    if minimum is not None:
        value = max(minimum, value)
    return value


def setting_bool(key: str, default: bool = False) -> bool:
    raw = setting(key, "1" if default else "0").lower()
    return raw in ("1", "true", "yes", "on")


def _resolve_bin(env_key: str, default_path: Path, command: str) -> Path:
    """按 环境变量 → tools/ 内置 → PATH 的顺序找可执行文件。"""
    raw = setting(env_key)
    candidates: list[Path] = []
    if raw:
        candidate = Path(raw).expanduser()
        candidates.append(candidate if candidate.is_absolute() else ROOT / candidate)
    candidates.append(default_path)
    found = shutil.which(command)
    if found:
        candidates.append(Path(found))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


class Config:
    def __init__(self) -> None:
        self.port = setting_int("PORT", 8765, 1)
        self.enabled = setting_bool("TUNNEL_ENABLED", False)
        self.provider = (setting("TUNNEL_PROVIDER", "ngrok").lower() or "ngrok")
        self.auth_token = setting("NGROK_AUTHTOKEN")
        self.domain = setting("NGROK_DOMAIN").lower().removeprefix("https://").removeprefix("http://").strip("/")
        local_appdata = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        self.ngrok_config = Path(
            setting("NGROK_CONFIG") or str(local_appdata / "ngrok" / "ngrok.yml")
        ).expanduser()
        self.ngrok_bin = _resolve_bin("NGROK_BIN", DEFAULT_NGROK_BIN, "ngrok")
        self.cloudflared_bin = _resolve_bin("CLOUDFLARED_BIN", DEFAULT_CLOUDFLARED_BIN, "cloudflared")
        # 应急用 cloudflared：默认 http2。cloudflared 默认 auto 会优先 QUIC，
        # 实测在 UDP 7844 被限的网络里会「URL 已分配但连接一直超时」，http2 更稳。
        self.cloudflared_protocol = setting("TUNNEL_CLOUDFLARED_PROTOCOL", "http2").lower() or "http2"
        self.health_interval = setting_int("TUNNEL_HEALTH_INTERVAL", 60, 15)
        self.restart_delay = setting_int("TUNNEL_RESTART_DELAY", 5, 2)
        self.max_failures = setting_int("TUNNEL_MAX_FAILURES", 3, 1)
        self.url_wait = setting_int("TUNNEL_URL_WAIT", 45, 10)
        self.verify_timeout = setting_int("TUNNEL_VERIFY_TIMEOUT", 30, 5)
        self.http_timeout = setting_int("TUNNEL_HTTP_TIMEOUT", 15, 5)
        self.local_health_url = f"http://127.0.0.1:{self.port}/api/health"
        # tunnel.json 超过这么久没刷新就认为失效（心跳每次成功都会刷新）
        self.state_ttl = max(self.health_interval * 3, 180)

    def config_has_authtoken(self) -> bool:
        try:
            text = self.ngrok_config.read_text(encoding="utf-8-sig")
        except OSError:
            return False
        return re.search(r"^\s*authtoken\s*:\s*\S+", text, re.MULTILINE) is not None


CFG = Config()


# ---------------------------------------------------------------------------
# 日志与基础工具
# ---------------------------------------------------------------------------
def log(message: str, level: str = "INFO") -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {level} {message}"
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass
    if _CONSOLE and sys.stdout is not None:
        try:
            print(line, flush=True)
        except Exception:
            pass


def out(message: str = "") -> None:
    if sys.stdout is not None:
        try:
            print(message, flush=True)
        except Exception:
            pass


def rotate_log() -> None:
    """日志超过 5MB 时滚动一份（只保留 tunnel.log.1）。"""
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 5 * 1024 * 1024:
            backup = LOG_FILE.with_name(LOG_FILE.name + ".1")
            backup.unlink(missing_ok=True)
            LOG_FILE.replace(backup)
    except OSError:
        pass


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def read_log_since(offset: int) -> str:
    """从日志的某个字节偏移往后读（最多 256KB）。"""
    try:
        size = LOG_FILE.stat().st_size
        if size <= offset:
            return ""
        with LOG_FILE.open("rb") as handle:
            handle.seek(max(offset, size - 256 * 1024))
            return handle.read().decode("utf-8", "replace")
    except OSError:
        return ""


def log_tail(max_bytes: int = 64 * 1024) -> str:
    """读日志尾部（用于识别 authtoken 无效这类「重试也没用」的错误）。"""
    try:
        size = LOG_FILE.stat().st_size
        with LOG_FILE.open("rb") as handle:
            handle.seek(max(0, size - max_bytes))
            return handle.read().decode("utf-8", "replace")
    except OSError:
        return ""


# 这些错误重试也不会好，需要用户去修配置：命中后加大重试间隔并给出明确提示
_FATAL_LOG_PATTERNS = (
    ("ERR_NGROK_105", "ngrok authtoken 无效或格式不对：请重新运行 tools\\ngrok_setup.ps1 填正确 token"),
    ("authentication failed", "ngrok 认证失败：authtoken 可能已失效，请重新运行 tools\\ngrok_setup.ps1"),
    ("ERR_NGROK_121", "ngrok 账号会话数超限（同一账号同时开了太多隧道），先关掉其它 ngrok"),
    ("failed to start tunnel", "ngrok 启动隧道失败，详见 data\\tunnel.log"),
)


def fatal_hint(since_offset: int | None = None) -> str | None:
    text = log_tail() if since_offset is None else read_log_since(since_offset)
    for pattern, hint in _FATAL_LOG_PATTERNS:
        if pattern in text:
            return hint
    return None


def _http_get(url: str, timeout: float, headers: dict[str, str] | None = None) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return int(getattr(response, "status", 200)), response.read()


def check_health(url: str, timeout: float | None = None) -> tuple[bool, str]:
    """GET <url>，要求 2xx 且 JSON 里 ok=true。

    带上 ``ngrok-skip-browser-warning``：ngrok 免费版只对浏览器 HTML 访问插
    提示页，带这个头（任意值）即可跳过；API 客户端本来也不受影响。
    """
    headers = {
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
        "ngrok-skip-browser-warning": "1",
        "Cache-Control": "no-store",
    }
    try:
        status, body = _http_get(url, timeout or CFG.http_timeout, headers)
    except urllib.error.HTTPError as error:
        return False, f"HTTP {error.code}"
    except Exception as error:  # URLError / socket.timeout / OSError
        return False, f"{type(error).__name__}: {error}"
    if not 200 <= status < 300:
        return False, f"HTTP {status}"
    try:
        payload = json.loads(body.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        snippet = body[:120].decode("utf-8", "replace").replace("\n", " ").strip()
        return False, f"非 JSON 响应（可能是 ngrok 提示页）：{snippet!r}"
    if isinstance(payload, dict) and payload.get("ok") is True:
        return True, "ok"
    return False, "JSON 中 ok 不为 true"


def sleep_interruptible(seconds: float, supervisor: "Supervisor | None" = None) -> bool:
    """可被 Ctrl+C / --stop 打断的 sleep；返回 False 表示收到停止请求。"""
    deadline = time.time() + seconds
    while time.time() < deadline:
        if supervisor is not None and supervisor.stop_requested:
            return False
        time.sleep(min(1.0, max(0.05, deadline - time.time())))
    return True


# ---------------------------------------------------------------------------
# 状态文件 / pid 文件
# ---------------------------------------------------------------------------
def read_state() -> dict | None:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not str(data.get("url") or "").startswith("https://"):
        return None
    return data


def write_state(url: str, kind: str) -> None:
    payload = {"url": url.rstrip("/"), "kind": kind, "updated_at": now_str()}
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, STATE_FILE)


def state_age_seconds(state: dict) -> float:
    try:
        updated = datetime.fromisoformat(str(state.get("updated_at") or ""))
    except ValueError:
        return float("inf")
    return max(0.0, (datetime.now() - updated).total_seconds())


def _process_alive(pid) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            code = ctypes.c_ulong()
            ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            ctypes.windll.kernel32.CloseHandle(handle)
            return code.value == 259  # STILL_ACTIVE
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def read_pid_file() -> dict:
    try:
        raw = PID_FILE.read_text(encoding="utf-8-sig").strip()
    except OSError:
        return {}
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    if raw.isdigit():  # 兼容早期「只写一个 pid」的格式
        return {"supervisor_pid": int(raw)}
    return {}


def write_pid_file(info: dict) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        PID_FILE.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    except OSError as error:
        log(f"写 pid 文件失败：{error}", "WARN")


def remove_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def kill_tree(pid, timeout: float = 6.0) -> bool:
    """结束某个进程及其子进程（Windows 用 taskkill /T /F）。"""
    if not _process_alive(pid):
        return False
    log(f"结束进程树 PID={pid}")
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(int(pid)), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        try:
            os.kill(int(pid), signal.SIGTERM)
        except OSError:
            pass
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _process_alive(pid):
            return True
        time.sleep(0.2)
    return not _process_alive(pid)


# ---------------------------------------------------------------------------
# 隧道提供方（ngrok / cloudflared）
# ---------------------------------------------------------------------------
def _creation_flags() -> int:
    return CREATE_NO_WINDOW if os.name == "nt" and not _CONSOLE else 0


def _child_env() -> dict[str, str]:
    env = os.environ.copy()
    for key in ("ELECTRON_RUN_AS_NODE", "ELECTRON_NO_ATTACH_CONSOLE", "NODE_OPTIONS"):
        env.pop(key, None)
    if CFG.auth_token:
        env["NGROK_AUTHTOKEN"] = CFG.auth_token
    return env


def ngrok_public_url(timeout: float = 3.0) -> tuple[str | None, str]:
    """从 ngrok 本地 4040 API 读当前 https 公网地址。"""
    try:
        _, body = _http_get(NGROK_API_URL, timeout, {"Accept": "application/json", "User-Agent": USER_AGENT})
        payload = json.loads(body.decode("utf-8", "replace"))
    except Exception as error:
        return None, f"无法访问 127.0.0.1:4040（ngrok 未运行或端口被其它 ngrok 占用）：{type(error).__name__}"
    candidates: list[tuple[int, str]] = []
    for tunnel in payload.get("tunnels") or []:
        public_url = str(tunnel.get("public_url") or "").rstrip("/")
        if not public_url.startswith("https://"):
            continue
        addr = str((tunnel.get("config") or {}).get("addr") or "")
        score = 0
        if f":{CFG.port}" in addr:
            score += 1
        if CFG.domain and CFG.domain in public_url:
            score += 2
        candidates.append((score, public_url))
    if not candidates:
        return None, "ngrok 已启动但还没有 https 隧道（或 authtoken 无效）"
    candidates.sort(key=lambda item: -item[0])
    return candidates[0][1], "ok"


class NgrokProvider:
    name = "ngrok"

    def available(self) -> tuple[bool, str]:
        if not CFG.ngrok_bin.is_file():
            return False, (
                f"找不到 ngrok.exe：{CFG.ngrok_bin}。"
                "请先运行 tools\\ngrok_setup.ps1（会自动下载），或设置 NGROK_BIN"
            )
        if not CFG.auth_token and not CFG.config_has_authtoken():
            return False, (
                "还没有 ngrok authtoken：请运行 tools\\ngrok_setup.ps1 引导配置"
                f"（免费注册：{NGROK_DASHBOARD_DOMAINS}）"
            )
        return True, "ok"

    def describe(self) -> str:
        return f"ngrok.exe = {CFG.ngrok_bin}；ngrok.yml = {CFG.ngrok_config}"

    def command(self, port: int) -> list[str]:
        args = [
            str(CFG.ngrok_bin),
            "http",
            str(port),
            "--log",
            "stdout",
            "--log-format",
            "logfmt",
            "--log-level",
            "info",
        ]
        if CFG.ngrok_config.is_file():
            args += ["--config", str(CFG.ngrok_config)]
        if CFG.domain:
            args += ["--url", f"https://{CFG.domain}"]
        return args

    def discover_url(self, child: subprocess.Popen, log_offset: int) -> tuple[str | None, str]:
        deadline = time.time() + CFG.url_wait
        reason = "超时"
        while time.time() < deadline:
            if child.poll() is not None:
                return None, f"ngrok 进程已退出（code={child.returncode}），详见 {LOG_FILE}"
            url, reason = ngrok_public_url()
            if url:
                return url, "ok"
            time.sleep(0.5)
        return None, f"{CFG.url_wait} 秒内没拿到公网地址：{reason}"

    def refresh_url(self, current_url: str) -> tuple[str | None, str]:
        url, reason = ngrok_public_url()
        if url is None:
            return None, reason
        return url, "ok"

    def release(self) -> None:
        """ngrok 正常退出会自动注销隧道，无需额外调用。"""


class CloudflaredProvider:
    """应急方案：cloudflared 快速隧道（免注册，但地址每次启动都变）。"""

    name = "cloudflared"
    URL_PATTERN = re.compile(r"https://[A-Za-z0-9][A-Za-z0-9-]*\.trycloudflare\.com")

    def available(self) -> tuple[bool, str]:
        if not CFG.cloudflared_bin.is_file():
            return False, (
                f"找不到 cloudflared.exe：{CFG.cloudflared_bin}。应急方案需先下载："
                "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
                "（保存为 tools\\cloudflared.exe）"
            )
        return True, "ok"

    def describe(self) -> str:
        return f"cloudflared.exe = {CFG.cloudflared_bin}（应急：地址每次启动都会变）"

    def command(self, port: int) -> list[str]:
        args = [
            str(CFG.cloudflared_bin),
            "tunnel",
            "--url",
            f"http://127.0.0.1:{port}",
            "--no-autoupdate",
            "--loglevel",
            "info",
        ]
        if CFG.cloudflared_protocol in ("auto", "http2", "quic"):
            args += ["--protocol", CFG.cloudflared_protocol]
        return args

    def _read_since(self, offset: int) -> str:
        return read_log_since(offset)

    def _url_from_log(self, offset: int) -> str | None:
        matches = self.URL_PATTERN.findall(self._read_since(offset))
        return matches[-1] if matches else None

    def _connection_registered(self, offset: int) -> bool:
        return "Registered tunnel connection" in self._read_since(offset)

    def discover_url(self, child: subprocess.Popen, log_offset: int) -> tuple[str | None, str]:
        deadline = time.time() + CFG.url_wait
        url: str | None = None
        while time.time() < deadline:
            if child.poll() is not None:
                return None, f"cloudflared 进程已退出（code={child.returncode}），详见 {LOG_FILE}"
            url = self._url_from_log(log_offset)
            # 日志里出现 URL 时隧道往往还没注册完成，等 "Registered tunnel connection" 再返回
            if url and self._connection_registered(log_offset):
                return url, "ok"
            time.sleep(0.5)
        if url:
            return None, f"已分配地址 {url}，但 {CFG.url_wait} 秒内未注册成功（可试 TUNNEL_CLOUDFLARED_PROTOCOL=quic）"
        return None, f"{CFG.url_wait} 秒内没从日志里解析到 trycloudflare.com 地址"

    def refresh_url(self, current_url: str) -> tuple[str | None, str]:
        # cloudflared 快速隧道地址固定到进程生命周期结束，无需刷新
        return current_url, "ok"

    def release(self) -> None:
        pass


def _providers() -> dict[str, object]:
    return {"ngrok": NgrokProvider(), "cloudflared": CloudflaredProvider()}


def _provider():
    providers = _providers()
    provider = providers.get(CFG.provider)
    if provider is None:
        # 未知 provider：退回 ngrok，并留下清晰日志
        log(f"未知 TUNNEL_PROVIDER={CFG.provider!r}，按 ngrok 处理", "WARN")
        return providers["ngrok"]
    return provider


# ---------------------------------------------------------------------------
# 守护进程
# ---------------------------------------------------------------------------
class Supervisor:
    def __init__(self, provider) -> None:
        self.provider = provider
        self.child: subprocess.Popen | None = None
        self.stop_requested = False

    # -- 信号 -------------------------------------------------------------
    def _handle_signal(self, *_args) -> None:
        self.stop_requested = True
        log("收到退出信号，准备停止隧道", "WARN")

    def _install_signals(self) -> None:
        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                signal.signal(sig, self._handle_signal)
            except (OSError, ValueError):
                pass

    # -- 进程 -------------------------------------------------------------
    def _spawn(self) -> subprocess.Popen:
        args = self.provider.command(CFG.port)
        log("启动隧道进程：" + " ".join(f'"{a}"' if " " in a else a for a in args))
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        handle = LOG_FILE.open("ab")
        try:
            child = subprocess.Popen(
                args,
                cwd=str(ROOT),
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=subprocess.STDOUT,
                creationflags=_creation_flags(),
                env=_child_env(),
            )
        finally:
            handle.close()  # 子进程已继承句柄，父进程这边可以关掉
        global _ATEXIT_CHILD
        _ATEXIT_CHILD = child
        return child

    def _terminate_child(self) -> None:
        global _ATEXIT_CHILD
        child, self.child = self.child, None
        _ATEXIT_CHILD = None
        if child is None:
            return
        if child.poll() is None:
            log("停止隧道子进程", "INFO")
            try:
                child.terminate()
            except OSError:
                pass
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                kill_tree(child.pid)
        try:
            self.provider.release()
        except Exception:
            pass

    def _write_pid(self, child_pid: int | None) -> None:
        write_pid_file(
            {
                "supervisor_pid": os.getpid(),
                "child_pid": child_pid,
                "provider": self.provider.name,
                "started_at": now_str(),
            }
        )

    def _cleanup_files(self) -> None:
        info = read_pid_file()
        if int(info.get("supervisor_pid") or 0) == os.getpid():
            remove_file(PID_FILE)
        # 隧道已停止：删掉公网地址，避免服务端 pairing 继续发一个死地址
        remove_file(STATE_FILE)

    # -- 主循环 -----------------------------------------------------------
    def run(self) -> int:
        ok, reason = self.provider.available()
        if not ok:
            log("隧道无法启动：" + reason, "ERROR")
            out("隧道无法启动：" + reason)
            return 2

        # 防重复：已有守护进程在跑就直接退出，由调用方决定是否先 --stop
        existing = read_pid_file()
        existing_pid = int(existing.get("supervisor_pid") or 0)
        if existing_pid and existing_pid != os.getpid() and _process_alive(existing_pid):
            message = f"已有隧道进程在运行（PID={existing_pid}），本次不重复启动"
            log(message, "WARN")
            out(message)
            return 3
        # 清理上一轮遗留的子进程（守护进程被强杀时可能出现）
        leftover_child = int(existing.get("child_pid") or 0)
        if leftover_child and leftover_child != os.getpid() and _process_alive(leftover_child):
            log(f"发现上一轮遗留的隧道子进程 PID={leftover_child}，先清理", "WARN")
            kill_tree(leftover_child)
        remove_file(PID_FILE)

        self._install_signals()
        rotate_log()
        log("=" * 60)
        log(f"隧道守护启动：provider={self.provider.name} port={CFG.port} 本机={CFG.local_health_url}")
        log(self.provider.describe())
        if CFG.provider == "ngrok" and not CFG.domain:
            log(f"未设置 NGROK_DOMAIN：使用 ngrok 账号自动分配的免费 dev domain（见 {NGROK_DASHBOARD_DOMAINS}）")
        self._write_pid(None)

        backoff = CFG.restart_delay
        try:
            while not self.stop_requested:
                url: str | None = None
                fatal_seen = False
                try:
                    offset = LOG_FILE.stat().st_size if LOG_FILE.exists() else 0
                    child = self._spawn()
                    self.child = child
                    self._write_pid(child.pid)
                    url, reason = self.provider.discover_url(child, offset)
                    if not url:
                        log("获取公网地址失败：" + reason, "WARN")
                        hint = fatal_hint(offset)
                        if hint:
                            fatal_seen = True
                            log("需要人工处理：" + hint, "ERROR")
                    else:
                        log(f"拿到公网地址：{url}，验证公网链路…")
                        verified, verify_reason = self._verify_public(url, CFG.verify_timeout)
                        if not verified:
                            log(f"公网链路验证失败（{verify_reason}），稍后重启隧道重试", "WARN")
                        else:
                            write_state(url, self.provider.name)
                            log(f"隧道就绪：{url} → 已写入 {STATE_FILE}")
                            backoff = CFG.restart_delay
                            self._heartbeat_loop(url, child)
                except KeyboardInterrupt:
                    self.stop_requested = True
                except Exception as error:  # 守护进程不能因为单次异常退出
                    log(f"隧道循环异常：{error!r}", "ERROR")
                finally:
                    self._terminate_child()
                if self.stop_requested:
                    break
                delay = max(backoff, 300) if fatal_seen else backoff
                log(f"{delay} 秒后重启隧道" + ("（配置问题，慢速重试）" if fatal_seen else ""), "WARN")
                if not sleep_interruptible(delay, self):
                    break
                backoff = max(backoff, 300) if fatal_seen else min(backoff * 2, 60)
        finally:
            self._terminate_child()
            self._cleanup_files()
            log("隧道守护已退出")
        return 0

    def _verify_public(self, url: str, timeout: int) -> tuple[bool, str]:
        deadline = time.time() + timeout
        reason = "超时"
        while time.time() < deadline:
            ok, reason = check_health(url.rstrip("/") + "/api/health")
            if ok:
                return True, "ok"
            if not sleep_interruptible(2, self):
                return False, "收到停止请求"
        return False, reason

    def _heartbeat_loop(self, url: str, child: subprocess.Popen) -> None:
        """隧道就绪后每隔 health_interval 秒校验一次；异常时返回由外层重启。"""
        failures = 0
        while not self.stop_requested:
            if not sleep_interruptible(CFG.health_interval, self):
                return
            if child.poll() is not None:
                log(f"隧道进程已退出（code={child.returncode}），准备重启", "WARN")
                return
            # 地址可能变化（ngrok 重建等），先刷新
            new_url, _ = self.provider.refresh_url(url)
            if new_url and new_url.rstrip("/") != url.rstrip("/"):
                url = new_url.rstrip("/")
                log(f"公网地址变化，更新为：{url}", "WARN")
            public_ok, public_reason = check_health(url.rstrip("/") + "/api/health")
            local_ok, local_reason = check_health(CFG.local_health_url, timeout=5)
            if public_ok:
                if failures:
                    log(f"公网链路恢复：{url}")
                failures = 0
                write_state(url, self.provider.name)
            elif not local_ok:
                # 本机服务没起来（比如正在重启）时不要重启隧道，等它回来
                log(f"本机服务不可用（{local_reason}），暂时跳过公网检查", "WARN")
            else:
                failures += 1
                log(
                    f"公网健康检查失败（{public_reason}），连续 {failures}/{CFG.max_failures} 次",
                    "WARN",
                )
                if failures >= CFG.max_failures:
                    log("公网连续失败，重启隧道进程", "WARN")
                    return


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------
def cmd_check() -> int:
    provider = _provider()
    lines: list[str] = []
    problems: list[str] = []

    lines.append(
        f"隧道开关：{'已启用' if CFG.enabled else '未启用'}（TUNNEL_ENABLED={'1' if CFG.enabled else '0'}），"
        f"provider={provider.name}，本机端口={CFG.port}"
    )
    if not CFG.enabled:
        lines.append("说明：TUNNEL_ENABLED=0，远程隧道不会启动（局域网直连/网页访问不受影响）。")
        lines.append("     需要远程访问时：运行 tools\\ngrok_setup.ps1，然后把 .env 的 TUNNEL_ENABLED 改成 1。")
        out("\n".join(lines))
        return 0

    ok, reason = provider.available()
    if ok:
        lines.append(f"依赖：✓ {provider.describe()}")
    else:
        lines.append(f"依赖：✗ {reason}")
        problems.append(reason)

    info = read_pid_file()
    supervisor_pid = int(info.get("supervisor_pid") or 0)
    child_pid = int(info.get("child_pid") or 0)
    if supervisor_pid and _process_alive(supervisor_pid):
        child_note = f"，隧道子进程 PID={child_pid} 存活" if child_pid and _process_alive(child_pid) else ""
        lines.append(f"守护进程：✓ 运行中 PID={supervisor_pid}{child_note}")
    else:
        lines.append("守护进程：✗ 未运行")
        problems.append("守护进程未运行")

    state = read_state()
    if state:
        age = state_age_seconds(state)
        age_text = "未知" if age == float("inf") else f"{age:.0f} 秒前"
        lines.append(f"状态文件：✓ {state.get('url')}（kind={state.get('kind')}，更新于 {age_text}）")
        if age == float("inf") or age > CFG.state_ttl:
            problems.append(f"data/tunnel.json 已过期（{age_text}，阈值 {CFG.state_ttl} 秒）")
    else:
        lines.append("状态文件：✗ 没有 data/tunnel.json（还没有可用的公网地址）")
        problems.append("data/tunnel.json 缺失")

    local_ok, local_reason = check_health(CFG.local_health_url, timeout=5)
    lines.append(f"本机服务：{'✓' if local_ok else '✗'} {CFG.local_health_url}（{local_reason}）")
    if not local_ok:
        problems.append("本机 8765 服务不可用")

    if provider.name == "ngrok" and supervisor_pid and _process_alive(supervisor_pid):
        api_url, api_reason = ngrok_public_url(timeout=3)
        lines.append(
            f"ngrok 4040 API：{'✓ ' + api_url if api_url else '✗ ' + api_reason}"
        )
        if not api_url and CFG.enabled:
            problems.append("ngrok 本地 API 没有隧道")
            hint = fatal_hint()
            if hint:
                problems.append(hint)

    if state and local_ok:
        public_url = str(state.get("url")).rstrip("/") + "/api/health"
        public_ok, public_reason = check_health(public_url, timeout=min(CFG.http_timeout, 10))
        lines.append(f"公网链路：{'✓' if public_ok else '✗'} {public_url}（{public_reason}）")
        if not public_ok:
            problems.append("公网 /api/health 不通")

    out("\n".join(lines))
    if problems:
        out("结论：不健康")
        for problem in dict.fromkeys(problems):
            out(f"  - {problem}")
        return 1
    out("结论：健康")
    return 0


def cmd_stop() -> int:
    info = read_pid_file()
    supervisor_pid = int(info.get("supervisor_pid") or 0)
    child_pid = int(info.get("child_pid") or 0)
    killed: list[int] = []
    if supervisor_pid and supervisor_pid != os.getpid() and _process_alive(supervisor_pid):
        if kill_tree(supervisor_pid):
            killed.append(supervisor_pid)
    if child_pid and child_pid != os.getpid() and _process_alive(child_pid):
        if kill_tree(child_pid):
            killed.append(child_pid)
    had_state = read_state() is not None
    remove_file(PID_FILE)
    remove_file(STATE_FILE)
    if killed:
        log(f"--stop 已结束隧道进程：{killed}")
        out(f"已停止隧道进程：{', '.join(str(pid) for pid in killed)}")
    elif supervisor_pid or child_pid:
        out("隧道进程已经不在了，已清理 pid / tunnel.json 残留文件")
    else:
        out("没有发现正在运行的隧道进程（已清理可能残留的 pid / tunnel.json）")
    if had_state:
        out("已删除 data/tunnel.json：手机 App 将不再拿到远程地址（恢复后重新生成）")
    return 0


def _atexit_cleanup() -> None:
    child = _ATEXIT_CHILD
    if child is None:
        return
    if child.poll() is None:
        try:
            child.terminate()
            child.wait(timeout=3)
        except Exception:
            try:
                kill_tree(child.pid)
            except Exception:
                pass


def main(argv: list[str] | None = None) -> int:
    global _CONSOLE
    parser = argparse.ArgumentParser(
        description="微信待办 · 公网隧道守护（ngrok 固定域名）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python tools\\tunnel.py              # 守护模式（被桌面快捷方式/launcher 拉起）\n"
            "  python tools\\tunnel.py --once       # 前台调试（Ctrl+C 退出）\n"
            "  python tools\\tunnel.py --check      # 查看当前隧道状态\n"
            "  python tools\\tunnel.py --stop       # 停止隧道\n"
        ),
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--once", action="store_true", help="前台调试模式：日志同时输出到控制台")
    group.add_argument("--check", action="store_true", help="只打印状态（健康返回 0，不健康返回 1）")
    group.add_argument("--stop", action="store_true", help="停止守护进程和子进程，清理 pid/tunnel.json")
    args = parser.parse_args(argv)

    default_console = bool(
        sys.stdout is not None and getattr(sys.stdout, "isatty", lambda: False)()
    )
    _CONSOLE = default_console or args.once
    atexit.register(_atexit_cleanup)

    if args.check:
        return cmd_check()
    if args.stop:
        return cmd_stop()
    if args.once:
        out(f"前台调试模式：日志同时写入 {LOG_FILE}，按 Ctrl+C 退出")
    supervisor = Supervisor(_provider())
    return supervisor.run()


if __name__ == "__main__":
    sys.exit(main())
