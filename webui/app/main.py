from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import get_config
from .db import get_conn, init_db
from .routers.system import router as system_router
from .schemas import (
    CookieJsonParseRequest,
    CookieProfileUpsertRequest,
    EnvImportRequest,
    TaskTemplateCreateRequest,
)
from .security import encrypt_text, encryption_configured, verify_basic_auth
from .services.cookie_service import (
    cookie_header_from_json_text,
    cookie_pairs_from_json_text,
    infer_site_from_json_text,
)
from .services.env_service import export_settings_to_env, import_env_to_settings
from .services.preview_service import preview_epub_file, preview_text_file
from .services.settings_service import (
    DEFAULT_SETTINGS,
    SOURCE_TYPES,
    load_settings,
    load_task_payload,
    mask_for_display,
    merged_settings,
    save_settings,
    validate_task_payload,
    validate_translation_settings,
)
from .services.task_service import (
    cancel_task,
    count_cookie_profile_task_refs,
    create_or_update_cookie_profile,
    create_task,
    create_task_template,
    delete_cookie_profile,
    detach_cookie_profile_from_non_running_tasks,
    get_artifact,
    get_logs_after,
    get_task,
    get_task_template,
    list_artifacts,
    list_cookie_profiles,
    list_task_templates,
    list_tasks,
)
from .services.worker import TaskWorker


app = FastAPI(title="Novel Grab + Translate WebUI")
cfg = get_config()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
app.mount("/static", StaticFiles(directory=str(Path(__file__).resolve().parent / "static")), name="static")
app.include_router(system_router)

worker: TaskWorker | None = None


@app.on_event("startup")
def _startup() -> None:
    global worker

    if cfg.enforce_secure_defaults:
        if cfg.basic_auth_user == "admin" and cfg.basic_auth_password == "change_me":
            raise RuntimeError(
                "Insecure default Basic Auth credentials are not allowed when WEBUI_ENFORCE_SECURE_DEFAULTS=true"
            )
        if not encryption_configured():
            raise RuntimeError(
                "Valid WEBUI_SECRET_KEY is required when WEBUI_ENFORCE_SECURE_DEFAULTS=true"
            )

    if cfg.require_secret_key and not encryption_configured():
        raise RuntimeError("WEBUI_SECRET_KEY is required when WEBUI_REQUIRE_SECRET_KEY=true")

    init_db()
    worker = TaskWorker()
    worker.start()


@app.on_event("shutdown")
def _shutdown() -> None:
    if worker:
        worker.stop()


def _row_to_dict(row: Any) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}


def _parse_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _safe_path(path_str: str) -> Path:
    path = Path(path_str).resolve()
    data_root = cfg.data_dir.resolve()
    try:
        path.relative_to(data_root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid path") from exc
    return path


def _find_artifact(conn, task_id: int, artifact_id: int) -> dict[str, Any]:
    row = get_artifact(conn, artifact_id)
    if not row or int(row["task_id"]) != task_id:
        raise HTTPException(status_code=404, detail="Artifact not found")
    return _row_to_dict(row)


def _preview_file(path: Path, page: int):
    if path.suffix.lower() == ".epub":
        return preview_epub_file(path, page=page)
    return preview_text_file(path, page=page, per_page=120)


@app.get("/", response_class=HTMLResponse)
def index(request: Request, _: str = Depends(verify_basic_auth)) -> HTMLResponse:
    with get_conn() as conn:
        tasks = [_row_to_dict(row) for row in list_tasks(conn, limit=200)]
    return templates.TemplateResponse(request, "index.html", {"tasks": tasks})


@app.get("/tasks/new", response_class=HTMLResponse)
def new_task_page(request: Request, _: str = Depends(verify_basic_auth)) -> HTMLResponse:
    with get_conn() as conn:
        settings = load_settings(conn)
        cookie_profiles = [_row_to_dict(row) for row in list_cookie_profiles(conn)]
        templates_list = [_row_to_dict(row) for row in list_task_templates(conn)]

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


@app.get("/tasks/{task_id}", response_class=HTMLResponse)
def task_detail_page(task_id: int, request: Request, _: str = Depends(verify_basic_auth)) -> HTMLResponse:
    with get_conn() as conn:
        row = get_task(conn, task_id)
        if not row:
            raise HTTPException(status_code=404, detail="Task not found")
        artifacts = [_row_to_dict(item) for item in list_artifacts(conn, task_id)]

    source_artifact = next((a for a in artifacts if a["kind"] == "source"), None)
    translated_artifact = next((a for a in artifacts if a["kind"] == "translated"), None)

    return templates.TemplateResponse(
        request,
        "task_detail.html",
        {
            "task": _row_to_dict(row),
            "artifacts": artifacts,
            "source_artifact_id": source_artifact["id"] if source_artifact else None,
            "translated_artifact_id": translated_artifact["id"] if translated_artifact else None,
        },
    )


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, _: str = Depends(verify_basic_auth)) -> HTMLResponse:
    with get_conn() as conn:
        settings = load_settings(conn)
        cookie_profiles = [_row_to_dict(row) for row in list_cookie_profiles(conn)]
        templates_list = [_row_to_dict(row) for row in list_task_templates(conn)]

    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "settings": mask_for_display(settings),
            "cookie_profiles": cookie_profiles,
            "templates": templates_list,
            "encryption_configured": encryption_configured(),
            "secret_key_required": cfg.require_secret_key,
        },
    )


@app.post("/api/settings")
async def api_save_settings(request: Request, _: str = Depends(verify_basic_auth)) -> JSONResponse:
    if request.headers.get("content-type", "").startswith("application/json"):
        body = await request.json()
        incoming = {k: str(v) for k, v in body.items()}
    else:
        form = await request.form()
        incoming = {k: str(v) for k, v in form.items() if not isinstance(v, UploadFile)}

    filtered = {k: v for k, v in incoming.items() if k in DEFAULT_SETTINGS}
    with get_conn() as conn:
        save_settings(conn, filtered)

    return JSONResponse({"ok": True})


@app.post("/api/settings/import-env")
async def api_import_env(request: Request, _: str = Depends(verify_basic_auth)) -> JSONResponse:
    raw_text = ""
    if request.headers.get("content-type", "").startswith("application/json"):
        body = await request.json()
        payload = EnvImportRequest.model_validate(body)
        raw_text = payload.env_text
    else:
        form = await request.form()
        raw_text = str(form.get("env_text", ""))
        env_file = form.get("env_file")
        if isinstance(env_file, UploadFile) and env_file.filename:
            raw_text = (await env_file.read()).decode("utf-8", errors="ignore")

    if not raw_text.strip():
        raise HTTPException(status_code=400, detail="env text or file is required")

    mapped = import_env_to_settings(raw_text)
    with get_conn() as conn:
        save_settings(conn, mapped)

    return JSONResponse({"ok": True, "imported_keys": sorted(mapped.keys())})


@app.get("/api/settings/export-env")
def api_export_env(_: str = Depends(verify_basic_auth)) -> PlainTextResponse:
    with get_conn() as conn:
        settings = load_settings(conn)
    text = export_settings_to_env(settings)
    return PlainTextResponse(text)


@app.post("/api/cookies/parse-json")
async def api_parse_cookie_json(request: Request, _: str = Depends(verify_basic_auth)) -> JSONResponse:
    raw_text = ""
    if request.headers.get("content-type", "").startswith("application/json"):
        body = await request.json()
        payload = CookieJsonParseRequest.model_validate(body)
        raw_text = payload.raw_text
    else:
        form = await request.form()
        raw_text = str(form.get("raw_text", ""))
        cookie_json_file = form.get("cookie_json_file")
        if isinstance(cookie_json_file, UploadFile) and cookie_json_file.filename:
            raw_text = (await cookie_json_file.read()).decode("utf-8", errors="ignore")

    if not raw_text.strip():
        raise HTTPException(status_code=400, detail="cookie json text or file is required")

    try:
        pairs = cookie_pairs_from_json_text(raw_text)
        header = cookie_header_from_json_text(raw_text)
        inferred_site = infer_site_from_json_text(raw_text)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"invalid cookie json: {exc}") from exc

    return JSONResponse(
        {
            "ok": True,
            "pairs": [{"name": k, "value": v} for k, v in pairs],
            "header": header,
            "inferred_site": inferred_site,
            "count": len(pairs),
        }
    )


@app.post("/api/cookies")
async def api_create_cookie_profile(request: Request, _: str = Depends(verify_basic_auth)) -> JSONResponse:
    inferred_site = ""
    allow_insecure = False

    if request.headers.get("content-type", "").startswith("application/json"):
        data = await request.json()
        allow_insecure = _parse_bool(data.get("allow_insecure"), default=False)

        payload = CookieProfileUpsertRequest.model_validate(data)
        name = payload.name.strip()
        site = payload.site.strip()
        cookie_value = payload.cookie.strip()
        profile_id = payload.profile_id
    else:
        form = await request.form()
        data = {k: v for k, v in form.items() if not isinstance(v, UploadFile)}
        allow_insecure = _parse_bool(data.get("allow_insecure"), default=False)

        name = str(data.get("name", "")).strip()
        site = str(data.get("site", "")).strip()
        cookie_value = str(data.get("cookie", "")).strip()
        profile_id_raw = str(data.get("profile_id", "")).strip()
        profile_id = int(profile_id_raw) if profile_id_raw else None

        cookie_json_file = form.get("cookie_json_file")
        if isinstance(cookie_json_file, UploadFile) and cookie_json_file.filename:
            raw_text = (await cookie_json_file.read()).decode("utf-8", errors="ignore").strip()
            if raw_text:
                try:
                    parsed_cookie = cookie_header_from_json_text(raw_text)
                    inferred_site = infer_site_from_json_text(raw_text)
                except Exception as exc:  # noqa: BLE001
                    raise HTTPException(status_code=400, detail=f"invalid cookie json: {exc}") from exc
                if cookie_value:
                    cookie_value = f"{cookie_value}; {parsed_cookie}"
                else:
                    cookie_value = parsed_cookie

    if not site and inferred_site:
        site = inferred_site
    if not site:
        site = "custom"

    if not name or not cookie_value:
        raise HTTPException(status_code=400, detail="name and cookie are required (cookie text or json file)")

    if not encryption_configured() and not allow_insecure:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "WEBUI_SECRET_KEY 未配置：为避免误以为 Cookie 已安全加密存储，默认禁止保存 Cookie 配置。请配置 WEBUI_SECRET_KEY 后重试；或勾选/确认“仍要以不安全回退密钥保存”。",
                "encryption_configured": False,
                "allow_insecure_supported": True,
            },
        )

    try:
        cookie_enc = encrypt_text(cookie_value)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=400,
            detail=f"invalid WEBUI_SECRET_KEY: {exc}",
        ) from exc

    with get_conn() as conn:
        try:
            new_id = create_or_update_cookie_profile(
                conn,
                profile_id=profile_id,
                name=name,
                site=site,
                cookie_enc=cookie_enc,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail="cookie profile not found") from exc
        except sqlite3.IntegrityError as exc:
            if "cookie_profiles.name" in str(exc):
                raise HTTPException(status_code=409, detail="cookie profile name already exists") from exc
            raise
    return JSONResponse({"ok": True, "id": new_id})


@app.delete("/api/cookies/{profile_id}")
def api_delete_cookie_profile(
    profile_id: int,
    force: bool = Query(default=False),
    _: str = Depends(verify_basic_auth),
) -> JSONResponse:
    with get_conn() as conn:
        total_refs, running_refs = count_cookie_profile_task_refs(conn, profile_id)

        if total_refs > 0 and not force:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "cookie profile is still referenced by tasks",
                    "total_refs": total_refs,
                    "running_refs": running_refs,
                    "force_supported": True,
                },
            )

        detached_refs = 0
        if force:
            if running_refs > 0:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": "cookie profile is referenced by running tasks",
                        "total_refs": total_refs,
                        "running_refs": running_refs,
                        "force_supported": False,
                    },
                )
            if total_refs > 0:
                detached_refs = detach_cookie_profile_from_non_running_tasks(conn, profile_id)

        try:
            deleted = delete_cookie_profile(conn, profile_id)
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="cookie profile is still referenced by tasks") from exc

    if not deleted:
        raise HTTPException(status_code=404, detail="cookie profile not found")

    return JSONResponse(
        {
            "ok": True,
            "deleted_id": profile_id,
            "forced": force,
            "detached_task_refs": detached_refs,
        }
    )


@app.post("/api/tasks")
async def api_create_task(
    request: Request,
    upload_file: UploadFile | None = File(default=None),
    _: str = Depends(verify_basic_auth),
) -> JSONResponse:
    form = await request.form()

    template_id_raw = str(form.get("template_id", "")).strip()
    if template_id_raw:
        with get_conn() as conn:
            tpl = get_task_template(conn, int(template_id_raw))
        if not tpl:
            raise HTTPException(status_code=404, detail="Template not found")
        payload = json.loads(tpl["payload_json"])
    else:
        payload = {}

    upload_path = ""
    if upload_file and upload_file.filename:
        suffix = Path(upload_file.filename).suffix.lower()
        temp_name = f"{uuid.uuid4().hex}{suffix}"
        target = cfg.upload_root / temp_name
        target.parent.mkdir(parents=True, exist_ok=True)

        with target.open("wb") as f:
            while True:
                chunk = await upload_file.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)

        await upload_file.close()
        upload_path = str(target)

    source_type = str(form.get("source_type", payload.get("source_type", "upload"))).strip()
    source_input = str(form.get("source_input", payload.get("source_input", ""))).strip()

    settings_overrides: dict[str, str] = dict(payload.get("settings_overrides", {}))
    for key, value in form.items():
        if isinstance(value, UploadFile):
            continue
        if key.startswith("override__"):
            settings_overrides[key.replace("override__", "", 1)] = str(value).strip()

    task_payload = {
        "mode": str(form.get("mode", payload.get("mode", "download_and_translate"))).strip(),
        "source_type": source_type,
        "source_input": source_input,
        "upload_path": upload_path or payload.get("upload_path", ""),
        "cookie_profile_id": int(str(form.get("cookie_profile_id", payload.get("cookie_profile_id", "0")) or "0"))
        or None,
        "backend": str(form.get("backend", payload.get("backend", "auto"))).strip(),
        "paid_policy": str(form.get("paid_policy", payload.get("paid_policy", "skip"))).strip(),
        "save_format": str(form.get("save_format", payload.get("save_format", "txt"))).strip(),
        "merge_all": _parse_bool(form.get("merge_all", payload.get("merge_all", "true")), default=True),
        "merged_name": str(form.get("merged_name", payload.get("merged_name", ""))).strip(),
        "record_chapter_number": _parse_bool(
            form.get("record_chapter_number", payload.get("record_chapter_number", "false")),
            default=False,
        ),
        "translate_mode": str(form.get("translate_mode", payload.get("translate_mode", "preview"))).strip(),
        "translation_output_mode": str(
            form.get("translation_output_mode", payload.get("translation_output_mode", "translated_only"))
        ).strip(),
        "test_num": str(form.get("test_num", payload.get("test_num", "80"))).strip(),
        "process_timeout": str(form.get("process_timeout", payload.get("process_timeout", ""))).strip(),
        "settings_overrides": settings_overrides,
    }

    if source_type == "upload":
        task_payload["source_input"] = ""

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

    return JSONResponse({"ok": True, "task_id": task_id})


@app.get("/api/tasks")
def api_list_tasks(_: str = Depends(verify_basic_auth)) -> JSONResponse:
    with get_conn() as conn:
        rows = list_tasks(conn, limit=300)
    data = [_row_to_dict(row) for row in rows]
    return JSONResponse({"items": data})


@app.get("/api/tasks/{task_id}")
def api_get_task(task_id: int, _: str = Depends(verify_basic_auth)) -> JSONResponse:
    with get_conn() as conn:
        row = get_task(conn, task_id)
        if not row:
            raise HTTPException(status_code=404, detail="Task not found")
        artifacts = [_row_to_dict(item) for item in list_artifacts(conn, task_id)]

    data = _row_to_dict(row)
    data["artifacts"] = artifacts
    return JSONResponse(data)


@app.get("/api/tasks/{task_id}/logs")
def api_task_logs(task_id: int, offset: int = Query(default=0), _: str = Depends(verify_basic_auth)) -> JSONResponse:
    with get_conn() as conn:
        logs = get_logs_after(conn, task_id, offset)
    items = [_row_to_dict(row) for row in logs]
    next_offset = items[-1]["id"] if items else offset
    return JSONResponse({"items": items, "next_offset": next_offset})


@app.get("/api/tasks/{task_id}/logs/stream")
async def api_task_logs_stream(
    task_id: int,
    request: Request,
    offset: int = Query(default=0, ge=0),
    _: str = Depends(verify_basic_auth),
) -> StreamingResponse:
    """
    SSE stream for task logs.

    Sends one event per log line:
      data: {"id":..., "created_at":..., "level":..., "message":...}

    Client can reconnect with last seen offset to resume.
    """

    async def _gen():
        nonlocal offset
        last_ping = time.monotonic()

        # initial comment to establish the stream
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
            else:
                now = time.monotonic()
                if now - last_ping >= 10.0:
                    yield ": ping\n\n"
                    last_ping = now

            await asyncio.sleep(0.5)

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # for nginx proxies
        },
    )


@app.post("/api/tasks/{task_id}/retry")
def api_retry_task(task_id: int, _: str = Depends(verify_basic_auth)) -> JSONResponse:
    with get_conn() as conn:
        row = get_task(conn, task_id)
        if not row:
            raise HTTPException(status_code=404, detail="Task not found")
        payload = load_task_payload(row)
        new_id = create_task(conn, payload, parent_task_id=task_id)
    return JSONResponse({"ok": True, "task_id": new_id})


@app.post("/api/tasks/{task_id}/run-full")
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
            source_path = _safe_path(source_path_raw)
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


@app.post("/api/tasks/{task_id}/cancel")
def api_cancel_task(task_id: int, _: str = Depends(verify_basic_auth)) -> JSONResponse:
    with get_conn() as conn:
        ok = cancel_task(conn, task_id)
    return JSONResponse({"ok": ok})


@app.post("/api/tasks/{task_id}/stop")
def api_stop_task(task_id: int, _: str = Depends(verify_basic_auth)) -> JSONResponse:
    if worker is None:
        raise HTTPException(status_code=500, detail="Worker is not initialized")
    ok = worker.stop_task(task_id)
    return JSONResponse({"ok": ok})


@app.get("/api/tasks/{task_id}/artifacts")
def api_task_artifacts(task_id: int, _: str = Depends(verify_basic_auth)) -> JSONResponse:
    with get_conn() as conn:
        items = [_row_to_dict(row) for row in list_artifacts(conn, task_id)]
    return JSONResponse({"items": items})


@app.get("/api/tasks/{task_id}/preview", response_class=HTMLResponse)
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
            left_artifact = _find_artifact(conn, task_id, artifact_id)
            left_path = _safe_path(left_artifact["file_path"])
        elif file:
            left_artifact = {"id": -1, "file_name": Path(file).name}
            left_path = _safe_path(file)
        else:
            raise HTTPException(status_code=400, detail="artifact_id or file is required")

        if not left_path.exists():
            raise HTTPException(status_code=404, detail="File not found")

        left_preview = _preview_file(left_path, page=page)

        compare_preview = None
        compare_artifact = None
        if compare_artifact_id is not None:
            compare_artifact = _find_artifact(conn, task_id, compare_artifact_id)
            right_path = _safe_path(compare_artifact["file_path"])
            if right_path.exists():
                compare_preview = _preview_file(right_path, page=page)

    total_pages = left_preview.total_pages
    if compare_preview:
        total_pages = max(total_pages, compare_preview.total_pages)

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


@app.get("/api/tasks/{task_id}/download")
def api_task_download(
    task_id: int,
    artifact_id: int = Query(...),
    _: str = Depends(verify_basic_auth),
) -> FileResponse:
    with get_conn() as conn:
        artifact = _find_artifact(conn, task_id, artifact_id)

    path = _safe_path(artifact["file_path"])
    if not path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(path=str(path), filename=artifact["file_name"])


@app.post("/api/templates")
async def api_save_template(
    payload: TaskTemplateCreateRequest,
    _: str = Depends(verify_basic_auth),
) -> JSONResponse:
    with get_conn() as conn:
        tid = create_task_template(conn, payload.name.strip(), payload.payload)

    return JSONResponse({"ok": True, "id": tid})
