import os
import time

HEADERS = {"X-Auth-Token": "test-token"}
ENV_PATH = os.environ.get("ENV_FILE", "")


def test_llm_settings_requires_token(client):
    assert client.get("/api/settings/llm").status_code == 401
    assert client.put("/api/settings/llm", json={"model": "x"}).status_code == 401


def test_get_llm_settings_defaults(client):
    # 先确保没有 key
    client.put("/api/settings/llm", headers=HEADERS, json={"api_key": ""})
    body = client.get("/api/settings/llm", headers=HEADERS).json()
    assert body["configured"] is False
    assert body["key_saved"] is False
    assert body["base_url"].startswith("http")
    assert body["model"]


def test_update_llm_settings_and_persist(client):
    response = client.put("/api/settings/llm", headers=HEADERS, json={
        "api_key": "sk-test-1234567890",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "enabled": True,
    })
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["configured"] is True
    assert body["key_saved"] is True
    assert "sk-test-1234567890" not in body["key_hint"]
    assert body["key_hint"].endswith("7890")

    # 写入的是临时 ENV_FILE，而不是项目真实的 .env
    text = open(ENV_PATH, encoding="utf-8").read()
    assert "LLM_API_KEY=sk-test-1234567890" in text
    assert "LLM_MODEL=deepseek-chat" in text


def test_invalid_base_url_rejected(client):
    response = client.put("/api/settings/llm", headers=HEADERS,
                          json={"base_url": "api.deepseek.com"})
    assert response.status_code == 422


def test_clear_llm_key_disables_it(client):
    response = client.put("/api/settings/llm", headers=HEADERS, json={"api_key": ""})
    assert response.status_code == 200
    body = response.json()
    assert body["configured"] is False
    assert body["enabled"] is False


def test_llm_test_without_key(client):
    body = client.post("/api/settings/llm/test", headers=HEADERS).json()
    assert body["ok"] is False
    assert "API Key" in body["error"]


def test_push_settings_update_and_persist(client):
    assert client.get("/api/settings/push").status_code == 401
    default = client.get("/api/settings/push", headers=HEADERS).json()
    assert default["configured"] is False

    response = client.put("/api/settings/push", headers=HEADERS, json={
        "bark_url": "https://api.day.app/TESTKEY1234",
        "ntfy_server": "https://ntfy.sh",
        "ntfy_topic": "wx-test-topic",
    })
    assert response.status_code == 200
    body = response.json()
    assert body["configured"] is True
    assert body["bark"] is True and body["ntfy"] is True
    assert body["bark_hint"].endswith("1234")
    assert "TESTKEY1234" not in body["bark_hint"]

    text = open(ENV_PATH, encoding="utf-8").read()
    assert "PUSH_BARK_URL=https://api.day.app/TESTKEY1234" in text
    assert "NTFY_TOPIC=wx-test-topic" in text

    # 校验非法地址
    bad = client.put("/api/settings/push", headers=HEADERS, json={"bark_url": "api.day.app/x"})
    assert bad.status_code == 422

    # 清空
    cleared = client.put("/api/settings/push", headers=HEADERS, json={
        "bark_url": "", "ntfy_topic": "",
    }).json()
    assert cleared["configured"] is False


def test_filter_settings_update(client):
    response = client.put("/api/settings/filters", headers=HEADERS, json={
        "include_private": True,
        "whitelist_mode": "pinned_custom",
        "group_whitelist": ["项目群", "wxid_friend"],
    })
    assert response.status_code == 200
    body = response.json()
    assert body["include_private"] is True
    assert body["whitelist_mode"] == "pinned_custom"
    assert body["group_whitelist"] == ["项目群", "wxid_friend"]

    fetched = client.get("/api/settings/filters", headers=HEADERS).json()
    assert fetched["whitelist_mode"] == "pinned_custom"

    text = open(ENV_PATH, encoding="utf-8").read()
    assert "INCLUDE_PRIVATE=1" in text
    assert "WHITELIST_MODE=pinned_custom" in text
    assert "GROUP_WHITELIST=项目群,wxid_friend" in text

    invalid = client.put("/api/settings/filters", headers=HEADERS, json={"whitelist_mode": "nope"})
    assert invalid.status_code == 422


def test_only_new_message_filter(client):
    service = client.app.state.service
    old = int(time.time()) - 3600
    now = int(time.time())
    assert service._is_new_message({"ts": old}) is False
    assert service._is_new_message({"ts": now}) is True
    service.settings.only_new_messages = False
    assert service._is_new_message({"ts": old}) is True
    service.settings.only_new_messages = True


def test_whitelist_modes(client):
    service = client.app.state.service
    service.pinned_ready = True
    service.pinned_sessions = {"1@chatroom"}

    service.settings.whitelist_mode = "pinned"
    service.settings.include_private = True
    assert service._message_allowed({"session_id": "1@chatroom", "is_group": True}) is True
    assert service._message_allowed({"session_id": "2@chatroom", "is_group": True}) is False

    service.settings.whitelist_mode = "pinned_custom"
    service.settings.group_whitelist = ["项目"]
    assert service._message_allowed(
        {"session_id": "9@chatroom", "group_name": "项目群", "is_group": True}
    ) is True
    assert service._message_allowed(
        {"session_id": "9@chatroom", "group_name": "闲聊群", "is_group": True}
    ) is False

    service.settings.whitelist_mode = "custom"
    service.settings.group_whitelist = ["张三"]
    assert service._message_allowed(
        {"session_id": "wxid_x", "group_name": "张三", "is_group": False}
    ) is True
    assert service._message_allowed(
        {"session_id": "wxid_y", "group_name": "李四", "is_group": False}
    ) is False

    service.settings.include_private = False
    assert service._message_allowed(
        {"session_id": "wxid_x", "group_name": "张三", "is_group": False}
    ) is False
