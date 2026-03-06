from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from html import unescape
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup
from ebooklib import ITEM_DOCUMENT, epub


@dataclass
class PreviewResult:
    title: str
    page: int
    total_pages: int
    lines: list[str]


def _paginate_lines(lines: list[str], page: int, per_page: int) -> PreviewResult:
    if per_page <= 0:
        per_page = 120
    total_pages = max(1, (len(lines) + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    end = start + per_page
    return PreviewResult(title="", page=page, total_pages=total_pages, lines=lines[start:end])


def _cache_key(path: Path) -> tuple[str, int, int]:
    stat = path.stat()
    return (str(path.resolve()), int(stat.st_mtime_ns), int(stat.st_size))


@lru_cache(maxsize=32)
def _load_text_lines_cached(path_str: str, _mtime_ns: int, _size: int) -> tuple[str, ...]:
    content = Path(path_str).read_text(encoding="utf-8", errors="ignore")
    return tuple(content.splitlines())


def preview_text_file(path: Path, page: int = 1, per_page: int = 120) -> PreviewResult:
    cache_key = _cache_key(path)
    lines = list(_load_text_lines_cached(*cache_key))
    result = _paginate_lines(lines, page, per_page)
    result.title = path.name
    return result


def _doc_to_text(item: Any) -> str:
    soup = BeautifulSoup(item.get_content(), "html.parser")
    text = soup.get_text("\n")
    text = unescape(text)
    clean_lines = [line.strip() for line in text.splitlines() if line.strip()]
    return "\n".join(clean_lines)


@lru_cache(maxsize=8)
def _load_epub_chapters_cached(path_str: str, _mtime_ns: int, _size: int) -> tuple[tuple[str, ...], ...]:
    book = epub.read_epub(path_str)
    docs = [item for item in book.get_items() if item.get_type() == ITEM_DOCUMENT]

    chapters: list[tuple[str, ...]] = []
    for item in docs:
        chapter_text = _doc_to_text(item)
        chapter_lines = tuple(chapter_text.splitlines()) if chapter_text else ("(empty chapter)",)
        chapters.append(chapter_lines)
    return tuple(chapters)


def preview_epub_file(path: Path, page: int = 1) -> PreviewResult:
    cache_key = _cache_key(path)
    chapters = _load_epub_chapters_cached(*cache_key)

    if not chapters:
        return PreviewResult(title=path.name, page=1, total_pages=1, lines=["No readable chapter found."])

    total_pages = len(chapters)
    page = max(1, min(page, total_pages))
    chapter_lines = list(chapters[page - 1])

    return PreviewResult(
        title=f"{path.name} - chapter {page}",
        page=page,
        total_pages=total_pages,
        lines=chapter_lines,
    )
