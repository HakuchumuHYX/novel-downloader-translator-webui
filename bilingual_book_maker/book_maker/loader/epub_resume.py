from __future__ import annotations

import json
import os
import pickle
from copy import copy
from pathlib import Path

from bs4 import BeautifulSoup as bs
from bs4.element import NavigableString
from ebooklib import ITEM_DOCUMENT, epub

from .common import load_resume_entries, save_resume_entries
from .epub_support import collect_translatable_nodes, is_special_text, make_new_book, node_text


def load_resume_state(path: str) -> list[str]:
    try:
        return load_resume_entries(path, mode="json")
    except Exception:
        try:
            with open(path, "rb") as file_obj:
                legacy_entries = pickle.load(file_obj)
            if isinstance(legacy_entries, list):
                return [str(item) for item in legacy_entries]
        except Exception as exc:
            raise Exception("can not load resume file") from exc
    raise Exception("can not load resume file")


def load_resume_state_with_metadata(path: str) -> dict:
    target = Path(path)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("p_to_save"), list):
            return data
        if isinstance(data, list):
            return {"version": 1, "p_to_save": [str(item) for item in data]}
    except Exception:
        entries = load_resume_state(path)
        return {"version": 1, "p_to_save": entries}
    return {"version": 1, "p_to_save": []}


def save_resume_state(path: str, entries: list[str], metadata: dict | None = None) -> None:
    save_resume_entries(path, entries, mode="json", atomic=True, metadata=metadata)


def save_temp_book(loader) -> None:
    origin_book_temp = epub.read_epub(loader.epub_name)
    new_temp_book = make_new_book(
        origin_book_temp,
        language=getattr(loader, "metadata_language", getattr(loader, "language", None)),
    )
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
                    if not node_text(paragraph) or is_special_text(node_text(paragraph)):
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
