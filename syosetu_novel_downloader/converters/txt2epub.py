import html
import json
import os
import re
from typing import Iterable

from ebooklib import epub

from downloader.utils import is_content_txt


def split_marked_chapters(text_content: str) -> list[tuple[str, str]]:
    chapters: list[tuple[str, list[str]]] = []
    current_title = ""
    current_body: list[str] = []

    for line in text_content.splitlines():
        if line.startswith("● "):
            if current_title or current_body:
                chapters.append((current_title or "Untitled", current_body))
            current_title = line[2:].strip() or "Untitled"
            current_body = []
        else:
            current_body.append(line)

    if current_title or current_body:
        chapters.append((current_title or "Untitled", current_body))

    return [(title, "\n".join(body).strip()) for title, body in chapters]


def body_to_html(chapter_body: str) -> str:
    paragraphs = [part for part in re.split(r"\n\s*\n", chapter_body) if part.strip()]
    if not paragraphs:
        return "<p></p>"

    html_parts: list[str] = []
    for paragraph in paragraphs:
        lines = [html.escape(line) for line in paragraph.splitlines()]
        html_parts.append("<p>" + "<br/>".join(lines) + "</p>")
    return "".join(html_parts)


def create_epub_from_txt(file_path, output_folder, language: str = "ja") -> str:
    print(f"convert {file_path}")
    with open(file_path, "r", encoding="utf-8") as file:
        text_content = file.read()

    chapters = split_marked_chapters(text_content)

    book = epub.EpubBook()

    book.set_identifier("id" + str(os.path.basename(file_path)))
    title = os.path.splitext(os.path.basename(file_path))[0]
    book.set_title(title)
    book.set_language(language)

    book.spine = ["nav"]

    for i, (chapter_title, chapter_body) in enumerate(chapters):
        c = epub.EpubHtml(
            title=chapter_title,
            file_name=f"chap_{i + 1}.xhtml",
            lang=language,
        )
        safe_title = html.escape(chapter_title)
        c.content = "<h1>" + safe_title + "</h1>" + body_to_html(chapter_body)

        book.add_item(c)

        book.spine.append(c)

    book.toc = tuple(book.spine[1:])
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    output_path = os.path.join(output_folder, f"{title}.epub")
    epub.write_epub(output_path, book, {})
    return output_path


def merge_chapters_to_txt(chapters: Iterable, output_path: str, record_chapter_number: bool = False) -> str:
    """
    Merge chapters into one txt file in strict original order.

    `chapters` is expected to be an iterable of Chapter-like objects
    with fields: index, title, content.
    """
    chapter_list = list(chapters or [])
    if not chapter_list:
        raise FileNotFoundError("No chapters available to merge")

    chapter_list.sort(key=lambda c: int(getattr(c, "index", 0)))

    lines: list[str] = []
    for chapter in chapter_list:
        index = int(getattr(chapter, "index", 0))
        title = str(getattr(chapter, "title", "") or "").strip()
        content = str(getattr(chapter, "content", "") or "").rstrip()

        if record_chapter_number:
            lines.append(f"● {title} [総第{index}話]")
        else:
            lines.append(f"● {title}")
        lines.append(content)
        lines.append("")

    with open(output_path, "w", encoding="utf-8") as merged:
        merged.write("\n".join(lines).rstrip() + "\n")
    return output_path


def merge_txt_files(input_dir, merged_filename="book.txt"):
    all_txt_files = [f for f in os.listdir(input_dir) if is_content_txt(f)]
    if not all_txt_files:
        raise FileNotFoundError(f"No txt files found in {input_dir}")

    # Special case:
    # When there's only ONE txt file and its name is exactly the merged output filename,
    # there is nothing to merge. Just return it.
    if len(all_txt_files) == 1 and all_txt_files[0] == merged_filename:
        return os.path.join(input_dir, merged_filename)

    txt_files = [f for f in all_txt_files if f != merged_filename]
    if not txt_files:
        merged_path = os.path.join(input_dir, merged_filename)
        if os.path.exists(merged_path):
            return merged_path
        raise FileNotFoundError(f"No txt files found in {input_dir}")

    txt_files = _sort_txt_files_for_merge(input_dir, txt_files)

    merged_path = os.path.join(input_dir, merged_filename)
    with open(merged_path, "w", encoding="utf-8") as merged:
        for idx, file in enumerate(txt_files):
            file_path = os.path.join(input_dir, file)
            with open(file_path, "r", encoding="utf-8") as src:
                content = src.read().strip()
            merged.write(content)
            if idx != len(txt_files) - 1:
                merged.write("\n\n")
    return merged_path


def _sort_txt_files_for_merge(input_dir: str, txt_files: list[str]) -> list[str]:
    order_file = os.path.join(input_dir, "_parts_order.json")
    if os.path.exists(order_file):
        try:
            part_titles = json.loads(open(order_file, "r", encoding="utf-8").read())
            if isinstance(part_titles, list) and part_titles:
                order_map = {
                    str(title): idx for idx, title in enumerate(part_titles) if isinstance(title, str)
                }

                def _part_sort_key(filename: str) -> tuple[int, int, str]:
                    stem = os.path.splitext(filename)[0]
                    idx = order_map.get(stem)
                    if idx is None:
                        return (1, 10**9, filename)
                    return (0, idx, filename)

                return sorted(txt_files, key=_part_sort_key)
        except Exception:
            pass

    return sorted(txt_files, key=_natural_filename_key)


def _natural_filename_key(filename: str) -> tuple[object, ...]:
    parts: list[object] = []
    for chunk in re.split(r"(\d+)", filename.lower()):
        if not chunk:
            continue
        parts.append(int(chunk) if chunk.isdigit() else chunk)
    return tuple(parts)


def convert_directory_txt_to_epub(*args):
    dir = os.path.join(*args)
    for file in os.listdir(dir):
        if is_content_txt(file):
            create_epub_from_txt(os.path.join(dir, file), dir)


def convert_single_txt_to_epub(file_path):
    output_folder = os.path.dirname(file_path)
    create_epub_from_txt(file_path, output_folder)
