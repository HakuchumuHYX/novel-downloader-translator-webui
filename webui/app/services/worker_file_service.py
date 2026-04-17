from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable


def safe_int(value) -> int:
    try:
        return int(value)
    except Exception:
        return 0


def file_has_content(path: Path) -> bool:
    try:
        return path.exists() and path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def translate_resume_state_path(source_path: Path) -> Path:
    return source_path.parent / f".{source_path.stem}.temp.bin"


def has_translate_resume_state(source_path: Path) -> bool:
    return file_has_content(translate_resume_state_path(source_path))


def resolve_source_file(download_root: Path, merged_name: str, save_format: str) -> Path:
    candidates = list_source_candidates(download_root, save_format=save_format)
    suffix = ".txt" if save_format == "txt" else ".epub"
    merged_name = (merged_name or "").strip()
    if merged_name:
        merged_candidate = f"{merged_name}{suffix}".lower()
        found = sorted(
            [
                path for path in candidates if path.suffix.lower() == suffix and path.name.lower() == merged_candidate
            ]
        )
        if found:
            return found[0]

    preferred = [path for path in candidates if path.suffix.lower() == suffix]
    if preferred:
        return preferred[0]
    if candidates:
        return candidates[0]

    raise RuntimeError(f"No source output file found under {download_root}")


def list_source_candidates(download_root: Path, save_format: str) -> list[Path]:
    def is_source_candidate(path: Path) -> bool:
        name = path.name.lower()
        excluded_markers = ("_翻译", "_bilingual", "_temp", "source_metadata", "readme")
        return not any(marker in name for marker in excluded_markers)

    return sorted(
        [
            path
            for path in [*download_root.rglob("*.txt"), *download_root.rglob("*.epub")]
            if is_source_candidate(path)
        ],
        key=lambda path: (path.suffix.lower() != f".{save_format}", -path.stat().st_size, str(path)),
    )


def resolve_translated_file(source_path: Path) -> Path | None:
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
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return matches[0] if matches else None


def collect_artifacts(task_root: Path) -> list[Path]:
    max_extra = max(1, int(os.getenv("WEBUI_MAX_EXTRA_ARTIFACTS", "200")))
    allowed_ext = {".log", ".json", ".txt", ".epub", ".md", ".pdf", ".srt"}
    ignore_dirs = {"__pycache__", ".pytest_cache", ".cache", "cache", "tmp", "temp"}

    preferred_roots: list[Path] = []
    downloads_dir = task_root / "downloads"
    if downloads_dir.exists():
        preferred_roots.append(downloads_dir)
    preferred_roots.append(task_root)

    seen_paths: set[Path] = set()
    scored: list[tuple[float, int, Path]] = []
    for root in preferred_roots:
        iterator = root.rglob("*") if root != task_root else root.glob("*")
        for path in iterator:
            if path in seen_paths:
                continue
            seen_paths.add(path)
            if not path.is_file():
                continue
            if path.name.startswith(".cookie_"):
                continue

            try:
                rel = path.relative_to(task_root)
            except ValueError:
                continue

            if any(part in ignore_dirs for part in rel.parts):
                continue
            if any(part.startswith(".") for part in rel.parts):
                continue
            if path.name != "manifest.json" and path.suffix.lower() not in allowed_ext:
                continue

            try:
                stat = path.stat()
            except OSError:
                continue
            scored.append((float(stat.st_mtime), int(stat.st_size), path))

    scored.sort(reverse=True)
    if len(scored) > max_extra:
        scored = scored[:max_extra]

    return sorted(path for _, __, path in scored)


def artifact_kind(file_path: Path) -> str:
    name = file_path.name.lower()
    translated_exts = {".txt", ".epub", ".md", ".pdf", ".srt"}
    if (
        any(name.endswith(f"_bilingual{ext}") for ext in translated_exts)
        or any(name.endswith(f"_翻译{ext}") for ext in translated_exts)
        or any(name.endswith(f"_翻译_temp{ext}") for ext in translated_exts)
    ):
        return "translated"
    if name.endswith("manifest.json"):
        return "manifest"
    if name.endswith(".log"):
        return "log"
    if file_path.suffix.lower() in {".txt", ".epub", ".md", ".pdf", ".srt"}:
        return "source"
    return "other"


def log_download_manifest_summary(
    task_id: int,
    download_root: Path,
    *,
    log: Callable[[int, str, str], None],
) -> None:
    manifests = sorted(
        [path for path in download_root.rglob("manifest.json") if path.is_file()],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not manifests:
        return

    manifest_path = manifests[0]
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        log(task_id, f"Download manifest exists but could not be parsed: {manifest_path}", "warning")
        return

    backend = str(payload.get("backend_used", "")).strip() or "unknown"
    status = str(payload.get("status", "")).strip() or "unknown"
    chapter_count = safe_int(payload.get("chapter_count"))
    expected_count = safe_int(payload.get("expected_chapter_count"))
    skipped = safe_int(payload.get("skipped_chapters"))

    log(
        task_id,
        "Download manifest summary: "
        f"backend={backend}, status={status}, chapter_count={chapter_count}, "
        f"expected={expected_count}, skipped={skipped}",
        "info",
    )

    reasons = payload.get("skipped_reasons")
    if isinstance(reasons, list):
        for reason in reasons:
            text = str(reason).strip()
            if text:
                log(task_id, f"Download skipped reason: {text}", "warning")
