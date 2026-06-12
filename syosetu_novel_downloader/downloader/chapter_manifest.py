from __future__ import annotations

import json
import os
from pathlib import Path

from .models import Chapter
from .utils import sanitize_filename


CHAPTER_MANIFEST_NAME = "_chapter_manifest.json"


def chapter_file_path(book_dir: Path, chapter_index: int) -> Path:
    return book_dir / "chapters" / f"{int(chapter_index):06d}.txt"


def _relative_path(book_dir: Path, path: Path) -> str:
    try:
        return str(path.relative_to(book_dir))
    except ValueError:
        return path.name


def load_chapter_manifest(book_dir: Path) -> dict[int, dict[str, str]]:
    manifest_path = book_dir / CHAPTER_MANIFEST_NAME
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    chapters = raw.get("chapters")
    if not isinstance(chapters, list):
        return {}

    entries: dict[int, dict[str, str]] = {}
    for entry in chapters:
        if not isinstance(entry, dict):
            continue
        try:
            index = int(entry.get("index"))
        except (TypeError, ValueError):
            continue
        entries[index] = {
            "status": str(entry.get("status") or ""),
            "file_path": str(entry.get("file_path") or ""),
            "volume": str(entry.get("volume") or ""),
            "title": str(entry.get("title") or ""),
        }
    return entries


def write_chapter_manifest_entry(
    book_dir: Path,
    chapter_index: int,
    file_path: str | Path,
    status: str,
    *,
    volume: str,
    title: str = "",
) -> None:
    entries = load_chapter_manifest(book_dir)
    path = Path(file_path)
    entries[int(chapter_index)] = {
        "status": status,
        "file_path": _relative_path(book_dir, path),
        "volume": volume,
        "title": title,
    }
    payload = {
        "version": 2,
        "chapters": [
            {"index": index, **entry}
            for index, entry in sorted(entries.items())
        ],
    }
    manifest_path = book_dir / CHAPTER_MANIFEST_NAME
    temp_path = manifest_path.with_suffix(manifest_path.suffix + ".tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(manifest_path)


def chapter_output_has_content(book_dir: Path, file_path: str | Path) -> bool:
    path = Path(file_path)
    if not path.is_absolute():
        path = book_dir / path
    return path.is_file() and bool(path.read_text(encoding="utf-8", errors="ignore").strip())


def filter_pending_chapter_jobs(book_dir: str | Path, jobs: list[tuple[int, str]]) -> tuple[list[tuple[int, str]], list[int]]:
    book_path = Path(book_dir)
    entries = load_chapter_manifest(book_path)
    pending: list[tuple[int, str]] = []
    skipped: list[int] = []
    for chapter_index, volume in jobs:
        entry = entries.get(int(chapter_index), {})
        status = str(entry.get("status") or "").lower()
        entry_path = str(entry.get("file_path") or _relative_path(book_path, chapter_file_path(book_path, chapter_index)))
        if status in {"ok", "completed"} and chapter_output_has_content(book_path, entry_path):
            skipped.append(chapter_index)
            continue
        pending.append((chapter_index, volume))
    return pending, skipped


def write_chapter_record(
    book_dir: Path,
    chapter_index: int,
    volume: str,
    title: str,
    content: str,
    *,
    chapter_suffix: str,
) -> Path:
    chapter_path = chapter_file_path(book_dir, chapter_index)
    chapter_path.parent.mkdir(parents=True, exist_ok=True)
    header = f"● {title}"
    if chapter_suffix:
        header = f"{header} {chapter_suffix}"
    temp_path = chapter_path.with_suffix(chapter_path.suffix + ".tmp")
    with open(temp_path, "w", encoding="utf-8") as handle:
        handle.write(f"{header}\n")
        handle.write(f"{content}\n")
        handle.flush()
        os.fsync(handle.fileno())
    temp_path.replace(chapter_path)
    write_chapter_manifest_entry(book_dir, chapter_index, chapter_path, "ok", volume=volume, title=title)
    return chapter_path


def _parse_chapter_file(path: Path, fallback_title: str, *, strip_index_suffix: bool) -> tuple[str, str]:
    text = path.read_text(encoding="utf-8", errors="ignore").strip()
    lines = text.splitlines()
    if lines and lines[0].startswith("● "):
        title = lines[0][2:].strip() or fallback_title
        if strip_index_suffix and title.endswith("]") and " [" in title:
            title = title.rsplit(" [", 1)[0]
        return title, "\n".join(lines[1:]).strip()
    return fallback_title, text


def chapters_from_manifest(book_dir: Path, *, strip_index_suffix: bool = False) -> list[Chapter]:
    chapters: list[Chapter] = []
    for index, entry in sorted(load_chapter_manifest(book_dir).items()):
        if str(entry.get("status") or "").lower() not in {"ok", "completed"}:
            continue
        rel = str(entry.get("file_path") or "")
        if not rel:
            continue
        path = Path(rel)
        if not path.is_absolute():
            path = book_dir / path
        if not chapter_output_has_content(book_dir, path):
            continue
        title, content = _parse_chapter_file(path, entry.get("title") or path.stem, strip_index_suffix=strip_index_suffix)
        chapters.append(
            Chapter(
                index=int(index),
                title=title,
                content=content,
                volume=str(entry.get("volume") or sanitize_filename(book_dir.name, default="book")),
                source_path=_relative_path(book_dir, path),
            )
        )
    return chapters
