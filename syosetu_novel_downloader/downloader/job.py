from __future__ import annotations

import json
import shutil
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from .adapters import NativeFallbackAdapter, NodeNovelAdapter
from .models import DownloadOptions, DownloadResult, RunManifest
from .utils import detect_site_from_url, sanitize_filename, write_manifest


class DownloadJob:
    def __init__(self, options: DownloadOptions):
        self.options = options
        self.options.output_dir = Path(self.options.output_dir)
        self.options.output_dir.mkdir(parents=True, exist_ok=True)

    def run(self) -> tuple[DownloadResult, Path]:
        started = datetime.now()
        errors: list[str] = []

        site = self.options.site
        if site == "auto":
            site = detect_site_from_url(self.options.url)

        adapters = self._build_adapter_chain(site)
        result: DownloadResult | None = None

        for adapter in adapters:
            if not adapter.supports(self.options):
                continue

            last_error = None
            for attempt in range(1, self.options.retries + 2):
                try:
                    result = adapter.fetch(self.options)
                    break
                except Exception as exc:  # noqa: BLE001
                    last_error = f"{adapter.name} attempt {attempt} failed: {exc}"
            if result:
                break
            if last_error:
                errors.append(last_error)

        if result is None:
            finished = datetime.now()
            manifest = RunManifest.build(
                status="failed",
                backend_used="none",
                site=site,
                source_url=self.options.url,
                output_dir=self.options.output_dir,
                title="",
                chapter_count=0,
                expected_chapter_count=0,
                skipped_chapters=0,
                skipped_reasons=[],
                paid_policy=self.options.paid_policy,
                errors=errors,
                started=started,
                finished=finished,
            )
            write_manifest(self.options.output_dir / "manifest.json", manifest.__dict__)
            raise RuntimeError("All backends failed: " + " | ".join(errors))

        output_book_dir = self._write_normalized_outputs(result)

        finished = datetime.now()
        manifest = RunManifest.build(
            status="ok",
            backend_used=result.backend,
            site=result.site,
            source_url=result.meta.source_url,
            output_dir=output_book_dir,
            title=result.meta.title,
            chapter_count=len(result.chapters),
            expected_chapter_count=result.meta.expected_chapter_count,
            skipped_chapters=result.skipped_chapters,
            skipped_reasons=result.skipped_reasons,
            paid_policy=self.options.paid_policy,
            errors=errors,
            started=started,
            finished=finished,
        )
        write_manifest(output_book_dir / "manifest.json", manifest.__dict__)

        return result, output_book_dir

    def _build_adapter_chain(self, site: str):
        if self.options.backend == "node":
            return [NodeNovelAdapter()]
        if self.options.backend == "native":
            return [NativeFallbackAdapter()]

        # auto: prefer node, fallback native
        chain = [NodeNovelAdapter(), NativeFallbackAdapter()]

        # native currently supports syosetu and novel18 only
        if site not in {"syosetu", "novel18"}:
            return [NodeNovelAdapter()]

        return chain

    def _write_normalized_outputs(self, result: DownloadResult) -> Path:
        book_title = sanitize_filename(result.meta.title or "book")
        book_dir = self.options.output_dir / book_title

        if book_dir.exists():
            shutil.rmtree(book_dir)
        book_dir.mkdir(parents=True, exist_ok=True)

        if not result.chapters:
            (book_dir / "README.txt").write_text(
                "No chapter text downloaded in current mode.",
                encoding="utf-8",
            )
            return book_dir

        grouped: dict[str, list] = defaultdict(list)
        for chapter in result.chapters:
            key = chapter.volume or book_title
            grouped[key].append(chapter)

        for volume, items in grouped.items():
            safe = sanitize_filename(volume, default=book_title)
            out = book_dir / f"{safe}.txt"
            lines: list[str] = []
            for chapter in items:
                if self.options.record_chapter_number:
                    lines.append(f"● {chapter.title} [総第{chapter.index}話]")
                else:
                    lines.append(f"● {chapter.title}")
                lines.append(chapter.content)
                lines.append("")
            out.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

        if result.raw_metadata_path:
            src = Path(result.raw_metadata_path)
            if src.exists():
                dst = book_dir / "source_metadata.json"
                try:
                    # normalize pretty json if possible
                    raw = json.loads(src.read_text(encoding="utf-8", errors="ignore"))
                    dst.write_text(
                        json.dumps(raw, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                except Exception:
                    shutil.copy2(src, dst)

        return book_dir
