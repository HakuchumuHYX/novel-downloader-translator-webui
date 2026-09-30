"""单线程任务队列：按顺序跑下载器 / 翻译器子进程，解析它们的进度输出写进数据库。

子进程用独立进程组启动，停止任务时整组杀掉。子进程输出协议：
    __PROGRESS__ {"stage": ..., "current": n, "total": m}
    __RESULT__ "产物路径"
其余行原样记为日志。
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

from downloader.common import detect_site

from . import settings as settings_store
from .config import ROOT, TASK_DIR
from .db import connect, now

LOG_KEEP = 2000


class TaskCanceled(Exception):
    pass


def task_status(task_id: int) -> str:
    with connect() as conn:
        row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return row["status"] if row else "canceled"  # 已被删除按取消处理


def terminate(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


class Worker(threading.Thread):
    def __init__(self) -> None:
        super().__init__(daemon=True, name="task-worker")
        self.wakeup = threading.Event()
        self.lock = threading.Lock()
        self.current: tuple[int, subprocess.Popen] | None = None
        self.log_count = 0

    def notify(self) -> None:
        self.wakeup.set()

    def stop_task(self, task_id: int) -> None:
        with self.lock:
            current = self.current
        if current and current[0] == task_id:
            terminate(current[1])

    def shutdown(self) -> None:
        with self.lock:
            current = self.current
        if current:
            terminate(current[1])

    def run(self) -> None:
        while True:
            task = self._claim_next()
            if task is None:
                self.wakeup.wait(5)
                self.wakeup.clear()
                continue
            try:
                self._process(task)
                self._finish(task["id"], "succeeded", "")
            except TaskCanceled:
                pass  # 状态已由停止/删除接口改掉
            except Exception as exc:  # noqa: BLE001 — 任何失败都要落到任务状态上
                self._log(task["id"], f"失败：{exc}")
                self._finish(task["id"], "failed", str(exc))

    def _claim_next(self):
        with connect() as conn:
            task = conn.execute("SELECT * FROM tasks WHERE status = 'queued' ORDER BY id LIMIT 1").fetchone()
            if task:
                conn.execute(
                    "UPDATE tasks SET status = 'running', started_at = ?, finished_at = NULL, error = '', "
                    "stage = '', progress_current = 0, progress_total = 0 WHERE id = ?",
                    (now(), task["id"]),
                )
        return task

    def _finish(self, task_id: int, status: str, error: str) -> None:
        with connect() as conn:
            # 只收尾仍在 running 的任务：被停止的任务已由接口改成 canceled
            conn.execute(
                "UPDATE tasks SET status = ?, error = ?, finished_at = ? WHERE id = ? AND status = 'running'",
                (status, error, now(), task_id),
            )

    def _log(self, task_id: int, message: str) -> None:
        with connect() as conn:
            conn.execute(
                "INSERT INTO task_logs (task_id, message, created_at) VALUES (?, ?, ?)",
                (task_id, message, now()),
            )
            self.log_count += 1
            if self.log_count % 200 == 0:
                conn.execute(
                    "DELETE FROM task_logs WHERE task_id = ? AND id <= "
                    "(SELECT id FROM task_logs WHERE task_id = ? ORDER BY id DESC LIMIT 1 OFFSET ?)",
                    (task_id, task_id, LOG_KEEP),
                )

    def _process(self, task) -> None:
        task_id = task["id"]
        options = json.loads(task["options_json"])
        task_dir = TASK_DIR / str(task_id)
        work, out = task_dir / "work", task_dir / "out"
        with connect() as conn:
            cfg = settings_store.load(conn)
        python = [sys.executable, "-u", "-m"]
        proxy = ["--proxy", cfg["proxy"]] if cfg["proxy"] else []

        if task["url"]:
            download_only = options["mode"] == "download"
            command = [*python, "downloader", "--url", task["url"], "--work", str(work)]
            command += ["--out", str(out if download_only else work), *proxy]
            if download_only and options["format"] == "epub":
                command.append("--epub")
            cookie = cfg[f"cookie_{detect_site(task['url'])}"]
            source = self._run(task_id, "download", command, {"DOWNLOADER_COOKIE": cookie})
            with connect() as conn:
                conn.execute("UPDATE tasks SET title = ? WHERE id = ?", (source.stem, task_id))
        else:
            source = task_dir / "input" / task["upload_name"]

        if options["mode"] == "translate":
            command = [*python, "translator", "--input", str(source), "--work", str(work), "--out", str(out)]
            if options["format"] == "epub":
                command.append("--epub")
            if options["bilingual"]:
                command.append("--bilingual")
            if options["test_blocks"]:
                command += ["--test-blocks", str(options["test_blocks"])]
            translator_cfg = {key: cfg[key] for key in settings_store.TRANSLATOR_KEYS}
            self._run(task_id, "translate", command, {"TRANSLATOR_CONFIG": json.dumps(translator_cfg)})

    def _run(self, task_id: int, stage: str, command: list[str], extra_env: dict[str, str]) -> Path:
        if task_status(task_id) != "running":
            raise TaskCanceled
        with connect() as conn:
            conn.execute(
                "UPDATE tasks SET stage = ?, progress_current = 0, progress_total = 0 WHERE id = ?",
                (stage, task_id),
            )

        proc = subprocess.Popen(
            command,
            cwd=ROOT,
            env={**os.environ, **extra_env},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
        )
        with self.lock:
            self.current = (task_id, proc)
        # 停止请求可能恰好落在子进程启动前
        if task_status(task_id) != "running":
            terminate(proc)

        result: Path | None = None
        last_line = ""
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip()
            if line.startswith("__PROGRESS__ "):
                progress = json.loads(line.removeprefix("__PROGRESS__ "))
                with connect() as conn:
                    conn.execute(
                        "UPDATE tasks SET progress_current = ?, progress_total = ? WHERE id = ?",
                        (progress["current"], progress["total"], task_id),
                    )
            elif line.startswith("__RESULT__ "):
                result = Path(json.loads(line.removeprefix("__RESULT__ ")))
            elif line:
                last_line = line
                self._log(task_id, line)
        code = proc.wait()
        with self.lock:
            self.current = None

        if task_status(task_id) != "running":
            raise TaskCanceled
        if code != 0 or result is None:
            label = "下载" if stage == "download" else "翻译"
            raise RuntimeError(f"{label}失败：{last_line or f'退出码 {code}'}")
        return result
