"""独立黑盒复现：手机离线修改待办，电脑端服务重启后 App 自动同步把修改「改回去」。

事故背景（2026-10-27 12:20，由 data/app.db 取证）：
- 用户在手机离线期间先 complete、再 restore（取消完成）、再 update 编辑
  （标题/备注/截止/优先级）；
- App 恢复联网后，一次 POST /api/app/actions 按上述顺序推送这 3 条操作，
  三条的 client_rev 都是 App 最后一次成功同步时拿到的同一个 rev R；
- 服务端逐条把 client_rev 与任务的「当前 rev」比较：complete 把任务 rev 抬到 R+1，
  restore 被判 stale（但仍生效，rev 到 R+2），update 被判 stale 直接丢弃并返回电脑端旧值；
- App 随后用服务端返回的 DTO 覆盖本地 → 用户离线时的修改丢失。

修复语义（见 docs/APP-SYNC-CONTRACT.md）：同一批 actions 是客户端的一次不可分割提交，
服务端按顺序在单事件循环内处理、批内无 await，批内不存在外部写入；
因此 stale 判定应以「批开始时该任务的 rev（batch base rev）」为基准，
而不是同批前面 action 已经抬高后的 rev。批开始前电脑端已改过（rev > client_rev）才是真实冲突，
真实冲突下 update 仍然 stale、不覆盖电脑端数据（电脑端优先）。

本文件只做 HTTP 黑盒测试：复用 tests/conftest.py 的临时 DB 与 client fixture，
不连接 data/app.db，不启动/停止真实服务。
"""
from __future__ import annotations

import uuid

HEADERS = {"X-Auth-Token": "test-token"}

PHONE_FIELDS = {
    "title": "手机离线改的标题",
    "notes": "手机离线补的备注",
    "due_at": "2031-03-04T05:06:07",
    "priority": "high",
}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _action_id() -> str:
    return uuid.uuid4().hex


def _create_task(client, title: str, **fields) -> dict:
    response = client.post("/api/tasks", headers=HEADERS, json={"title": title, **fields})
    assert response.status_code == 201, response.text
    return response.json()["task"]


def _find_task(client, task_id: str, status: str = "all") -> dict:
    """GET /api/tasks 查任务（手机视角的电脑端权威数据）。"""
    response = client.get("/api/tasks", headers=HEADERS, params={"status": status, "limit": 1000})
    assert response.status_code == 200, response.text
    for task in response.json()["tasks"]:
        if task["id"] == task_id:
            return task
    raise AssertionError(f"任务 {task_id} 不在 GET /api/tasks?status={status} 结果里")


def _synced_dto(client, task_id: str) -> dict:
    """GET /api/app/sync?cursor=0：模拟手机首次/全量同步后拿到的任务 rev R。"""
    response = client.get("/api/app/sync", headers=HEADERS, params={"cursor": 0})
    assert response.status_code == 200, response.text
    body = response.json()
    for dto in body["tasks"]:
        if dto["id"] == task_id:
            return dto
    raise AssertionError(f"任务 {task_id} 不在 /api/app/sync 全量快照里")


def _post_actions(client, actions: list[dict]) -> list[dict]:
    """一次 POST /api/app/actions 推送整批离线操作，返回 results（顺序与请求一致）。"""
    response = client.post("/api/app/actions", headers=HEADERS, json={"actions": actions})
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["results"]) == len(actions), body
    return body["results"]


# ---------------------------------------------------------------------------
# A. 事故复现：同批 complete → restore → update，update 必须生效（stale=false）
# ---------------------------------------------------------------------------
def test_incident_offline_batch_complete_restore_update(client):
    """事故主用例：手机离线期间 complete → restore → update，三条在同一批推送。

    修复前行为（基线）：complete 成功把 rev 从 R 抬到 R+1，
    restore / update 都拿 client_rev=R 与当前 rev 比较 → stale=true；
    其中 update 被丢弃，返回电脑端旧值，App 覆盖本地 → 用户修改“改回去”。
    修复后：三条都 ok，update stale=false，手机编辑的四个字段全部落库，状态回到 pending。
    """
    desktop_title = f"事故复现-电脑端标题-{uuid.uuid4().hex[:6]}"
    task = _create_task(
        client,
        desktop_title,
        notes="电脑端旧备注",
        due_at="2031-01-01T08:00:00",
        priority="normal",
    )
    synced = _synced_dto(client, task["id"])
    client_rev = synced["rev"]
    assert client_rev > 0
    assert synced["title"] == desktop_title
    assert synced["status"] == "pending"

    actions = [
        {"action_id": _action_id(), "type": "complete", "task_id": task["id"], "client_rev": client_rev},
        {"action_id": _action_id(), "type": "restore", "task_id": task["id"], "client_rev": client_rev},
        {
            "action_id": _action_id(),
            "type": "update",
            "task_id": task["id"],
            "client_rev": client_rev,
            "fields": dict(PHONE_FIELDS),
        },
    ]
    complete, restore, update = _post_actions(client, actions)  # 单次请求 = 单批

    assert [item["ok"] for item in (complete, restore, update)] == [True, True, True], (
        "同批三条离线操作都应成功："
        + repr([(item["type"] if "type" in item else "", item["ok"], item.get("error")) for item in (complete, restore, update)])
    )

    # 关键：update 不应被误判 stale
    assert update["stale"] is False, (
        f"update 被误判 stale：update.stale={update['stale']!r}，"
        f"返回的 task title={update['task']['title']!r}（期望手机标题 {PHONE_FIELDS['title']!r}）"
    )
    assert update["task"]["title"] == PHONE_FIELDS["title"]
    assert update["task"]["notes"] == PHONE_FIELDS["notes"]
    assert update["task"]["due_at"] == PHONE_FIELDS["due_at"]
    assert update["task"]["priority"] == PHONE_FIELDS["priority"]

    # 落库后手机再拉/电脑端看到的是用户离线修改，且状态为 pending
    latest = _find_task(client, task["id"])
    assert latest["title"] == PHONE_FIELDS["title"], (
        f"标题被改回去：{latest['title']!r} != {PHONE_FIELDS['title']!r}（电脑端旧值 {desktop_title!r}）"
    )
    assert latest["notes"] == PHONE_FIELDS["notes"]
    assert latest["due_at"] == PHONE_FIELDS["due_at"]
    assert latest["priority"] == PHONE_FIELDS["priority"]
    assert latest["status"] == "pending"


# ---------------------------------------------------------------------------
# B. 真实冲突保护：批开始前电脑端已改过，手机 update 仍不覆盖电脑端
# ---------------------------------------------------------------------------
def test_real_conflict_still_desktop_wins(client):
    """真实冲突保护：批开始前电脑端已把任务 rev 抬高（client_rev 过期）。

    此时整批的基准 rev 已经是电脑端新 rev，手机 complete 仍应生效但 stale=true；
    手机 update 是真实冲突，stale=true 且不覆盖电脑端标题（电脑端优先）。
    """
    task = _create_task(client, "冲突保护-电脑端旧标题")
    synced = _synced_dto(client, task["id"])
    client_rev = synced["rev"]

    patched = client.patch(
        f"/api/tasks/{task['id']}", headers=HEADERS, json={"title": "冲突保护-电脑端新标题"}
    )
    assert patched.status_code == 200, patched.text
    desktop_title = patched.json()["task"]["title"]
    assert desktop_title == "冲突保护-电脑端新标题"
    assert patched.json()["task"]["rev"] > client_rev

    complete, update = _post_actions(
        client,
        [
            {"action_id": _action_id(), "type": "complete", "task_id": task["id"], "client_rev": client_rev},
            {
                "action_id": _action_id(),
                "type": "update",
                "task_id": task["id"],
                "client_rev": client_rev,
                "fields": {"title": "冲突保护-手机过期标题"},
            },
        ],
    )

    # 状态类操作即便 stale 也仍生效（App 离线完成的动作不能丢），但会标记 stale
    assert complete["ok"] is True
    assert complete["stale"] is True, "批开始前电脑端已改过，complete 应标记 stale=true"
    assert complete["task"]["status"] == "done"

    # 真实冲突下 update 必须 stale 且不覆盖电脑端
    assert update["ok"] is True
    assert update["stale"] is True, "真实冲突下 update 必须 stale=true（不能借批内基准绕过）"
    assert update["task"]["title"] == desktop_title, (
        f"真实冲突下手机 update 覆盖了电脑端：{update['task']['title']!r} != {desktop_title!r}"
    )

    latest = _find_task(client, task["id"])
    assert latest["title"] == desktop_title
    assert latest["status"] == "done"


# ---------------------------------------------------------------------------
# C. 同批两条 update（同 client_rev）都生效，最终为后者（说明性用例）
# ---------------------------------------------------------------------------
def test_same_batch_two_updates_both_apply_last_wins(client):
    """同批两条 update 共用同一 client_rev：都是本批连续编辑，都应生效，后者覆盖前者。"""
    task = _create_task(client, "同批双更新-原标题")
    synced = _synced_dto(client, task["id"])
    client_rev = synced["rev"]

    first, second = _post_actions(
        client,
        [
            {
                "action_id": _action_id(),
                "type": "update",
                "task_id": task["id"],
                "client_rev": client_rev,
                "fields": {"title": "同批双更新-第一次"},
            },
            {
                "action_id": _action_id(),
                "type": "update",
                "task_id": task["id"],
                "client_rev": client_rev,
                "fields": {"title": "同批双更新-第二次"},
            },
        ],
    )

    assert first["ok"] is True and first["stale"] is False, (
        f"同批第一条 update 被误判 stale：{first.get('stale')!r}"
    )
    assert second["ok"] is True and second["stale"] is False, (
        f"同批第二条 update 被误判 stale：{second.get('stale')!r}，"
        f"title={second['task']['title']!r}"
    )
    assert second["task"]["title"] == "同批双更新-第二次"
    latest = _find_task(client, task["id"])
    assert latest["title"] == "同批双更新-第二次"
