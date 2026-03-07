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
from ..security import decrypt_text, sanitize_log
from .settings_service import load_settings, merged_settings
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
            self._terminate_process(process)
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
            self._terminate_process(process)
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
                status, error_code = self._classify_error(exc)
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

    def _classify_error(self, exc: Exception) -> tuple[str, str]:
        msg = str(exc)
        low = msg.lower()
        if "__task_paused__" in low:
            return "paused", "PAUSED"
        if "__task_stopped__" in low:
            return "canceled", "STOPPED"
        if "__task_timeout__" in low:
            return "failed", "PROCESS_TIMEOUT"

        # Stage-aware classification first (avoid false matches from file paths like /downloads/... in translate stage).
        if "download_stage:" in low:
            return "failed", "DOWNLOAD_FAILED"
        if "translate_stage:" in low:
            return "failed", "TRANSLATE_FAILED"

        if "auth" in low or "forbidden" in low or "unauthorized" in low or "cookie" in low:
            return "failed", "AUTH_FAILED"
        if "translate" in low or "translated output" in low:
            return "failed", "TRANSLATE_FAILED"
        if "download" in low or "node backend" in low or "native downloader" in low:
            return "failed", "DOWNLOAD_FAILED"
        return "failed", "UNKNOWN"

    def _log(self, task_id: int, message: str, level: str = "info") -> None:
        clean = sanitize_log(message)
        with get_conn() as conn:
            append_log(conn, task_id, clean, level=level)

    def _process_task(self, task_id: int) -> None:
        cfg = get_config()
        with get_conn() as conn:
            task = get_task(conn, task_id)
            if not task:
                return
            payload = json.loads(task["payload_json"])
            base_settings = load_settings(conn)

        effective_settings = merged_settings(base_settings, payload.get("settings_overrides", {}))

        task_root = cfg.task_root / str(task_id)
        task_root.mkdir(parents=True, exist_ok=True)
        download_root = task_root / "downloads"
        download_root.mkdir(parents=True, exist_ok=True)

        self._log(task_id, f"Task started. source_type={payload.get('source_type')} mode={payload.get('mode')}")

        source_path = Path(payload.get("upload_path", ""))

        if payload.get("source_type") != "upload":
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
            try:
                with get_conn() as conn:
                    update_task_progress(conn, task_id, stage="translate")
                translated_path = self._run_translate(task_id, source_path, payload, effective_settings)
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(f"TRANSLATE_STAGE: {exc}") from exc

        with get_conn() as conn:
            clear_artifacts(conn, task_id)
            if source_path.exists():
                add_artifact(conn, task_id, "source", source_path)
            if translated_path and translated_path.exists():
                add_artifact(conn, task_id, "translated", translated_path)

            existing = {Path(r["file_path"]) for r in list_artifacts(conn, task_id)}
            for extra in self._collect_artifacts(task_root):
                if extra in existing:
                    continue
                kind = self._artifact_kind(extra)
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
        site_map = {
            "syosetu": "syosetu",
            "syosetu-r18": "novel18",
            "kakuyomu": "kakuyomu",
        }
        site = site_map[payload["source_type"]]
        save_format = payload.get("save_format") or settings.get("save_format", "txt")

        command = [
            cfg.downloader_python,
            str(cfg.downloader_entry),
            "--site",
            site,
            "--backend",
            payload.get("backend") or settings.get("backend", "auto"),
            "--paid-policy",
            payload.get("paid_policy") or settings.get("paid_policy", "skip"),
            "--save-format",
            save_format,
            "--output-dir",
            str(download_root),
            "--merged-name",
            payload.get("merged_name") or settings.get("merged_name", ""),
            "--timeout",
            payload.get("timeout") or settings.get("timeout", "240"),
            "--retries",
            payload.get("retries") or settings.get("retries", "2"),
            "--rate-limit",
            payload.get("rate_limit") or settings.get("rate_limit", "1.0"),
        ]

        if str(payload.get("merge_all", settings.get("merge_all", "true"))).lower() in {
            "1",
            "true",
            "yes",
            "on",
        }:
            command.append("--merge-all")

        if str(payload.get("record_chapter_number", settings.get("record_chapter_number", "false"))).lower() in {
            "1",
            "true",
            "yes",
            "on",
        }:
            command.append("--record-chapter-number")

        source_input = str(payload.get("source_input", "")).strip()
        if source_input.startswith("http://") or source_input.startswith("https://"):
            command.extend(["--url", source_input])
        else:
            command.extend(["--novel_id", source_input])

        cookie_profile_id = payload.get("cookie_profile_id")
        if cookie_profile_id:
            with get_conn() as conn:
                profile = get_cookie_profile(conn, int(cookie_profile_id))
            if profile:
                cookie_value = decrypt_text(profile["cookie_enc"]).strip()
                if cookie_value:
                    # Pass as header-style cookie string for downloader compatibility.
                    command.extend(["--cookie", cookie_value])
                else:
                    self._log(task_id, f"Cookie profile {cookie_profile_id} is empty", level="warning")

        timeout_seconds = int(
            payload.get("process_timeout") or settings.get("process_timeout") or cfg.process_timeout_seconds
        )

        self._log(task_id, "Running downloader command")
        self._run_command(task_id, command, cwd=str(cfg.downloader_entry.parent), timeout_seconds=timeout_seconds)

        source_path = self._resolve_source_file(
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

        self._log(task_id, f"Download source resolved: {source_path}")
        return source_path

    def _run_translate(
        self,
        task_id: int,
        source_path: Path,
        payload: dict[str, Any],
        settings: dict[str, str],
    ) -> Path:
        cfg = get_config()
        command = [
            cfg.translator_python,
            str(cfg.translator_entry),
            "--book_name",
            str(source_path),
            "--model",
            settings.get("model", "openai"),
            "--language",
            settings.get("language", "zh-hans"),
        ]

        optional_pairs = {
            "model_list": "--model_list",
            "api_base": "--api_base",
            "source_lang": "--source_lang",
            "temperature": "--temperature",
            "accumulated_num": "--accumulated_num",
            "parallel_workers": "--parallel-workers",
            "context_paragraph_limit": "--context_paragraph_limit",
            "block_size": "--block_size",
            "proxy": "--proxy",
            "translation_style": "--translation_style",
            "batch_size": "--batch_size",
            "interval": "--interval",
            "deployment_id": "--deployment_id",
            "translate_tags": "--translate-tags",
            "exclude_translate_tags": "--exclude_translate-tags",
        }

        for key, arg in optional_pairs.items():
            value = settings.get(key, "")
            if value != "":
                command.extend([arg, value])

        prompt_file = settings.get("prompt_file", "")
        prompt_text = settings.get("prompt_text", "")
        prompt_system = settings.get("prompt_system", "")
        prompt_user = settings.get("prompt_user", "")
        if prompt_file:
            command.extend(["--prompt", prompt_file])
        elif prompt_text:
            command.extend(["--prompt", prompt_text])
        elif prompt_user:
            prompt_payload = {"user": prompt_user}
            if prompt_system:
                prompt_payload["system"] = prompt_system
            command.extend(["--prompt", json.dumps(prompt_payload, ensure_ascii=False)])

        if settings.get("use_context", "false").lower() in {"1", "true", "yes", "on"}:
            command.append("--use_context")
        if settings.get("resume", "false").lower() in {"1", "true", "yes", "on"}:
            command.append("--resume")
        if settings.get("allow_navigable_strings", "false").lower() in {"1", "true", "yes", "on"}:
            command.append("--allow_navigable_strings")

        key_map = {
            "openai_key": "--openai_key",
            "claude_key": "--claude_key",
            "gemini_key": "--gemini_key",
            "groq_key": "--groq_key",
            "xai_key": "--xai_key",
            "qwen_key": "--qwen_key",
            "caiyun_key": "--caiyun_key",
            "deepl_key": "--deepl_key",
            "custom_api": "--custom_api",
        }
        for key, arg in key_map.items():
            value = settings.get(key, "")
            if value:
                command.extend([arg, value])

        translate_mode = payload.get("translate_mode", "preview")
        if translate_mode == "preview":
            command.append("--test")
            command.extend(["--test_num", str(payload.get("test_num") or settings.get("test_num", "80"))])

        translation_output_mode = str(payload.get("translation_output_mode", "translated_only")).strip()
        if translation_output_mode == "translated_only":
            command.append("--single_translate")

        timeout_seconds = int(payload.get("process_timeout") or settings.get("process_timeout") or cfg.process_timeout_seconds)
        self._log(task_id, "Running translator command")
        self._run_command(task_id, command, cwd=str(cfg.translator_entry.parent), timeout_seconds=timeout_seconds)

        translated = self._resolve_translated_file(source_path)
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
        with get_conn() as conn:
            set_task_pid(conn, task_id, None)

    def _terminate_process(self, process: subprocess.Popen[str]) -> None:
        cfg = get_config()
        if process.poll() is not None:
            return

        # Best-effort terminate -> kill -> reap (avoid leaving zombies behind).
        try:
            process.terminate()
            process.wait(timeout=cfg.stop_grace_seconds)
            return
        except subprocess.TimeoutExpired:
            pass
        except Exception:
            pass

        try:
            process.kill()
        except Exception:
            return

        try:
            process.wait(timeout=5)
        except Exception:
            # As a last resort, let the caller decide how to proceed.
            pass

    def _maybe_update_progress_throttled(self, task_id: int, evt: dict[str, Any]) -> None:
        now = time.monotonic()
        with self._progress_lock:
            last_ts, last_evt = self._progress_state.get(task_id, (0.0, {}))
            if evt == last_evt and (now - last_ts) < (self._progress_min_interval_seconds * 4):
                return
            if (now - last_ts) < self._progress_min_interval_seconds:
                return
            self._progress_state[task_id] = (now, dict(evt))

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

    def _run_command(self, task_id: int, command: list[str], cwd: str, timeout_seconds: int) -> None:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
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
                    self._terminate_process(process)
                    break

                now = time.monotonic()
                if now - last_stop_check >= stop_check_interval:
                    with get_conn() as conn:
                        if is_stop_requested(conn, task_id):
                            stopped = True
                        if is_pause_requested(conn, task_id):
                            paused = True
                    last_stop_check = now

                if stopped or paused:
                    self._terminate_process(process)
                    break

            # Ensure the process is reaped even after terminate/kill.
            try:
                rc = process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self._terminate_process(process)
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
                raise RuntimeError(f"Command failed with exit code {rc}: {self._redact_command(command)}")
        finally:
            self._unregister_process(task_id)

    def _redact_command(self, command: list[str]) -> str:
        sensitive_flags = {
            "--openai_key",
            "--claude_key",
            "--gemini_key",
            "--groq_key",
            "--xai_key",
            "--qwen_key",
            "--caiyun_key",
            "--deepl_key",
            "--custom_api",
            "--cookie",
            "--cookie-file",
        }

        redacted: list[str] = []
        hide_next = False
        for token in command:
            if hide_next:
                redacted.append("***")
                hide_next = False
                continue

            redacted.append(token)
            if token in sensitive_flags:
                hide_next = True

        return " ".join(redacted)

    def _resolve_source_file(self, download_root: Path, merged_name: str, save_format: str) -> Path:
        suffix = ".txt" if save_format == "txt" else ".epub"
        merged_name = (merged_name or "").strip()
        if merged_name:
            merged_candidate = f"{merged_name}{suffix}"
            found = sorted(download_root.rglob(merged_candidate))
            if found:
                return found[0]

        if save_format == "txt":
            txts = sorted(download_root.rglob("*.txt"), key=lambda p: p.stat().st_size, reverse=True)
            if txts:
                return txts[0]
        else:
            epubs = sorted(download_root.rglob("*.epub"), key=lambda p: p.stat().st_size, reverse=True)
            if epubs:
                return epubs[0]

        all_candidates = sorted(
            [*download_root.rglob("*.txt"), *download_root.rglob("*.epub")],
            key=lambda p: p.stat().st_size,
            reverse=True,
        )
        if all_candidates:
            return all_candidates[0]

        raise RuntimeError(f"No source output file found under {download_root}")

    def _resolve_translated_file(self, source_path: Path) -> Path | None:
        stem = source_path.stem
        parent = source_path.parent
        suffix = source_path.suffix.lower()

        preferred = "_翻译"
        legacy = "_bilingual"

        if suffix == ".txt":
            for marker in (preferred, legacy):
                candidate = parent / f"{stem}{marker}.txt"
                if candidate.exists():
                    return candidate

        if suffix == ".epub":
            for marker in (preferred, legacy):
                candidate = parent / f"{stem}{marker}.epub"
                if candidate.exists():
                    return candidate

        matches = sorted(
            [
                *parent.glob(f"{stem}{preferred}*"),
                *parent.glob(f"{stem}{legacy}*"),
            ],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return matches[0] if matches else None

    def _collect_artifacts(self, task_root: Path) -> list[Path]:
        """
        Collect extra artifacts under task_root.

        Notes:
        - source/translated main outputs are already added explicitly; here we mainly pick up
          manifest/logs and other useful files.
        - avoid exposing hidden/temp/cache files, and cap the total count to keep the UI responsive.
        """
        max_extra = max(1, int(os.getenv("WEBUI_MAX_EXTRA_ARTIFACTS", "200")))
        allowed_ext = {".log", ".json", ".txt", ".epub", ".md", ".pdf", ".srt"}
        ignore_dirs = {"__pycache__", ".pytest_cache", ".cache", "cache", "tmp", "temp"}

        scored: list[tuple[float, int, Path]] = []
        for p in task_root.rglob("*"):
            if not p.is_file():
                continue

            # never expose temp cookie files as downloadable artifacts
            if p.name.startswith(".cookie_"):
                continue

            try:
                rel = p.relative_to(task_root)
            except ValueError:
                # should not happen, but keep it safe
                continue

            # exclude hidden paths and common cache/temp folders
            if any(part in ignore_dirs for part in rel.parts):
                continue
            if any(part.startswith(".") for part in rel.parts):
                continue

            # allowlist by file type/name
            if p.name != "manifest.json" and p.suffix.lower() not in allowed_ext:
                continue

            try:
                st = p.stat()
            except OSError:
                continue

            scored.append((float(st.st_mtime), int(st.st_size), p))

        scored.sort(reverse=True)
        if len(scored) > max_extra:
            scored = scored[:max_extra]

        files = [p for _, __, p in scored]
        return sorted(files)

    def _artifact_kind(self, file_path: Path) -> str:
        name = file_path.name.lower()
        if (
            name.endswith("_bilingual.txt")
            or name.endswith("_bilingual.epub")
            or name.endswith("_翻译.txt")
            or name.endswith("_翻译.epub")
        ):
            return "translated"
        if name.endswith("manifest.json"):
            return "manifest"
        if name.endswith(".log"):
            return "log"
        if file_path.suffix.lower() in {".txt", ".epub", ".md", ".pdf", ".srt"}:
            return "source"
        return "other"

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
