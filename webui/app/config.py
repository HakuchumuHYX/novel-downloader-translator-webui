from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


def _as_bool(value: str, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class AppConfig:
    base_dir: Path
    data_dir: Path
    db_path: Path
    task_root: Path
    upload_root: Path
    worker_interval_seconds: float
    cleanup_days: int
    app_env: str
    enforce_secure_defaults: bool
    basic_auth_user: str
    basic_auth_password: str
    secret_key: str
    require_secret_key: bool
    process_timeout_seconds: int
    stop_grace_seconds: int
    downloader_python: str
    downloader_entry: Path
    translator_python: str
    translator_entry: Path


def _normalize_translator_entry(raw_value: str, project_root: Path) -> Path:
    path = Path(raw_value or str(project_root / "bilingual_book_maker"))
    if path.suffix.lower() == ".py":
        path = path.parent
    return path.resolve()


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    base_dir = Path(__file__).resolve().parents[2]
    project_root = base_dir.parent

    data_dir = Path(os.getenv("WEBUI_DATA_DIR", str(project_root / "data"))).resolve()
    db_path = Path(os.getenv("WEBUI_DB_PATH", str(data_dir / "webui.sqlite3"))).resolve()
    task_root = Path(os.getenv("WEBUI_TASK_ROOT", str(data_dir / "tasks"))).resolve()
    upload_root = Path(os.getenv("WEBUI_UPLOAD_ROOT", str(data_dir / "uploads"))).resolve()

    app_env = os.getenv("WEBUI_ENV", "dev").strip().lower() or "dev"
    default_enforce_secure = app_env in {"prod", "production"}
    return AppConfig(
        base_dir=base_dir,
        data_dir=data_dir,
        db_path=db_path,
        task_root=task_root,
        upload_root=upload_root,
        worker_interval_seconds=float(os.getenv("WEBUI_WORKER_INTERVAL", "1.0")),
        cleanup_days=int(os.getenv("WEBUI_CLEANUP_DAYS", "14")),
        app_env=app_env,
        enforce_secure_defaults=_as_bool(
            os.getenv("WEBUI_ENFORCE_SECURE_DEFAULTS", "true" if default_enforce_secure else "false"),
            default=default_enforce_secure,
        ),
        basic_auth_user=os.getenv("WEBUI_BASIC_AUTH_USER", "admin"),
        basic_auth_password=os.getenv("WEBUI_BASIC_AUTH_PASSWORD", "change_me"),
        secret_key=os.getenv("WEBUI_SECRET_KEY", ""),
        require_secret_key=_as_bool(os.getenv("WEBUI_REQUIRE_SECRET_KEY", "false")),
        process_timeout_seconds=max(60, int(os.getenv("WEBUI_PROCESS_TIMEOUT", "7200"))),
        stop_grace_seconds=max(1, int(os.getenv("WEBUI_STOP_GRACE_SECONDS", "8"))),
        downloader_python=os.getenv("DOWNLOADER_PYTHON", "python"),
        downloader_entry=Path(
            os.getenv(
                "DOWNLOADER_ENTRY",
                str(project_root / "syosetu_novel_downloader" / "main.py"),
            )
        ).resolve(),
        translator_python=os.getenv("TRANSLATOR_PYTHON", "python"),
        translator_entry=_normalize_translator_entry(
            os.getenv(
                "TRANSLATOR_ENTRY",
                str(project_root / "bilingual_book_maker"),
            ),
            project_root,
        ),
    )
