import os
import tempfile

_TMP_DIR = tempfile.mkdtemp(prefix="wechat-tasks-test-")

os.environ.update({
    "APP_TOKEN": "test-token",
    "DATA_DIR": _TMP_DIR,
    "DB_PATH": os.path.join(_TMP_DIR, "test.db"),
    "ENV_FILE": os.path.join(_TMP_DIR, "test.env"),
    "DEMO_MODE": "1",
    # 单测必须与真实 WeChatDataAnalysis / 真实 .env 隔离：
    "DATA_SOURCE": "demo",
    "WCDA_BASE": "",
    "WCDA_CATCHUP_SECONDS": "0",
    "WCDA_BACKFILL_MINUTES": "0",
    "WEFLOW_BASE": "http://127.0.0.1:9",
    "WEFLOW_TOKEN": "",
    "BATCH_SECONDS": "0",
    "LLM_ENABLED": "0",
    "LLM_API_KEY": "",
    "PUSH_BARK_URL": "",
    "NTFY_SERVER": "https://ntfy.sh",
    "NTFY_TOPIC": "",
    "INCLUDE_PRIVATE": "0",
    "WHITELIST_MODE": "all",
    "GROUP_WHITELIST": "",
    "ONLY_NEW_MESSAGES": "1",
    "DAILY_DIGEST_TIME": "",
    "RETENTION_DAYS": "30",
    "ME_NAMES": "",
    "ME_WXID": "",
    "IDENTITY_FILTER": "1",
    "IDENTITY_FUZZY": "1",
})

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

TOKEN = "test-token"
HEADERS = {"X-Auth-Token": TOKEN}


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as test_client:
        yield test_client
