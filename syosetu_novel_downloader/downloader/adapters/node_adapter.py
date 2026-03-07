from __future__ import annotations

import json
import shutil
import shutil as cmdshutil
import subprocess
import tempfile
import time
from pathlib import Path

from ..models import BookMeta, Chapter, DownloadOptions, DownloadResult
from ..utils import detect_site_from_url, emit_progress, sanitize_filename
from .base import BackendAdapter


class NodeNovelAdapter(BackendAdapter):
    name = "node"

    def supports(self, options: DownloadOptions) -> bool:
        site = options.site if options.site != "auto" else detect_site_from_url(options.url)
        return site in {"syosetu", "novel18", "kakuyomu"}

    def fetch(self, options: DownloadOptions) -> DownloadResult:
        site = options.site if options.site != "auto" else detect_site_from_url(options.url)
        site_id = "kakuyomu" if site == "kakuyomu" else "syosetu"

        temp_dir = Path(tempfile.mkdtemp(prefix="_node_job_", dir=options.output_dir))
        cookie_temp_file: Path | None = None
        cookie_temp_dir: Path | None = None

        try:
            command = _build_node_command(
                site_id=site_id,
                output_dir=temp_dir,
                url=options.url,
            )

            # Keep debug output small unless explicitly needed
            command.insert(-1, "--debug=false")

            if options.paid_policy == "metadata":
                command.insert(-1, "--disableDownload")

            cookie_file_arg = _resolve_cookie_file(options)
            if cookie_file_arg:
                if cookie_file_arg.parent.name.startswith("_cookie_"):
                    cookie_temp_file = cookie_file_arg
                    cookie_temp_dir = cookie_file_arg.parent
                command.insert(-1, "--cookiesFile")
                command.insert(-1, str(cookie_file_arg))

            expected_total = 0
            last_emitted: tuple[int, int] | None = None
            probe_interval = 0.5
            emit_progress("download", 0, 0, "chapter")
            last_emitted = (0, 0)

            process = subprocess.Popen(
                command,
                cwd=str(Path(__file__).resolve().parents[2]),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="ignore",
            )

            deadline = time.monotonic() + max(1, options.timeout)
            timed_out = False

            while True:
                rc = process.poll()

                metadata_path = _pick_live_metadata_json(temp_dir)
                if metadata_path:
                    _, parsed_total = _parse_node_metadata(metadata_path)
                    if parsed_total > 0:
                        expected_total = parsed_total

                current_count = _count_downloaded_txt(temp_dir)
                current_evt = (current_count, expected_total)
                if current_evt != last_emitted:
                    emit_progress("download", current_count, expected_total, "chapter")
                    last_emitted = current_evt

                if rc is not None:
                    break

                if time.monotonic() > deadline:
                    timed_out = True
                    process.kill()
                    break

                time.sleep(probe_interval)

            stdout_text, stderr_text = process.communicate()
            if timed_out:
                raise RuntimeError(
                    "Node backend failed: timeout after "
                    f"{options.timeout}s. "
                    + (stderr_text.strip() or stdout_text.strip() or "unknown error")
                )

            if process.returncode != 0:
                raise RuntimeError(
                    "Node backend failed: "
                    + (stderr_text.strip() or stdout_text.strip() or "unknown error")
                )

            root = _find_node_work_root(temp_dir)
            metadata_json = _pick_metadata_json(root)
            meta_title, expected_count = _parse_node_metadata(metadata_json)

            txt_files = sorted(root.rglob("*.txt"))
            chapters = _parse_node_txt_chapters(root, txt_files)

            final_total = expected_count if expected_count > 0 else expected_total
            final_current = len(chapters)
            if (final_current, final_total) != last_emitted:
                emit_progress("download", final_current, final_total, "chapter")

            if options.paid_policy != "metadata" and not chapters:
                raise RuntimeError("Node backend produced no chapter text")

            skipped = 0
            skipped_reasons: list[str] = []
            if expected_count > 0 and len(chapters) < expected_count:
                skipped = expected_count - len(chapters)
                skipped_reasons.append(
                    f"Downloaded {len(chapters)}/{expected_count} chapters"
                )
                if options.paid_policy == "fail":
                    raise RuntimeError(
                        f"Missing chapters: downloaded {len(chapters)} expected {expected_count}"
                    )

            meta = BookMeta(
                title=meta_title or sanitize_filename(root.name, "book"),
                source_url=options.url,
                site=site,
                expected_chapter_count=expected_count,
            )

            return DownloadResult(
                backend=self.name,
                site=site,
                meta=meta,
                chapters=chapters,
                skipped_chapters=skipped,
                skipped_reasons=skipped_reasons,
                raw_metadata_path=str(metadata_json) if metadata_json else "",
            )
        finally:
            if cookie_temp_file and cookie_temp_file.exists():
                cookie_temp_file.unlink(missing_ok=True)
            if cookie_temp_dir and cookie_temp_dir.exists():
                shutil.rmtree(cookie_temp_dir, ignore_errors=True)
            if temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)
            if options.rate_limit > 0:
                time.sleep(options.rate_limit)


def _build_node_command(*, site_id: str, output_dir: Path, url: str) -> list[str]:
    npx = cmdshutil.which("npx")
    if npx:
        return [
            npx,
            "novel-downloader-cli",
            "--siteID",
            site_id,
            "--outputDir",
            str(output_dir),
            "--disableCheckExists",
            url,
        ]

    npm = cmdshutil.which("npm")
    if npm:
        return [
            npm,
            "exec",
            "--yes",
            "novel-downloader-cli",
            "--",
            "--siteID",
            site_id,
            "--outputDir",
            str(output_dir),
            "--disableCheckExists",
            url,
        ]

    raise FileNotFoundError("Neither npx nor npm was found in PATH")


def _resolve_cookie_file(options: DownloadOptions) -> Path | None:
    if options.cookie_file:
        path = Path(options.cookie_file)
        if not path.exists():
            raise FileNotFoundError(f"Cookie file not found: {path}")
        return path

    if not options.cookie.strip():
        return None

    # Write raw cookie header into a temporary Netscape-like file that the node tool accepts.
    # Format fallback: domain\tflag\tpath\tsecure\texpire\tname\tvalue
    host = ""
    try:
        from urllib.parse import urlparse

        host = (urlparse(options.url).hostname or "")
    except Exception:
        host = ""

    temp_dir = Path(tempfile.mkdtemp(prefix="_cookie_", dir=options.output_dir))
    cookie_path = temp_dir / "cookies.txt"

    lines = ["# Netscape HTTP Cookie File"]
    for pair in options.cookie.split(";"):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        key, value = pair.split("=", 1)
        domain = host or "localhost"
        lines.append(f"{domain}\tTRUE\t/\tFALSE\t2147483647\t{key.strip()}\t{value.strip()}")

    cookie_path.write_text("\n".join(lines), encoding="utf-8")
    return cookie_path


def _find_node_work_root(temp_dir: Path) -> Path:
    candidates = [p for p in temp_dir.iterdir() if p.is_dir()]
    if not candidates:
        raise RuntimeError("Node backend did not produce output folders")

    # Most outputs are temp_dir/<site>/<work>
    for first in candidates:
        second_level = [p for p in first.iterdir() if p.is_dir()]
        if second_level:
            return second_level[0]

    return candidates[0]


def _pick_metadata_json(root: Path) -> Path | None:
    json_files = sorted(root.glob("*.json"), key=lambda p: p.stat().st_size, reverse=True)
    if json_files:
        return json_files[0]
    return None


def _pick_live_metadata_json(temp_dir: Path) -> Path | None:
    candidates = sorted(
        [p for p in temp_dir.rglob("*.json") if p.is_file()],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for path in candidates:
        if path.name.lower() == "manifest.json":
            continue
        return path
    return None


def _count_downloaded_txt(temp_dir: Path) -> int:
    return sum(1 for p in temp_dir.rglob("*.txt") if p.is_file() and "README.txt" not in p.name)


def _parse_node_metadata(path: Path | None) -> tuple[str, int]:
    if not path or not path.exists():
        return "", 0

    try:
        raw = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return "", 0

    title = str(raw.get("novel_title") or "").strip()
    chapter_length = raw.get("chapter_length")
    if isinstance(chapter_length, int):
        expected = chapter_length
    else:
        expected = 0

    return title, expected


def _parse_node_txt_chapters(root: Path, txt_files: list[Path]) -> list[Chapter]:
    chapters: list[Chapter] = []
    idx = 1

    for txt in txt_files:
        text = txt.read_text(encoding="utf-8", errors="ignore").strip()
        if not text:
            continue

        rel = txt.relative_to(root)
        rel_parts = list(rel.parts)
        volume = " / ".join(rel_parts[:-1]) if len(rel_parts) > 1 else ""

        lines = text.splitlines()
        chapter_title = lines[0].strip() if lines else txt.stem
        content = "\n".join(lines[1:]).strip() if len(lines) > 1 else text

        chapters.append(
            Chapter(
                index=idx,
                title=chapter_title or txt.stem,
                content=content,
                volume=volume,
                source_path=str(rel),
            )
        )
        idx += 1

    return chapters
