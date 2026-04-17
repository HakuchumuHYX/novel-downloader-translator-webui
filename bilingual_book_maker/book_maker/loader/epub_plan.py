from __future__ import annotations

from collections.abc import Callable

from ebooklib import ITEM_DOCUMENT

from .epub_support import count_translatable_nodes, should_translate_item


def total_paragraph_count(
    all_items,
    trans_taglist,
    *,
    allow_navigable_strings: bool,
    only_filelist: str,
    exclude_filelist: str,
) -> int:
    total = 0
    for item in all_items:
        if item.get_type() != ITEM_DOCUMENT:
            continue
        total += count_translatable_nodes(
            item,
            trans_taglist,
            allow_navigable_strings=allow_navigable_strings,
            only_filelist=only_filelist,
            exclude_filelist=exclude_filelist,
        )
    return total


def build_chapter_plan(
    document_items,
    trans_taglist,
    *,
    allow_navigable_strings: bool,
    only_filelist: str,
    exclude_filelist: str,
    is_saved_index: Callable[[int], bool],
) -> tuple[list, list[int], list[bool]]:
    chapter_targets = [
        item for item in document_items if should_translate_item(item, only_filelist, exclude_filelist)
    ]

    chapter_offsets: list[int] = []
    chapter_counts: list[int] = []
    running_offset = 0
    for item in chapter_targets:
        chapter_offsets.append(running_offset)
        node_count = count_translatable_nodes(
            item,
            trans_taglist,
            allow_navigable_strings=allow_navigable_strings,
            only_filelist=only_filelist,
            exclude_filelist=exclude_filelist,
        )
        chapter_counts.append(node_count)
        running_offset += node_count

    completed_flags: list[bool] = []
    for idx, node_count in enumerate(chapter_counts):
        if node_count <= 0:
            completed_flags.append(True)
            continue
        start = chapter_offsets[idx]
        completed_flags.append(all(is_saved_index(start + pos) for pos in range(node_count)))

    return chapter_targets, chapter_offsets, completed_flags
