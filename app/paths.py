"""路径唯一事实源：开发态与 PyInstaller 冻结态共用同一套分层约定。

分层契约（详见 docs/目录分层.md）
--------------------------------
开发态（``python run.py``）::

    APP_DIR = 项目根 = wechat-task-hub/
    BUNDLE_DIR = APP_DIR
    web/ assets/ tools/ data/ .env 全在项目根

PyInstaller onedir 冻结态（exe 在 dist/WeChatTaskHub/）::

    APP_DIR    = exe 所在目录（可写安装目录）
    BUNDLE_DIR = sys._MEIPASS（即 _internal/，只读随包资源）
    web/ assets/ tools/ data/ .env 外置于 exe 同级；
    web/ assets/ 找不到时回落到 BUNDLE_DIR（spec 里也随包一份作兜底）。

规则：**除本文件外，app/ 下任何模块都不得再用 ``Path(__file__)`` 推导目录**。
需要路径一律 ``from .paths import ...``；新增路径先在这里定义常量。

环境变量覆盖（相对路径一律相对 APP_DIR 解析）：
    DATA_DIR   运行数据目录（默认 APP_DIR/data）
    ENV_FILE   配置文件（默认 APP_DIR/.env）
    WEB_DIR    静态站点目录（默认 APP_DIR/web，其次 BUNDLE_DIR/web）
    ASSETS_DIR 只读资源目录（默认 APP_DIR/assets，其次 BUNDLE_DIR/assets）
    TOOLS_DIR  工具目录（默认 APP_DIR/tools）

日志／pid／tunnel.json 等运行态文件仍放在 ``DATA_DIR`` 下（沿用既有约定），
因此 ``LOG_DIR`` 只是 ``DATA_DIR`` 的语义别名，不额外分层。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

#: 是否运行在 PyInstaller 冻结产物里
FROZEN: bool = bool(getattr(sys, "frozen", False))


def _app_dir() -> Path:
    """可写安装目录：冻结态 = exe 所在目录；开发态 = 项目根。"""
    if FROZEN:
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _resolve(raw: str | None, default: Path) -> Path:
    """环境变量覆盖：空值用默认；相对路径相对 APP_DIR 解析。"""
    if not raw or not str(raw).strip():
        return default
    candidate = Path(str(raw).strip()).expanduser()
    if not candidate.is_absolute():
        candidate = APP_DIR / candidate
    return candidate


def _first_existing(*candidates: Path) -> Path:
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return candidates[-1]


APP_DIR: Path = _app_dir()
#: 随包只读资源根：开发态同 APP_DIR；冻结态 = _MEIPASS（_internal/）
BUNDLE_DIR: Path = Path(getattr(sys, "_MEIPASS", APP_DIR)).resolve() if FROZEN else APP_DIR

#: 运行数据（SQLite、日志、pid、tunnel.json）；可写，升级时保留
DATA_DIR: Path = _resolve(os.environ.get("DATA_DIR"), APP_DIR / "data")
#: 配置文件；可写，升级时保留
ENV_FILE: Path = _resolve(os.environ.get("ENV_FILE"), APP_DIR / ".env")
#: 前端静态站点：优先 exe 同级（可热更新），其次包内兜底
WEB_DIR: Path = _resolve(
    os.environ.get("WEB_DIR"),
    _first_existing(APP_DIR / "web", BUNDLE_DIR / "web"),
)
#: 只读资源（图标等）
ASSETS_DIR: Path = _resolve(
    os.environ.get("ASSETS_DIR"),
    _first_existing(APP_DIR / "assets", BUNDLE_DIR / "assets"),
)
#: 工具目录（ngrok.exe / cloudflared.exe / tunnel.py 等）；外置，可独立更新
TOOLS_DIR: Path = _resolve(os.environ.get("TOOLS_DIR"), APP_DIR / "tools")
#: 日志目录别名：日志与运行数据同层（沿用 data/*.log）
LOG_DIR: Path = DATA_DIR


def ensure_runtime_dirs() -> None:
    """创建运行期可写目录（幂等）。服务/启动器/隧道启动前都应调用。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def describe() -> dict[str, str]:
    """诊断用：返回当前进程解析出的全部路径。"""
    return {
        "frozen": str(FROZEN),
        "app_dir": str(APP_DIR),
        "bundle_dir": str(BUNDLE_DIR),
        "data_dir": str(DATA_DIR),
        "env_file": str(ENV_FILE),
        "web_dir": str(WEB_DIR),
        "assets_dir": str(ASSETS_DIR),
        "tools_dir": str(TOOLS_DIR),
    }


__all__ = [
    "FROZEN",
    "APP_DIR",
    "BUNDLE_DIR",
    "DATA_DIR",
    "ENV_FILE",
    "WEB_DIR",
    "ASSETS_DIR",
    "TOOLS_DIR",
    "LOG_DIR",
    "ensure_runtime_dirs",
    "describe",
]
