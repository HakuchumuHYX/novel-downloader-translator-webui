from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path

from kakuyomu import Kakuyomu

from ..models import BookMeta, DownloadOptions, DownloadResult
from ..utils import detect_site_from_url, emit_progress
from .base import BackendAdapter
from .native_common import chapters_from_txt_files


class NativeKakuyomuAdapter(BackendAdapter):
    name = "native_kakuyomu"

    def supports(self, options: DownloadOptions) -> bool:
        site = options.site if options.site != "auto" else detect_site_from_url(options.url)
        return site == "kakuyomu"

    def fetch(self, options: DownloadOptions) -> DownloadResult:
        site = options.site if options.site != "auto" else detect_site_from_url(options.url)
        if site != "kakuyomu":
            raise RuntimeError("Native kakuyomu adapter supports kakuyomu only")

        temp_dir = Path(tempfile.mkdtemp(prefix="_native_kakuyomu_", dir=options.output_dir))

        try:
            book_dir = asyncio.run(
                _run_native_kakuyomu_download(
                    options.url,
                    options.proxy,
                    options.cookie,
                    temp_dir,
                )
            )

            txt_files = sorted(book_dir.glob("*.txt"))
            chapters = chapters_from_txt_files(book_dir, txt_files, strip_index_suffix=True)

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
            )
        finally:
            if temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)


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
