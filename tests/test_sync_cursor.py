"""同步游标协议级回归测试（2026-10-28 事故根因）。

事故（2026-10-28，由 Lead 取证确认）：手机待办比电脑少一条（task e7b218f1e66d，pending、
due_at=NULL、rev 167）。根因是安卓端把 `POST /api/app/actions` 响应里的 `cursor` 当成了
`GET /api/app/sync` 的拉取游标：

1. 手机上次 /sync 拿到 cursor=C（此时电脑端已有 A）；
2. 手机离线期间电脑端新建 B（rev 抬升到 > C，手机从未拉过）；
3. 手机恢复联网推送 outbox，`/api/app/actions` 响应 cursor = 服务端最新 rev（>= B 的 rev）；
4. 旧策略拿这个 cursor 去 /sync 拉增量 → 服务端认为「B 的 rev 已被拉过」，B 永久丢失；
   删除墓碑同理（墓碑 rev 也落在被跳过的区间里）。

正确策略：`/api/app/actions` 响应的 cursor 只是信息字段，App 必须继续用上一次 `/sync` 保存的
cursor 拉增量（见 docs/APP-SYNC-CONTRACT.md §5 末条与 §6 第 3 步）。

本文件用 FastAPI TestClient + 真实 /api/app/* 端点做协议级复现：同一组服务端状态下，分别用
「actions 响应 cursor」（旧错误策略）和「上次 /sync cursor」（正确策略）拉增量，断言前者漏掉
离线期间的变更、后者能拿到——即证明根因，并防止服务端增量语义回退。

conftest.py 已隔离 DEMO_MODE / test-token / 临时 DB，不触碰真实 data/app.db。
"""
from __future__ import annotations

import uuid

HEADERS = {"X-Auth-Token": "test-token"}


# ---------------------------------------------------------------------------
# helpers（风格与 tests/test_incident_repro.py / test_mobile.py 保持一致）
# ---------------------------------------------------------------------------
def _action_id() -> str:
    return uuid.uuid4().hex


def _create_task(client, title: str, **fields) -> dict:
    response = client.post("/api/tasks", headers=HEADERS, json={"title": title, **fields})
    assert response.status_code == 201, response.text
    return response.json()["task"]


def _sync(client, cursor: int = 0) -> dict:
    response = client.get("/api/app/sync", headers=HEADERS, params={"cursor": cursor})
    assert response.status_code == 200, response.text
    return response.json()


def _post_actions(client, actions: list[dict]) -> dict:
    response = client.post("/api/app/actions", headers=HEADERS, json={"actions": actions})
    assert response.status_code == 200, response.text
    return response.json()


def _find_dto(body: dict, task_id: str) -> dict:
    for dto in body["tasks"]:
        if dto["id"] == task_id:
            return dto
    raise AssertionError(f"任务 {task_id} 不在 /api/app/sync 结果里")


def _task_rev(client, task_id: str) -> int:
    """电脑端视角读任务当前 rev（GET /api/tasks，不推进手机游标）。"""
    response = client.get(
        "/api/tasks", headers=HEADERS, params={"status": "all", "limit": 1000}
    )
    assert response.status_code == 200, response.text
    for task in response.json()["tasks"]:
        if task["id"] == task_id:
            return int(task["rev"])
    raise AssertionError(f"任务 {task_id} 不在 GET /api/tasks 结果里")


def _ids(items: list[dict]) -> list[str]:
    return [item["id"] for item in items]


def _complete_action(client, task: dict, client_rev: int) -> dict:
    """模拟手机推送 outbox：离线前拉到的 rev 作为 client_rev 完成该任务。"""
    return {
        "action_id": _action_id(),
        "type": "complete",
        "task_id": task["id"],
        "client_rev": client_rev,
    }


# ---------------------------------------------------------------------------
# A. 离线期间电脑端【新建任务】：旧策略（actions cursor）漏掉，正确策略拿到
# ---------------------------------------------------------------------------
def test_actions_cursor_must_not_advance_sync_cursor(client):
    """复现 2026-10-28 事故主场景：离线期间电脑端新建 B，手机用 actions cursor 拉取会漏 B。

    断言分两半：
    ① 旧错误策略（/sync?cursor=actions.cursor）拿不到 B —— 证明「手机少一条」的根因；
    ② 正确策略（/sync?cursor=上次 /sync 的 cursor）能拿到 B。
    若服务端增量语义或 actions cursor 语义回退（例如 cursor 不再越过离线 rev，或 /sync 在
    actions cursor 处仍返回全量），本用例会失败。
    """
    task_a = _create_task(client, f"游标事故-A-离线前已同步-{uuid.uuid4().hex[:6]}")

    # 手机上一次同步：拿到 cursor=C 与 A 的 rev（作为 outbox 的 client_rev）
    baseline = _sync(client, cursor=0)
    assert baseline["full"] is True
    last_sync_cursor = baseline["cursor"]
    dto_a = _find_dto(baseline, task_a["id"])
    client_rev = int(dto_a["rev"])
    assert client_rev > 0

    # 手机离线期间：电脑端新建 B，rev 抬升到 C 之后（手机从未拉过）
    task_b = _create_task(client, f"游标事故-B-离线期间电脑端新建-{uuid.uuid4().hex[:6]}")
    rev_b = _task_rev(client, task_b["id"])
    assert rev_b > last_sync_cursor, (
        f"B 的 rev({rev_b}) 应大于手机最后同步的 cursor({last_sync_cursor})"
    )

    # 手机恢复联网：先推送 outbox（完成 A），响应 cursor 已越过 B 的 rev
    body = _post_actions(client, [_complete_action(client, task_a, client_rev)])
    assert body["results"][0]["ok"] is True
    actions_cursor = int(body["cursor"])
    assert actions_cursor >= rev_b, (
        f"actions 响应 cursor({actions_cursor}) 应已越过离线期间 B 的 rev({rev_b})"
    )

    # ① 旧错误策略：把 actions 响应的 cursor 当拉取游标 → 漏掉 B
    wrong = _sync(client, cursor=actions_cursor)
    assert wrong["full"] is False, "用 actions cursor 拉增量不应触发 full 快照"
    assert task_b["id"] not in _ids(wrong["tasks"]), (
        "旧策略居然拿到了 B，无法证明事故根因（或服务端增量语义已变）"
    )

    # ② 正确策略：用上次 /sync 保存的 cursor → 必须拿到 B
    good = _sync(client, cursor=last_sync_cursor)
    assert good["full"] is False
    assert task_b["id"] in _ids(good["tasks"]), (
        f"用上次 /sync cursor({last_sync_cursor}) 拉增量漏掉了 B"
    )
    assert _find_dto(good, task_b["id"])["rev"] == rev_b
    # A 的完成也没被跳过（rev 也在该区间内）
    assert task_a["id"] in _ids(good["tasks"])


# ---------------------------------------------------------------------------
# B. 离线期间电脑端【删除任务】：旧策略漏墓碑，正确策略能收到 deleted
# ---------------------------------------------------------------------------
def test_actions_cursor_must_not_skip_delete_tombstone(client):
    """墓碑版本：离线期间电脑端删除 B，旧策略（actions cursor）漏掉删除墓碑，正确策略收到。"""
    task_a = _create_task(client, f"墓碑事故-A-离线前已同步-{uuid.uuid4().hex[:6]}")
    task_b = _create_task(client, f"墓碑事故-B-离线期间被电脑端删除-{uuid.uuid4().hex[:6]}")

    baseline = _sync(client, cursor=0)
    assert baseline["full"] is True
    last_sync_cursor = baseline["cursor"]
    client_rev = int(_find_dto(baseline, task_a["id"])["rev"])
    # 手机离线前已同步到 B（否则「漏墓碑」无从谈起）
    assert task_b["id"] in _ids(baseline["tasks"])

    # 手机离线期间：电脑端删除 B（物理删除 + 写墓碑，rev 抬升）
    deleted = client.delete(f"/api/tasks/{task_b['id']}", headers=HEADERS)
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["ok"] is True

    # 手机恢复联网推送 outbox → actions cursor 已越过墓碑 rev
    body = _post_actions(client, [_complete_action(client, task_a, client_rev)])
    assert body["results"][0]["ok"] is True
    actions_cursor = int(body["cursor"])
    assert actions_cursor > last_sync_cursor

    # ① 旧错误策略：漏掉删除墓碑，B 在手机端永远留着
    wrong = _sync(client, cursor=actions_cursor)
    assert wrong["full"] is False
    assert task_b["id"] not in wrong["deleted"], (
        "旧策略居然收到了 B 的删除墓碑，无法证明事故根因（或服务端墓碑语义已变）"
    )
    assert task_b["id"] not in _ids(wrong["tasks"])

    # ② 正确策略：收到 B 的删除墓碑
    good = _sync(client, cursor=last_sync_cursor)
    assert good["full"] is False
    assert task_b["id"] in good["deleted"], (
        f"用上次 /sync cursor({last_sync_cursor}) 拉增量漏掉了 B 的删除墓碑"
    )


# ---------------------------------------------------------------------------
# C. counts 分桶口径：pending + due_at=NULL → later（不是 today）
# ---------------------------------------------------------------------------
def _web_counts(client) -> dict:
    response = client.get(
        "/api/tasks", headers=HEADERS, params={"status": "all", "limit": 1000}
    )
    assert response.status_code == 200, response.text
    return response.json()["counts"]


def test_counts_pending_without_due_is_later(client):
    """无截止时间的 pending 任务在 counts 里算「以后」不算「今天」（web/App 同口径）。

    /api/tasks 与 /api/app/sync 都直接暴露 db.counts()，两者必须一致。
    基线「无截止 → today_n」时本用例失败。
    """
    web_before = _web_counts(client)
    app_before = _sync(client, cursor=0)["counts"]
    assert set(("today", "week", "later", "done")) <= set(web_before)
    assert set(("today", "week", "later", "done")) <= set(app_before)

    task = _create_task(client, f"无截止时间-归以后-{uuid.uuid4().hex[:6]}")
    assert task["due_at"] is None
    assert task["status"] == "pending"

    web_after = _web_counts(client)
    assert web_after["today"] == web_before["today"], (
        f"无截止 pending 任务被算进了「今天」：today {web_before['today']} → {web_after['today']}"
    )
    assert web_after["later"] == web_before["later"] + 1, (
        f"无截止 pending 任务应计入「以后」：later {web_before['later']} → {web_after['later']}"
    )

    app_after = _sync(client, cursor=0)["counts"]
    assert app_after["today"] == app_before["today"], (
        f"/api/app/sync counts 把无截止任务算进「今天」："
        f"{app_before['today']} → {app_after['today']}"
    )
    assert app_after["later"] == app_before["later"] + 1, (
        f"/api/app/sync counts 未把无截止任务计入「以后」："
        f"{app_before['later']} → {app_after['later']}"
    )
