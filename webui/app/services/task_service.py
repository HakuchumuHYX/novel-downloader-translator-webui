from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..db import utcnow_iso


TASK_LOG_MAX_LINES = max(100, int(os.getenv("WEBUI_TASK_LOG_MAX_LINES", "2000")))


def _iso_from_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def create_task(conn: sqlite3.Connection, payload: dict[str, Any], parent_task_id: int | None = None) -> int:
    now = utcnow_iso()
    cur = conn.execute(
        """
        INSERT INTO tasks(
            status, mode, source_type, source_input, upload_path,
            cookie_profile_id, payload_json, parent_task_id,
            created_at, stop_requested
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
        """,
        (
            "queued",
            payload.get("mode", "download_and_translate"),
            payload.get("source_type", "upload"),
            payload.get("source_input", ""),
            payload.get("upload_path", ""),
            payload.get("cookie_profile_id"),
            json.dumps(payload, ensure_ascii=False),
            parent_task_id,
            now,
        ),
    )
    return int(cur.lastrowid)


def list_tasks(conn: sqlite3.Connection, limit: int = 100) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM tasks ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()


def get_task(conn: sqlite3.Connection, task_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()


def get_next_queued_task(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM tasks WHERE status = 'queued' ORDER BY id ASC LIMIT 1"
    ).fetchone()


def claim_next_queued_task(conn: sqlite3.Connection) -> sqlite3.Row | None:
    while True:
        row = conn.execute("SELECT id FROM tasks WHERE status = 'queued' ORDER BY id ASC LIMIT 1").fetchone()
        if not row:
            return None

        task_id = int(row["id"])
        cur = conn.execute(
            """
            UPDATE tasks
            SET status = 'running',
                started_at = ?,
                error_message = '',
                error_code = '',
                stop_requested = 0
            WHERE id = ? AND status = 'queued'
            """,
            (utcnow_iso(), task_id),
        )
        if cur.rowcount > 0:
            return get_task(conn, task_id)


def set_task_running(conn: sqlite3.Connection, task_id: int) -> None:
    conn.execute(
        """
        UPDATE tasks
        SET status = 'running',
            started_at = ?,
            error_message = '',
            error_code = '',
            stop_requested = 0
        WHERE id = ?
        """,
        (utcnow_iso(), task_id),
    )


def set_task_pid(conn: sqlite3.Connection, task_id: int, pid: int | None) -> None:
    conn.execute("UPDATE tasks SET running_pid = ? WHERE id = ?", (pid, task_id))


def request_stop_task(conn: sqlite3.Connection, task_id: int) -> bool:
    cur = conn.execute(
        "UPDATE tasks SET stop_requested = 1 WHERE id = ? AND status = 'running'",
        (task_id,),
    )
    return cur.rowcount > 0


def is_stop_requested(conn: sqlite3.Connection, task_id: int) -> bool:
    row = conn.execute("SELECT stop_requested FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return bool(row and int(row["stop_requested"]) == 1)


def set_task_finished(
    conn: sqlite3.Connection,
    task_id: int,
    *,
    status: str,
    download_output_dir: str = "",
    source_full_book_path: str = "",
    translated_output_path: str = "",
    error_message: str = "",
    error_code: str = "",
) -> None:
    conn.execute(
        """
        UPDATE tasks
        SET status = ?,
            finished_at = ?,
            download_output_dir = ?,
            source_full_book_path = ?,
            translated_output_path = ?,
            error_message = ?,
            error_code = ?,
            running_pid = NULL
        WHERE id = ?
        """,
        (
            status,
            utcnow_iso(),
            download_output_dir,
            source_full_book_path,
            translated_output_path,
            error_message,
            error_code,
            task_id,
        ),
    )


def cancel_task(conn: sqlite3.Connection, task_id: int) -> bool:
    cur = conn.execute(
        "UPDATE tasks SET status = 'canceled', finished_at = ?, error_code = 'CANCELED_QUEUED' WHERE id = ? AND status = 'queued'",
        (utcnow_iso(), task_id),
    )
    return cur.rowcount > 0


def append_log(conn: sqlite3.Connection, task_id: int, message: str, level: str = "info") -> None:
    conn.execute(
        "INSERT INTO task_logs(task_id, level, message, created_at) VALUES(?, ?, ?, ?)",
        (task_id, level, message, utcnow_iso()),
    )
    conn.execute(
        """
        DELETE FROM task_logs
        WHERE task_id = ?
          AND id NOT IN (
              SELECT id
              FROM task_logs
              WHERE task_id = ?
              ORDER BY id DESC
              LIMIT ?
          )
        """,
        (task_id, task_id, TASK_LOG_MAX_LINES),
    )


def get_logs_after(conn: sqlite3.Connection, task_id: int, offset: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM task_logs WHERE task_id = ? AND id > ? ORDER BY id ASC",
        (task_id, max(0, offset)),
    ).fetchall()


def add_artifact(conn: sqlite3.Connection, task_id: int, kind: str, file_path: Path) -> None:
    if not file_path.exists() or not file_path.is_file():
        return
    stat = file_path.stat()
    conn.execute(
        """
        INSERT INTO task_artifacts(task_id, kind, file_name, file_path, file_size, modified_at, created_at)
        VALUES(?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(task_id, file_path) DO UPDATE SET
            kind = excluded.kind,
            file_name = excluded.file_name,
            file_size = excluded.file_size,
            modified_at = excluded.modified_at
        """,
        (
            task_id,
            kind,
            file_path.name,
            str(file_path),
            int(stat.st_size),
            _iso_from_ts(stat.st_mtime),
            utcnow_iso(),
        ),
    )


def clear_artifacts(conn: sqlite3.Connection, task_id: int) -> None:
    conn.execute("DELETE FROM task_artifacts WHERE task_id = ?", (task_id,))


def list_artifacts(conn: sqlite3.Connection, task_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM task_artifacts WHERE task_id = ? ORDER BY id ASC",
        (task_id,),
    ).fetchall()


def get_artifact(conn: sqlite3.Connection, artifact_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM task_artifacts WHERE id = ?", (artifact_id,)).fetchone()


def create_or_update_cookie_profile(
    conn: sqlite3.Connection,
    *,
    profile_id: int | None,
    name: str,
    site: str,
    cookie_enc: str,
) -> int:
    now = utcnow_iso()
    if profile_id:
        cur = conn.execute(
            """
            UPDATE cookie_profiles
            SET name = ?, site = ?, cookie_enc = ?, updated_at = ?
            WHERE id = ?
            """,
            (name, site, cookie_enc, now, profile_id),
        )
        if cur.rowcount == 0:
            raise LookupError("cookie profile not found")
        return profile_id

    cur = conn.execute(
        """
        INSERT INTO cookie_profiles(name, site, cookie_enc, created_at, updated_at)
        VALUES(?, ?, ?, ?, ?)
        """,
        (name, site, cookie_enc, now, now),
    )
    return int(cur.lastrowid)


def list_cookie_profiles(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM cookie_profiles ORDER BY id DESC").fetchall()


def get_cookie_profile(conn: sqlite3.Connection, profile_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM cookie_profiles WHERE id = ?", (profile_id,)).fetchone()


def count_cookie_profile_task_refs(conn: sqlite3.Connection, profile_id: int) -> tuple[int, int]:
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS total_refs,
            SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) AS running_refs
        FROM tasks
        WHERE cookie_profile_id = ?
        """,
        (profile_id,),
    ).fetchone()

    if row is None:
        return 0, 0

    total_refs = int(row["total_refs"] or 0)
    running_refs = int(row["running_refs"] or 0)
    return total_refs, running_refs


def detach_cookie_profile_from_non_running_tasks(conn: sqlite3.Connection, profile_id: int) -> int:
    cur = conn.execute(
        """
        UPDATE tasks
        SET cookie_profile_id = NULL
        WHERE cookie_profile_id = ?
          AND status != 'running'
        """,
        (profile_id,),
    )
    return int(cur.rowcount)


def delete_cookie_profile(conn: sqlite3.Connection, profile_id: int) -> bool:
    cur = conn.execute("DELETE FROM cookie_profiles WHERE id = ?", (profile_id,))
    return cur.rowcount > 0


def create_task_template(conn: sqlite3.Connection, name: str, payload: dict[str, Any]) -> int:
    cur = conn.execute(
        """
        INSERT INTO task_templates(name, payload_json, created_at)
        VALUES(?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET payload_json = excluded.payload_json
        RETURNING id
        """,
        (name, json.dumps(payload, ensure_ascii=False), utcnow_iso()),
    )
    row = cur.fetchone()
    return int(row[0])


def list_task_templates(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM task_templates ORDER BY id DESC").fetchall()


def get_task_template(conn: sqlite3.Connection, template_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM task_templates WHERE id = ?", (template_id,)).fetchone()
