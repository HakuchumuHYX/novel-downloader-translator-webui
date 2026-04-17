from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse

from ..services.settings_service import DEFAULT_SETTINGS, load_settings, save_settings
from ..security import verify_basic_auth
from ..schemas import EnvImportRequest
from ..services.env_service import export_settings_to_env, import_env_to_settings
from ..option_registry import parse_bool
from ..db import get_conn


router = APIRouter()


@router.post("/api/settings")
async def api_save_settings(request: Request, _: str = Depends(verify_basic_auth)) -> JSONResponse:
    if request.headers.get("content-type", "").startswith("application/json"):
        body = await request.json()
        incoming = {key: str(value) for key, value in body.items()}
    else:
        form = await request.form()
        incoming = {key: str(value) for key, value in form.items() if not isinstance(value, UploadFile)}

    filtered = {key: value for key, value in incoming.items() if key in DEFAULT_SETTINGS}
    clear_keys = {
        key.replace("clear__", "", 1)
        for key, value in incoming.items()
        if key.startswith("clear__") and parse_bool(value, default=False)
    }
    with get_conn() as conn:
        try:
            save_settings(conn, filtered, clear_keys=clear_keys)
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return JSONResponse({"ok": True})


@router.post("/api/settings/import-env")
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


@router.get("/api/settings/export-env")
def api_export_env(_: str = Depends(verify_basic_auth)) -> PlainTextResponse:
    with get_conn() as conn:
        settings = load_settings(conn)
    return PlainTextResponse(export_settings_to_env(settings))
