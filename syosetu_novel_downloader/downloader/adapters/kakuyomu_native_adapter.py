from __future__ import annotations

import asyncio
from pathlib import Path
from urllib.parse import urlparse

from kakuyomu import Kakuyomu

from ..chapter_manifest import chapters_from_manifest
from ..models import BookMeta, DownloadOptions, DownloadResult
from ..utils import detect_site_from_url, emit_progress, sanitize_filename
from .base import BackendAdapter


class NativeKakuyomuAdapter(BackendAdapter):
    name = "native_kakuyomu"

    def supports(self, options: DownloadOptions) -> bool:
        site = options.site if options.site != "auto" else detect_site_from_url(options.url)
        return site == "kakuyomu"

    def work_dir(self, options: DownloadOptions) -> Path:
        return _kakuyomu_work_dir(options.output_dir, options.url)

    def fetch(self, options: DownloadOptions) -> DownloadResult:
        site = options.site if options.site != "auto" else detect_site_from_url(options.url)
        if site != "kakuyomu":
            raise RuntimeError("Native kakuyomu adapter supports kakuyomu only")

        temp_dir = self.work_dir(options)
        temp_dir.mkdir(parents=True, exist_ok=True)

        try:
            book_dir = asyncio.run(
                _run_native_kakuyomu_download(
                    options.url,
                    options.proxy,
                    options.cookie,
                    temp_dir,
                )
            )

            chapters = chapters_from_manifest(book_dir, strip_index_suffix=True)

            if not chapters:
                raise RuntimeError("Native kakuyomu downloader produced no chapter content")

            meta = BookMeta(
                title=book_dir.name,
                source_url=options.url,
                site=site,
                expected_chapter_count=len(chapters),
            )

            return DownloadResult(
                backend=self.name,
                site=site,
                meta=meta,
                chapters=chapters,
                work_dir=str(temp_dir),
            )
        finally:
            pass


def _kakuyomu_work_dir(output_dir: Path, url: str) -> Path:
    parts = [part for part in urlparse(str(url or "")).path.split("/") if part]
    work_id = ""
    if "works" in parts:
        index = parts.index("works")
        if index + 1 < len(parts):
            work_id = parts[index + 1]
    if not work_id and parts:
        work_id = parts[-1]
    return output_dir / ".work" / f"native_kakuyomu_{sanitize_filename(work_id, default='kakuyomu')}"


async def _run_native_kakuyomu_download(
    work_url: str,
    proxy: str,
    cookie: str,
    temp_dir: Path,
) -> Path:
    def _progress_cb(current: int, total: int) -> None:
        emit_progress("download", current, total, "chapter")

    kakuyomu = Kakuyomu(
        work_url,
        proxy=proxy,
        cookie=cookie,
        progress_callback=_progress_cb,
    )
    await kakuyomu.async_init()
    try:
        await kakuyomu.async_download(str(temp_dir))
    finally:
        await kakuyomu.async_close()

    dirs = [p for p in temp_dir.iterdir() if p.is_dir()]
    if not dirs:
        raise RuntimeError("Native kakuyomu downloader produced no output directory")

    return dirs[0]
