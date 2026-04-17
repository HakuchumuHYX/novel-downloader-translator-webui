from __future__ import annotations

import shutil
import subprocess
from typing import Any

from ..config import get_config
from ..security import encryption_configured


def _format_bytes(value: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    size = float(max(0, value))
    unit_index = 0
    while size >= 1024.0 and unit_index < len(units) - 1:
        size /= 1024.0
        unit_index += 1
    if unit_index == 0:
        return f"{int(size)} {units[unit_index]}"
    return f"{size:.1f} {units[unit_index]}"


def _run_cmd_status(command: list[str], cwd: str | None = None) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=5,
        )
        output = (proc.stdout or proc.stderr or "").strip()
        return {"ok": proc.returncode == 0, "code": proc.returncode, "output": output[:500]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "code": -1, "output": str(exc)}


def collect_system_status() -> dict[str, Any]:
    cfg = get_config()
    disk = shutil.disk_usage(cfg.data_dir)
    usage_percent = round((disk.used / disk.total) * 100, 1) if disk.total > 0 else 0.0
    return {
        "encryption_configured": encryption_configured(),
        "secret_key_required": cfg.require_secret_key,
        "app_env": cfg.app_env,
        "enforce_secure_defaults": cfg.enforce_secure_defaults,
        "paths": {
            "data_dir": str(cfg.data_dir),
            "db_path": str(cfg.db_path),
            "downloader_entry": str(cfg.downloader_entry),
            "translator_entry": str(cfg.translator_entry),
            "downloader_exists": cfg.downloader_entry.exists(),
            "translator_exists": cfg.translator_entry.exists(),
        },
        "commands": {
            "downloader_python": _run_cmd_status([cfg.downloader_python, "--version"]),
            "translator_python": _run_cmd_status([cfg.translator_python, "--version"]),
            "node": _run_cmd_status(["node", "--version"]),
            "npm": _run_cmd_status(["npm", "--version"]),
        },
        "disk": {
            "total_bytes": disk.total,
            "used_bytes": disk.used,
            "free_bytes": disk.free,
            "total": _format_bytes(disk.total),
            "used": _format_bytes(disk.used),
            "free": _format_bytes(disk.free),
            "usage_percent": usage_percent,
            "usage_summary": f"{_format_bytes(disk.used)} / {_format_bytes(disk.total)} ({usage_percent:.1f}%)",
        },
    }
