from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse

from ..db import get_conn
from ..runtime import get_app_config, get_worker
from ..schemas import TaskBatchActionRequest, TaskPurgeRequest, TaskTemplateCreateRequest
from ..security import verify_basic_auth
from ..services.settings_service import (
    load_settings,
    load_task_payload,
    merged_settings,
    validate_task_payload,
    validate_translation_settings,
)
from ..services.task_management_service import (
    delete_task_records,
    finalize_task_delete,
    find_artifact,
    normalize_page_size,
    preview_file,
    purge_task_outputs,
    row_to_dict,
    safe_delete_upload_file,
    safe_path,
    safe_task_file_path,
)
from ..services.task_payload_service import build_task_payload, task_parallel_workers
from ..services.task_service import (
    cancel_task,
    count_tasks,
    create_task,
    create_task_template,
    get_logs_after,
    get_task,
    get_task_template,
    list_artifacts,
    list_tasks,
    resume_task,
)
from ..ui import templates


router = APIRouter()


@router.post("/api/tasks")
async def api_create_task(
    request: Request,
    upload_file: UploadFile | None = File(default=None),
    _: str = Depends(verify_basic_auth),
) -> JSONResponse:
    form = await request.form()

    template_id_raw = str(form.get("template_id", "")).strip()
    if template_id_raw:
        with get_conn() as conn:
            template_row = get_task_template(conn, int(template_id_raw))
        if not template_row:
            raise HTTPException(status_code=404, detail="Template not found")
        payload = load_task_payload(template_row)
    else:
        payload = {}

    upload_path = ""
    task_id: int | None = None
    if upload_file and upload_file.filename:
        suffix = Path(upload_file.filename).suffix.lower()
        temp_name = f"{uuid.uuid4().hex}{suffix}"
        target = get_app_config().upload_root / temp_name
        target.parent.mkdir(parents=True, exist_ok=True)

        with target.open("wb") as file_obj:
            while True:
                chunk = await upload_file.read(1024 * 1024)
                if not chunk:
                    break
                file_obj.write(chunk)

        await upload_file.close()
        upload_path = str(target)

    task_payload_model = build_task_payload(form, payload, upload_path)
    task_payload = task_payload_model.to_record()

    try:
        validation = validate_task_payload(task_payload)
        if not validation.ok:
            raise HTTPException(status_code=400, detail=validation.message)

        with get_conn() as conn:
            base_settings = load_settings(conn)
            effective_settings = merged_settings(base_settings, task_payload.get("settings_overrides", {}))
            settings_validation = validate_translation_settings(effective_settings)
            if not settings_validation.ok:
                raise HTTPException(status_code=400, detail=settings_validation.message)

            task_id = create_task(conn, task_payload)
            template_name = str(form.get("save_as_template", "")).strip()
            if template_name:
                create_task_template(conn, template_name, task_payload)
    except Exception:
        if upload_path and task_id is None:
            safe_delete_upload_file(upload_path, get_app_config())
        raise

    return JSONResponse({"ok": True, "task_id": task_id})


@router.get("/api/tasks")
def api_list_tasks(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=100, ge=1, le=500),
    _: str = Depends(verify_basic_auth),
) -> JSONResponse:
    page_size = normalize_page_size(page_size, default=100, maximum=500)
    offset = (page - 1) * page_size
    with get_conn() as conn:
        total = count_tasks(conn)
        rows = list_tasks(conn, limit=page_size, offset=offset)
    return JSONResponse(
        {
            "items": [row_to_dict(row) for row in rows],
            "page": page,
            "page_size": page_size,
            "total": total,
        }
    )


@router.get("/api/tasks/{task_id}")
def api_get_task(task_id: int, _: str = Depends(verify_basic_auth)) -> JSONResponse:
    with get_conn() as conn:
        row = get_task(conn, task_id)
        if not row:
            raise HTTPException(status_code=404, detail="Task not found")
        artifacts = [row_to_dict(item) for item in list_artifacts(conn, task_id)]
        settings = load_settings(conn)

    data = row_to_dict(row)
    data["parallel_workers"] = task_parallel_workers(row, base_settings=settings)
    data["artifacts"] = artifacts
    return JSONResponse(data)


@router.post("/api/tasks/{task_id}/purge")
async def api_purge_task(
    task_id: int,
    request: Request,
    _: str = Depends(verify_basic_auth),
) -> JSONResponse:
    if request.headers.get("content-type", "").startswith("application/json"):
        body = await request.json()
        payload = TaskPurgeRequest.model_validate(body)
    else:
        form = await request.form()
        payload = TaskPurgeRequest.model_validate(
            {
                "scope": str(form.get("scope", "downloads")).strip() or "downloads",
                "delete_upload": str(form.get("delete_upload", "false")).strip().lower() in {"1", "true", "yes", "on"},
                "force": str(form.get("force", "false")).strip().lower() in {"1", "true", "yes", "on"},
            }
        )

    with get_conn() as conn:
        deleted_paths, deleted_upload = purge_task_outputs(
            conn,
            task_id,
            scope=payload.scope,
            delete_upload=payload.delete_upload,
            force=payload.force,
            cfg=get_app_config(),
        )

    return JSONResponse(
        {
            "ok": True,
            "task_id": task_id,
            "scope": payload.scope,
            "deleted_paths": deleted_paths,
            "deleted_upload": deleted_upload,
        }
    )


@router.delete("/api/tasks/{task_id}")
def api_delete_task(
    task_id: int,
    force: bool = Query(default=False),
    delete_task_dir: bool = Query(default=True),
    delete_upload: bool = Query(default=False),
    cascade: bool = Query(default=False),
    _: str = Depends(verify_basic_auth),
) -> JSONResponse:
    with get_conn() as conn:
        deleted_ids, upload_paths = delete_task_records(conn, task_id, force=force, cascade=cascade)

    deleted_paths, deleted_upload_count = finalize_task_delete(
        deleted_ids,
        upload_paths,
        delete_task_dir=delete_task_dir,
        delete_upload=delete_upload,
        cfg=get_app_config(),
    )

    return JSONResponse(
        {
            "ok": True,
            "deleted_id": task_id,
            "deleted_ids": deleted_ids,
            "deleted_paths": deleted_paths,
            "deleted_upload_count": deleted_upload_count,
            "cascade": cascade,
        }
    )


@router.post("/api/tasks/manage/batch-purge")
async def api_batch_purge_tasks(
    request: Request,
    _: str = Depends(verify_basic_auth),
) -> JSONResponse:
    body = await request.json()
    payload = TaskBatchActionRequest.model_validate(body)

    task_ids = sorted({int(task_id) for task_id in payload.task_ids if int(task_id) > 0})
    if not task_ids:
        raise HTTPException(status_code=400, detail="task_ids is required")

    deleted_count = 0
    errors: list[dict[str, Any]] = []

    for task_id in task_ids:
        try:
            with get_conn() as conn:
                purge_task_outputs(
                    conn,
                    task_id,
                    scope=payload.scope,
                    delete_upload=payload.delete_upload,
                    force=payload.force,
                    cfg=get_app_config(),
                )
            deleted_count += 1
        except HTTPException as exc:
            errors.append({"task_id": task_id, "reason": str(exc.detail)})
        except Exception as exc:  # noqa: BLE001
            errors.append({"task_id": task_id, "reason": str(exc)})

    return JSONResponse({"ok": True, "processed": len(task_ids), "succeeded": deleted_count, "errors": errors})


@router.post("/api/tasks/manage/batch-delete")
async def api_batch_delete_tasks(
    request: Request,
    _: str = Depends(verify_basic_auth),
) -> JSONResponse:
    body = await request.json()
    payload = TaskBatchActionRequest.model_validate(body)

    task_ids = sorted({int(task_id) for task_id in payload.task_ids if int(task_id) > 0})
    if not task_ids:
        raise HTTPException(status_code=400, detail="task_ids is required")

    deleted_count = 0
    deleted_task_ids: list[int] = []
    errors: list[dict[str, Any]] = []

    for task_id in task_ids:
        try:
            with get_conn() as conn:
                per_deleted, upload_paths = delete_task_records(
                    conn,
                    task_id,
                    force=payload.force,
                    cascade=payload.cascade,
                )

            finalize_task_delete(
                per_deleted,
                upload_paths,
                delete_task_dir=True,
                delete_upload=payload.delete_upload,
                cfg=get_app_config(),
            )
            deleted_count += 1
            deleted_task_ids.extend(per_deleted)
        except sqlite3.IntegrityError:
            errors.append({"task_id": task_id, "reason": "has child tasks (use cascade=true)"})
        except HTTPException as exc:
            errors.append({"task_id": task_id, "reason": str(exc.detail)})
        except Exception as exc:  # noqa: BLE001
            errors.append({"task_id": task_id, "reason": str(exc)})

    return JSONResponse(
        {
            "ok": True,
            "processed": len(task_ids),
            "succeeded": deleted_count,
            "deleted_task_ids": sorted(set(deleted_task_ids)),
            "cascade": payload.cascade,
            "errors": errors,
        }
    )


@router.get("/api/tasks/{task_id}/logs")
def api_task_logs(task_id: int, offset: int = Query(default=0), _: str = Depends(verify_basic_auth)) -> JSONResponse:
    with get_conn() as conn:
        logs = get_logs_after(conn, task_id, offset)
    items = [row_to_dict(row) for row in logs]
    next_offset = items[-1]["id"] if items else offset
    return JSONResponse({"items": items, "next_offset": next_offset})


@router.get("/api/tasks/{task_id}/logs/stream")
async def api_task_logs_stream(
    task_id: int,
    request: Request,
    offset: int = Query(default=0, ge=0),
    _: str = Depends(verify_basic_auth),
) -> StreamingResponse:
    async def _gen():
        nonlocal offset
        last_ping = time.monotonic()
        yield ": ok\n\n"

        while True:
            if await request.is_disconnected():
                break

            with get_conn() as conn:
                rows = get_logs_after(conn, task_id, offset)

            if rows:
                for row in rows:
                    payload = {
                        "id": int(row["id"]),
                        "created_at": row["created_at"],
                        "level": row["level"],
                        "message": row["message"],
                    }
                    offset = int(row["id"])
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                last_ping = time.monotonic()
            elif time.monotonic() - last_ping >= 10.0:
                yield ": ping\n\n"
                last_ping = time.monotonic()

            await asyncio.sleep(0.5)

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/api/tasks/{task_id}/retry")
def api_retry_task(task_id: int, _: str = Depends(verify_basic_auth)) -> JSONResponse:
    with get_conn() as conn:
        row = get_task(conn, task_id)
        if not row:
            raise HTTPException(status_code=404, detail="Task not found")
        payload = load_task_payload(row)
        new_id = create_task(conn, payload, parent_task_id=task_id)
    return JSONResponse({"ok": True, "task_id": new_id})


@router.post("/api/tasks/{task_id}/run-full")
def api_run_full_task(task_id: int, _: str = Depends(verify_basic_auth)) -> JSONResponse:
    with get_conn() as conn:
        row = get_task(conn, task_id)
        if not row:
            raise HTTPException(status_code=404, detail="Task not found")

        payload = load_task_payload(row)
        payload["mode"] = "download_and_translate"
        payload["translate_mode"] = "full"

        reused_source = False
        source_path_raw = str(row["source_full_book_path"] or "").strip()
        if source_path_raw:
            source_path = safe_path(source_path_raw, get_app_config())
            if source_path.exists() and source_path.is_file():
                payload["source_type"] = "upload"
                payload["upload_path"] = str(source_path)
                payload["source_input"] = ""
                payload["cookie_profile_id"] = None
                reused_source = True

        validation = validate_task_payload(payload)
        if not validation.ok:
            raise HTTPException(status_code=400, detail=validation.message)

        new_id = create_task(conn, payload, parent_task_id=task_id)

    return JSONResponse({"ok": True, "task_id": new_id, "reused_source": reused_source})


@router.post("/api/tasks/{task_id}/cancel")
def api_cancel_task(task_id: int, _: str = Depends(verify_basic_auth)) -> JSONResponse:
    with get_conn() as conn:
        row = get_task(conn, task_id)
        if not row:
            raise HTTPException(status_code=404, detail="Task not found")
        if row["status"] != "queued":
            raise HTTPException(status_code=409, detail="Only queued tasks can be canceled")
        ok = cancel_task(conn, task_id)
    return JSONResponse({"ok": ok})


@router.post("/api/tasks/{task_id}/stop")
def api_stop_task(task_id: int, _: str = Depends(verify_basic_auth)) -> JSONResponse:
    worker = get_worker()
    if worker is None:
        raise HTTPException(status_code=500, detail="Worker is not initialized")
    return JSONResponse({"ok": worker.stop_task(task_id)})


@router.post("/api/tasks/{task_id}/pause")
def api_pause_task(task_id: int, _: str = Depends(verify_basic_auth)) -> JSONResponse:
    worker = get_worker()
    if worker is None:
        raise HTTPException(status_code=500, detail="Worker is not initialized")
    return JSONResponse({"ok": worker.pause_task(task_id)})


@router.post("/api/tasks/{task_id}/resume")
def api_resume_task(task_id: int, _: str = Depends(verify_basic_auth)) -> JSONResponse:
    with get_conn() as conn:
        ok = resume_task(conn, task_id)
    return JSONResponse({"ok": ok})


@router.get("/api/tasks/{task_id}/artifacts")
def api_task_artifacts(task_id: int, _: str = Depends(verify_basic_auth)) -> JSONResponse:
    with get_conn() as conn:
        row = get_task(conn, task_id)
        if not row:
            raise HTTPException(status_code=404, detail="Task not found")
        items = [row_to_dict(item) for item in list_artifacts(conn, task_id)]
    return JSONResponse({"items": items})


@router.get("/api/tasks/{task_id}/preview", response_class=HTMLResponse)
def api_task_preview(
    task_id: int,
    request: Request,
    artifact_id: int | None = Query(default=None),
    compare_artifact_id: int | None = Query(default=None),
    file: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    _: str = Depends(verify_basic_auth),
) -> HTMLResponse:
    with get_conn() as conn:
        if artifact_id is not None:
            left_artifact = find_artifact(conn, task_id, artifact_id)
            left_path = safe_path(left_artifact["file_path"], get_app_config())
        elif file:
            left_artifact = {"id": -1, "file_name": Path(file).name}
            left_path = safe_task_file_path(task_id, file, get_app_config())
        else:
            raise HTTPException(status_code=400, detail="artifact_id or file is required")

        if not left_path.exists():
            raise HTTPException(status_code=404, detail="File not found")

        left_preview = preview_file(left_path, page=page)
        compare_preview = None
        compare_artifact = None
        if compare_artifact_id is not None:
            compare_artifact = find_artifact(conn, task_id, compare_artifact_id)
            right_path = safe_path(compare_artifact["file_path"], get_app_config())
            if right_path.exists():
                compare_preview = preview_file(right_path, page=page)

    total_pages = max(
        left_preview.total_pages,
        compare_preview.total_pages if compare_preview else left_preview.total_pages,
    )
    return templates.TemplateResponse(
        request,
        "preview.html",
        {
            "task_id": task_id,
            "title": left_preview.title,
            "lines": left_preview.lines,
            "page": page,
            "total_pages": total_pages,
            "artifact_path": str(left_path),
            "artifact_id": left_artifact.get("id"),
            "compare_artifact": compare_artifact,
            "compare_preview": compare_preview,
            "compare_artifact_id": compare_artifact_id,
        },
    )


@router.get("/api/tasks/{task_id}/download")
def api_task_download(
    task_id: int,
    artifact_id: int = Query(...),
    _: str = Depends(verify_basic_auth),
) -> FileResponse:
    with get_conn() as conn:
        artifact = find_artifact(conn, task_id, artifact_id)

    path = safe_path(artifact["file_path"], get_app_config())
    if not path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path=str(path), filename=artifact["file_name"])


@router.post("/api/templates")
async def api_save_template(
    payload: TaskTemplateCreateRequest,
    _: str = Depends(verify_basic_auth),
) -> JSONResponse:
    with get_conn() as conn:
        template_id = create_task_template(conn, payload.name.strip(), payload.payload)

    return JSONResponse({"ok": True, "id": template_id})
