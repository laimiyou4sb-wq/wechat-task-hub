HEADERS = {"X-Auth-Token": "test-token"}


def test_health(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data_source"] in ("wcda", "weflow", "demo")
    assert "source" in body
    assert "weflow" in body and "push" in body
    assert "caldav" not in body


def test_tasks_require_token(client):
    assert client.get("/api/tasks").status_code == 401
    assert client.get("/api/tasks", headers={"X-Auth-Token": "wrong"}).status_code == 401


def test_create_list_and_complete_task(client):
    created = client.post("/api/tasks", headers=HEADERS, json={
        "title": "买牛奶",
        "due_at": "2030-10-24T20:00:00",
        "notes": "顺便买鸡蛋",
    })
    assert created.status_code == 201
    task = created.json()["task"]
    assert task["title"] == "买牛奶"
    assert task["status"] == "pending"
    assert task["due_at"] == "2030-10-24T20:00:00"

    listed = client.get("/api/tasks", headers=HEADERS)
    assert listed.status_code == 200
    assert any(item["id"] == task["id"] for item in listed.json()["tasks"])
    assert "counts" in listed.json()

    patched = client.patch(f"/api/tasks/{task['id']}", headers=HEADERS, json={"status": "done"})
    assert patched.status_code == 200
    assert patched.json()["task"]["status"] == "done"

    detail = client.get(f"/api/tasks/{task['id']}", headers=HEADERS)
    assert detail.status_code == 200
    assert detail.json()["task"]["id"] == task["id"]


def test_create_task_validation(client):
    assert client.post("/api/tasks", headers=HEADERS, json={"title": ""}).status_code == 422
    assert client.post("/api/tasks", headers=HEADERS, json={
        "title": "坏时间", "due_at": "not-a-date",
    }).status_code == 422


def test_snooze_defaults_to_one_hour(client):
    created = client.post("/api/tasks", headers=HEADERS, json={"title": "稍后处理"}).json()["task"]
    patched = client.patch(f"/api/tasks/{created['id']}", headers=HEADERS, json={"status": "snoozed"})
    assert patched.status_code == 200
    assert patched.json()["task"]["status"] == "snoozed"
    assert patched.json()["task"]["snooze_until"]


def test_demo_seed_creates_tasks(client):
    response = client.post("/api/demo/seed", headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["tasks_created"] >= 2

    listed = client.get("/api/tasks", headers=HEADERS).json()
    titles = [task["title"] for task in listed["tasks"]]
    assert any("报名表" in title or "组会" in title or "作业" in title for title in titles)


def test_patch_notes_and_clear(client):
    created = client.post("/api/tasks", headers=HEADERS, json={
        "title": "备注可编辑",
        "notes": "原始备注",
    }).json()["task"]
    assert created["notes"] == "原始备注"

    # 改备注：只有 notes 变化，其他字段不受影响
    patched = client.patch(f"/api/tasks/{created['id']}", headers=HEADERS,
                           json={"notes": "  新的备注内容  "})
    assert patched.status_code == 200
    task = patched.json()["task"]
    assert task["notes"] == "新的备注内容"   # 前后空格被 strip
    assert task["title"] == "备注可编辑"
    assert task["status"] == "pending"

    # detail 读回一致
    detail = client.get(f"/api/tasks/{created['id']}", headers=HEADERS).json()["task"]
    assert detail["notes"] == "新的备注内容"

    # 空串 = 清空备注
    cleared = client.patch(f"/api/tasks/{created['id']}", headers=HEADERS, json={"notes": ""})
    assert cleared.status_code == 200
    assert cleared.json()["task"]["notes"] == ""
    assert client.get(f"/api/tasks/{created['id']}", headers=HEADERS).json()["task"]["notes"] == ""

    # 不传 notes = 不修改（None 语义）
    kept = client.patch(f"/api/tasks/{created['id']}", headers=HEADERS, json={"title": "改名后"})
    assert kept.status_code == 200
    assert kept.json()["task"]["title"] == "改名后"
    assert kept.json()["task"]["notes"] == ""


def test_patch_notes_is_truncated_to_2000(client):
    created = client.post("/api/tasks", headers=HEADERS, json={"title": "超长备注"}).json()["task"]
    long_notes = "备" * 2500
    patched = client.patch(f"/api/tasks/{created['id']}", headers=HEADERS,
                           json={"notes": long_notes})
    assert patched.status_code == 200
    assert len(patched.json()["task"]["notes"]) == 2000


def test_health_includes_identity(client):
    body = client.get("/api/health").json()
    assert "identity" in body
    assert "judgments" in body["identity"]
    assert "account" in body["identity"]


def test_identity_settings_roundtrip_and_check(client):
    assert client.get("/api/settings/identity").status_code == 401

    initial = client.get("/api/settings/identity", headers=HEADERS)
    assert initial.status_code == 200
    body = initial.json()
    assert "aliases" in body and "groups" in body and "judgments" in body

    updated = client.put("/api/settings/identity", headers=HEADERS, json={
        "me_names": ["大王", "001大王"],
        "identity_filter": True,
        "identity_fuzzy": False,
    })
    assert updated.status_code == 200
    payload = updated.json()
    assert payload["manual_names"] == ["大王", "001大王"]
    assert payload["fuzzy_enabled"] is False
    assert payload["persisted"] is True

    # 判定测试：命中手动别名 → me
    checked = client.post("/api/identity/check", headers=HEADERS,
                          json={"text": "@001大王 明天交表"}).json()
    assert checked["verdict"]["kind"] == "me"
    # 无法识别的 @ → none（不影响原有待办逻辑）
    unknown = client.post("/api/identity/check", headers=HEADERS,
                          json={"text": "@路人甲 你去交表"}).json()
    assert unknown["verdict"]["kind"] == "none"
    # 未指定对象 → none
    plain = client.post("/api/identity/check", headers=HEADERS,
                        json={"text": "明天下午三点开组会"}).json()
    assert plain["verdict"]["kind"] == "none"

    judgments = client.get("/api/identity/judgments", headers=HEADERS).json()
    assert "judgments" in judgments and "last_drops" in judgments

    refreshed = client.post("/api/identity/refresh", headers=HEADERS)
    assert refreshed.status_code == 200
    assert refreshed.json()["ok"] is True

    # 恢复默认，避免影响其它测试
    client.put("/api/settings/identity", headers=HEADERS, json={
        "me_names": [],
        "identity_filter": True,
        "identity_fuzzy": True,
    })


def test_identity_check_requires_text(client):
    assert client.post("/api/identity/check", headers=HEADERS,
                       json={"text": "   "}).status_code == 422


def test_task_detail_includes_match_reason(client):
    service = client.app.state.service
    row, _ = service.db.create_task(
        title="判定依据示例", group_name="测试群", match_reason="@我（001大王）",
    )
    detail = client.get(f"/api/tasks/{row['id']}", headers=HEADERS).json()
    assert detail["task"]["match_reason"] == "@我（001大王）"


def test_manual_catchup_endpoint_removed(client):
    """补采已改为全自动：手动补采接口不再提供。"""
    response = client.post("/api/catchup", headers=HEADERS, json={"minutes": 120})
    assert response.status_code in (404, 405)


def test_health_includes_catchup(client):
    body = client.get("/api/health").json()
    assert "catchup" in body
    assert "seconds" in body["catchup"]


def test_pinned_refresh_endpoint(client):
    assert client.post("/api/pinned/refresh").status_code == 401
    body = client.post("/api/pinned/refresh", headers=HEADERS).json()
    assert body["ok"] is True
    assert "pinned_count" in body and "pinned" in body


def test_caldav_endpoints_removed(client):
    """废弃的 CalDAV 方案已删除：两个路由不再存在。

    GET 未命中静态兜底返回 404；非 GET 请求由 ``StaticFiles`` 兜底返回 405，
    两者都证明路由未注册（与 /api 下其它不存在的路径行为一致）。
    """
    assert client.get("/api/caldav/info", headers=HEADERS).status_code == 404
    assert client.post("/api/caldav/sync",
                       headers=HEADERS).status_code in (404, 405)
    # 路由不存在 → 不会先触发 401 鉴权失败
    assert client.get("/api/caldav/info").status_code == 404
    assert client.post("/api/caldav/sync").status_code in (404, 405)
