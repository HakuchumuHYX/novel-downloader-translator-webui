from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .config import get_config
from .db import get_conn, init_db
from .routers.cookies import router as cookies_router
from .routers.pages import router as pages_router
from .routers.settings import router as settings_router
from .routers.system import router as system_router
from .routers.tasks import router as tasks_router
from .runtime import get_worker, set_app_config, set_worker
from .security import encryption_configured
from .services.task_service import reconcile_orphan_running_tasks
from .services.worker import TaskWorker
from .ui import STATIC_DIR


@asynccontextmanager
async def lifespan(_: FastAPI):
    cfg = get_config()
    set_app_config(cfg)

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
    set_worker(worker)
    worker.start()
    try:
        yield
    finally:
        running_worker = get_worker()
        if running_worker:
            running_worker.stop()
        set_worker(None)
        set_app_config(None)


def create_app() -> FastAPI:
    app = FastAPI(title="Novel Grab + Translate WebUI", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(system_router)
    app.include_router(pages_router)
    app.include_router(settings_router)
    app.include_router(cookies_router)
    app.include_router(tasks_router)
    return app


app = create_app()
