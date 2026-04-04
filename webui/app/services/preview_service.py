from __future__ import annotations

from dataclasses import dataclass
from html import unescape
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup
from ebooklib import ITEM_DOCUMENT, epub

try:
    import fitz
except Exception:
    fitz = None


@dataclass
class PreviewResult:
    title: str
    page: int
    total_pages: int
    lines: list[str]


def _sanitize_lines(lines: list[str]) -> list[str]:
    return [line.rstrip("\n") for line in lines]


def preview_text_file(path: Path, page: int = 1, per_page: int = 120) -> PreviewResult:
    if per_page <= 0:
        per_page = 120

    total_lines = 0
    with path.open("r", encoding="utf-8", errors="ignore") as handle:
        for _ in handle:
            total_lines += 1

    total_pages = max(1, (total_lines + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    end = start + per_page

    page_lines: list[str] = []
    with path.open("r", encoding="utf-8", errors="ignore") as handle:
        for index, raw_line in enumerate(handle):
            if index < start:
                continue
            if index >= end:
                break
            page_lines.append(raw_line.rstrip("\n"))

    return PreviewResult(title=path.name, page=page, total_pages=total_pages, lines=_sanitize_lines(page_lines))


def _doc_to_text(item: Any) -> str:
    soup = BeautifulSoup(item.get_content(), "html.parser")
    text = soup.get_text("\n")
    text = unescape(text)
    clean_lines = [line.strip() for line in text.splitlines() if line.strip()]
    return "\n".join(clean_lines)


def preview_epub_file(path: Path, page: int = 1) -> PreviewResult:
    book = epub.read_epub(str(path))
    docs = [item for item in book.get_items() if item.get_type() == ITEM_DOCUMENT]

    if not docs:
        return PreviewResult(title=path.name, page=1, total_pages=1, lines=["No readable chapter found."])

    total_pages = len(docs)
    page = max(1, min(page, total_pages))
    chapter_text = _doc_to_text(docs[page - 1])
    chapter_lines = chapter_text.splitlines() if chapter_text else ["(empty chapter)"]

    return PreviewResult(
        title=f"{path.name} - chapter {page}",
        page=page,
        total_pages=total_pages,
        lines=_sanitize_lines(chapter_lines),
    )


def preview_pdf_file(path: Path, page: int = 1) -> PreviewResult:
    if fitz is None:
        raise RuntimeError("PyMuPDF is required to preview PDF files")

    with fitz.open(path) as document:
        total_pages = max(1, document.page_count)
        page = max(1, min(page, total_pages))
        text = document.load_page(page - 1).get_text("text")

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        lines = ["(empty page)"]

    return PreviewResult(
        title=f"{path.name} - page {page}",
        page=page,
        total_pages=total_pages,
        lines=_sanitize_lines(lines),
    )
