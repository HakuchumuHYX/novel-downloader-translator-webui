from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from ..config import AppConfig
from .preview_service import preview_epub_file, preview_pdf_file, preview_text_file
from .task_service import (
    clear_artifacts,
    clear_task_output_paths,
    delete_task as delete_task_row,
    get_artifact,
    get_task,
    list_task_descendants,
    list_tasks_by_ids,
)


def row_to_dict(row: Any) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def normalize_page_size(value: int, default: int, maximum: int) -> int:
    return max(1, min(maximum, value or default))


def can_manage_task(status: str, force: bool) -> tuple[bool, str]:
    if status == "running":
        return False, "running task cannot be managed directly; stop it first"
    if status in {"queued", "paused"} and not force:
        return False, "queued/paused task requires force=true"
    return True, ""


def safe_path(path_str: str, cfg: AppConfig) -> Path:
    path = Path(path_str).resolve()
    data_root = cfg.data_dir.resolve()
    try:
        path.relative_to(data_root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid path") from exc
    return path


def safe_task_file_path(task_id: int, path_str: str, cfg: AppConfig) -> Path:
    path = safe_path(path_str, cfg)
    task_root = (cfg.task_root / str(task_id)).resolve()
    try:
        path.relative_to(task_root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="file path is outside current task root") from exc
    return path


def find_artifact(conn: sqlite3.Connection, task_id: int, artifact_id: int) -> dict[str, Any]:
    row = get_artifact(conn, artifact_id)
    if not row or int(row["task_id"]) != task_id:
        raise HTTPException(status_code=404, detail="Artifact not found")
    return row_to_dict(row)


def preview_file(path: Path, page: int):
    suffix = path.suffix.lower()
    if suffix == ".epub":
        return preview_epub_file(path, page=page)
    if suffix == ".pdf":
        return preview_pdf_file(path, page=page)
    if suffix in {".txt", ".md", ".srt"}:
        return preview_text_file(path, page=page, per_page=120)
    raise HTTPException(status_code=400, detail=f"Preview is not supported for {suffix or 'this file type'}")


def safe_delete_dir(target: Path, root: Path) -> bool:
    target = target.resolve()
    root = root.resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Refusing to delete path outside allowed root: {target}") from exc

    if not target.exists():
        return False
    if target.is_dir():
        shutil.rmtree(target, ignore_errors=True)
        return True
    target.unlink(missing_ok=True)
    return True


def safe_delete_upload_file(path_str: str, cfg: AppConfig) -> bool:
    if not path_str.strip():
        return False
    target = Path(path_str).resolve()
    try:
        target.relative_to(cfg.upload_root.resolve())
    except ValueError:
        return False
    if not target.exists() or not target.is_file():
        return False
    target.unlink(missing_ok=True)
    return True


def purge_task_outputs(
    conn: sqlite3.Connection,
    task_id: int,
    *,
    scope: str,
    delete_upload: bool,
    force: bool,
    cfg: AppConfig,
) -> tuple[list[str], bool]:
    row = get_task(conn, task_id)
    if not row:
        raise HTTPException(status_code=404, detail="Task not found")

    can_manage, reason = can_manage_task(str(row["status"]), force=force)
    if not can_manage:
        raise HTTPException(status_code=409, detail=reason)

    clear_artifacts(conn, task_id)
    clear_task_output_paths(conn, task_id)
    upload_path = str(row["upload_path"] or "")

    task_root = cfg.task_root / str(task_id)
    deleted_paths: list[str] = []

    if scope == "task_dir":
        if safe_delete_dir(task_root, cfg.task_root):
            deleted_paths.append(str(task_root))
    else:
        downloads_dir = task_root / "downloads"
        if safe_delete_dir(downloads_dir, cfg.task_root):
            deleted_paths.append(str(downloads_dir))

    return deleted_paths, safe_delete_upload_file(upload_path, cfg) if delete_upload else False


def delete_task_records(
    conn: sqlite3.Connection,
    task_id: int,
    *,
    force: bool,
    cascade: bool,
) -> tuple[list[int], list[str]]:
    row = get_task(conn, task_id)
    if not row:
        raise HTTPException(status_code=404, detail="Task not found")

    target_rows: list[Any] = [row]
    if cascade:
        descendant_ids = list_task_descendants(conn, task_id)
        if descendant_ids:
            target_rows.extend(list_tasks_by_ids(conn, descendant_ids))

    for task_row in target_rows:
        can_manage, reason = can_manage_task(str(task_row["status"]), force=force)
        if not can_manage:
            raise HTTPException(
                status_code=409,
                detail=f"task {int(task_row['id'])} cannot be deleted: {reason}",
            )

    if cascade:
        delete_order = [int(task_row["id"]) for task_row in target_rows if int(task_row["id"]) != task_id]
        delete_order.sort(reverse=True)
        delete_order.append(task_id)
    else:
        delete_order = [task_id]

    deleted_ids: list[int] = []
    upload_paths: list[str] = []
    for tid in delete_order:
        task_row = get_task(conn, tid)
        if not task_row:
            continue
        upload_paths.append(str(task_row["upload_path"] or ""))
        if delete_task_row(conn, tid):
            deleted_ids.append(tid)

    if not deleted_ids:
        raise HTTPException(status_code=404, detail="Task not found")

    return sorted(set(deleted_ids)), upload_paths


def finalize_task_delete(
    task_ids: list[int],
    upload_paths: list[str],
    *,
    delete_task_dir: bool,
    delete_upload: bool,
    cfg: AppConfig,
) -> tuple[list[str], int]:
    deleted_paths: list[str] = []
    if delete_task_dir:
        for task_id in task_ids:
            task_root = cfg.task_root / str(task_id)
            if safe_delete_dir(task_root, cfg.task_root):
                deleted_paths.append(str(task_root))

    deleted_upload_count = 0
    if delete_upload:
        for upload_path in upload_paths:
            if safe_delete_upload_file(upload_path, cfg):
                deleted_upload_count += 1

    return deleted_paths, deleted_upload_count
