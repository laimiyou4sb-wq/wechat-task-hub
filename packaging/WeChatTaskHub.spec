# -*- mode: python ; coding: utf-8 -*-
"""WeChatTaskHub · PyInstaller 打包描述（onedir / windowed）。

固定契约
--------
入口：项目根 ``launcher.pyw``（无控制台窗口）。
形态：onedir，产物 ``dist/WeChatTaskHub/WeChatTaskHub.exe`` + ``_internal/``。
资源：``web/``、``assets/`` 随包进 ``_internal/`` 作兜底；运行时优先使用
      exe 同级的外置 ``web/``、``assets/``（前端可热更新）。
工具：``tools/`` 只外置（ngrok.exe / cloudflared.exe 体积大，不进包），
      但 ``tools/tunnel.py`` 作为 Python 模块被 hiddenimport 打进包内，
      供 ``WeChatTaskHub.exe --tunnel`` 调用。

构建入口：``packaging/build.ps1``（不要直接手敲 pyinstaller 参数）。
"""
from pathlib import Path
import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# SPECPATH 是本 spec 所在目录（packaging/），项目根是其上一级。
PROJECT_ROOT = Path(SPECPATH).parent
APP_DIR = PROJECT_ROOT / "app"
WEB_DIR = PROJECT_ROOT / "web"
ASSETS_DIR = PROJECT_ROOT / "assets"
ICON_FILE = ASSETS_DIR / "wechat-task.ico"
ENTRY_FILE = PROJECT_ROOT / "launcher.pyw"

# ---------------------------------------------------------------------------
# hiddenimports
#
# 1) app/*.py：入口通过 ``app.cli``、uvicorn 字符串导入等方式间接加载模块，
#    静态分析容易漏；直接扫描目录，把所有业务模块显式纳入。
# 2) openai / brotli：第三方可选依赖（brotli 在 try/except 里 import，静态
#    分析可能忽略，显式列出保证压缩中间件可用）。
# 3) tzdata：Windows 上没有系统时区库，zoneinfo 依赖 tzdata 数据包。
# 4) tools.tunnel：隧道脚本无 __init__.py（隐式命名空间包），但会被
#    app/cli.py 以 ``from tools.tunnel import main`` 导入，必须显式纳入。
# 5) uvicorn 全量子模块：uvicorn 大量使用动态导入（协议/日志/lifespan），
#    用 collect_submodules 一次收全。
# ---------------------------------------------------------------------------
app_modules = [
    f"app.{path.stem}"
    for path in sorted(APP_DIR.glob("*.py"))
    if path.stem != "__init__"
]

hiddenimports = []
hiddenimports += ["app"]
hiddenimports += app_modules
hiddenimports += [
    "openai",
    "brotli",
    "tzdata",
    "tools.tunnel",
]
hiddenimports += collect_submodules("uvicorn")

# ---------------------------------------------------------------------------
# datas：随包只读资源进 _internal/
#
# web/、assets/ 同时外置在 exe 同级（由 build.ps1 复制），包内这份只在
# 外置目录被删/被改坏时兜底（app/paths.py 的 _first_existing 回落逻辑）。
# tzdata 的 zoneinfo 数据同样必须随包。
# ---------------------------------------------------------------------------
datas = [
    (str(WEB_DIR), "web"),
    (str(ASSETS_DIR), "assets"),
]
datas += collect_data_files("tzdata")

# build.ps1 生成 version_info.txt 后通过环境变量传入；缺失时不带版本资源也能构建。
_version_file = os.environ.get("WECHAT_TASK_VERSION_FILE", "").strip()
if not _version_file or not Path(_version_file).is_file():
    _version_file = None

a = Analysis(
    [str(ENTRY_FILE)],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="WeChatTaskHub",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # GUI 子系统：双击不弹黑框
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON_FILE) if ICON_FILE.is_file() else None,
    version=_version_file,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="WeChatTaskHub",
)
