from __future__ import annotations

import os
import pickle
from copy import copy

from bs4 import BeautifulSoup as bs
from bs4.element import NavigableString
from ebooklib import ITEM_DOCUMENT, epub

from .epub_support import collect_translatable_nodes, is_special_text, make_new_book


def load_resume_state(path: str) -> list[str]:
    try:
        with open(path, "rb") as file_obj:
            return pickle.load(file_obj)
    except Exception as exc:
        raise Exception("can not load resume file") from exc


def save_resume_state(path: str, entries: list[str]) -> None:
    try:
        with open(path, "wb") as file_obj:
            pickle.dump(entries, file_obj)
    except Exception as exc:
        raise Exception("can not save resume file") from exc


def save_temp_book(loader) -> None:
    origin_book_temp = epub.read_epub(loader.epub_name)
    new_temp_book = make_new_book(origin_book_temp)
    p_to_save_len = len(loader.p_to_save)
    trans_taglist = loader.translate_tags.split(",")
    index = 0

    try:
        for item in origin_book_temp.get_items():
            if item.get_type() == ITEM_DOCUMENT:
                soup = bs(item.content, "html.parser")
                p_list = collect_translatable_nodes(
                    soup,
                    trans_taglist,
                    allow_navigable_strings=loader.allow_navigable_strings,
                )
                for paragraph in p_list:
                    if not paragraph.text or is_special_text(paragraph.text):
                        continue
                    if index < p_to_save_len:
                        new_paragraph = copy(paragraph)
                        if isinstance(paragraph, NavigableString):
                            new_paragraph = loader.p_to_save[index]
                        else:
                            new_paragraph.string = loader.p_to_save[index]
                        loader.helper.insert_trans(
                            paragraph,
                            new_paragraph.string,
                            loader.translation_style,
                            loader.single_translate,
                        )
                        index += 1
                    else:
                        break
                if soup:
                    item.content = soup.encode()
            new_temp_book.add_item(item)

        name, _ = os.path.splitext(loader.epub_name)
        epub.write_epub(f"{name}_翻译_temp.epub", new_temp_book, {})
    except Exception as exc:
        print(exc)
