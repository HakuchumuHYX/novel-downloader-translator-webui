"""把以「● 标题」分章的 txt 生成 epub。下载器的 epub 输出也用这里。"""
from __future__ import annotations

import html
import re
from pathlib import Path

from ebooklib import epub


def language_code(language: str) -> str:
    # 阅读器按 dc:language 选字体，中文书标成 ja 会用日文字形显示汉字
    if "繁" in language:
        return "zh-TW"
    if "中" in language or language.lower().startswith("zh"):
        return "zh-CN"
    return language


def split_chapters(text: str, default_title: str) -> list[tuple[str, str]]:
    chapters: list[tuple[str, list[str]]] = []
    for line in text.splitlines():
        if line.startswith("● "):
            chapters.append((line[2:].strip() or default_title, []))
        else:
            if not chapters:
                chapters.append((default_title, []))
            chapters[-1][1].append(line)
    return [(title, "\n".join(body).strip()) for title, body in chapters]


def _body_html(body: str) -> str:
    paragraphs = [part for part in re.split(r"\n\s*\n", body) if part.strip()]
    return "".join("<p>" + "<br/>".join(html.escape(line) for line in part.splitlines()) + "</p>" for part in paragraphs)


def write_epub(text: str, path: Path, *, title: str, language: str) -> None:
    book = epub.EpubBook()
    book.set_identifier(f"translator-webui-{title}")
    book.set_title(title)
    book.set_language(language)

    items = []
    for index, (chapter_title, body) in enumerate(split_chapters(text, title), start=1):
        item = epub.EpubHtml(title=chapter_title, file_name=f"chap_{index:04d}.xhtml", lang=language)
        item.content = f"<h1>{html.escape(chapter_title)}</h1>{_body_html(body)}"
        book.add_item(item)
        items.append(item)

    book.toc = items
    book.spine = ["nav", *items]
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    epub.write_epub(str(path), book)
