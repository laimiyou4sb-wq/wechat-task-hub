"""配置加载：优先读 .env，其次读环境变量。"""
from __future__ import annotations

import os
import secrets
import socket
from dataclasses import dataclass, field
from datetime import timedelta, timezone
from pathlib import Path

from .paths import APP_DIR, DATA_DIR as DEFAULT_DATA_DIR, ENV_FILE as DEFAULT_ENV_FILE
from .version import __version__

#: 兼容旧代码：项目根 / 冻结态 exe 目录，等价于 APP_DIR（唯一事实源见 app/paths.py）
BASE_DIR = APP_DIR
#: 当前进程使用的配置文件（= paths.ENV_FILE，已应用 ENV_FILE 环境变量覆盖）
ENV_FILE = DEFAULT_ENV_FILE


def ensure_env_file() -> Path:
    """首次运行生成最小 `.env`（复制模板 + 随机 APP_TOKEN），返回配置文件路径。

    打包后的 exe 没有源码与说明可参照，双击启动时必须能直接得到一份可用的
    配置：复制 `APP_DIR/.env.example`，把占位 `APP_TOKEN` 换成随机串（不能沿用
    模板里的弱口令），其余键保持模板默认值。已存在则不动（幂等，升级不覆盖）。
    """
    if ENV_FILE.exists():
        return ENV_FILE
    template = APP_DIR / ".env.example"
    try:
        text = template.read_text(encoding="utf-8-sig") if template.is_file() else ""
    except OSError:
        text = ""
    token = secrets.token_urlsafe(16)
    lines: list[str] = []
    replaced = False
    for raw in text.splitlines():
        if raw.strip().startswith("APP_TOKEN="):
            lines.append(f"APP_TOKEN={token}")
            replaced = True
        else:
            lines.append(raw)
    if not replaced:
        lines.append(f"APP_TOKEN={token}")
    try:
        ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
        ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        # 安装目录不可写（如 Program Files）：不阻塞启动，退回默认配置运行，
        # 由启动说明提示用户换到可写目录。
        pass
    return ENV_FILE


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_env_file(DEFAULT_ENV_FILE)


def _get(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _bool(name: str, default: bool = False) -> bool:
    raw = _get(name, "1" if default else "0").lower()
    return raw in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(_get(name, str(default)))
    except ValueError:
        return default


def _list(name: str) -> list[str]:
    raw = _get(name)
    return [item.strip() for item in raw.replace("，", ",").split(",") if item.strip()]


def local_tz():
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(_get("APP_TZ", "Asia/Shanghai"))
    except Exception:  # Windows 上没有 tzdata 时退化为 UTC+8
        return timezone(timedelta(hours=8))


def detect_lan_ip() -> str:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
        finally:
            sock.close()
    except Exception:
        return "127.0.0.1"


@dataclass
class Settings:
    host: str = "0.0.0.0"
    port: int = 8765
    app_token: str = "wechat-tasks"
    app_base_url: str = ""
    data_dir: Path = DEFAULT_DATA_DIR
    db_path: Path = DEFAULT_DATA_DIR / "app.db"
    env_file: Path = DEFAULT_ENV_FILE

    data_source: str = "weflow"
    wcda_base: str = "http://127.0.0.1:10392"
    wcda_interval_ms: int = 2000
    wcda_account: str = ""
    wcda_include_sent: bool = False
    # 调试/回放：只订阅某个会话，并从指定 localId 之后回放（联调用，默认关闭）
    wcda_replay_username: str = ""
    wcda_replay_since_id: int = 0
    wcda_allow_replay: bool = False

    # 只处理服务启动后新收到的消息（不回溯历史）
    only_new_messages: bool = True

    # wcda 补采兜底：实时 SSE 偶尔会漏消息，按会话列表/最新消息定期补齐
    wcda_catchup_seconds: int = 120          # 0 = 关闭
    wcda_catchup_lookback_minutes: int = 30  # 每轮至少回看的窗口（兜实时流漏消息）
    wcda_catchup_on_start_minutes: int = 0   # 启动时按固定窗口回补（0 = 用会话水位补）
    wcda_catchup_limit: int = 300            # 旧版单次拉取条数（兼容回退路径）
    # 补采增强：按会话水位 + 分页读取，覆盖长时间离线期间漏采的消息
    wcda_catchup_max_hours: int = 168        # 单会话单轮最长回看（默认 7 天）
    wcda_catchup_page_size: int = 500        # 分页每页条数（WCDA 支持 offset/order）
    wcda_catchup_max_messages: int = 50000   # 单会话单轮最多拉取条数（防接口异常失控）
    wcda_catchup_round_budget: int = 2000    # 每轮最多处理条数（0 = 不限制），超出留到下一轮
    wcda_catchup_overlap_seconds: int = 300  # 水位重叠回看，容忍写入延迟/时钟误差
    wcda_catchup_batch_messages: int = 80    # 每批归纳条数（沿用事件账本的批次粒度）
    # 无水位且服务启动前没有该会话任何历史消息时，自动回看的窗口（小时）
    wcda_catchup_empty_history_hours: int = 24
    # 新置顶的聊天纳入监控时，自动回补最近 N 分钟（0 = 不回补，只从下一轮实时流开始）
    wcda_new_pin_backfill_minutes: int = 120

    weflow_base: str = "http://127.0.0.1:5031"
    weflow_token: str = ""
    group_whitelist: list[str] = field(default_factory=list)
    # all | pinned | custom | pinned_custom
    whitelist_mode: str = "all"
    whitelist_mode_valid = ("all", "pinned", "custom", "pinned_custom")
    pinned_refresh_seconds: int = 60
    include_private: bool = False
    catchup_minutes: int = 720

    me_names: list[str] = field(default_factory=list)

    # 身份识别：@/点名是否指向我（详见 app/identity.py、app/mentions.py）
    me_wxid: str = ""
    identity_filter: bool = True
    identity_fuzzy: bool = True
    identity_ttl_seconds: int = 600

    batch_seconds: int = 300
    batch_max_messages: int = 80

    # 事件账本：按会话微批次 + 静默封口
    event_idle_seconds: int = 20
    event_max_wait_seconds: int = 300
    event_max_messages: int = 30
    event_max_chars: int = 6000

    llm_enabled: bool = False
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"
    llm_timeout: int = 60

    bark_url: str = ""
    ntfy_server: str = "https://ntfy.sh"
    ntfy_topic: str = ""
    ntfy_token: str = ""
    quiet_start: str = "23:00"
    quiet_end: str = "07:30"
    digest_time: str = "08:30"
    digest_enabled: bool = True

    demo_mode: bool = False
    retention_days: int = 30
    version: str = __version__

    @classmethod
    def load(cls) -> "Settings":
        # 路径覆盖一律相对 APP_DIR 解析（开发态=项目根；冻结态=exe 所在目录）
        data_dir = Path(_get("DATA_DIR", str(DEFAULT_DATA_DIR))).expanduser()
        if not data_dir.is_absolute():
            data_dir = APP_DIR / data_dir
        db_path = Path(_get("DB_PATH", str(data_dir / "app.db"))).expanduser()
        if not db_path.is_absolute():
            db_path = APP_DIR / db_path
        env_file = Path(_get("ENV_FILE", str(DEFAULT_ENV_FILE))).expanduser()
        if not env_file.is_absolute():
            env_file = APP_DIR / env_file
        llm_key = _get("LLM_API_KEY")
        return cls(
            host=_get("HOST", "0.0.0.0"),
            port=_int("PORT", 8765),
            app_token=_get("APP_TOKEN", "wechat-tasks"),
            app_base_url=_get("APP_BASE_URL").rstrip("/"),
            data_dir=data_dir,
            db_path=db_path,
            env_file=env_file,
            data_source=_get("DATA_SOURCE", "weflow").lower() or "weflow",
            wcda_base=_get("WCDA_BASE", "http://127.0.0.1:10392").rstrip("/"),
            wcda_interval_ms=_int("WCDA_INTERVAL_MS", 500),
            wcda_account=_get("WCDA_ACCOUNT"),
            wcda_include_sent=_bool("WCDA_INCLUDE_SENT", False),
            wcda_replay_username=_get("WCDA_REPLAY_USERNAME"),
            wcda_replay_since_id=_int("WCDA_REPLAY_SINCE_LOCAL_ID", 0),
            wcda_allow_replay=_bool("WCDA_ALLOW_REPLAY", False),
            only_new_messages=_bool("ONLY_NEW_MESSAGES", True),
            wcda_catchup_seconds=max(0, _int("WCDA_CATCHUP_SECONDS", 120)),
            wcda_catchup_lookback_minutes=max(1, _int("WCDA_CATCHUP_LOOKBACK_MINUTES", 30)),
            wcda_catchup_on_start_minutes=max(0, _int("WCDA_BACKFILL_MINUTES", 0)),
            wcda_catchup_limit=max(20, _int("WCDA_CATCHUP_LIMIT", 300)),
            wcda_catchup_max_hours=max(1, _int("WCDA_CATCHUP_MAX_HOURS", 168)),
            wcda_catchup_page_size=max(50, min(_int("WCDA_CATCHUP_PAGE_SIZE", 500), 1000)),
            wcda_catchup_max_messages=max(1000, _int("WCDA_CATCHUP_MAX_MESSAGES", 50000)),
            wcda_catchup_round_budget=max(0, _int("WCDA_CATCHUP_ROUND_BUDGET", 2000)),
            wcda_catchup_overlap_seconds=max(0, _int("WCDA_CATCHUP_OVERLAP_SECONDS", 300)),
            wcda_catchup_batch_messages=max(5, min(_int("WCDA_CATCHUP_BATCH_MESSAGES", 80), 500)),
            wcda_catchup_empty_history_hours=max(1, _int("WCDA_CATCHUP_EMPTY_HISTORY_HOURS", 24)),
            weflow_base=_get("WEFLOW_BASE", "http://127.0.0.1:5031").rstrip("/"),
            weflow_token=_get("WEFLOW_TOKEN"),
            group_whitelist=_list("GROUP_WHITELIST") or _list("WEFLOW_GROUPS"),
            whitelist_mode=(_get("WHITELIST_MODE", "all").lower()
                            if _get("WHITELIST_MODE", "all").lower() in cls.whitelist_mode_valid else "all"),
            pinned_refresh_seconds=max(30, _int("PINNED_REFRESH_SECONDS", 60)),
            wcda_new_pin_backfill_minutes=max(0, _int("WCDA_NEW_PIN_BACKFILL_MINUTES", 120)),
            include_private=_bool("INCLUDE_PRIVATE", _bool("WEFLOW_INCLUDE_PRIVATE", False)),
            catchup_minutes=_int("WEFLOW_CATCHUP_MINUTES", 720),
            me_names=_list("ME_NAMES"),
            me_wxid=_get("ME_WXID"),
            identity_filter=_bool("IDENTITY_FILTER", True),
            identity_fuzzy=_bool("IDENTITY_FUZZY", True),
            identity_ttl_seconds=max(60, _int("IDENTITY_TTL_SECONDS", 600)),
            batch_seconds=max(0, _int("BATCH_SECONDS", 300)),
            batch_max_messages=max(1, _int("BATCH_MAX_MESSAGES", 80)),
            event_idle_seconds=max(5, _int("EVENT_IDLE_SECONDS", 20)),
            event_max_wait_seconds=max(30, _int("EVENT_MAX_WAIT_SECONDS", 300)),
            event_max_messages=max(5, _int("EVENT_MAX_MESSAGES", 30)),
            event_max_chars=max(500, _int("EVENT_MAX_CHARS", 6000)),
            llm_enabled=_bool("LLM_ENABLED", bool(llm_key)) and bool(llm_key),
            llm_base_url=_get("LLM_BASE_URL", "https://api.deepseek.com/v1").rstrip("/"),
            llm_api_key=llm_key,
            llm_model=_get("LLM_MODEL", "deepseek-chat"),
            llm_timeout=_int("LLM_TIMEOUT", 60),
            bark_url=_get("PUSH_BARK_URL").rstrip("/"),
            ntfy_server=_get("NTFY_SERVER", "https://ntfy.sh").rstrip("/"),
            ntfy_topic=_get("NTFY_TOPIC"),
            ntfy_token=_get("NTFY_TOKEN"),
            quiet_start=_get("PUSH_QUIET_START", "23:00"),
            quiet_end=_get("PUSH_QUIET_END", "07:30"),
            digest_time=_get("DAILY_DIGEST_TIME", "08:30"),
            digest_enabled=bool(_get("DAILY_DIGEST_TIME", "08:30")),
            demo_mode=_bool("DEMO_MODE", False),
            retention_days=_int("RETENTION_DAYS", 30),
        )

    @property
    def weflow_configured(self) -> bool:
        return bool(self.weflow_token)

    @property
    def base_url(self) -> str:
        if self.app_base_url:
            return self.app_base_url
        return f"http://{detect_lan_ip()}:{self.port}"
