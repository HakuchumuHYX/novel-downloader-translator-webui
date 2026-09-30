from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .config import ROOT
from .db import connect, init_db, now
from .routes import public, router
from .worker import Worker


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    with connect() as conn:
        # 上次进程退出时还在跑的任务已经没有子进程了；重试会从缓存续跑
        conn.execute(
            "UPDATE tasks SET status = 'failed', error = '服务重启导致中断，点重试可续跑', finished_at = ? "
            "WHERE status = 'running'",
            (now(),),
        )
    worker = Worker()
    app.state.worker = worker
    worker.start()
    yield
    worker.shutdown()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=ROOT / "app" / "static"), name="static")
app.include_router(public)
app.include_router(router)
