from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from ..db import get_conn
from ..option_registry import SOURCE_TYPES
from ..runtime import get_app_config
from ..security import encryption_configured, verify_basic_auth
from ..services.settings_service import load_settings, mask_for_display
from ..services.task_payload_service import task_parallel_workers
from ..services.task_service import (
    count_tasks,
    get_task,
    list_artifacts,
    list_cookie_profiles,
    list_task_templates,
    list_tasks,
)
from ..services.task_management_service import normalize_page_size, row_to_dict
from ..ui import templates


router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    _: str = Depends(verify_basic_auth),
) -> HTMLResponse:
    page_size = normalize_page_size(page_size, default=50, maximum=200)
    offset = (page - 1) * page_size
    with get_conn() as conn:
        total = count_tasks(conn)
        tasks = [row_to_dict(row) for row in list_tasks(conn, limit=page_size, offset=offset)]
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "tasks": tasks,
            "page": page,
            "page_size": page_size,
            "has_prev": page > 1,
            "has_next": offset + len(tasks) < total,
            "total": total,
        },
    )


@router.get("/tasks/new", response_class=HTMLResponse)
def new_task_page(request: Request, _: str = Depends(verify_basic_auth)) -> HTMLResponse:
    with get_conn() as conn:
        settings = load_settings(conn)
        cookie_profiles = [row_to_dict(row) for row in list_cookie_profiles(conn)]
        templates_list = [row_to_dict(row) for row in list_task_templates(conn)]

    return templates.TemplateResponse(
        request,
        "new_task.html",
        {
            "settings": settings,
            "cookie_profiles": cookie_profiles,
            "templates": templates_list,
            "source_types": sorted(SOURCE_TYPES),
        },
    )


@router.get("/tasks/manage", response_class=HTMLResponse)
def tasks_manage_page(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=100, ge=1, le=500),
    _: str = Depends(verify_basic_auth),
) -> HTMLResponse:
    page_size = normalize_page_size(page_size, default=100, maximum=500)
    offset = (page - 1) * page_size
    with get_conn() as conn:
        total = count_tasks(conn)
        tasks = [row_to_dict(row) for row in list_tasks(conn, limit=page_size, offset=offset)]
    return templates.TemplateResponse(
        request,
        "task_manage.html",
        {
            "tasks": tasks,
            "page": page,
            "page_size": page_size,
            "has_prev": page > 1,
            "has_next": offset + len(tasks) < total,
            "total": total,
        },
    )


@router.get("/tasks/{task_id}", response_class=HTMLResponse)
def task_detail_page(task_id: int, request: Request, _: str = Depends(verify_basic_auth)) -> HTMLResponse:
    with get_conn() as conn:
        row = get_task(conn, task_id)
        if not row:
            raise HTTPException(status_code=404, detail="Task not found")
        artifacts = [row_to_dict(item) for item in list_artifacts(conn, task_id)]
        settings = load_settings(conn)

    source_artifact = next((artifact for artifact in artifacts if artifact["kind"] == "source"), None)
    translated_artifact = next((artifact for artifact in artifacts if artifact["kind"] == "translated"), None)
    task_data = row_to_dict(row)
    task_data["parallel_workers"] = task_parallel_workers(row, base_settings=settings)

    return templates.TemplateResponse(
        request,
        "task_detail.html",
        {
            "task": task_data,
            "artifacts": artifacts,
            "source_artifact_id": source_artifact["id"] if source_artifact else None,
            "translated_artifact_id": translated_artifact["id"] if translated_artifact else None,
        },
    )


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, _: str = Depends(verify_basic_auth)) -> HTMLResponse:
    with get_conn() as conn:
        settings = load_settings(conn)
        cookie_profiles = [row_to_dict(row) for row in list_cookie_profiles(conn)]
        templates_list = [row_to_dict(row) for row in list_task_templates(conn)]

    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "settings": mask_for_display(settings),
            "cookie_profiles": cookie_profiles,
            "templates": templates_list,
            "encryption_configured": encryption_configured(),
            "secret_key_required": get_app_config().require_secret_key,
        },
    )
