from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .config import get_config


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row

    # WAL improves concurrency, but writers can still hit "database is locked" briefly.
    # busy_timeout lets SQLite wait a bit for locks instead of failing immediately.
    busy_timeout_ms = int(os.getenv("WEBUI_SQLITE_BUSY_TIMEOUT_MS", "5000"))
    if busy_timeout_ms > 0:
        conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms};")

    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


@contextmanager
def get_conn() -> sqlite3.Connection:
    cfg = get_config()
    conn = _connect(cfg.db_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl_suffix: str) -> None:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column in existing:
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_suffix}")


def init_db() -> None:
    cfg = get_config()
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.task_root.mkdir(parents=True, exist_ok=True)
    cfg.upload_root.mkdir(parents=True, exist_ok=True)

    conn = _connect(cfg.db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                is_secret INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS cookie_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                site TEXT NOT NULL,
                cookie_enc TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS task_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                status TEXT NOT NULL,
                mode TEXT NOT NULL,
                source_type TEXT NOT NULL,
                source_input TEXT NOT NULL DEFAULT '',
                upload_path TEXT NOT NULL DEFAULT '',
                cookie_profile_id INTEGER,
                payload_json TEXT NOT NULL,
                download_output_dir TEXT NOT NULL DEFAULT '',
                source_full_book_path TEXT NOT NULL DEFAULT '',
                translated_output_path TEXT NOT NULL DEFAULT '',
                error_message TEXT NOT NULL DEFAULT '',
                parent_task_id INTEGER,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                FOREIGN KEY(cookie_profile_id) REFERENCES cookie_profiles(id),
                FOREIGN KEY(parent_task_id) REFERENCES tasks(id)
            );

            CREATE TABLE IF NOT EXISTS task_artifacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                file_name TEXT NOT NULL,
                file_path TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS task_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                level TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_tasks_status_created
            ON tasks(status, created_at);

            CREATE INDEX IF NOT EXISTS idx_task_logs_task_id_id
            ON task_logs(task_id, id);

            CREATE INDEX IF NOT EXISTS idx_task_artifacts_task_id
            ON task_artifacts(task_id);
            """
        )

        _ensure_column(conn, "tasks", "error_code", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "tasks", "running_pid", "INTEGER")
        _ensure_column(conn, "tasks", "stop_requested", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "tasks", "pause_requested", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "tasks", "stage", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "tasks", "download_current", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "tasks", "download_total", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "tasks", "translate_current", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "tasks", "translate_total", "INTEGER NOT NULL DEFAULT 0")

        _ensure_column(conn, "task_artifacts", "file_size", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "task_artifacts", "modified_at", "TEXT NOT NULL DEFAULT ''")

        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_task_artifacts_task_path ON task_artifacts(task_id, file_path)"
        )
        conn.commit()
    except sqlite3.OperationalError as exc:
        if "readonly" in str(exc).lower():
            raise RuntimeError(
                f"Database path is not writable: {cfg.db_path}. "
                "If you are running Docker on Windows with a bind mount, set WEBUI_CONTAINER_USER=0:0 "
                "in .env or fix write permissions for ./data."
            ) from exc
        raise
    finally:
        conn.close()
