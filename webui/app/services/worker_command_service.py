from __future__ import annotations

import os
import signal
import subprocess


SENSITIVE_FLAGS = {
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


def classify_worker_error(exc: Exception) -> tuple[str, str]:
    msg = str(exc)
    low = msg.lower()
    if "__task_paused__" in low:
        return "paused", "PAUSED"
    if "__task_stopped__" in low:
        return "canceled", "STOPPED"
    if "__task_timeout__" in low:
        return "failed", "PROCESS_TIMEOUT"
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


def redact_command(command: list[str]) -> str:
    redacted: list[str] = []
    hide_next = False
    for token in command:
        if hide_next:
            redacted.append("***")
            hide_next = False
            continue
        redacted.append(token)
        if token in SENSITIVE_FLAGS:
            hide_next = True
    return " ".join(redacted)


def terminate_process(process: subprocess.Popen[str], *, stop_grace_seconds: float, reason: str = "") -> None:
    if process.poll() is not None:
        return

    reason_norm = (reason or "").strip().lower()

    def send(sig: int) -> bool:
        try:
            os.killpg(process.pid, sig)
            return True
        except Exception:
            try:
                process.send_signal(sig)
                return True
            except Exception:
                return False

    def wait(timeout_seconds: float) -> bool:
        if timeout_seconds <= 0:
            return False
        try:
            process.wait(timeout=timeout_seconds)
            return True
        except subprocess.TimeoutExpired:
            return False
        except Exception:
            return False

    if reason_norm == "paused":
        if send(signal.SIGINT) and wait(max(float(stop_grace_seconds), 8.0)):
            return

    if send(signal.SIGTERM) and wait(max(float(stop_grace_seconds), 2.0)):
        return

    if not send(signal.SIGKILL):
        return
    wait(5.0)
