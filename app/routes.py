from __future__ import annotations

import hmac
import json
import shutil
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from downloader.common import detect_site, sanitize_filename

from . import settings as settings_store
from .config import AUTH_PASSWORD, AUTH_USER, ROOT, TASK_DIR
from .db import connect, now

templates = Jinja2Templates(directory=ROOT / "app" / "templates")
templates.env.globals["STATUS_LABELS"] = {
    "queued": "排队中",
    "running": "进行中",
    "succeeded": "已完成",
    "failed": "失败",
    "canceled": "已停止",
}


def require_auth(credentials: HTTPBasicCredentials = Depends(HTTPBasic())) -> None:
    user_ok = hmac.compare_digest(credentials.username.encode(), AUTH_USER.encode())
    password_ok = hmac.compare_digest(credentials.password.encode(), AUTH_PASSWORD.encode())
    if not (user_ok and password_ok):
        raise HTTPException(401, "认证失败", headers={"WWW-Authenticate": "Basic"})


def require_fetch(request: Request) -> None:
    # 变更类请求只接受页面 JS 发出的请求：跨站表单带不上这个头，算是最简单的 CSRF 防护
    if request.headers.get("x-requested-with") != "fetch":
        raise HTTPException(403, "缺少 X-Requested-With: fetch")


public = APIRouter()
router = APIRouter(dependencies=[Depends(require_auth)])
MUTATING = [Depends(require_fetch)]


@public.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


def task_view(row) -> dict:
    task = dict(row)
    task["options"] = json.loads(task.pop("options_json"))
    return task


def out_files(task_id: int) -> list[dict]:
    out = TASK_DIR / str(task_id) / "out"
    if not out.is_dir():
        return []
    return [{"name": p.name, "size": p.stat().st_size} for p in sorted(out.iterdir()) if p.is_file()]


def get_task(conn, task_id: int):
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "任务不存在")
    return row


def add_log(conn, task_id: int, message: str) -> None:
    conn.execute("INSERT INTO task_logs (task_id, message, created_at) VALUES (?, ?, ?)", (task_id, message, now()))


def list_tasks() -> list[dict]:
    with connect() as conn:
        return [task_view(row) for row in conn.execute("SELECT * FROM tasks ORDER BY id DESC LIMIT 200")]


# ---------- 页面 ----------


@router.get("/", response_class=HTMLResponse)
def index_page(request: Request):
    with connect() as conn:
        cfg = settings_store.load(conn)
    return templates.TemplateResponse(
        request,
        "index.html",
        {"tasks": list_tasks(), "api_ready": bool(cfg["api_key"] and cfg["model"])},
    )


@router.get("/tasks/{task_id}", response_class=HTMLResponse)
def task_page(request: Request, task_id: int):
    with connect() as conn:
        task = task_view(get_task(conn, task_id))
    return templates.TemplateResponse(request, "task.html", {"task": task, "files": out_files(task_id)})


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    with connect() as conn:
        cfg = settings_store.load(conn)
    secrets = {key: settings_store.mask(cfg.pop(key)) for key in settings_store.SECRETS}
    return templates.TemplateResponse(request, "settings.html", {"cfg": cfg, "secrets": secrets})


@router.get("/tasks/{task_id}/files/{name}")
def download_file(task_id: int, name: str):
    if name not in {f["name"] for f in out_files(task_id)}:
        raise HTTPException(404, "文件不存在")
    return FileResponse(TASK_DIR / str(task_id) / "out" / name, filename=name)


# ---------- 任务接口 ----------


@router.get("/api/tasks")
def api_list_tasks() -> list[dict]:
    return list_tasks()


@router.post("/api/tasks", dependencies=MUTATING)
def api_create_task(
    request: Request,
    url: str = Form(""),
    file: UploadFile | None = File(None),
    mode: Literal["download", "translate"] = Form("translate"),
    format: Literal["txt", "epub"] = Form("txt"),
    bilingual: bool = Form(False),
    test_blocks: int = Form(0, ge=0),
) -> dict:
    url = url.strip()
    has_file = file is not None and bool(file.filename)
    if bool(url) == has_file:
        raise HTTPException(400, "链接和文件二选一")

    with connect() as conn:
        cfg = settings_store.load(conn)
    upload_name = ""
    if url:
        try:
            site = detect_site(url)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if site == "novel18" and not cfg["cookie_novel18"]:
            raise HTTPException(400, "novel18 没有 Cookie 下载不了，请先在设置页填写")
        title = url
    else:
        original = Path(file.filename)
        suffix = original.suffix.lower()
        if suffix not in (".txt", ".epub"):
            raise HTTPException(400, "只支持 txt 和 epub")
        upload_name = sanitize_filename(original.stem) + suffix
        title = Path(upload_name).stem
        mode = "translate"
        if suffix == ".epub":
            format = "epub"
    if mode == "translate" and not (cfg["api_key"] and cfg["model"]):
        raise HTTPException(400, "请先在设置页填写 API Key 和模型")

    options = {"mode": mode, "format": format, "bilingual": bilingual, "test_blocks": test_blocks}
    with connect() as conn:
        task_id = conn.execute(
            "INSERT INTO tasks (status, title, url, upload_name, options_json, created_at) VALUES ('queued', ?, ?, ?, ?, ?)",
            (title, url, upload_name, json.dumps(options), now()),
        ).lastrowid
        # 在同一事务里落盘上传文件：提交前 worker 看不到这条任务
        if has_file:
            input_dir = TASK_DIR / str(task_id) / "input"
            input_dir.mkdir(parents=True, exist_ok=True)
            with (input_dir / upload_name).open("wb") as fh:
                shutil.copyfileobj(file.file, fh)
    request.app.state.worker.notify()
    return {"id": task_id}


@router.get("/api/tasks/{task_id}")
def api_task_state(task_id: int, after: int = 0) -> dict:
    with connect() as conn:
        task = task_view(get_task(conn, task_id))
        if after:
            logs = conn.execute(
                "SELECT id, message, created_at FROM task_logs WHERE task_id = ? AND id > ? ORDER BY id LIMIT 500",
                (task_id, after),
            ).fetchall()
        else:
            logs = conn.execute(
                "SELECT id, message, created_at FROM task_logs WHERE task_id = ? ORDER BY id DESC LIMIT 300",
                (task_id,),
            ).fetchall()[::-1]
    return {"task": task, "files": out_files(task_id), "logs": [dict(row) for row in logs]}


@router.post("/api/tasks/{task_id}/stop", dependencies=MUTATING)
def api_stop_task(request: Request, task_id: int) -> dict:
    with connect() as conn:
        get_task(conn, task_id)
        changed = conn.execute(
            "UPDATE tasks SET status = 'canceled', finished_at = ? WHERE id = ? AND status IN ('queued', 'running')",
            (now(), task_id),
        ).rowcount
        if not changed:
            raise HTTPException(409, "任务没有在运行")
        add_log(conn, task_id, "已手动停止")
    request.app.state.worker.stop_task(task_id)
    return {"ok": True}


def requeue(request: Request, task_id: int, *, full: bool) -> dict:
    with connect() as conn:
        task = task_view(get_task(conn, task_id))
        if task["status"] in ("queued", "running"):
            raise HTTPException(409, "任务还没结束")
        options = task["options"]
        if full:
            if options["mode"] != "translate" or not options["test_blocks"]:
                raise HTTPException(409, "只有试译任务能转全文")
            options["test_blocks"] = 0
        conn.execute(
            "UPDATE tasks SET status = 'queued', options_json = ?, error = '', finished_at = NULL WHERE id = ?",
            (json.dumps(options), task_id),
        )
        add_log(conn, task_id, "转全文，重新排队" if full else "重试，重新排队")
    request.app.state.worker.notify()
    return {"ok": True}


@router.post("/api/tasks/{task_id}/retry", dependencies=MUTATING)
def api_retry_task(request: Request, task_id: int) -> dict:
    return requeue(request, task_id, full=False)


@router.post("/api/tasks/{task_id}/full", dependencies=MUTATING)
def api_full_task(request: Request, task_id: int) -> dict:
    return requeue(request, task_id, full=True)


@router.delete("/api/tasks/{task_id}", dependencies=MUTATING)
def api_delete_task(task_id: int) -> dict:
    with connect() as conn:
        if get_task(conn, task_id)["status"] == "running":
            raise HTTPException(409, "先停止任务再删除")
        conn.execute("DELETE FROM task_logs WHERE task_id = ?", (task_id,))
        conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    shutil.rmtree(TASK_DIR / str(task_id), ignore_errors=True)
    return {"ok": True}


# ---------- 设置接口 ----------


@router.post("/api/settings", dependencies=MUTATING)
async def api_save_settings(request: Request) -> dict:
    form = await request.form()
    try:
        with connect() as conn:
            settings_store.save(conn, {key: str(value) for key, value in form.items()})
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True}
