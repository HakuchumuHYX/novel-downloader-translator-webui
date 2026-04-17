from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from pathlib import Path

from syosetu import Syosetu

from ..models import BookMeta, Chapter, DownloadOptions, DownloadResult
from ..utils import detect_site_from_url, emit_progress
from .base import BackendAdapter
from .native_common import parse_native_volume_txt


class NativeFallbackAdapter(BackendAdapter):
    name = "native"

    def supports(self, options: DownloadOptions) -> bool:
        site = options.site if options.site != "auto" else detect_site_from_url(options.url)
        if site not in {"syosetu", "novel18"}:
            return False
        if options.paid_policy != "skip":
            return False
        if site == "novel18" and (options.cookie.strip() or options.cookie_file.strip()):
            return False
        return True

    def fetch(self, options: DownloadOptions) -> DownloadResult:
        site = options.site if options.site != "auto" else detect_site_from_url(options.url)
        if site not in {"syosetu", "novel18"}:
            raise RuntimeError("Native fallback currently supports syosetu/novel18 only")
        if options.paid_policy != "skip":
            raise RuntimeError("Native fallback does not support paid_policy other than skip")
        if site == "novel18" and (options.cookie.strip() or options.cookie_file.strip()):
            raise RuntimeError("Native fallback does not support authenticated novel18 cookies")

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

            chapters: list[Chapter] = []
            chapter_index = 1
            for txt in _iter_volume_txt_files_in_order(book_dir):
                volume_name = txt.stem
                sections = parse_native_volume_txt(txt)
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
        part_titles = await syosetu.get_novel_part_titles()
        await syosetu.async_download(str(temp_dir))
    finally:
        await syosetu.async_close()

    dirs = [p for p in temp_dir.iterdir() if p.is_dir()]
    if not dirs:
        raise RuntimeError("Native downloader produced no output directory")

    book_dir = dirs[0]
    if part_titles:
        order_file = book_dir / "_parts_order.json"
        order_file.write_text(
            json.dumps(part_titles, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    return book_dir


def _iter_volume_txt_files_in_order(book_dir: Path) -> list[Path]:
    txt_files = [p for p in book_dir.glob("*.txt")]

    order_file = book_dir / "_parts_order.json"
    if not order_file.exists():
        return sorted(txt_files)

    try:
        part_titles = json.loads(order_file.read_text(encoding="utf-8"))
    except Exception:
        return sorted(txt_files)

    if not isinstance(part_titles, list) or not part_titles:
        return sorted(txt_files)

    order_map = {
        str(title): idx for idx, title in enumerate(part_titles) if isinstance(title, str)
    }
    if not order_map:
        return sorted(txt_files)

    def _sort_key(path: Path) -> tuple[int, int, str]:
        idx = order_map.get(path.stem)
        if idx is None:
            return (1, 10**9, path.name)
        return (0, idx, path.name)

    return sorted(txt_files, key=_sort_key)
