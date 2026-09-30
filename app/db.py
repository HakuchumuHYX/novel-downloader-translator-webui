from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from .config import DATA_DIR, DB_PATH, TASK_DIR

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL,            -- queued / running / succeeded / failed / canceled
    title TEXT NOT NULL,
    url TEXT NOT NULL DEFAULT '',
    upload_name TEXT NOT NULL DEFAULT '',
    options_json TEXT NOT NULL,      -- {"mode", "format", "bilingual", "test_blocks"}
    stage TEXT NOT NULL DEFAULT '',  -- download / translate
    progress_current INTEGER NOT NULL DEFAULT 0,
    progress_total INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS task_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_task_logs_task ON task_logs(task_id, id);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db() -> None:
    TASK_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        # worker 线程和请求线程同时读写，WAL 避免读被写阻塞
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
