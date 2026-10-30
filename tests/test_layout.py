"""目录分层契约测试（对应 docs/目录分层.md 与 app/paths.py）。

覆盖任务要求的五类断言：

a) 开发态路径常量：APP_DIR=项目根、DATA_DIR=根/data、ENV_FILE=根/.env、
   WEB_DIR=根/web、ASSETS_DIR=根/assets、TOOLS_DIR=根/tools，
   ensure_runtime_dirs() 幂等；
b) PyInstaller onedir 冻结态模拟：reload app.paths 后 APP_DIR=exe 所在目录、
   BUNDLE_DIR=_MEIPASS、exe 同级 web/ 存在时优先外部、缺失时回落 BUNDLE_DIR/web；
c) config.Settings.load() 对 DATA_DIR 环境变量的覆盖（相对路径相对 APP_DIR、
   绝对路径原样）；
d) 静态检查：app/ 下 ``Path(__file__)`` 只允许出现在 app/paths.py；
e) 入口冒烟：``python -m app.cli version`` 与 ``python launcher.pyw --version``。

冻结态模拟会 reload ``app.paths``，因此必须用 try/finally 恢复 sys 属性与
环境变量，并 reload 回开发态，避免污染同一次 pytest 会话里的其它测试。
本文件只读项目文件，不写入项目目录（临时文件都在 pytest tmp_path 下）。
"""
from __future__ import annotations

import importlib
import os
import re
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import app.paths as paths
from app.version import __version__

PROJECT_ROOT = Path(__file__).resolve().parent.parent
APP_DIR = PROJECT_ROOT / "app"

#: app/paths.py 支持的环境变量覆盖项；冻结态模拟前要清空才能验证默认推导。
PATH_ENV_KEYS = ("DATA_DIR", "ENV_FILE", "WEB_DIR", "ASSETS_DIR", "TOOLS_DIR")
_SYS_ATTRS = ("frozen", "_MEIPASS", "executable")

#: import 时的初始状态（conftest 会先设置临时 DATA_DIR/ENV_FILE，那是测试环境
#: 的正常开发态），用于断言每个会 reload 的测试结束后完全复原。
_RESTORE_ATTRS = (
    "FROZEN", "APP_DIR", "BUNDLE_DIR", "DATA_DIR",
    "ENV_FILE", "WEB_DIR", "ASSETS_DIR", "TOOLS_DIR",
)
_INITIAL_STATE = {attr: getattr(paths, attr) for attr in _RESTORE_ATTRS}
_INITIAL_SYS = {name: (hasattr(sys, name), getattr(sys, name, None)) for name in _SYS_ATTRS}


def _assert_dev_state_restored() -> None:
    """断言 reload 结束后 app.paths 与 sys 都回到 import 时的开发态。"""
    for attr in _RESTORE_ATTRS:
        assert getattr(paths, attr) == _INITIAL_STATE[attr], attr
    for name, (had_attr, value) in _INITIAL_SYS.items():
        assert hasattr(sys, name) == had_attr, f"sys.{name} 未恢复"
        if had_attr:
            assert getattr(sys, name) == value, f"sys.{name} 未恢复"


@contextmanager
def _reloaded_paths(*, clear_env=False, env=None, frozen=None,
                    meipass=None, executable=None):
    """临时改环境/运行形态后 reload app.paths；退出时严格复原并 reload 回来。

    clear_env: 删除所有路径覆盖环境变量（验证默认推导）；
    env:       额外设置的覆盖环境变量（值会转成字符串）；
    frozen / meipass / executable: 模拟 PyInstaller 冻结态用的 sys 属性。
    """
    saved_env = {key: os.environ.get(key) for key in PATH_ENV_KEYS}
    try:
        if clear_env:
            for key in PATH_ENV_KEYS:
                os.environ.pop(key, None)
        for key, value in (env or {}).items():
            os.environ[key] = str(value)
        if frozen is not None:
            sys.frozen = bool(frozen)
        if meipass is not None:
            sys._MEIPASS = str(meipass)
        if executable is not None:
            sys.executable = str(executable)
        yield importlib.reload(paths)
    finally:
        # 先恢复 sys 属性：原本不存在的属性必须删除，不能留下 frozen/_MEIPASS
        for name, (had_attr, value) in _INITIAL_SYS.items():
            if had_attr:
                setattr(sys, name, value)
            elif hasattr(sys, name):
                delattr(sys, name)
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        importlib.reload(paths)


# ---------------------------------------------------------------------------
# a) 开发态路径常量
# ---------------------------------------------------------------------------
def test_dev_path_constants():
    with _reloaded_paths(clear_env=True) as p:
        assert p.FROZEN is False
        assert p.APP_DIR == PROJECT_ROOT
        assert p.BUNDLE_DIR == PROJECT_ROOT
        assert p.DATA_DIR == PROJECT_ROOT / "data"
        assert p.ENV_FILE == PROJECT_ROOT / ".env"
        assert p.WEB_DIR == PROJECT_ROOT / "web"
        assert p.ASSETS_DIR == PROJECT_ROOT / "assets"
        assert p.TOOLS_DIR == PROJECT_ROOT / "tools"
        assert p.LOG_DIR == p.DATA_DIR

        info = p.describe()
        assert set(info) == {
            "frozen", "app_dir", "bundle_dir", "data_dir",
            "env_file", "web_dir", "assets_dir", "tools_dir",
        }
        assert info["frozen"] == "False"
        assert info["app_dir"] == str(PROJECT_ROOT)
        assert info["web_dir"] == str(PROJECT_ROOT / "web")
    _assert_dev_state_restored()


def test_ensure_runtime_dirs_idempotent(tmp_path):
    data_dir = tmp_path / "runtime-data"
    with _reloaded_paths(clear_env=True, env={"DATA_DIR": str(data_dir)}) as p:
        assert p.DATA_DIR == data_dir
        assert not data_dir.exists()
        p.ensure_runtime_dirs()
        assert data_dir.is_dir()
        p.ensure_runtime_dirs()  # 幂等：已存在时不报错
        assert data_dir.is_dir()
    _assert_dev_state_restored()


# ---------------------------------------------------------------------------
# b) 冻结态（PyInstaller onedir）模拟
# ---------------------------------------------------------------------------
def test_frozen_layout_and_web_fallback(tmp_path):
    install_dir = tmp_path / "WeChatTaskHub"
    bundle_dir = tmp_path / "_internal"
    install_dir.mkdir()
    (bundle_dir / "web").mkdir(parents=True)
    (bundle_dir / "assets").mkdir(parents=True)
    exe = install_dir / "WeChatTaskHub.exe"
    exe.write_bytes(b"stub")

    with _reloaded_paths(clear_env=True, frozen=True,
                         meipass=bundle_dir, executable=exe) as p:
        assert p.FROZEN is True
        assert p.APP_DIR == install_dir.resolve()
        assert p.BUNDLE_DIR == bundle_dir.resolve()
        assert p.DATA_DIR == install_dir / "data"
        assert p.ENV_FILE == install_dir / ".env"
        assert p.TOOLS_DIR == install_dir / "tools"
        assert p.describe()["frozen"] == "True"

        # exe 同级 web/、assets/ 存在 → 优先外部（可热更新）
        (install_dir / "web").mkdir()
        (install_dir / "assets").mkdir()
        p = importlib.reload(paths)
        assert p.WEB_DIR == install_dir / "web"
        assert p.ASSETS_DIR == install_dir / "assets"

        # exe 同级缺失 → 回落包内兜底 BUNDLE_DIR/web、BUNDLE_DIR/assets
        (install_dir / "web").rmdir()
        (install_dir / "assets").rmdir()
        p = importlib.reload(paths)
        assert p.WEB_DIR == bundle_dir / "web"
        assert p.ASSETS_DIR == bundle_dir / "assets"
    _assert_dev_state_restored()


# ---------------------------------------------------------------------------
# c) config.Settings.load() 的 DATA_DIR 覆盖
# ---------------------------------------------------------------------------
def test_settings_data_dir_override(monkeypatch, tmp_path):
    from app import config

    # 相对路径 → 相对 APP_DIR（开发态即项目根）
    monkeypatch.setenv("DATA_DIR", "rel-data")
    monkeypatch.delenv("DB_PATH", raising=False)
    settings = config.Settings.load()
    assert settings.data_dir == PROJECT_ROOT / "rel-data"
    assert settings.db_path == settings.data_dir / "app.db"

    # 绝对路径 → 原样使用
    absolute = tmp_path / "abs-data"
    monkeypatch.setenv("DATA_DIR", str(absolute))
    settings = config.Settings.load()
    assert settings.data_dir == absolute
    assert settings.db_path == absolute / "app.db"


# ---------------------------------------------------------------------------
# d) 静态检查：Path(__file__) 只允许出现在 app/paths.py
# ---------------------------------------------------------------------------
def test_path_file_only_in_paths_module():
    pattern = re.compile(r"Path\(\s*__file__\s*\)")
    scanned = 0
    hits: dict[str, int] = {}
    for py_file in APP_DIR.rglob("*.py"):
        scanned += 1
        text = py_file.read_text(encoding="utf-8")
        count = len(pattern.findall(text))
        if count:
            hits[py_file.relative_to(PROJECT_ROOT).as_posix()] = count

    # 防止扫描逻辑失效（paths.py 里必须仍能命中）
    assert scanned >= 10, f"app/ 下只扫描到 {scanned} 个 py 文件"
    assert hits.get("app/paths.py", 0) >= 1, "app/paths.py 应包含 Path(__file__) 推导"
    offenders = {name: count for name, count in hits.items() if name != "app/paths.py"}
    assert offenders == {}, f"以下模块违反路径契约（只允许 app/paths.py）：{offenders}"


# ---------------------------------------------------------------------------
# e) 入口冒烟（subprocess，真实解释器）
# ---------------------------------------------------------------------------
def _smoke_env(tmp_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update({
        "DATA_DIR": str(tmp_path / "data"),
        "ENV_FILE": str(tmp_path / "smoke.env"),
        "DB_PATH": str(tmp_path / "data" / "app.db"),
        "DEMO_MODE": "1",
        "TUNNEL_ENABLED": "0",
        "PYTHONIOENCODING": "utf-8",
    })
    return env


def _run(args: list[str], env: dict[str, str], timeout: int = 180) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, *args],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=timeout,
    )


def test_entry_point_module_cli_version(tmp_path):
    result = _run(["-m", "app.cli", "version"], _smoke_env(tmp_path))
    assert result.returncode == 0, f"stderr={result.stderr}"
    assert result.stdout.strip() == __version__


def test_entry_point_launcher_version(tmp_path):
    result = _run(["launcher.pyw", "--version"], _smoke_env(tmp_path))
    assert result.returncode == 0, f"stderr={result.stderr}"
    assert result.stdout.strip() == __version__


# ---------------------------------------------------------------------------
# f) 首次运行生成 .env（打包后的双击启动路径）
# ---------------------------------------------------------------------------
def test_ensure_env_file_first_run(monkeypatch, tmp_path):
    from app import config

    target = tmp_path / "first-run.env"
    monkeypatch.setattr(config, "ENV_FILE", target)

    assert not target.exists()
    created = config.ensure_env_file()
    assert created == target
    assert target.is_file()

    text = target.read_text(encoding="utf-8")
    assert "APP_TOKEN=" in text
    assert "change-me-please" not in text  # 不能沿用模板占位弱口令
    token_line = next(line for line in text.splitlines() if line.startswith("APP_TOKEN="))
    assert len(token_line.split("=", 1)[1]) >= 16

    # 幂等：已存在 .env 时不覆盖（升级/二次启动保留用户配置）
    before = target.read_text(encoding="utf-8")
    config.ensure_env_file()
    assert target.read_text(encoding="utf-8") == before
