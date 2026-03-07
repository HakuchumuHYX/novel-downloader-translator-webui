from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path

from syosetu import Syosetu

from ..models import BookMeta, Chapter, DownloadOptions, DownloadResult
from ..utils import detect_site_from_url, emit_progress
from .base import BackendAdapter


class NativeFallbackAdapter(BackendAdapter):
    name = "native"

    def supports(self, options: DownloadOptions) -> bool:
        site = options.site if options.site != "auto" else detect_site_from_url(options.url)
        return site in {"syosetu", "novel18"}

    def fetch(self, options: DownloadOptions) -> DownloadResult:
        site = options.site if options.site != "auto" else detect_site_from_url(options.url)
        if site not in {"syosetu", "novel18"}:
            raise RuntimeError("Native fallback currently supports syosetu/novel18 only")

        temp_dir = Path(tempfile.mkdtemp(prefix="_native_job_", dir=options.output_dir))

        try:
            novel_id = options.url.rstrip("/").split("/")[-1]
            if site == "novel18":
                book_dir = asyncio.run(
                    _run_native_download(
                        novel_id,
                        options.proxy,
                        temp_dir,
                        base_url="https://novel18.syosetu.com",
                        over18=True,
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
                    )
                )

            chapters = []
            chapter_index = 1
            for txt in sorted(book_dir.glob("*.txt")):
                volume_name = txt.stem
                sections = _parse_native_volume_txt(txt)
                for title, content in sections:
                    chapters.append(
                        Chapter(
                            index=chapter_index,
                            title=title,
                            content=content,
                            volume=volume_name,
                            source_path=str(txt.relative_to(book_dir)),
                        )
                    )
                    chapter_index += 1

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
            )
        finally:
            if temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)


async def _run_native_download(
    novel_id: str,
    proxy: str,
    temp_dir: Path,
    *,
    base_url: str,
    over18: bool,
) -> Path:
    def _progress_cb(current: int, total: int) -> None:
        emit_progress("download", current, total, "chapter")

    syosetu = Syosetu(
        novel_id,
        proxy,
        base_url=base_url,
        over18=over18,
        progress_callback=_progress_cb,
    )
    await syosetu.async_init()
    try:
        await syosetu.async_download(str(temp_dir))
    finally:
        await syosetu.async_close()

    dirs = [p for p in temp_dir.iterdir() if p.is_dir()]
    if not dirs:
        raise RuntimeError("Native downloader produced no output directory")
    return dirs[0]


def _parse_native_volume_txt(path: Path) -> list[tuple[str, str]]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    lines = text.splitlines()

    parsed: list[tuple[str, str]] = []
    current_title = ""
    buffer: list[str] = []

    for line in lines:
        if line.startswith("● "):
            if current_title:
                parsed.append((current_title, "\n".join(buffer).strip()))
            current_title = line[2:].strip()
            buffer = []
        else:
            buffer.append(line)

    if current_title:
        parsed.append((current_title, "\n".join(buffer).strip()))

    return parsed
