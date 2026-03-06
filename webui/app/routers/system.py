from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..security import verify_basic_auth
from ..services.system_service import collect_system_status


router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))


@router.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/system", response_class=HTMLResponse)
def system_page(request: Request, _: str = Depends(verify_basic_auth)) -> HTMLResponse:
    status = collect_system_status()
    return templates.TemplateResponse(request, "system.html", {"status": status})


@router.get("/api/system/status")
def api_system_status(_: str = Depends(verify_basic_auth)) -> JSONResponse:
    return JSONResponse(collect_system_status())


@router.get("/redirect/settings")
def redirect_settings() -> RedirectResponse:
    return RedirectResponse(url="/settings", status_code=302)
