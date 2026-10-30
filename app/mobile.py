"""自研安卓 App 的直连同步 API（/api/app）。

设计见 docs/APP-SYNC-CONTRACT.md：
- 电脑端是唯一权威数据源；
- App 用「rev 游标拉增量 + action_id 幂等推操作」同步；
- 不依赖第三方待办 App / 第三方代理服务。
"""
from __future__ import annotations

import base64
import json
import os
import re
import socket
import subprocess
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from .api import get_service, require_token
from .config import detect_lan_ip, local_tz
from .utils import now_iso, now_local, parse_iso, to_iso
from .version import __version__

router = APIRouter(prefix="/api/app")

APP_API_VERSION = 1
MANUAL_GROUP = "手机添加"

ActionType = Literal[
    "complete", "uncomplete", "ignore", "restore", "snooze", "update", "create", "delete"
]


class AppAction(BaseModel):
    action_id: str = Field(min_length=1, max_length=64)
    type: ActionType
    task_id: str | None = None
    local_id: str | None = None
    client_rev: int | None = None
    client_updated_at: str | None = None
    snooze_until: str | None = None
    fields: dict[str, Any] | None = None


class AppActionsRequest(BaseModel):
    actions: list[AppAction] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 序列化
# ---------------------------------------------------------------------------
def _task_dto(task: dict) -> dict:
    return {
        "id": task.get("id") or "",
        "title": task.get("title") or "",
        "notes": task.get("notes") or "",
        "due_at": task.get("due_at"),
        "priority": task.get("priority") or "normal",
        "status": task.get("status") or "pending",
        "group_name": task.get("group_name") or "",
        "event_id": task.get("event_id") or "",
        "source_count": len(task.get("source_msg_ids") or []),
        "created_at": task.get("created_at") or "",
        "updated_at": task.get("updated_at") or "",
        "completed_at": task.get("completed_at"),
        "snooze_until": task.get("snooze_until"),
        "rev": int(task.get("rev") or 0),
    }


def _bad(error: str) -> ValueError:
    return ValueError(error)


def _norm_due(value: Any) -> str | None:
    """把 App 传来的截止时间规整成无时区本地 ISO；None/空串 = 清空。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    parsed = parse_iso(text)
    if parsed is None:
        raise _bad("invalid_due_at")
    return to_iso(parsed)


def _norm_notes(value: Any) -> str:
    return str(value if value is not None else "").strip()[:2000]


def _norm_priority(value: Any) -> str:
    priority = str(value or "normal").strip().lower()
    if priority not in ("high", "normal", "low"):
        raise _bad("invalid_priority")
    return priority


def _norm_title(value: Any) -> str:
    title = str(value if value is not None else "").strip()
    if not title:
        raise _bad("invalid_title")
    return title[:500]


def _action_ok(action_id: str, task: dict | None, *, stale: bool = False, deduped: bool = False) -> dict:
    return {
        "action_id": action_id,
        "ok": True,
        "deduped": deduped,
        "stale": stale,
        "task": _task_dto(task) if task else None,
    }


def _action_error(action_id: str, error: str) -> dict:
    return {
        "action_id": action_id,
        "ok": False,
        "deduped": False,
        "stale": False,
        "task": None,
        "error": error,
    }


def _is_stale(task: dict | None, action: "AppAction", base: dict | None = None) -> bool:
    """冲突检测：优先用 rev（秒级时间戳会撞车），兼容旧的 updated_at 方式。

    base 是该任务在「本批」首次出现时的服务端状态（rev/updated_at），由 app_actions 传入。
    同一批里的多条操作都带 App 本地的同一个旧 client_rev，若拿它和已被本客户端前面操作
    抬高后的「当前 rev」比，会把同端连续操作误判成 stale（2026-10-27 事故）。所以批内
    一律和基准比；base 缺省（单条调用）时退回与原语义一致的当前值比较。
    """
    if not task:
        return False
    reference = base if base is not None else task
    if action.client_rev is not None:
        try:
            return int(action.client_rev) != int(reference.get("rev") or 0)
        except (TypeError, ValueError):
            return True
    if not action.client_updated_at:
        return False
    return str(action.client_updated_at).strip() != str(reference.get("updated_at") or "").strip()


# ---------------------------------------------------------------------------
# 配对：局域网地址 + 远程地址 + 配对码
# ---------------------------------------------------------------------------
def _is_private_ipv4(ip: str) -> bool:
    try:
        parts = [int(part) for part in ip.split(".")]
    except ValueError:
        return False
    if len(parts) != 4 or any(part < 0 or part > 255 for part in parts):
        return False
    if parts[0] == 10:
        return True
    if parts[0] == 172 and 16 <= parts[1] <= 31:
        return True
    if parts[0] == 192 and parts[1] == 168:
        return True
    if parts[0] == 100 and 64 <= parts[1] <= 127:  # CGNAT / Tailscale，手机端用不上
        return False
    if parts[0] in (127, 169) or parts[0] >= 224:
        return False
    return False


def _ipconfig_ips() -> list[tuple[str, str]]:
    """Windows 下解析 `ipconfig`，返回 (适配器名, IPv4) 列表（只用私有地址）。"""
    if os.name != "nt":
        return []
    try:
        raw = subprocess.run(["ipconfig"], capture_output=True, timeout=6).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    text = ""
    for encoding in ("mbcs", "gbk", "utf-8"):
        try:
            text = raw.decode(encoding)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if not text:
        return []
    items: list[tuple[str, str]] = []
    adapter = ""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.endswith(":") and ("adapter" in stripped.lower() or "适配器" in stripped):
            adapter = stripped.rstrip(":")
            continue
        if "ipv4" in stripped.lower() or "IPv4 地址" in stripped:
            match = re.search(r"(\d{1,3}(?:\.\d{1,3}){3})", stripped)
            if match and _is_private_ipv4(match.group(1)):
                items.append((adapter, match.group(1)))
    return items


# 虚拟/隧道/USB 共享网卡：能连但优先级低（真局域网网卡优先）
_VIRTUAL_ADAPTER_HINTS = (
    "ndis", "tailscale", "teredo", "vethernet", "docker", "wsl", "vmware",
    "virtualbox", "bluetooth", "pseudo",
)
_LAN_IP_CACHE: dict[str, Any] = {"at": 0.0, "ips": []}


def _lan_ips() -> list[str]:
    """本机所有可被手机访问的局域网 IPv4（真实网卡优先，USB 共享/虚拟网卡靠后）。"""
    import time as _time

    cached = _LAN_IP_CACHE["ips"]
    if cached and _time.time() - float(_LAN_IP_CACHE["at"]) < 30:
        return list(cached)

    pairs = _ipconfig_ips()
    real = [ip for name, ip in pairs if not any(hint in name.lower() for hint in _VIRTUAL_ADAPTER_HINTS)]
    virtual = [ip for name, ip in pairs if any(hint in name.lower() for hint in _VIRTUAL_ADAPTER_HINTS)]

    ordered: list[str] = []
    primary = detect_lan_ip()
    for ip in [primary, *real, *virtual]:
        if _is_private_ipv4(ip) and ip not in ordered:
            ordered.append(ip)
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if _is_private_ipv4(ip) and ip not in ordered:
                ordered.append(ip)
    except OSError:
        pass
    _LAN_IP_CACHE["ips"] = ordered
    _LAN_IP_CACHE["at"] = _time.time()
    return ordered


def _read_tunnel(settings) -> dict | None:
    """读取 tools/tunnel.py 写出的 data/tunnel.json。"""
    path = settings.data_dir / "tunnel.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    url = str(data.get("url") or "").strip().rstrip("/")
    if not url.startswith("http"):
        return None
    return {"url": url, "kind": "remote", "source": data.get("kind") or "tunnel"}


def _pairing_payload(request: Request) -> dict:
    service = get_service(request)
    settings = service.settings
    port = settings.port
    endpoints: list[dict] = []
    for ip in _lan_ips():
        endpoints.append({"kind": "lan", "url": f"http://{ip}:{port}", "priority": len(endpoints)})

    remote_urls: list[str] = []
    tunnel = _read_tunnel(settings)
    if tunnel:
        remote_urls.append(tunnel["url"])
    base = str(settings.app_base_url or "").strip().rstrip("/")
    if base.startswith("http") and base not in remote_urls:
        remote_urls.append(base)
    for url in remote_urls:
        endpoints.append({"kind": "remote", "url": url, "priority": len(remote_urls)})

    host = ""
    try:
        host = socket.gethostname()
    except OSError:
        host = ""
    db = get_service(request).db
    payload = {
        "v": APP_API_VERSION,
        "name": f"{host} 微信待办".strip(),
        "token": settings.app_token,
        "endpoints": [{"kind": item["kind"], "url": item["url"]} for item in endpoints],
        "ts": now_iso(),
    }
    code = base64.urlsafe_b64encode(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return {
        "v": APP_API_VERSION,
        "name": payload["name"],
        "token": settings.app_token,
        "endpoints": endpoints,
        "code": code,
        "lan_urls": [item["url"] for item in endpoints if item["kind"] == "lan"],
        "remote_urls": [item["url"] for item in endpoints if item["kind"] == "remote"],
        "app_last_sync": db.get_meta("app_last_sync"),
        "app_last_sync_client": db.get_meta("app_last_sync_client"),
        "task_count": len(db.list_tasks("pending", 1000)),
    }


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------
@router.get("/ping")
async def app_ping(request: Request) -> dict:
    require_token(request)
    return {
        "ok": True,
        "app": "wechat-task-hub",
        "version": __version__,
        "api_version": APP_API_VERSION,
        "server_time": now_iso(),
        "tz": os.getenv("APP_TZ", "Asia/Shanghai"),
    }


@router.get("/pairing")
async def app_pairing(request: Request) -> dict:
    require_token(request)
    return _pairing_payload(request)


@router.get("/sync")
async def app_sync(
    request: Request,
    cursor: int = Query(default=0, ge=0),
    limit: int = Query(default=500, ge=1, le=2000),
) -> dict:
    """增量/全量拉取任务。cursor=0（首次）或非法时返回全量快照。"""
    require_token(request)
    service = get_service(request)
    db = service.db
    db.reactivate_snoozed()  # 到期的「稍后」任务回到待办，App 立刻能看到
    try:
        db.set_meta("app_last_sync", now_iso())
        db.set_meta("app_last_sync_client", request.client.host if request.client else "")
    except Exception:  # 统计信息写失败不影响同步
        pass

    max_rev = db.current_rev()
    full = cursor <= 0 or cursor > max_rev
    if full:
        tasks = db.all_tasks()
        deleted: list[str] = []
        out_cursor = max_rev
        has_more = False
    else:
        task_rows = db.tasks_since(cursor, limit)
        tomb_rows = db.tombstones_since(cursor, limit)
        tasks_truncated = len(task_rows) >= limit
        tomb_truncated = len(tomb_rows) >= limit
        tasks = task_rows
        deleted = [row["id"] for row in tomb_rows]
        has_more = tasks_truncated or tomb_truncated
        out_cursor = max_rev
        if tasks_truncated and task_rows:
            out_cursor = min(out_cursor, int(task_rows[-1].get("rev") or 0))
        if tomb_truncated and tomb_rows:
            out_cursor = min(out_cursor, int(tomb_rows[-1].get("rev") or 0))
        if has_more and out_cursor <= cursor:
            # 极端情况下避免原地打转
            out_cursor = cursor + 1

    return {
        "server_time": now_iso(),
        "cursor": out_cursor,
        "full": full,
        "has_more": has_more,
        "tasks": [_task_dto(task) for task in tasks],
        "deleted": deleted,
        "counts": db.counts(),
    }


@router.get("/tasks/{task_id}")
async def app_task_detail(task_id: str, request: Request) -> dict:
    require_token(request)
    service = get_service(request)
    db = service.db
    task = db.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    event = db.get_event(task.get("event_id") or "")
    messages = db.messages_by_ids(task.get("source_msg_ids") or [])
    sources = []
    for message in messages:
        ts = int(message.get("ts") or 0)
        try:
            iso = to_iso(datetime.fromtimestamp(ts, local_tz()).replace(tzinfo=None))
        except (OverflowError, OSError, ValueError):
            iso = ""
        sources.append(
            {
                "rawid": message.get("rawid") or "",
                "group_name": message.get("group_name") or "",
                "sender": message.get("sender") or "",
                "content": message.get("content") or "",
                "ts": iso,
            }
        )
    return {
        "task": _task_dto(task),
        "event": (
            {
                "id": event.get("id") or "",
                "title": event.get("title") or "",
                "summary": event.get("summary") or "",
                "status": event.get("status") or "",
                "group_name": event.get("group_name") or "",
            }
            if event
            else None
        ),
        "sources": sources,
    }


def _apply_action(db, action: AppAction, base: dict | None = None, trusted: bool = False) -> dict:
    """执行单条操作；返回写入幂等表的结果（不含 deduped 字段）。

    base 是同一批里该任务首次出现时的服务端状态（rev/updated_at），用于批内 stale 判定；
    见 _is_stale 与 app_actions 里的「批内基准」说明。
    trusted=True 表示该任务由本客户端在**同一批里刚创建**（或本批 create 去重命中），
    电脑端不可能在批内插入修改；此时 stale 一律视为 false，避免 create 后同批的 update
    因 client_rev 还是本地初值（0）被误判 stale 而丢弃（用户对新建任务的编辑会静默丢失）。
    """
    kind = action.type

    if kind == "create":
        fields = action.fields or {}
        try:
            title = _norm_title(fields.get("title"))
            notes = _norm_notes(fields.get("notes"))
            due_at = _norm_due(fields.get("due_at"))
            priority = _norm_priority(fields.get("priority"))
        except ValueError as error:
            return _action_error(action.action_id, str(error))
        task, _created = db.create_task(
            title=title,
            notes=notes,
            due_at=due_at,
            priority=priority,
            group_name=MANUAL_GROUP,
        )
        return _action_ok(action.action_id, task)

    task = db.get_task(action.task_id or "") if action.task_id else None
    if task is None:
        return _action_error(action.action_id, "not_found")
    stale = False if trusted else _is_stale(task, action, base)

    if kind == "delete":
        db.delete_task(task["id"])
        return _action_ok(action.action_id, None, stale=stale)

    if kind in ("complete", "uncomplete", "ignore", "restore", "snooze"):
        fields: dict[str, Any] = {}
        if kind == "complete":
            fields["status"] = "done"
        elif kind == "uncomplete":
            fields["status"] = "pending"
        elif kind == "ignore":
            fields["status"] = "ignored"
        elif kind == "restore":
            fields["status"] = "pending"
        else:  # snooze
            fields["status"] = "snoozed"
            try:
                fields["snooze_until"] = _norm_due(action.snooze_until) or to_iso(
                    now_local().replace(microsecond=0)
                )
            except ValueError:
                fields["snooze_until"] = None
        updated = db.update_task(task["id"], fields)
        return _action_ok(action.action_id, updated, stale=stale)

    # update
    if stale:
        # 电脑端在手机操作之后又改过：不覆盖电脑端，回最新值让 App 刷新
        return _action_ok(action.action_id, db.get_task(task["id"]), stale=True)
    raw_fields = action.fields or {}
    updates: dict[str, Any] = {}
    try:
        if "title" in raw_fields:
            updates["title"] = _norm_title(raw_fields.get("title"))
        if "notes" in raw_fields:
            updates["notes"] = _norm_notes(raw_fields.get("notes"))
        if "due_at" in raw_fields:
            updates["due_at"] = _norm_due(raw_fields.get("due_at"))
        if "priority" in raw_fields:
            updates["priority"] = _norm_priority(raw_fields.get("priority"))
    except ValueError as error:
        return _action_error(action.action_id, str(error))
    if not updates:
        return _action_ok(action.action_id, task, stale=False)
    updated = db.update_task(task["id"], updates)
    return _action_ok(action.action_id, updated, stale=False)


@router.post("/actions")
async def app_actions(payload: AppActionsRequest, request: Request) -> dict:
    require_token(request)
    service = get_service(request)
    db = service.db
    if len(payload.actions) > 50:
        raise HTTPException(status_code=422, detail="一次最多 50 条操作")

    results: list[dict] = []
    # 「批内基准」：同一批里同一任务可能有多条操作（如离线积压的 complete → restore → update），
    # 它们都带 App 本地同一个旧 client_rev。单请求内没有 await、不可能有外部写入，所以本批
    # 首次看到某任务时记下当时的服务端 rev/updated_at；该任务本批后续动作一律与基准比较，
    # 避免被本客户端前面的操作抬高 rev 后误判 stale。真实电脑端冲突（批内首次动作就与基准
    # 不符）仍会被正确判 stale，update 依旧不覆盖电脑端。
    batch_base: dict[str, dict] = {}
    # 「同批 local_id 解析」：手机离线新建任务后立刻编辑时，outbox 是
    # [create(local_id=local-x), update(task_id=local-x)] 同一批发；后一条还没拿到真实 id。
    # 这里登记本批 create 成功 / 去重命中返回的 local_id → 真实 id，后续动作先解析 task_id，
    # 否则 update 会返回 not_found（App 收到 not_found 会删本地行并丢操作，用户编辑静默丢失）。
    local_map: dict[str, str] = {}
    # 本批由本客户端新建的任务 id：批内不可能有电脑端并发修改，这些任务的 stale 恒为 false。
    created_in_batch: set[str] = set()
    for action in payload.actions:
        cached = db.get_app_action(action.action_id)
        if cached is not None:
            try:
                stored = json.loads(cached.get("result") or "{}")
            except ValueError:
                stored = {}
            if not isinstance(stored, dict):
                stored = {}
            # 整批重发时 create 已去重：仍要登记 local_id，同批后续新动作才能解析到真实任务
            if action.type == "create" and action.local_id:
                stored_task = stored.get("task")
                if stored.get("ok") and isinstance(stored_task, dict) and stored_task.get("id"):
                    real_id = str(stored_task["id"])
                    local_map[action.local_id] = real_id
                    created_in_batch.add(real_id)
            stored["action_id"] = action.action_id
            stored["deduped"] = True
            results.append(stored)
            continue
        if action.task_id and action.task_id in local_map:
            action.task_id = local_map[action.task_id]  # 同批 create 的 local_id → 真实 task_id
        trusted = bool(action.task_id and action.task_id in created_in_batch)
        if not trusted and action.task_id and action.task_id not in batch_base:
            base_task = db.get_task(action.task_id)
            if base_task is not None:
                batch_base[action.task_id] = {
                    "rev": int(base_task.get("rev") or 0),
                    "updated_at": str(base_task.get("updated_at") or ""),
                }
        base = batch_base.get(action.task_id) if action.task_id else None
        try:
            result = _apply_action(db, action, base, trusted=trusted)
        except Exception as error:  # 单条失败不影响后续
            result = _action_error(action.action_id, f"internal: {error}")
        db.save_app_action(action.action_id, result)
        result["deduped"] = False
        results.append(result)
        # create 成功新建：登记 local_id → 真实 id，同批后续动作（update/complete/delete）要用
        if action.type == "create" and action.local_id and result.get("ok"):
            created_task = result.get("task")
            if isinstance(created_task, dict) and created_task.get("id"):
                real_id = str(created_task["id"])
                local_map[action.local_id] = real_id
                created_in_batch.add(real_id)

    return {"server_time": now_iso(), "cursor": db.current_rev(), "results": results}
