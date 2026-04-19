from __future__ import annotations

import string

from bs4 import BeautifulSoup as bs
from bs4 import Tag
from bs4.element import NavigableString
from ebooklib import epub

from .helper import is_text_link


def is_special_text(text: str) -> bool:
    return (
        text.isdigit()
        or text.isspace()
        or is_text_link(text)
        or all(char in string.punctuation for char in text)
    )


def fix_toc_uids(toc, counter=None):
    if counter is None:
        counter = [0]

    fixed_toc = []
    for item in toc:
        if isinstance(item, tuple):
            section, sub_items = item
            if hasattr(section, "uid") and section.uid is None:
                section.uid = f"navpoint-{counter[0]}"
                counter[0] += 1
            fixed_toc.append((section, fix_toc_uids(sub_items, counter)))
        elif hasattr(item, "uid"):
            if item.uid is None:
                item.uid = f"navpoint-{counter[0]}"
                counter[0] += 1
            fixed_toc.append(item)
        else:
            fixed_toc.append(item)

    return fixed_toc


def make_new_book(book):
    new_book = epub.EpubBook()
    allowed_ns = set(epub.NAMESPACES.keys()) | set(epub.NAMESPACES.values())

    for namespace, metas in book.metadata.items():
        if namespace not in allowed_ns:
            continue

        if isinstance(metas, dict):
            entries = (
                (name, value, others)
                for name, values in metas.items()
                for value, others in ((item if isinstance(item, tuple) else (item, None)) for item in values)
            )
        else:
            entries = metas

        for entry in entries:
            if not entry or not isinstance(entry, tuple):
                continue
            if len(entry) == 3:
                name, value, others = entry
            elif len(entry) == 2:
                name, value = entry
                others = None
            else:
                continue

            if others:
                new_book.add_metadata(namespace, name, value, others)
            else:
                new_book.add_metadata(namespace, name, value)

    new_book.spine = book.spine
    new_book.toc = fix_toc_uids(book.toc)
    return new_book


def extract_paragraph(paragraph, exclude_translate_tags: str):
    for tag_name in exclude_translate_tags.split(","):
        if isinstance(paragraph, NavigableString):
            continue
        for tag in paragraph.find_all(tag_name):
            tag.extract()
    return paragraph


def has_nested_child(element, trans_taglist) -> bool:
    if isinstance(element, Tag):
        for child in element.children:
            if child.name in trans_taglist:
                return True
            if has_nested_child(child, trans_taglist):
                return True
    return False


def filter_nested_nodes(p_list, trans_taglist):
    return [paragraph for paragraph in p_list if not has_nested_child(paragraph, trans_taglist)]


def collect_text_nodes(soup) -> list[NavigableString]:
    root = soup.body or soup
    result: list[NavigableString] = []
    for node in root.find_all(string=True):
        parent_name = getattr(getattr(node, "parent", None), "name", "")
        if parent_name in {"head", "meta", "script", "style", "title"}:
            continue
        result.append(node)
    return result


def collect_translatable_nodes(
    soup,
    trans_taglist,
    *,
    allow_navigable_strings: bool,
):
    paragraphs = filter_nested_nodes(soup.findAll(trans_taglist), trans_taglist)
    if allow_navigable_strings:
        paragraphs.extend(collect_text_nodes(soup))
        return paragraphs
    if paragraphs:
        return paragraphs
    return collect_text_nodes(soup)


def should_translate_item(item, only_filelist: str, exclude_filelist: str) -> bool:
    if only_filelist and item.file_name not in only_filelist.split(","):
        return False
    if not only_filelist and item.file_name in exclude_filelist.split(","):
        return False
    return True


def count_translatable_nodes(
    item,
    trans_taglist,
    *,
    allow_navigable_strings: bool,
    only_filelist: str,
    exclude_filelist: str,
) -> int:
    if not should_translate_item(item, only_filelist, exclude_filelist):
        return 0

    soup = bs(item.content, "html.parser")
    paragraphs = collect_translatable_nodes(
        soup,
        trans_taglist,
        allow_navigable_strings=allow_navigable_strings,
    )

    count = 0
    for paragraph in paragraphs:
        text = paragraph.text if hasattr(paragraph, "text") else str(paragraph)
        if not text or is_special_text(text):
            continue
        count += 1
    return count
