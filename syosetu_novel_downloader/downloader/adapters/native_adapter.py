from __future__ import annotations

import asyncio
from pathlib import Path

from syosetu import Syosetu

from ..chapter_manifest import chapters_from_manifest
from ..models import BookMeta, DownloadOptions, DownloadResult
from ..utils import detect_site_from_url, emit_progress, extract_syosetu_novel_id
from .base import BackendAdapter


class NativeFallbackAdapter(BackendAdapter):
    name = "native"

    def supports(self, options: DownloadOptions) -> bool:
        site = options.site if options.site != "auto" else detect_site_from_url(options.url)
        if site not in {"syosetu", "novel18"}:
            return False
        if options.paid_policy != "skip":
            return False
        return True

    def work_dir(self, options: DownloadOptions) -> Path:
        site = options.site if options.site != "auto" else detect_site_from_url(options.url)
        novel_id = extract_syosetu_novel_id(options.url)
        return options.output_dir / ".work" / f"{site}_{novel_id}"

    def fetch(self, options: DownloadOptions) -> DownloadResult:
        site = options.site if options.site != "auto" else detect_site_from_url(options.url)
        if site not in {"syosetu", "novel18"}:
            raise RuntimeError("Native fallback currently supports syosetu/novel18 only")
        if options.paid_policy != "skip":
            raise RuntimeError("Native fallback does not support paid_policy other than skip")

        temp_dir = self.work_dir(options)
        temp_dir.mkdir(parents=True, exist_ok=True)
        novel_id = extract_syosetu_novel_id(options.url)

        try:
            if site == "novel18":
                book_dir = asyncio.run(
                    _run_native_download(
                        novel_id,
                        options.proxy,
                        temp_dir,
                        base_url="https://novel18.syosetu.com",
                        over18=True,
                        cookie_header=options.cookie,
                    )
                )
            else:
                book_dir = asyncio.run(
                    _run_native_download(
                        novel_id,
                        options.proxy,
                        temp_dir,
                        base_url="https://ncode.syosetu.com",
                        over18=False,
                        cookie_header=options.cookie,
                    )
                )

            chapters = chapters_from_manifest(book_dir)

            if not chapters:
                raise RuntimeError("Native downloader produced no chapter content")

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


async def _run_native_download(
    novel_id: str,
    proxy: str,
    temp_dir: Path,
    *,
    base_url: str,
    over18: bool,
    cookie_header: str,
) -> Path:
    def _progress_cb(current: int, total: int) -> None:
        emit_progress("download", current, total, "chapter")

    syosetu = Syosetu(
        novel_id,
        proxy,
        base_url=base_url,
        over18=over18,
        cookie_header=cookie_header,
        progress_callback=_progress_cb,
    )
    await syosetu.async_init()
    try:
        book_dir = Path(await syosetu.async_download(str(temp_dir)))
    finally:
        await syosetu.async_close()

    return book_dir
