from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..config import get_config
from ..db import get_conn
from ..option_registry import parse_bool
from ..security import decrypt_text, sanitize_log
from ..task_models import TaskPayload
from .command_builder import build_downloader_command, build_translator_command
from .settings_service import load_settings, load_task_payload, merged_settings
from .worker_command_service import classify_worker_error, redact_command, terminate_process
from .worker_file_service import (
    artifact_kind,
    collect_artifacts,
    file_has_content,
    has_translate_resume_state,
    list_source_candidates,
    log_download_manifest_summary,
    resolve_source_file,
    resolve_translated_file,
    safe_int,
    translate_resume_state_path,
)
from .task_service import (
    add_artifact,
    append_log,
    claim_next_queued_task,
    clear_artifacts,
    clear_pause_requested,
    get_cookie_profile,
    get_task,
    is_pause_requested,
    is_stop_requested,
    list_artifacts,
    mark_task_paused,
    request_pause_task,
    request_stop_task,
    set_task_finished,
    set_task_pid,
    update_task_progress,
)


class TaskWorker(threading.Thread):
    def __init__(self) -> None:
        super().__init__(daemon=True)
        self._stop_event = threading.Event()
        self._cleanup_tick = 0
        self._proc_lock = threading.Lock()
        self._running: dict[int, subprocess.Popen[str]] = {}
        # Track intentional local termination reasons to distinguish pause/stop from real command failures.
        self._terminate_reason: dict[int, str] = {}

        # Throttle high-frequency progress events to avoid hammering SQLite.
        self._progress_lock = threading.Lock()
        self._progress_state: dict[int, tuple[float, dict[str, Any]]] = {}
        # Keep the latest progress event even if throttled, then flush it when command exits.
        self._progress_latest: dict[int, dict[str, Any]] = {}
        self._progress_min_interval_seconds = float(os.getenv("WEBUI_PROGRESS_MIN_INTERVAL_SECONDS", "0.5"))

    def stop(self) -> None:
        self._stop_event.set()

    def stop_task(self, task_id: int) -> bool:
        with get_conn() as conn:
            flagged = request_stop_task(conn, task_id)

        with self._proc_lock:
            process = self._running.get(task_id)
            if process and process.poll() is None:
                self._terminate_reason[task_id] = "stopped"

        if process and process.poll() is None:
            self._terminate_process(process, reason="stopped")
            return True

        # Fallback for orphan running tasks (e.g. after restart):
        # stop flag is set but there is no local subprocess to consume it.
        if flagged:
            with get_conn() as conn:
                task = get_task(conn, task_id)
                if task and task["status"] == "running":
                    append_log(
                        conn,
                        task_id,
                        "Stop requested but no active worker process found; marking task as canceled.",
                        level="warning",
                    )
                    set_task_finished(
                        conn,
                        task_id,
                        status="canceled",
                        error_message="Task was running without active worker process; canceled by stop request.",
                        error_code="STOPPED_ORPHAN",
                    )
                    return True

        return flagged

    def pause_task(self, task_id: int) -> bool:
        """
        Request pausing a running task.

        The worker will terminate the current subprocess and mark the task as paused.
        """
        with get_conn() as conn:
            flagged = request_pause_task(conn, task_id)

        with self._proc_lock:
            process = self._running.get(task_id)
            if process and process.poll() is None:
                self._terminate_reason[task_id] = "paused"

        if process and process.poll() is None:
            self._terminate_process(process, reason="paused")
            return True

        # Fallback for orphan running tasks (e.g. after restart):
        # pause flag is set but there is no local subprocess to consume it.
        if flagged:
            with get_conn() as conn:
                task = get_task(conn, task_id)
                if task and task["status"] == "running":
                    append_log(
                        conn,
                        task_id,
                        "Pause requested but no active worker process found; marking task as paused.",
                        level="warning",
                    )
                    mark_task_paused(conn, task_id)
                    append_log(conn, task_id, "Task paused", level="info")
                    return True

        return flagged

    def run(self) -> None:
        cfg = get_config()
        while not self._stop_event.is_set():
            task_id = None
            with get_conn() as conn:
                row = claim_next_queued_task(conn)
                if row:
                    task_id = int(row["id"])

            if task_id is None:
                self._maybe_cleanup()
                self._stop_event.wait(cfg.worker_interval_seconds)
                continue

            try:
                self._process_task(task_id)
            except Exception as exc:  # noqa: BLE001
                status, error_code = classify_worker_error(exc)
                self._log(task_id, f"Worker error: {exc}", level="error")
                with get_conn() as conn:
                    task = get_task(conn, task_id)
                    if not task:
                        continue

                    # Pause is not a "finished" state; don't set finished_at.
                    if status == "paused" and task["status"] == "running":
                        clear_pause_requested(conn, task_id)
                        mark_task_paused(conn, task_id)
                        append_log(conn, task_id, "Task paused", level="info")
                        continue

                    if task["status"] == "running":
                        set_task_finished(
                            conn,
                            task_id,
                            status=status,
                            error_message=str(exc),
                            error_code=error_code,
                        )

    def _log(self, task_id: int, message: str, level: str = "info") -> None:
        clean = sanitize_log(message)
        with get_conn() as conn:
            append_log(conn, task_id, clean, level=level)

    def _try_reuse_download_source(
        self,
        task_id: int,
        payload: dict[str, Any],
        settings: dict[str, str],
        download_root: Path,
        source_full_book_path: str,
    ) -> Path | None:
        source_full_book_path = (source_full_book_path or "").strip()
        if source_full_book_path:
            candidate = Path(source_full_book_path)
            if file_has_content(candidate):
                self._log(task_id, f"Resuming with existing source_full_book_path: {candidate}")
                return candidate

        save_format = payload.get("save_format") or settings.get("save_format", "txt")
        merged_name = payload.get("merged_name") or settings.get("merged_name", "")
        translating_task = payload.get("mode", "download_and_translate") == "download_and_translate"
        merge_all_enabled = str(payload.get("merge_all", settings.get("merge_all", "true"))).lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

        # Translation requires a single source file. If an older task/download
        # only produced multiple volume files and no merged source, force the
        # worker to redownload with merge enabled instead of translating one
        # arbitrary candidate.
        if translating_task and not merge_all_enabled:
            candidates = list_source_candidates(download_root, save_format=save_format)
            if len(candidates) > 1:
                self._log(
                    task_id,
                    "Existing download outputs are split across multiple files; redownloading with merge enabled for translation.",
                    level="warning",
                )
                return None

        try:
            candidate = resolve_source_file(
                download_root,
                merged_name=merged_name,
                save_format=save_format,
            )
        except Exception:
            return None

        if file_has_content(candidate):
            self._log(task_id, f"Resuming with existing downloaded source: {candidate}")
            return candidate

        return None

    def _process_task(self, task_id: int) -> None:
        cfg = get_config()
        with get_conn() as conn:
            task = get_task(conn, task_id)
            if not task:
                return
            payload = load_task_payload(task)
            base_settings = load_settings(conn)

        effective_settings = merged_settings(base_settings, payload.get("settings_overrides", {}))

        task_root = cfg.task_root / str(task_id)
        task_root.mkdir(parents=True, exist_ok=True)
        download_root = task_root / "downloads"
        download_root.mkdir(parents=True, exist_ok=True)

        self._log(task_id, f"Task started. source_type={payload.get('source_type')} mode={payload.get('mode')}")

        source_path = Path(payload.get("upload_path", ""))
        task_stage = str(task["stage"] or "").strip().lower()
        translate_current = safe_int(task["translate_current"])
        resume_translate = task_stage == "translate" or translate_current > 0

        if payload.get("source_type") != "upload":
            reused_source = self._try_reuse_download_source(
                task_id,
                payload,
                effective_settings,
                download_root,
                str(task["source_full_book_path"] or ""),
            )
            if reused_source is not None:
                source_path = reused_source
                self._log(task_id, "Skipping download stage because source output already exists")
            else:
                if resume_translate:
                    self._log(
                        task_id,
                        "Resume requested from translate stage, but no reusable source output was found; restarting download.",
                        level="warning",
                    )
                try:
                    with get_conn() as conn:
                        update_task_progress(conn, task_id, stage="download")
                    source_path = self._run_download(task_id, payload, effective_settings, download_root)
                except Exception as exc:  # noqa: BLE001
                    raise RuntimeError(f"DOWNLOAD_STAGE: {exc}") from exc
        else:
            if not source_path.exists():
                raise FileNotFoundError(f"Uploaded file not found: {source_path}")
            self._log(task_id, f"Using uploaded file: {source_path}")

        translated_path = Path("")
        if payload.get("mode", "download_and_translate") == "download_and_translate":
            if resume_translate and not has_translate_resume_state(source_path):
                self._log(
                    task_id,
                    "Translate resume state file is missing; continuing without --resume.",
                    level="warning",
                )
                resume_translate = False

            try:
                with get_conn() as conn:
                    update_task_progress(conn, task_id, stage="translate")
                translated_path = self._run_translate(
                    task_id,
                    source_path,
                    payload,
                    effective_settings,
                    force_resume=resume_translate,
                )
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(f"TRANSLATE_STAGE: {exc}") from exc

        with get_conn() as conn:
            clear_artifacts(conn, task_id)
            if source_path.exists():
                add_artifact(conn, task_id, "source", source_path)
            if translated_path and translated_path.exists():
                add_artifact(conn, task_id, "translated", translated_path)

            existing = {Path(r["file_path"]) for r in list_artifacts(conn, task_id)}
            for extra in collect_artifacts(task_root):
                if extra in existing:
                    continue
                kind = artifact_kind(extra)
                add_artifact(conn, task_id, kind, extra)
                existing.add(extra)

            set_task_finished(
                conn,
                task_id,
                status="succeeded",
                download_output_dir=str(download_root),
                source_full_book_path=str(source_path),
                translated_output_path=str(translated_path) if translated_path else "",
                error_message="",
                error_code="",
            )

        self._log(task_id, "Task completed", level="info")

    def _run_download(
        self,
        task_id: int,
        payload: dict[str, Any],
        settings: dict[str, str],
        download_root: Path,
    ) -> Path:
        cfg = get_config()
        payload_model = TaskPayload.model_validate(payload)
        save_format = payload_model.save_format or settings.get("save_format", "txt")

        cookie_profile_id = payload.get("cookie_profile_id")
        cookie_header = ""
        if cookie_profile_id:
            with get_conn() as conn:
                profile = get_cookie_profile(conn, int(cookie_profile_id))
            if profile:
                cookie_header = decrypt_text(profile["cookie_enc"]).strip()
                if cookie_header:
                    # Pass as header-style cookie string for downloader compatibility.
                    pass
                else:
                    self._log(task_id, f"Cookie profile {cookie_profile_id} is empty", level="warning")

        command, forced_merge_for_translate = build_downloader_command(
            cfg,
            payload_model,
            settings,
            download_root,
            cookie_header=cookie_header,
        )
        if forced_merge_for_translate:
            self._log(
                task_id,
                "Translation task requires a single merged source; enabling merge_all for this download run.",
                level="warning",
            )

        timeout_seconds = int(
            payload.get("process_timeout") or settings.get("process_timeout") or cfg.process_timeout_seconds
        )

        self._log(task_id, "Running downloader command")
        self._run_command(task_id, command, cwd=str(cfg.downloader_entry.parent), timeout_seconds=timeout_seconds)

        source_path = resolve_source_file(
            download_root,
            merged_name=payload.get("merged_name") or settings.get("merged_name", ""),
            save_format=save_format,
        )

        if not source_path.exists():
            raise RuntimeError(f"Downloader finished but source output file does not exist: {source_path}")

        try:
            source_size = source_path.stat().st_size
        except OSError as exc:
            raise RuntimeError(f"Failed to stat downloaded source output file: {source_path}") from exc

        if source_size <= 0:
            raise RuntimeError(f"Downloader finished but source output file is empty: {source_path}")

        log_download_manifest_summary(task_id, download_root, log=self._log)
        self._log(task_id, f"Download source resolved: {source_path}")
        return source_path

    def _run_translate(
        self,
        task_id: int,
        source_path: Path,
        payload: dict[str, Any],
        settings: dict[str, str],
        *,
        force_resume: bool = False,
    ) -> Path:
        cfg = get_config()
        payload_model = TaskPayload.model_validate(payload)
        resume_requested = force_resume or parse_bool(settings.get("resume", "false"), default=False)
        has_resume_state = file_has_content(translate_resume_state_path(source_path))
        if resume_requested and not has_resume_state:
            self._log(
                task_id,
                f"Resume requested but state file not found: {translate_resume_state_path(source_path)}; running without --resume.",
                level="warning",
            )
        command = build_translator_command(
            cfg,
            source_path,
            payload_model,
            settings,
            force_resume=force_resume,
            has_resume_state=has_resume_state,
        )

        timeout_seconds = int(payload.get("process_timeout") or settings.get("process_timeout") or cfg.process_timeout_seconds)
        self._log(task_id, "Running translator command")
        self._run_command(task_id, command, cwd=str(cfg.translator_entry.parent), timeout_seconds=timeout_seconds)

        translated = resolve_translated_file(source_path)
        if translated and translated.exists():
            try:
                translated_size = translated.stat().st_size
            except OSError as exc:
                raise RuntimeError(f"Failed to stat translated output file: {translated}") from exc
            if translated_size <= 0:
                raise RuntimeError(f"Translation finished but translated output file is empty: {translated}")

            self._log(task_id, f"Translation output resolved: {translated}")
            return translated

        raise RuntimeError("Translation finished but translated output file was not found")

    def _register_process(self, task_id: int, process: subprocess.Popen[str]) -> None:
        with self._proc_lock:
            self._running[task_id] = process
            self._terminate_reason.pop(task_id, None)
        with get_conn() as conn:
            set_task_pid(conn, task_id, process.pid)

    def _unregister_process(self, task_id: int) -> None:
        with self._proc_lock:
            self._running.pop(task_id, None)
            self._terminate_reason.pop(task_id, None)
        with self._progress_lock:
            self._progress_state.pop(task_id, None)
            self._progress_latest.pop(task_id, None)
        with get_conn() as conn:
            set_task_pid(conn, task_id, None)

    def _terminate_process(self, process: subprocess.Popen[str], reason: str = "") -> None:
        terminate_process(process, stop_grace_seconds=float(get_config().stop_grace_seconds), reason=reason)

    def _apply_progress_event_to_db(self, task_id: int, evt: dict[str, Any]) -> None:
        stage = str(evt.get("stage") or "").strip()
        cur = evt.get("current")
        total = evt.get("total")

        try:
            cur_i = int(cur) if cur is not None else None
        except Exception:
            cur_i = None
        try:
            total_i = int(total) if total is not None else None
        except Exception:
            total_i = None

        with get_conn() as conn:
            kwargs: dict[str, Any] = {"stage": stage or None}
            if stage == "download":
                kwargs["download_current"] = cur_i
                kwargs["download_total"] = total_i
            elif stage == "translate":
                kwargs["translate_current"] = cur_i
                kwargs["translate_total"] = total_i
            else:
                # Unknown stage: still allow setting stage text only.
                kwargs.pop("download_current", None)
                kwargs.pop("download_total", None)
                kwargs.pop("translate_current", None)
                kwargs.pop("translate_total", None)

            update_task_progress(conn, task_id, **kwargs)

    def _maybe_update_progress_throttled(self, task_id: int, evt: dict[str, Any]) -> None:
        now = time.monotonic()
        evt_copy = dict(evt)

        with self._progress_lock:
            self._progress_latest[task_id] = evt_copy
            last_ts, last_evt = self._progress_state.get(task_id, (0.0, {}))
            if evt_copy == last_evt and (now - last_ts) < (self._progress_min_interval_seconds * 4):
                return
            if (now - last_ts) < self._progress_min_interval_seconds:
                return
            self._progress_state[task_id] = (now, evt_copy)

        self._apply_progress_event_to_db(task_id, evt_copy)

    def _flush_latest_progress(self, task_id: int) -> None:
        with self._progress_lock:
            evt = self._progress_latest.get(task_id)
            if not evt:
                return
            evt_copy = dict(evt)
            self._progress_state[task_id] = (time.monotonic(), evt_copy)

        self._apply_progress_event_to_db(task_id, evt_copy)

    def _run_command(self, task_id: int, command: list[str], cwd: str, timeout_seconds: int) -> None:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
        )
        self._register_process(task_id, process)

        q: queue.Queue[str | None] = queue.Queue()

        def _reader() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                q.put(line)
            q.put(None)

        t = threading.Thread(target=_reader, daemon=True)
        t.start()

        started = time.monotonic()
        timed_out = False
        stopped = False
        paused = False
        stop_check_interval = 1.0
        last_stop_check = 0.0

        try:
            while True:
                try:
                    item = q.get(timeout=0.2)
                except queue.Empty:
                    item = ""

                if item is None:
                    # Reader reached EOF; all buffered output has been consumed.
                    break

                if item:
                    stripped = item.rstrip("\n")
                    if stripped.startswith("__WEBUI_PROGRESS__"):
                        payload = stripped[len("__WEBUI_PROGRESS__") :].strip()
                        try:
                            evt = json.loads(payload)
                            if isinstance(evt, dict):
                                self._maybe_update_progress_throttled(task_id, evt)
                        except Exception:
                            # Avoid breaking the worker due to malformed progress line.
                            self._log(task_id, f"Invalid progress event: {payload}", level="warning")
                    else:
                        self._log(task_id, stripped)

                # Only enforce timeout/stop/pause while process is still alive.
                if process.poll() is not None:
                    continue

                elapsed = time.monotonic() - started
                if elapsed > timeout_seconds:
                    timed_out = True
                    self._terminate_process(process, reason="timeout")
                    break

                now = time.monotonic()
                if now - last_stop_check >= stop_check_interval:
                    with get_conn() as conn:
                        if is_stop_requested(conn, task_id):
                            stopped = True
                        if is_pause_requested(conn, task_id):
                            paused = True
                    last_stop_check = now

                if stopped:
                    self._terminate_process(process, reason="stopped")
                    break
                if paused:
                    self._terminate_process(process, reason="paused")
                    break

            # Ensure the process is reaped even after terminate/kill.
            try:
                rc = process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self._terminate_process(process, reason="timeout")
                try:
                    rc = process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        process.kill()
                    except Exception:
                        pass
                    rc = process.wait()

            # Drain any remaining buffered output quickly (best effort).
            drain_deadline = time.monotonic() + 0.5
            while time.monotonic() < drain_deadline:
                try:
                    item = q.get_nowait()
                except queue.Empty:
                    break
                if item is None:
                    break
                if item:
                    stripped = item.rstrip("\n")
                    if stripped.startswith("__WEBUI_PROGRESS__"):
                        payload = stripped[len("__WEBUI_PROGRESS__") :].strip()
                        try:
                            evt = json.loads(payload)
                            if isinstance(evt, dict):
                                self._maybe_update_progress_throttled(task_id, evt)
                        except Exception:
                            self._log(task_id, f"Invalid progress event: {payload}", level="warning")
                    else:
                        self._log(task_id, stripped)

            # Distinguish intentional local terminate (pause/stop) from real non-zero command failures.
            local_reason = ""
            with self._proc_lock:
                local_reason = self._terminate_reason.get(task_id, "")

            if rc < 0 and local_reason == "paused":
                raise RuntimeError("__TASK_PAUSED__")
            if rc < 0 and local_reason == "stopped":
                raise RuntimeError("__TASK_STOPPED__")

            if paused:
                raise RuntimeError("__TASK_PAUSED__")
            if stopped:
                raise RuntimeError("__TASK_STOPPED__")
            if timed_out:
                raise RuntimeError(f"__TASK_TIMEOUT__ command exceeded {timeout_seconds}s")
            if rc != 0:
                raise RuntimeError(f"Command failed with exit code {rc}: {redact_command(command)}")
        finally:
            try:
                self._flush_latest_progress(task_id)
            finally:
                self._unregister_process(task_id)

    def _maybe_cleanup(self) -> None:
        self._cleanup_tick += 1
        if self._cleanup_tick % 120 != 0:
            return

        cfg = get_config()
        days = cfg.cleanup_days
        statuses = {"succeeded", "failed", "canceled"}

        with get_conn() as conn:
            try:
                runtime_settings = load_settings(conn)
                days = int(runtime_settings.get("cleanup_days", days))
                parsed = [x.strip() for x in runtime_settings.get("cleanup_statuses", "").split(",") if x.strip()]
                if parsed:
                    statuses = set(parsed)
            except Exception:
                pass

            if days <= 0:
                return

            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            placeholders = ",".join(["?"] * len(statuses))
            rows = conn.execute(
                f"SELECT id, finished_at, status FROM tasks WHERE status IN ({placeholders})",
                tuple(statuses),
            ).fetchall()

            for row in rows:
                finished_at = row["finished_at"]
                if not finished_at:
                    continue
                try:
                    finished_dt = datetime.fromisoformat(finished_at)
                except ValueError:
                    continue
                if finished_dt.tzinfo is None:
                    finished_dt = finished_dt.replace(tzinfo=timezone.utc)

                if finished_dt >= cutoff:
                    continue

                task_id = int(row["id"])
                task_dir = cfg.task_root / str(task_id)
                if task_dir.exists():
                    shutil.rmtree(task_dir, ignore_errors=True)

                has_children = conn.execute(
                    "SELECT 1 FROM tasks WHERE parent_task_id = ? LIMIT 1",
                    (task_id,),
                ).fetchone()
                if has_children:
                    continue

                conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
