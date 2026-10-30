"""DELETE /api/tasks/{id}：真删除接口（DB 层 + HTTP 层）。"""
HEADERS = {"X-Auth-Token": "test-token"}


def _create(client, title: str, **extra) -> dict:
    response = client.post("/api/tasks", headers=HEADERS, json={"title": title, **extra})
    assert response.status_code == 201
    return response.json()["task"]


def _all_ids(client) -> list[str]:
    body = client.get("/api/tasks", headers=HEADERS, params={"status": "all", "limit": 1000}).json()
    return [task["id"] for task in body["tasks"]]


def test_delete_requires_token(client):
    task = _create(client, "无令牌不能删")
    assert client.delete(f"/api/tasks/{task['id']}").status_code == 401
    assert client.delete(f"/api/tasks/{task['id']}",
                        headers={"X-Auth-Token": "wrong"}).status_code == 401
    # 鉴权失败时任务必须原样保留
    detail = client.get(f"/api/tasks/{task['id']}", headers=HEADERS)
    assert detail.status_code == 200
    assert detail.json()["task"]["title"] == "无令牌不能删"


def test_delete_task_success_and_list_shrinks(client):
    keep = _create(client, "保留的任务")
    victim = _create(client, "要被删除的任务", notes="删掉我", priority="high")
    before = _all_ids(client)
    pending_before = client.get("/api/health").json()["tasks"]["pending"]
    assert victim["id"] in before

    response = client.delete(f"/api/tasks/{victim['id']}", headers=HEADERS)
    assert response.status_code == 200
    assert response.json() == {"ok": True, "id": victim["id"]}

    # 删除后 detail 404、列表 -1、pending 计数 -1
    assert client.get(f"/api/tasks/{victim['id']}", headers=HEADERS).status_code == 404
    after = _all_ids(client)
    assert len(after) == len(before) - 1
    assert victim["id"] not in after
    pending_after = client.get("/api/health").json()["tasks"]["pending"]
    assert pending_after == pending_before - 1

    # 其它任务不受影响
    assert keep["id"] in after
    kept = client.get(f"/api/tasks/{keep['id']}", headers=HEADERS).json()["task"]
    assert kept["title"] == "保留的任务"
    assert kept["status"] == "pending"

    # 重复删除：已不存在 → 404，列表不再变化（幂等）
    again = client.delete(f"/api/tasks/{victim['id']}", headers=HEADERS)
    assert again.status_code == 404
    assert again.json()["detail"] == "task not found"
    assert len(_all_ids(client)) == len(after)


def test_delete_unknown_id_returns_404(client):
    response = client.delete("/api/tasks/000000000000", headers=HEADERS)
    assert response.status_code == 404
    assert response.json()["detail"] == "task not found"


def test_delete_done_task_is_removed_from_all_views(client):
    task = _create(client, "先完成再删除")
    patched = client.patch(f"/api/tasks/{task['id']}", headers=HEADERS, json={"status": "done"})
    assert patched.status_code == 200

    assert client.delete(f"/api/tasks/{task['id']}", headers=HEADERS).status_code == 200
    assert client.get(f"/api/tasks/{task['id']}", headers=HEADERS).status_code == 404
    done_ids = [item["id"] for item in client.get(
        "/api/tasks", headers=HEADERS, params={"status": "done", "limit": 1000}).json()["tasks"]]
    assert task["id"] not in done_ids


def test_db_delete_task_returns_bool(client):
    service = client.app.state.service
    task = _create(client, "db 层删除")
    assert service.db.delete_task(task["id"]) is True
    assert service.db.delete_task(task["id"]) is False
