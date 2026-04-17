from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse

from ..db import get_conn
from ..schemas import CookieJsonParseRequest, CookieProfileUpsertRequest
from ..security import encrypt_text, encryption_configured, verify_basic_auth
from ..services.cookie_service import (
    cookie_header_from_json_text,
    cookie_pairs_from_json_text,
    infer_site_from_json_text,
)
from ..services.task_service import (
    count_cookie_profile_task_refs,
    create_or_update_cookie_profile,
    delete_cookie_profile,
    detach_cookie_profile_from_non_running_tasks,
)


router = APIRouter()


@router.post("/api/cookies/parse-json")
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
            "pairs": [{"name": key, "value": value} for key, value in pairs],
            "header": header,
            "inferred_site": inferred_site,
            "count": len(pairs),
        }
    )


@router.post("/api/cookies")
async def api_create_cookie_profile(request: Request, _: str = Depends(verify_basic_auth)) -> JSONResponse:
    inferred_site = ""

    if request.headers.get("content-type", "").startswith("application/json"):
        data = await request.json()
        payload = CookieProfileUpsertRequest.model_validate(data)
        name = payload.name.strip()
        site = payload.site.strip()
        cookie_value = payload.cookie.strip()
        profile_id = payload.profile_id
    else:
        form = await request.form()
        data = {key: value for key, value in form.items() if not isinstance(value, UploadFile)}
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
                cookie_value = f"{cookie_value}; {parsed_cookie}" if cookie_value else parsed_cookie

    if not site and inferred_site:
        site = inferred_site
    if not site:
        site = "custom"

    if not name or not cookie_value:
        raise HTTPException(status_code=400, detail="name and cookie are required (cookie text or json file)")

    if not encryption_configured():
        raise HTTPException(
            status_code=400,
            detail="WEBUI_SECRET_KEY 未配置：Cookie 配置只能在启用有效密钥后保存。",
        )

    try:
        cookie_enc = encrypt_text(cookie_value)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"invalid WEBUI_SECRET_KEY: {exc}") from exc

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


@router.delete("/api/cookies/{profile_id}")
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
