import os
import re

from ebooklib import epub


def create_epub_from_txt(file_path, output_folder):
    print(f"convert {file_path}")
    with open(file_path, 'r', encoding='utf-8') as file:
        text_content = file.read()

    chapters = re.split(r'● ', text_content)

    book = epub.EpubBook()

    book.set_identifier('id' + str(os.path.basename(file_path)))
    title = os.path.basename(file_path).split(".")[:-1][0]
    book.set_title(title)
    book.set_language('ja')

    book.spine = ['nav']

    for i, chapter in enumerate(chapters):
        if not chapter.strip():
            continue

        chapter_title, _, chapter_body = chapter.partition("\n")
        c = epub.EpubHtml(
            title=chapter_title,
            file_name=f"chap_{i+1}.xhtml",
            lang='ja',
        )
        c.content = '<h1>' + chapter_title + '</h1>' + chapter_body

        book.add_item(c)

        book.spine.append(c)

    book.toc = tuple(book.spine[1:])
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    epub.write_epub(os.path.join(output_folder, f'{title}.epub'), book, {})


def merge_txt_files(input_dir, merged_filename="full_book.txt"):
    txt_files = sorted(
        [
            f for f in os.listdir(input_dir)
            if f.endswith(".txt") and f != merged_filename
        ]
    )
    if not txt_files:
        raise FileNotFoundError(f"No txt files found in {input_dir}")

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


def convert_directory_txt_to_epub(*args):
    dir = os.path.join(*args)
    for file in os.listdir(dir):
        if file.endswith(".txt"):
            create_epub_from_txt(os.path.join(dir, file), dir)


def convert_single_txt_to_epub(file_path):
    output_folder = os.path.dirname(file_path)
    create_epub_from_txt(file_path, output_folder)
