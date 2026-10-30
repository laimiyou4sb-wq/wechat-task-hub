"""自研安卓 App 同步 API（/api/app/*）测试。"""
from __future__ import annotations

import base64
import json
import uuid

HEADERS = {"X-Auth-Token": "test-token"}


def _new_task(client, title: str, **kwargs) -> dict:
    payload = {"title": title, **kwargs}
    response = client.post("/api/tasks", headers=HEADERS, json=payload)
    assert response.status_code == 201, response.text
    return response.json()["task"]


def _action_id() -> str:
    return uuid.uuid4().hex


def _sync(client, cursor: int = 0) -> dict:
    response = client.get(f"/api/app/sync?cursor={cursor}", headers=HEADERS)
    assert response.status_code == 200, response.text
    return response.json()


def _post_actions(client, actions: list[dict]) -> dict:
    response = client.post("/api/app/actions", headers=HEADERS, json={"actions": actions})
    assert response.status_code == 200, response.text
    return response.json()


def test_app_requires_token(client):
    assert client.get("/api/app/ping").status_code == 401
    assert client.get("/api/app/sync").status_code == 401
    assert client.get("/api/app/pairing").status_code == 401
    assert client.post("/api/app/actions", json={"actions": []}).status_code == 401
    assert client.get("/api/app/ping", headers={"X-Auth-Token": "nope"}).status_code == 401


def test_app_ping(client):
    body = client.get("/api/app/ping", headers=HEADERS).json()
    assert body["ok"] is True
    assert body["app"] == "wechat-task-hub"
    assert body["server_time"]
    assert body["tz"]


def test_pairing_payload_and_code(client):
    body = client.get("/api/app/pairing", headers=HEADERS).json()
    assert body["token"] == "test-token"
    assert body["v"] == 1
    assert body["code"]
    assert isinstance(body["endpoints"], list)
    # 局域网地址至少包含 127.0.0.1 之外的本机地址，或者为空（无网卡环境）
    for item in body["endpoints"]:
        assert item["kind"] in ("lan", "remote")
        assert item["url"].startswith("http")

    padded = body["code"] + "=" * (-len(body["code"]) % 4)
    decoded = json.loads(base64.urlsafe_b64decode(padded.encode()).decode("utf-8"))
    assert decoded["v"] == 1
    assert decoded["token"] == "test-token"
    assert isinstance(decoded["endpoints"], list)


def test_sync_full_snapshot(client):
    task = _new_task(client, "同步-全量-买电池")
    body = _sync(client, cursor=0)
    assert body["full"] is True
    assert body["cursor"] > 0
    ids = [item["id"] for item in body["tasks"]]
    assert task["id"] in ids
    dto = next(item for item in body["tasks"] if item["id"] == task["id"])
    assert dto["title"] == "同步-全量-买电池"
    assert dto["status"] == "pending"
    assert dto["rev"] > 0
    assert "counts" in body


def test_sync_incremental(client):
    first = _new_task(client, "同步-增量-旧任务")
    baseline = _sync(client, cursor=0)
    cursor = baseline["cursor"]

    # 无变化时增量应为空
    empty = _sync(client, cursor=cursor)
    assert empty["full"] is False
    assert empty["tasks"] == []
    assert empty["deleted"] == []

    second = _new_task(client, "同步-增量-新任务")
    incremental = _sync(client, cursor=cursor)
    assert incremental["full"] is False
    ids = [item["id"] for item in incremental["tasks"]]
    assert second["id"] in ids
    assert first["id"] not in ids
    assert incremental["cursor"] > cursor


def test_action_complete_and_idempotent(client):
    task = _new_task(client, "动作-完成-交作业")
    action = {
        "action_id": _action_id(),
        "type": "complete",
        "task_id": task["id"],
        "client_updated_at": task["updated_at"],
    }
    result = _post_actions(client, [action])
    first = result["results"][0]
    assert first["ok"] is True
    assert first["deduped"] is False
    assert first["task"]["status"] == "done"
    assert first["task"]["completed_at"]

    # 重复提交同一个 action_id：直接返回首次结果，且不再产生新的写入
    before = client.get("/api/tasks", headers=HEADERS).json()["tasks"]
    repeat = _post_actions(client, [action])["results"][0]
    assert repeat["deduped"] is True
    assert repeat["task"]["id"] == task["id"]
    after = client.get("/api/tasks", headers=HEADERS).json()["tasks"]
    assert [item["updated_at"] for item in before] == [item["updated_at"] for item in after]


def test_action_create_update_delete(client):
    created = _post_actions(
        client,
        [
            {
                "action_id": _action_id(),
                "type": "create",
                "local_id": "local-abc",
                "fields": {"title": "动作-新建-手机加的任务", "due_at": "2031-01-02T09:00:00", "priority": "high"},
            }
        ],
    )["results"][0]
    assert created["ok"] is True
    new_task = created["task"]
    assert new_task["title"] == "动作-新建-手机加的任务"
    assert new_task["group_name"] == "手机添加"
    assert new_task["due_at"] == "2031-01-02T09:00:00"

    updated = _post_actions(
        client,
        [
            {
                "action_id": _action_id(),
                "type": "update",
                "task_id": new_task["id"],
                "client_updated_at": new_task["updated_at"],
                "fields": {"title": "动作-新建-改标题", "due_at": None, "priority": "low", "notes": "备注"},
            }
        ],
    )["results"][0]
    assert updated["ok"] is True
    assert updated["stale"] is False
    assert updated["task"]["title"] == "动作-新建-改标题"
    assert updated["task"]["due_at"] is None
    assert updated["task"]["priority"] == "low"
    assert updated["task"]["notes"] == "备注"

    deleted = _post_actions(
        client, [{"action_id": _action_id(), "type": "delete", "task_id": new_task["id"]}]
    )["results"][0]
    assert deleted["ok"] is True
    assert deleted["task"] is None
    ids = [item["id"] for item in client.get("/api/tasks", headers=HEADERS, params={"status": "all"}).json()["tasks"]]
    assert new_task["id"] not in ids


def test_action_update_stale_keeps_server_value(client):
    task = _new_task(client, "动作-冲突-电脑端优先")
    # 模拟电脑端在手机编辑之后又改了一次
    patched = client.patch(f"/api/tasks/{task['id']}", headers=HEADERS, json={"title": "动作-冲突-电脑端新标题"})
    assert patched.status_code == 200
    latest = patched.json()["task"]

    result = _post_actions(
        client,
        [
            {
                "action_id": _action_id(),
                "type": "update",
                "task_id": task["id"],
                "client_rev": task["rev"],  # 旧 rev（电脑端已经又改过一次）
                "fields": {"title": "动作-冲突-手机旧标题"},
            }
        ],
    )["results"][0]
    assert result["ok"] is True
    assert result["stale"] is True
    assert result["task"]["title"] == "动作-冲突-电脑端新标题"
    assert result["task"]["updated_at"] == latest["updated_at"]


def test_action_errors(client):
    missing = _post_actions(
        client, [{"action_id": _action_id(), "type": "complete", "task_id": "does-not-exist"}]
    )["results"][0]
    assert missing["ok"] is False
    assert missing["error"] == "not_found"

    task = _new_task(client, "动作-参数错误")
    bad = _post_actions(
        client,
        [
            {
                "action_id": _action_id(),
                "type": "update",
                "task_id": task["id"],
                "fields": {"title": "   "},
            }
        ],
    )["results"][0]
    assert bad["ok"] is False
    assert bad["error"]

    bad_create = _post_actions(
        client, [{"action_id": _action_id(), "type": "create", "fields": {"title": ""}}]
    )["results"][0]
    assert bad_create["ok"] is False


def test_delete_tombstone_visible_in_sync(client):
    task = _new_task(client, "同步-墓碑-会被删")
    baseline = _sync(client, cursor=0)
    cursor = baseline["cursor"]

    client.delete(f"/api/tasks/{task['id']}", headers=HEADERS)
    incremental = _sync(client, cursor=cursor)
    assert task["id"] in incremental["deleted"]


def test_task_detail_with_sources(client):
    task = _new_task(client, "详情-来源-看原文")
    detail = client.get(f"/api/app/tasks/{task['id']}", headers=HEADERS)
    assert detail.status_code == 200
    body = detail.json()
    assert body["task"]["id"] == task["id"]
    assert body["event"] is None or isinstance(body["event"], dict)
    assert isinstance(body["sources"], list)
    assert client.get("/api/app/tasks/not-found", headers=HEADERS).status_code == 404


def test_actions_limit(client):
    actions = [{"action_id": _action_id(), "type": "create", "fields": {"title": f"批量-{i}"}} for i in range(51)]
    response = client.post("/api/app/actions", headers=HEADERS, json={"actions": actions})
    assert response.status_code == 422


def test_snooze_action_default_and_explicit(client):
    task = _new_task(client, "动作-稍后")
    snoozed = _post_actions(
        client, [{"action_id": _action_id(), "type": "snooze", "task_id": task["id"]}]
    )["results"][0]
    assert snoozed["ok"] is True
    assert snoozed["task"]["status"] == "snoozed"
    assert snoozed["task"]["snooze_until"]

    explicit = _post_actions(
        client,
        [
            {
                "action_id": _action_id(),
                "type": "snooze",
                "task_id": task["id"],
                "snooze_until": "2031-05-06T07:08:09",
            }
        ],
    )["results"][0]
    assert explicit["task"]["snooze_until"] == "2031-05-06T07:08:09"


def test_pairing_reads_tunnel_url(client):
    """data/tunnel.json 存在时，配对信息里远程地址要排在最前。"""
    data_dir = client.app.state.service.settings.data_dir
    tunnel_path = data_dir / "tunnel.json"
    payload = {
        "url": "https://unit-test.ngrok-free.app",
        "kind": "ngrok",
        "updated_at": "2026-10-26T12:00:00",
    }
    tunnel_path.write_text(json.dumps(payload), encoding="utf-8")
    try:
        body = client.get("/api/app/pairing", headers=HEADERS).json()
        assert body["remote_urls"][0] == "https://unit-test.ngrok-free.app"
        decoded_padded = body["code"] + "=" * (-len(body["code"]) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(decoded_padded.encode()).decode("utf-8"))
        assert "https://unit-test.ngrok-free.app" in [item["url"] for item in decoded["endpoints"]]
        # 局域网地址优先
        kinds = [item["kind"] for item in decoded["endpoints"]]
        if "lan" in kinds:
            assert kinds.index("lan") < kinds.index("remote")
    finally:
        tunnel_path.unlink(missing_ok=True)
    # 删除后不再回落（APP_BASE_URL 在测试环境为空）
    body = client.get("/api/app/pairing", headers=HEADERS).json()
    assert "https://unit-test.ngrok-free.app" not in body["remote_urls"]


# ---------------------------------------------------------------------------
# 批内基准 rev：同一批里同一任务的多条操作，不能被本客户端前面的操作误判 stale
# （2026-10-27 事故：服务重启后 App 一次推 8 条积压操作，批内 complete/restore
#   把 rev 抬高，后面的 update 被判 stale 丢弃，用户离线编辑被电脑端旧值覆盖）
# ---------------------------------------------------------------------------
def test_actions_batch_self_conflict_not_stale(client):
    """批次 [complete, restore, update] 共用一个 client_rev：三条都必须生效且不 stale。"""
    task = _new_task(client, "批内基准-离线编辑")
    client_rev = task["rev"]
    results = _post_actions(
        client,
        [
            {"action_id": _action_id(), "type": "complete", "task_id": task["id"], "client_rev": client_rev},
            {"action_id": _action_id(), "type": "restore", "task_id": task["id"], "client_rev": client_rev},
            {
                "action_id": _action_id(),
                "type": "update",
                "task_id": task["id"],
                "client_rev": client_rev,
                "fields": {
                    "title": "批内基准-手机改的标题",
                    "notes": "批内基准-手机改的备注",
                    "due_at": "2031-03-04T05:06:07",
                    "priority": "high",
                },
            },
        ],
    )["results"]
    assert [item["ok"] for item in results] == [True, True, True]
    assert [item["stale"] for item in results] == [False, False, False]
    final = results[2]["task"]
    assert final["status"] == "pending"  # restore 后回到待办
    assert final["title"] == "批内基准-手机改的标题"
    assert final["notes"] == "批内基准-手机改的备注"
    assert final["due_at"] == "2031-03-04T05:06:07"
    assert final["priority"] == "high"
    # 落库结果同样是手机值（说明 update 真的写进去了，不是只改了返回值）
    tasks = client.get("/api/tasks", headers=HEADERS, params={"status": "all"}).json()["tasks"]
    stored = next(item for item in tasks if item["id"] == task["id"])
    assert stored["title"] == "批内基准-手机改的标题"
    assert stored["due_at"] == "2031-03-04T05:06:07"


def test_actions_batch_two_updates_same_client_rev(client):
    """同一批里连续两条 update、client_rev 相同：两条都生效，最终以最后一条为准。"""
    task = _new_task(client, "批内基准-连续编辑-旧")
    client_rev = task["rev"]
    results = _post_actions(
        client,
        [
            {
                "action_id": _action_id(),
                "type": "update",
                "task_id": task["id"],
                "client_rev": client_rev,
                "fields": {"title": "批内基准-连续编辑-A"},
            },
            {
                "action_id": _action_id(),
                "type": "update",
                "task_id": task["id"],
                "client_rev": client_rev,
                "fields": {"title": "批内基准-连续编辑-B", "notes": "第二条备注"},
            },
        ],
    )["results"]
    assert [item["stale"] for item in results] == [False, False]
    assert results[0]["task"]["title"] == "批内基准-连续编辑-A"
    assert results[1]["task"]["title"] == "批内基准-连续编辑-B"
    assert results[1]["task"]["notes"] == "第二条备注"


def test_actions_batch_stale_real_conflict_keeps_server(client):
    """真实电脑端冲突：批内首次动作的 client_rev 就已过期时，update 一律不覆盖电脑端。"""
    task = _new_task(client, "批内基准-真实冲突-旧标题")
    patched = client.patch(
        f"/api/tasks/{task['id']}", headers=HEADERS, json={"title": "电脑端新标题", "notes": "电脑端备注"}
    )
    assert patched.status_code == 200
    latest = patched.json()["task"]
    results = _post_actions(
        client,
        [
            {
                "action_id": _action_id(),
                "type": "update",
                "task_id": task["id"],
                "client_rev": task["rev"],  # 旧 rev：电脑端已经又改过一次
                "fields": {"title": "手机旧标题"},
            },
            {
                "action_id": _action_id(),
                "type": "update",
                "task_id": task["id"],
                "client_rev": task["rev"],
                "fields": {"title": "手机旧标题2", "notes": "手机旧备注"},
            },
        ],
    )["results"]
    assert [item["stale"] for item in results] == [True, True]
    for item in results:
        assert item["task"]["title"] == "电脑端新标题"
        assert item["task"]["notes"] == "电脑端备注"
        assert item["task"]["updated_at"] == latest["updated_at"]


def test_actions_batch_stale_status_runs_but_update_blocked(client):
    """真实冲突下批次 [complete, update]：状态类操作照常执行（stale=true），update 仍不覆盖电脑端。"""
    task = _new_task(client, "批内基准-真实冲突-完成")
    patched = client.patch(
        f"/api/tasks/{task['id']}", headers=HEADERS, json={"title": "电脑端标题-完成场景"}
    )
    assert patched.status_code == 200
    results = _post_actions(
        client,
        [
            {
                "action_id": _action_id(),
                "type": "complete",
                "task_id": task["id"],
                "client_rev": task["rev"],  # 旧 rev：真实冲突
            },
            {
                "action_id": _action_id(),
                "type": "update",
                "task_id": task["id"],
                "client_rev": task["rev"],
                "fields": {"title": "手机标题", "priority": "high"},
            },
        ],
    )["results"]
    # complete：明确意图，照常生效但标 stale
    assert results[0]["ok"] is True
    assert results[0]["stale"] is True
    assert results[0]["task"]["status"] == "done"
    assert results[0]["task"]["title"] == "电脑端标题-完成场景"
    # update：不覆盖电脑端，返回最新 DTO
    assert results[1]["ok"] is True
    assert results[1]["stale"] is True
    assert results[1]["task"]["status"] == "done"
    assert results[1]["task"]["title"] == "电脑端标题-完成场景"
    assert results[1]["task"]["priority"] == "normal"


def test_actions_batch_retry_deduped(client):
    """整批重发（网络超时后重试）：同一批 action_id 全部 deduped，重复提交不产生新写入。"""
    task = _new_task(client, "批内基准-整批重发")
    actions = [
        {"action_id": _action_id(), "type": "complete", "task_id": task["id"], "client_rev": task["rev"]},
        {
            "action_id": _action_id(),
            "type": "update",
            "task_id": task["id"],
            "client_rev": task["rev"],
            "fields": {"title": "批内基准-整批重发-手机标题"},
        },
    ]
    first = _post_actions(client, actions)
    assert [item["deduped"] for item in first["results"]] == [False, False]
    assert first["results"][1]["stale"] is False
    second = _post_actions(client, actions)
    assert [item["deduped"] for item in second["results"]] == [True, True]
    assert second["cursor"] == first["cursor"]  # 去重不写库，全局 rev 不变
    assert second["results"][1]["task"]["title"] == "批内基准-整批重发-手机标题"


def test_actions_batch_legacy_client_updated_at(client):
    """兼容旧版 client_updated_at：批内 complete 之后的 update 也按批内基准判断，不 stale。"""
    task = _new_task(client, "批内基准-旧版时间戳")
    results = _post_actions(
        client,
        [
            {
                "action_id": _action_id(),
                "type": "complete",
                "task_id": task["id"],
                "client_updated_at": task["updated_at"],
            },
            {
                "action_id": _action_id(),
                "type": "update",
                "task_id": task["id"],
                "client_updated_at": task["updated_at"],
                "fields": {"title": "批内基准-旧版时间戳-改"},
            },
        ],
    )["results"]
    assert [item["stale"] for item in results] == [False, False]
    assert results[1]["task"]["title"] == "批内基准-旧版时间戳-改"


# ---------------------------------------------------------------------------
# 同批 create 后引用 local_id：手机离线新建任务后立刻编辑，outbox 是
# [create(local_id=local-x), update(task_id=local-x)] 同一批发。服务端要能把 local_id
# 解析成真实 id，否则 update 返回 not_found，App 删本地行并丢操作，用户编辑静默丢失。
# ---------------------------------------------------------------------------
def test_actions_batch_create_then_update_local_id(client):
    """同批 [create(local_id), update(task_id=local_id)]：编辑必须生效且不 stale。"""
    local_id = f"local-{uuid.uuid4().hex[:8]}"
    created, updated = _post_actions(
        client,
        [
            {
                "action_id": _action_id(),
                "type": "create",
                "local_id": local_id,
                "fields": {"title": "同批新建-初始标题", "priority": "normal"},
            },
            {
                "action_id": _action_id(),
                "type": "update",
                "task_id": local_id,  # 同批内还没拿到真实 id
                "client_rev": 0,
                "fields": {
                    "title": "同批新建-手机编辑后标题",
                    "priority": "high",
                    "notes": "同批新建-编辑后的备注",
                },
            },
        ],
    )["results"]
    assert created["ok"] is True
    real_id = created["task"]["id"]
    assert updated["ok"] is True, updated
    assert updated["stale"] is False
    assert updated["task"]["id"] == real_id
    assert updated["task"]["title"] == "同批新建-手机编辑后标题"
    assert updated["task"]["priority"] == "high"
    assert updated["task"]["notes"] == "同批新建-编辑后的备注"
    # 落库核对：电脑端看到的是用户编辑后的内容（不是被静默丢弃的初始标题）
    tasks = client.get("/api/tasks", headers=HEADERS, params={"status": "all"}).json()["tasks"]
    stored = next(item for item in tasks if item["id"] == real_id)
    assert stored["title"] == "同批新建-手机编辑后标题"
    assert stored["priority"] == "high"
    assert stored["notes"] == "同批新建-编辑后的备注"


def test_actions_batch_create_then_complete_local_id(client):
    """同批 [create, complete(local_id)]：新建后立刻标记完成，complete 要落到真实任务上。"""
    local_id = f"local-{uuid.uuid4().hex[:8]}"
    created, completed = _post_actions(
        client,
        [
            {
                "action_id": _action_id(),
                "type": "create",
                "local_id": local_id,
                "fields": {"title": "同批新建-完成后"},
            },
            {
                "action_id": _action_id(),
                "type": "complete",
                "task_id": local_id,
                "client_rev": 0,
            },
        ],
    )["results"]
    assert created["ok"] is True
    real_id = created["task"]["id"]
    assert completed["ok"] is True, completed
    assert completed["stale"] is False
    assert completed["task"]["id"] == real_id
    assert completed["task"]["status"] == "done"
    assert completed["task"]["completed_at"]


def test_actions_batch_create_then_delete_local_id(client):
    """同批 [create, delete(local_id)]：新建后立刻删除，delete 要删掉真实任务。"""
    local_id = f"local-{uuid.uuid4().hex[:8]}"
    created, deleted = _post_actions(
        client,
        [
            {
                "action_id": _action_id(),
                "type": "create",
                "local_id": local_id,
                "fields": {"title": "同批新建-随后删除"},
            },
            {"action_id": _action_id(), "type": "delete", "task_id": local_id},
        ],
    )["results"]
    assert created["ok"] is True
    real_id = created["task"]["id"]
    assert deleted["ok"] is True, deleted
    assert deleted["task"] is None
    ids = [
        item["id"]
        for item in client.get("/api/tasks", headers=HEADERS, params={"status": "all"}).json()["tasks"]
    ]
    assert real_id not in ids


def test_actions_batch_create_update_retry_deduped(client):
    """整批重发（create+update 都已处理过）：全部 deduped，返回的 task 仍是编辑后结果。"""
    local_id = f"local-{uuid.uuid4().hex[:8]}"
    actions = [
        {
            "action_id": _action_id(),
            "type": "create",
            "local_id": local_id,
            "fields": {"title": "同批重发-初始标题"},
        },
        {
            "action_id": _action_id(),
            "type": "update",
            "task_id": local_id,
            "client_rev": 0,
            "fields": {"title": "同批重发-编辑后标题"},
        },
    ]
    first = _post_actions(client, actions)
    assert [item["ok"] for item in first["results"]] == [True, True]
    assert first["results"][1]["stale"] is False
    real_id = first["results"][0]["task"]["id"]

    second = _post_actions(client, actions)
    assert [item["deduped"] for item in second["results"]] == [True, True]
    assert second["cursor"] == first["cursor"]  # 去重不写库，全局 rev 不变
    assert second["results"][0]["task"]["id"] == real_id
    assert second["results"][1]["task"]["id"] == real_id
    assert second["results"][1]["task"]["title"] == "同批重发-编辑后标题"


def test_actions_batch_deduped_create_then_new_update_local_id(client):
    """create 上一批已成功（重发时 deduped），同批后面的新 update 仍要解析 local_id 并生效。"""
    local_id = f"local-{uuid.uuid4().hex[:8]}"
    create_action = {
        "action_id": _action_id(),
        "type": "create",
        "local_id": local_id,
        "fields": {"title": "去重后新编辑-初始标题"},
    }
    first = _post_actions(client, [create_action])
    real_id = first["results"][0]["task"]["id"]

    retried = _post_actions(
        client,
        [
            create_action,  # 同一个 action_id：去重命中，还要登记 local_id
            {
                "action_id": _action_id(),
                "type": "update",
                "task_id": local_id,
                "client_rev": 0,
                "fields": {"title": "去重后新编辑-新标题"},
            },
        ],
    )["results"]
    assert retried[0]["deduped"] is True
    assert retried[1]["ok"] is True, retried[1]
    assert retried[1]["stale"] is False
    assert retried[1]["task"]["id"] == real_id
    assert retried[1]["task"]["title"] == "去重后新编辑-新标题"


def test_sync_incremental_pagination(client):
    baseline = _sync(client, cursor=0)
    cursor = baseline["cursor"]
    created = [_new_task(client, f"分页-{i}") for i in range(5)]
    wanted = {task["id"] for task in created}

    seen: set[str] = set()
    rounds = 0
    while True:
        rounds += 1
        assert rounds < 10, "增量同步分页没有收敛"
        page = client.get(f"/api/app/sync?cursor={cursor}&limit=2", headers=HEADERS).json()
        assert page["full"] is False
        seen |= {item["id"] for item in page["tasks"]}
        cursor = page["cursor"]
        if not page["has_more"]:
            break
    assert wanted <= seen

