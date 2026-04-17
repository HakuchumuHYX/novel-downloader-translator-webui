from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import AppConfig, get_config
from .db import get_conn, init_db
from .routers.system import router as system_router
from .schemas import (
    CookieJsonParseRequest,
    CookieProfileUpsertRequest,
    EnvImportRequest,
    TaskBatchActionRequest,
    TaskPurgeRequest,
    TaskTemplateCreateRequest,
)
from .security import encrypt_text, encryption_configured, verify_basic_auth
from .services.cookie_service import (
    cookie_header_from_json_text,
    cookie_pairs_from_json_text,
    infer_site_from_json_text,
)
from .services.env_service import export_settings_to_env, import_env_to_settings
from .services.preview_service import preview_epub_file, preview_pdf_file, preview_text_file
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
    clear_artifacts,
    clear_task_output_paths,
    count_tasks,
    count_cookie_profile_task_refs,
    create_or_update_cookie_profile,
    create_task,
    create_task_template,
    delete_cookie_profile,
    delete_task as delete_task_row,
    detach_cookie_profile_from_non_running_tasks,
    get_artifact,
    get_logs_after,
    get_task,
    get_task_template,
    list_artifacts,
    list_cookie_profiles,
    list_task_templates,
    list_tasks,
    list_tasks_by_ids,
    list_task_descendants,
    reconcile_orphan_running_tasks,
    resume_task,
)
from .services.worker import TaskWorker


templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
cfg: AppConfig | None = None
worker: TaskWorker | None = None


def _get_cfg() -> AppConfig:
    global cfg
    if cfg is None:
        cfg = get_config()
    return cfg


@asynccontextmanager
async def lifespan(_: FastAPI):
    global worker
    global cfg

    cfg = get_config()

    if cfg.enforce_secure_defaults:
        if cfg.basic_auth_user == "admin" and cfg.basic_auth_password == "change_me":
            raise RuntimeError(
                "Insecure default Basic Auth credentials are not allowed when WEBUI_ENFORCE_SECURE_DEFAULTS=true"
            )
        if not encryption_configured():
            raise RuntimeError("Valid WEBUI_SECRET_KEY is required when WEBUI_ENFORCE_SECURE_DEFAULTS=true")

    if cfg.require_secret_key and not encryption_configured():
        raise RuntimeError("WEBUI_SECRET_KEY is required when WEBUI_REQUIRE_SECRET_KEY=true")

    init_db()
    with get_conn() as conn:
        reconcile_orphan_running_tasks(conn)
    worker = TaskWorker()
    worker.start()
    try:
        yield
    finally:
        if worker:
            worker.stop()


app = FastAPI(title="Novel Grab + Translate WebUI", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(Path(__file__).resolve().parent / "static")), name="static")
app.include_router(system_router)


def _row_to_dict(row: Any) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}


def _parse_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _safe_path(path_str: str) -> Path:
    path = Path(path_str).resolve()
    data_root = _get_cfg().data_dir.resolve()
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
    suffix = path.suffix.lower()
    if suffix == ".epub":
        return preview_epub_file(path, page=page)
    if suffix == ".pdf":
        return preview_pdf_file(path, page=page)
    if suffix in {".txt", ".md", ".srt"}:
        return preview_text_file(path, page=page, per_page=120)
    raise HTTPException(status_code=400, detail=f"Preview is not supported for {suffix or 'this file type'}")


def _normalize_parallel_workers(value: Any, fallback: str = "5") -> str:
    try:
        ivalue = int(str(value).strip())
        if ivalue >= 1:
            return str(ivalue)
    except Exception:
        pass
    return fallback


def _task_parallel_workers(task_row: Any, base_settings: dict[str, str] | None = None) -> str:
    fallback = _normalize_parallel_workers(
        (base_settings or {}).get("parallel_workers", "5"),
        fallback="5",
    )

    try:
        payload = json.loads(task_row["payload_json"])
        overrides = payload.get("settings_overrides", {}) or {}
        return _normalize_parallel_workers(overrides.get("parallel_workers", ""), fallback=fallback)
    except Exception:
        return fallback


def _safe_delete_dir(target: Path, root: Path) -> bool:
    target = target.resolve()
    root = root.resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Refusing to delete path outside allowed root: {target}") from exc

    if not target.exists():
        return False
    if target.is_dir():
        shutil.rmtree(target, ignore_errors=True)
        return True
    target.unlink(missing_ok=True)
    return True


def _safe_delete_upload_file(path_str: str) -> bool:
    if not path_str.strip():
        return False
    target = Path(path_str).resolve()
    try:
        target.relative_to(_get_cfg().upload_root.resolve())
    except ValueError:
        # Some tasks (for example run-full) intentionally reuse a source file
        # from a previous task directory rather than from the upload root.
        # Treat those paths as non-deletable uploads instead of failing the
        # whole management request after the DB row has already been removed.
        return False
    if not target.exists() or not target.is_file():
        return False
    target.unlink(missing_ok=True)
    return True


def _safe_task_file_path(task_id: int, path_str: str) -> Path:
    path = _safe_path(path_str)
    task_root = (_get_cfg().task_root / str(task_id)).resolve()
    try:
        path.relative_to(task_root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="file path is outside current task root") from exc
    return path


def _normalize_page_size(value: int, default: int, maximum: int) -> int:
    return max(1, min(maximum, value or default))


def _can_manage_task(status: str, force: bool) -> tuple[bool, str]:
    if status == "running":
        return False, "running task cannot be managed directly; stop it first"
    if status in {"queued", "paused"} and not force:
        return False, "queued/paused task requires force=true"
    return True, ""


@app.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    _: str = Depends(verify_basic_auth),
) -> HTMLResponse:
    page_size = _normalize_page_size(page_size, default=50, maximum=200)
    offset = (page - 1) * page_size
    with get_conn() as conn:
        total = count_tasks(conn)
        tasks = [_row_to_dict(row) for row in list_tasks(conn, limit=page_size, offset=offset)]
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


@app.get("/tasks/manage", response_class=HTMLResponse)
def tasks_manage_page(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=100, ge=1, le=500),
    _: str = Depends(verify_basic_auth),
) -> HTMLResponse:
    page_size = _normalize_page_size(page_size, default=100, maximum=500)
    offset = (page - 1) * page_size
    with get_conn() as conn:
        total = count_tasks(conn)
        tasks = [_row_to_dict(row) for row in list_tasks(conn, limit=page_size, offset=offset)]
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


@app.get("/tasks/{task_id}", response_class=HTMLResponse)
def task_detail_page(task_id: int, request: Request, _: str = Depends(verify_basic_auth)) -> HTMLResponse:
    with get_conn() as conn:
        row = get_task(conn, task_id)
        if not row:
            raise HTTPException(status_code=404, detail="Task not found")
        artifacts = [_row_to_dict(item) for item in list_artifacts(conn, task_id)]
        settings = load_settings(conn)

    source_artifact = next((a for a in artifacts if a["kind"] == "source"), None)
    translated_artifact = next((a for a in artifacts if a["kind"] == "translated"), None)
    task_data = _row_to_dict(row)
    task_data["parallel_workers"] = _task_parallel_workers(row, settings)

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
            "secret_key_required": _get_cfg().require_secret_key,
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
    clear_keys = {
        key.replace("clear__", "", 1)
        for key, value in incoming.items()
        if key.startswith("clear__") and _parse_bool(value, default=False)
    }
    with get_conn() as conn:
        try:
            save_settings(conn, filtered, clear_keys=clear_keys)
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

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
        try:
            save_settings(conn, mapped)
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

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
    task_id: int | None = None
    if upload_file and upload_file.filename:
        suffix = Path(upload_file.filename).suffix.lower()
        temp_name = f"{uuid.uuid4().hex}{suffix}"
        target = _get_cfg().upload_root / temp_name
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

    parallel_workers_override = settings_overrides.get("parallel_workers", "")
    if str(parallel_workers_override).strip() != "":
        settings_overrides["parallel_workers"] = _normalize_parallel_workers(
            parallel_workers_override,
            fallback="5",
        )
    else:
        settings_overrides.pop("parallel_workers", None)

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
            _safe_delete_upload_file(upload_path)
        raise

    return JSONResponse({"ok": True, "task_id": task_id})


@app.get("/api/tasks")
def api_list_tasks(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=100, ge=1, le=500),
    _: str = Depends(verify_basic_auth),
) -> JSONResponse:
    page_size = _normalize_page_size(page_size, default=100, maximum=500)
    offset = (page - 1) * page_size
    with get_conn() as conn:
        total = count_tasks(conn)
        rows = list_tasks(conn, limit=page_size, offset=offset)
    data = [_row_to_dict(row) for row in rows]
    return JSONResponse(
        {
            "items": data,
            "page": page,
            "page_size": page_size,
            "total": total,
        }
    )


@app.get("/api/tasks/{task_id}")
def api_get_task(task_id: int, _: str = Depends(verify_basic_auth)) -> JSONResponse:
    with get_conn() as conn:
        row = get_task(conn, task_id)
        if not row:
            raise HTTPException(status_code=404, detail="Task not found")
        artifacts = [_row_to_dict(item) for item in list_artifacts(conn, task_id)]
        settings = load_settings(conn)

    data = _row_to_dict(row)
    data["parallel_workers"] = _task_parallel_workers(row, settings)
    data["artifacts"] = artifacts
    return JSONResponse(data)


@app.post("/api/tasks/{task_id}/purge")
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
                "delete_upload": _parse_bool(form.get("delete_upload", False), default=False),
                "force": _parse_bool(form.get("force", False), default=False),
            }
        )

    with get_conn() as conn:
        row = get_task(conn, task_id)
        if not row:
            raise HTTPException(status_code=404, detail="Task not found")

        can_manage, reason = _can_manage_task(str(row["status"]), force=payload.force)
        if not can_manage:
            raise HTTPException(status_code=409, detail=reason)

        clear_artifacts(conn, task_id)
        clear_task_output_paths(conn, task_id)
        upload_path = str(row["upload_path"] or "")

    task_root = _get_cfg().task_root / str(task_id)
    deleted_paths: list[str] = []

    if payload.scope == "task_dir":
        if _safe_delete_dir(task_root, _get_cfg().task_root):
            deleted_paths.append(str(task_root))
    else:
        downloads_dir = task_root / "downloads"
        if _safe_delete_dir(downloads_dir, _get_cfg().task_root):
            deleted_paths.append(str(downloads_dir))

    deleted_upload = False
    if payload.delete_upload:
        deleted_upload = _safe_delete_upload_file(upload_path)

    return JSONResponse(
        {
            "ok": True,
            "task_id": task_id,
            "scope": payload.scope,
            "deleted_paths": deleted_paths,
            "deleted_upload": deleted_upload,
        }
    )


@app.delete("/api/tasks/{task_id}")
def api_delete_task(
    task_id: int,
    force: bool = Query(default=False),
    delete_task_dir: bool = Query(default=True),
    delete_upload: bool = Query(default=False),
    cascade: bool = Query(default=False),
    _: str = Depends(verify_basic_auth),
) -> JSONResponse:
    deleted_ids: list[int] = []
    upload_paths: list[str] = []

    with get_conn() as conn:
        row = get_task(conn, task_id)
        if not row:
            raise HTTPException(status_code=404, detail="Task not found")

        target_rows: list[Any] = [row]
        if cascade:
            descendant_ids = list_task_descendants(conn, task_id)
            if descendant_ids:
                descendant_rows = list_tasks_by_ids(conn, descendant_ids)
                target_rows.extend(descendant_rows)

        for t in target_rows:
            can_manage, reason = _can_manage_task(str(t["status"]), force=force)
            if not can_manage:
                raise HTTPException(
                    status_code=409,
                    detail=f"task {int(t['id'])} cannot be deleted: {reason}",
                )

        if cascade:
            delete_order = [int(t["id"]) for t in target_rows if int(t["id"]) != task_id]
            delete_order.sort(reverse=True)
            delete_order.append(task_id)
        else:
            delete_order = [task_id]

        for tid in delete_order:
            trow = get_task(conn, tid)
            if not trow:
                continue
            upload_paths.append(str(trow["upload_path"] or ""))
            deleted = delete_task_row(conn, tid)
            if deleted:
                deleted_ids.append(tid)

    if not deleted_ids:
        raise HTTPException(status_code=404, detail="Task not found")

    deleted_paths: list[str] = []
    if delete_task_dir:
        for tid in deleted_ids:
            task_root = _get_cfg().task_root / str(tid)
            if _safe_delete_dir(task_root, _get_cfg().task_root):
                deleted_paths.append(str(task_root))

    deleted_upload_count = 0
    if delete_upload:
        for up in upload_paths:
            if _safe_delete_upload_file(up):
                deleted_upload_count += 1

    return JSONResponse(
        {
            "ok": True,
            "deleted_id": task_id,
            "deleted_ids": sorted(set(deleted_ids)),
            "deleted_paths": deleted_paths,
            "deleted_upload_count": deleted_upload_count,
            "cascade": cascade,
        }
    )


@app.post("/api/tasks/manage/batch-purge")
async def api_batch_purge_tasks(
    request: Request,
    _: str = Depends(verify_basic_auth),
) -> JSONResponse:
    body = await request.json()
    payload = TaskBatchActionRequest.model_validate(body)

    task_ids = sorted({int(tid) for tid in payload.task_ids if int(tid) > 0})
    if not task_ids:
        raise HTTPException(status_code=400, detail="task_ids is required")

    with get_conn() as conn:
        rows = list_tasks_by_ids(conn, task_ids)
        row_map = {int(row["id"]): row for row in rows}

    deleted_count = 0
    errors: list[dict[str, Any]] = []

    for task_id in task_ids:
        row = row_map.get(task_id)
        if not row:
            errors.append({"task_id": task_id, "reason": "not found"})
            continue

        can_manage, reason = _can_manage_task(str(row["status"]), force=payload.force)
        if not can_manage:
            errors.append({"task_id": task_id, "reason": reason})
            continue

        task_root = _get_cfg().task_root / str(task_id)
        try:
            if payload.scope == "task_dir":
                _safe_delete_dir(task_root, _get_cfg().task_root)
            else:
                _safe_delete_dir(task_root / "downloads", _get_cfg().task_root)

            if payload.delete_upload:
                _safe_delete_upload_file(str(row["upload_path"] or ""))

            with get_conn() as conn:
                clear_artifacts(conn, task_id)
                clear_task_output_paths(conn, task_id)

            deleted_count += 1
        except HTTPException as exc:
            errors.append({"task_id": task_id, "reason": str(exc.detail)})
        except Exception as exc:  # noqa: BLE001
            errors.append({"task_id": task_id, "reason": str(exc)})

    return JSONResponse({"ok": True, "processed": len(task_ids), "succeeded": deleted_count, "errors": errors})


@app.post("/api/tasks/manage/batch-delete")
async def api_batch_delete_tasks(
    request: Request,
    _: str = Depends(verify_basic_auth),
) -> JSONResponse:
    body = await request.json()
    payload = TaskBatchActionRequest.model_validate(body)

    task_ids = sorted({int(tid) for tid in payload.task_ids if int(tid) > 0})
    if not task_ids:
        raise HTTPException(status_code=400, detail="task_ids is required")

    deleted_count = 0
    deleted_task_ids: list[int] = []
    errors: list[dict[str, Any]] = []

    for task_id in task_ids:
        try:
            with get_conn() as conn:
                row = get_task(conn, task_id)
                if not row:
                    errors.append({"task_id": task_id, "reason": "not found"})
                    continue

                target_rows: list[Any] = [row]
                if payload.cascade:
                    descendant_ids = list_task_descendants(conn, task_id)
                    if descendant_ids:
                        target_rows.extend(list_tasks_by_ids(conn, descendant_ids))

                blocked = None
                for t in target_rows:
                    can_manage, reason = _can_manage_task(str(t["status"]), force=payload.force)
                    if not can_manage:
                        blocked = {"task_id": int(t["id"]), "reason": reason}
                        break
                if blocked:
                    errors.append(
                        {
                            "task_id": task_id,
                            "reason": f"blocked by task {blocked['task_id']}: {blocked['reason']}",
                        }
                    )
                    continue

                if payload.cascade:
                    delete_order = [int(t["id"]) for t in target_rows if int(t["id"]) != task_id]
                    delete_order.sort(reverse=True)
                    delete_order.append(task_id)
                else:
                    delete_order = [task_id]

                per_deleted: list[int] = []
                upload_paths: list[str] = []
                for tid in delete_order:
                    trow = get_task(conn, tid)
                    if not trow:
                        continue
                    upload_paths.append(str(trow["upload_path"] or ""))
                    if delete_task_row(conn, tid):
                        per_deleted.append(tid)

            if not per_deleted:
                errors.append({"task_id": task_id, "reason": "delete returned false"})
                continue

            for tid in per_deleted:
                _safe_delete_dir(_get_cfg().task_root / str(tid), _get_cfg().task_root)
            if payload.delete_upload:
                for up in upload_paths:
                    _safe_delete_upload_file(up)

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
        row = get_task(conn, task_id)
        if not row:
            raise HTTPException(status_code=404, detail="Task not found")
        if row["status"] != "queued":
            raise HTTPException(status_code=409, detail="Only queued tasks can be canceled")
        ok = cancel_task(conn, task_id)
    return JSONResponse({"ok": ok})


@app.post("/api/tasks/{task_id}/stop")
def api_stop_task(task_id: int, _: str = Depends(verify_basic_auth)) -> JSONResponse:
    if worker is None:
        raise HTTPException(status_code=500, detail="Worker is not initialized")
    ok = worker.stop_task(task_id)
    return JSONResponse({"ok": ok})


@app.post("/api/tasks/{task_id}/pause")
def api_pause_task(task_id: int, _: str = Depends(verify_basic_auth)) -> JSONResponse:
    if worker is None:
        raise HTTPException(status_code=500, detail="Worker is not initialized")
    ok = worker.pause_task(task_id)
    return JSONResponse({"ok": ok})


@app.post("/api/tasks/{task_id}/resume")
def api_resume_task(task_id: int, _: str = Depends(verify_basic_auth)) -> JSONResponse:
    with get_conn() as conn:
        ok = resume_task(conn, task_id)
    return JSONResponse({"ok": ok})


@app.get("/api/tasks/{task_id}/artifacts")
def api_task_artifacts(task_id: int, _: str = Depends(verify_basic_auth)) -> JSONResponse:
    with get_conn() as conn:
        row = get_task(conn, task_id)
        if not row:
            raise HTTPException(status_code=404, detail="Task not found")
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
            left_path = _safe_task_file_path(task_id, file)
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
