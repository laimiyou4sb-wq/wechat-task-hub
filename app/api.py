"""HTTP API。"""
from __future__ import annotations

import hmac
from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from .service import AppService
from .utils import now_local, parse_iso, to_iso

router = APIRouter(prefix="/api")


def get_service(request: Request) -> AppService:
    return request.app.state.service


def require_token(request: Request, token: str | None = None) -> None:
    expected = request.app.state.service.settings.app_token
    provided = request.headers.get("X-Auth-Token") or token or request.query_params.get("token") or ""
    if not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="unauthorized")


class TaskCreate(BaseModel):
    title: str
    due_at: str | None = None
    notes: str = ""
    priority: Literal["high", "normal", "low"] = "normal"


class TaskPatch(BaseModel):
    status: Literal["pending", "done", "ignored", "snoozed"] | None = None
    snooze_until: str | None = None
    title: str | None = None
    due_at: str | None = None
    priority: Literal["high", "normal", "low"] | None = None
    notes: str | None = None   # None=不修改；""=清空；内容截断到 2000 字


class LlmSettingsPatch(BaseModel):
    api_key: str | None = None   # None=不修改；""=清除
    base_url: str | None = None
    model: str | None = None
    enabled: bool | None = None


class PushSettingsPatch(BaseModel):
    bark_url: str | None = None
    ntfy_server: str | None = None
    ntfy_topic: str | None = None
    ntfy_token: str | None = None


class FilterSettingsPatch(BaseModel):
    include_private: bool | None = None
    whitelist_mode: Literal["all", "pinned", "custom", "pinned_custom"] | None = None
    group_whitelist: list[str] | None = None


class IdentitySettingsPatch(BaseModel):
    me_names: list[str] | None = None
    me_wxid: str | None = None
    identity_filter: bool | None = None
    identity_fuzzy: bool | None = None


class IdentityCheckRequest(BaseModel):
    text: str
    session_id: str = ""


class IdentityRefreshRequest(BaseModel):
    session_ids: list[str] | None = None


class FailedRetryRequest(BaseModel):
    rawids: list[str] | None = None   # None/空 = 按失败时间倒序重试一批
    limit: int = 500


def _clean_due(value: str | None) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    parsed = parse_iso(str(value))
    if parsed is None:
        raise HTTPException(status_code=422, detail="due_at 需要 ISO 时间，例如 2026-10-25T18:00:00")
    return to_iso(parsed)


@router.get("/health")
async def health(request: Request) -> dict:
    return get_service(request).health()


@router.get("/tasks")
async def list_tasks(
    request: Request,
    status: Literal["pending", "done", "ignored", "snoozed", "all"] = Query(default="pending"),
    limit: int = Query(default=200, ge=1, le=1000),
) -> dict:
    require_token(request)
    service = get_service(request)
    tasks = service.db.list_tasks(status, limit)
    return {"tasks": tasks, "counts": service.db.counts()}


@router.get("/tasks/{task_id}")
async def task_detail(task_id: str, request: Request) -> dict:
    require_token(request)
    detail = get_service(request).task_detail(task_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="task not found")
    return detail


@router.post("/tasks", status_code=201)
async def create_task(payload: TaskCreate, request: Request) -> dict:
    require_token(request)
    title = payload.title.strip()
    if not title:
        raise HTTPException(status_code=422, detail="title 不能为空")
    service = get_service(request)
    task, _ = service.db.create_task(
        title=title,
        notes=payload.notes.strip(),
        due_at=_clean_due(payload.due_at),
        priority=payload.priority,
        group_name="手动添加",
    )
    return {"task": task}


@router.patch("/tasks/{task_id}")
async def update_task(task_id: str, payload: TaskPatch, request: Request) -> dict:
    require_token(request)
    service = get_service(request)
    fields: dict = {}
    if payload.status is not None:
        fields["status"] = payload.status
    if payload.title is not None:
        if not payload.title.strip():
            raise HTTPException(status_code=422, detail="title 不能为空")
        fields["title"] = payload.title.strip()
    if payload.due_at is not None:
        fields["due_at"] = _clean_due(payload.due_at)
    if payload.priority is not None:
        fields["priority"] = payload.priority
    if payload.notes is not None:
        # 允许空串清空备注；超长截断，避免备注撑爆推送 / 前端渲染
        fields["notes"] = payload.notes.strip()[:2000]
    if payload.status == "snoozed" and payload.snooze_until is None:
        fields["snooze_until"] = to_iso(now_local() + timedelta(hours=1))
    elif payload.snooze_until is not None:
        fields["snooze_until"] = _clean_due(payload.snooze_until)
    task = service.db.update_task(task_id, fields)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    return {"task": task}


@router.delete("/tasks/{task_id}")
async def delete_task(task_id: str, request: Request) -> dict:
    """真删除任务。"""
    require_token(request)
    if not get_service(request).db.delete_task(task_id):
        raise HTTPException(status_code=404, detail="task not found")
    return {"ok": True, "id": task_id}


@router.get("/settings/llm")
async def get_llm_settings(request: Request) -> dict:
    require_token(request)
    return get_service(request).llm_settings()


@router.put("/settings/llm")
async def update_llm_settings(payload: LlmSettingsPatch, request: Request) -> dict:
    require_token(request)
    try:
        settings = get_service(request).update_llm_settings(
            api_key=payload.api_key,
            base_url=payload.base_url,
            model=payload.model,
            enabled=payload.enabled,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {"ok": True, **settings}


@router.get("/settings/push")
async def get_push_settings(request: Request) -> dict:
    require_token(request)
    return get_service(request).push_settings()


@router.put("/settings/push")
async def update_push_settings(payload: PushSettingsPatch, request: Request) -> dict:
    require_token(request)
    try:
        settings = get_service(request).update_push_settings(
            bark_url=payload.bark_url,
            ntfy_server=payload.ntfy_server,
            ntfy_topic=payload.ntfy_topic,
            ntfy_token=payload.ntfy_token,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {"ok": True, **settings}


@router.get("/settings/filters")
async def get_filter_settings(request: Request) -> dict:
    require_token(request)
    return get_service(request).filter_settings()


@router.put("/settings/filters")
async def update_filter_settings(payload: FilterSettingsPatch, request: Request) -> dict:
    require_token(request)
    try:
        settings = get_service(request).update_filter_settings(
            include_private=payload.include_private,
            whitelist_mode=payload.whitelist_mode,
            group_whitelist=payload.group_whitelist,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {"ok": True, **settings}


@router.get("/settings/identity")
async def get_identity_settings(request: Request) -> dict:
    require_token(request)
    return get_service(request).identity_settings()


@router.put("/settings/identity")
async def update_identity_settings(payload: IdentitySettingsPatch, request: Request) -> dict:
    require_token(request)
    try:
        return await get_service(request).update_identity_settings(
            me_names=payload.me_names,
            me_wxid=payload.me_wxid,
            identity_filter=payload.identity_filter,
            identity_fuzzy=payload.identity_fuzzy,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.post("/identity/refresh")
async def refresh_identity(request: Request, payload: IdentityRefreshRequest | None = None) -> dict:
    require_token(request)
    session_ids = payload.session_ids if payload is not None else None
    return await get_service(request).refresh_identity(session_ids)


@router.post("/identity/check")
async def check_identity(payload: IdentityCheckRequest, request: Request) -> dict:
    require_token(request)
    if not payload.text.strip():
        raise HTTPException(status_code=422, detail="text 不能为空")
    return await get_service(request).check_identity(payload.text, payload.session_id)


@router.get("/identity/judgments")
async def identity_judgments(request: Request) -> dict:
    require_token(request)
    return get_service(request).identity_judgments()


@router.get("/collect/state")
async def collection_state(request: Request) -> dict:
    """采集水位与参数快照：每会话上次采到哪里、单轮上限、待处理条数、上次结果。"""
    require_token(request)
    return get_service(request).collection_state()


@router.get("/catchup/state")
async def catchup_state(request: Request) -> dict:
    """兼容旧路径（deprecated）：等价于 GET /api/collect/state。"""
    require_token(request)
    return get_service(request).collection_state()


@router.get("/collect/failed")
async def failed_queue(
    request: Request,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """失败队列：单条消息处理失败次数超过 MESSAGE_MAX_ATTEMPTS 后的快照。"""
    require_token(request)
    return get_service(request).failed_queue(limit=limit, offset=offset)


@router.post("/collect/failed/retry")
async def retry_failed(request: Request, payload: FailedRetryRequest | None = None) -> dict:
    """把失败队列消息放回待处理（不传 rawids = 按失败时间倒序重试一批）。"""
    require_token(request)
    rawids = payload.rawids if payload is not None else None
    limit = payload.limit if payload is not None else 500
    return get_service(request).retry_failed(rawids, limit=limit)


@router.post("/pinned/refresh")
async def refresh_pinned(request: Request) -> dict:
    """立即同步「置顶」变化（在微信里置顶/取消置顶后不用等定时刷新）。"""
    require_token(request)
    service = get_service(request)
    added = await service.refresh_pinned()
    pinned = [
        {"session_id": session_id, "group_name": service.pinned_names.get(session_id, "")}
        for session_id in sorted(service.pinned_sessions)
    ]
    return {"ok": True, "added": added, "pinned_count": len(pinned), "pinned": pinned}


@router.post("/settings/llm/test")
async def test_llm(request: Request) -> dict:
    require_token(request)
    return await get_service(request).test_llm()


@router.post("/push/test")
async def push_test(request: Request) -> dict:
    require_token(request)
    return await get_service(request).test_push()


@router.post("/demo/seed")
async def demo_seed(request: Request) -> dict:
    require_token(request)
    service = get_service(request)
    if not service.settings.demo_mode:
        raise HTTPException(status_code=403, detail="demo mode disabled")
    stats = await service.seed_demo()
    return {"ok": True, **stats}

