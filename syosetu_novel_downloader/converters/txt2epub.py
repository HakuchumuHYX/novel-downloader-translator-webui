import json
import os
import re
from typing import Iterable

from ebooklib import epub


def create_epub_from_txt(file_path, output_folder):
    print(f"convert {file_path}")
    with open(file_path, "r", encoding="utf-8") as file:
        text_content = file.read()

    chapters = re.split(r"● ", text_content)

    book = epub.EpubBook()

    book.set_identifier("id" + str(os.path.basename(file_path)))
    title = os.path.basename(file_path).split(".")[:-1][0]
    book.set_title(title)
    book.set_language("ja")

    book.spine = ["nav"]

    for i, chapter in enumerate(chapters):
        if not chapter.strip():
            continue

        chapter_title, _, chapter_body = chapter.partition("\n")
        c = epub.EpubHtml(
            title=chapter_title,
            file_name=f"chap_{i + 1}.xhtml",
            lang="ja",
        )
        c.content = "<h1>" + chapter_title + "</h1>" + chapter_body

        book.add_item(c)

        book.spine.append(c)

    book.toc = tuple(book.spine[1:])
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    epub.write_epub(os.path.join(output_folder, f"{title}.epub"), book, {})


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


def merge_txt_files(input_dir, merged_filename="full_book.txt"):
    all_txt_files = [f for f in os.listdir(input_dir) if f.endswith(".txt")]
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

    # Fallback: preserve creation/download order as much as possible.
    return sorted(
        txt_files,
        key=lambda f: (
            os.stat(os.path.join(input_dir, f)).st_mtime_ns,
            f,
        ),
    )


def convert_directory_txt_to_epub(*args):
    dir = os.path.join(*args)
    for file in os.listdir(dir):
        if file.endswith(".txt"):
            create_epub_from_txt(os.path.join(dir, file), dir)


def convert_single_txt_to_epub(file_path):
    output_folder = os.path.dirname(file_path)
    create_epub_from_txt(file_path, output_folder)
