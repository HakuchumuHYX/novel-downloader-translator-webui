from __future__ import annotations

import shutil
import subprocess
from typing import Any

from ..config import get_config
from ..security import encryption_configured


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
            "total": disk.total,
            "used": disk.used,
            "free": disk.free,
        },
    }
