from __future__ import annotations

from copy import copy, deepcopy

from bs4 import BeautifulSoup as bs
from bs4.element import NavigableString

from book_maker.utils import num_tokens_from_text

from .epub_support import collect_translatable_nodes, extract_paragraph, is_special_text
from .helper import not_trans, shorter_result_link


def create_chapter_translator(loader):
    source = loader.translate_model
    try:
        translator = deepcopy(source)
    except Exception:
        translator = loader._create_translator_instance()

    deployment_id = getattr(source, "deployment_id", None)
    if deployment_id and hasattr(translator, "set_deployment_id"):
        translator.set_deployment_id(deployment_id)

    model_list_values = list(getattr(source, "_model_list_values", []) or [])
    if model_list_values and hasattr(translator, "set_model_list"):
        translator.set_model_list(model_list_values)
    elif getattr(source, "model", None):
        translator.model = source.model

    if hasattr(source, "interval") and hasattr(translator, "set_interval"):
        translator.set_interval(source.interval)

    return translator


def translate_with_chapter_context(
    loader,
    translator,
    text,
    chapter_context_list,
    chapter_translated_list,
):
    if not translator.context_flag:
        return translator.translate(text)

    original_context = getattr(translator, "context_list", [])
    original_translated = getattr(translator, "context_translated_list", [])

    try:
        translator.context_list = chapter_context_list.copy()
        translator.context_translated_list = chapter_translated_list.copy()

        result = translator.translate(text)

        chapter_context_list[:] = translator.context_list
        chapter_translated_list[:] = translator.context_translated_list
        return result
    finally:
        translator.context_list = original_context
        translator.context_translated_list = original_translated


def translate_paragraphs_acc_parallel(
    loader,
    p_list,
    send_num,
    translator,
    chapter_context_list,
    chapter_translated_list,
):
    count = 0
    wait_p_list = []

    class ChapterHelper:
        def __init__(self):
            self.translator = translator

        def translate_with_context(self, text):
            return translate_with_chapter_context(
                loader,
                self.translator,
                text,
                chapter_context_list,
                chapter_translated_list,
            )

        def deal_old(self, pending, single_translate):
            if not pending:
                return

            original_context = getattr(self.translator, "context_list", [])
            original_translated = getattr(self.translator, "context_translated_list", [])

            try:
                self.translator.context_list = chapter_context_list.copy()
                self.translator.context_translated_list = chapter_translated_list.copy()
                result_txt_list = self.translator.translate_list(pending)
                chapter_context_list[:] = self.translator.context_list
                chapter_translated_list[:] = self.translator.context_translated_list

                for idx, paragraph in enumerate(pending):
                    if idx < len(result_txt_list):
                        loader.helper.insert_trans(
                            paragraph,
                            shorter_result_link(result_txt_list[idx]),
                            loader.translation_style,
                            single_translate,
                        )
            finally:
                self.translator.context_list = original_context
                self.translator.context_translated_list = original_translated

            pending.clear()

        def deal_new(self, paragraph, pending, single_translate):
            self.deal_old(pending, single_translate)
            translation = self.translate_with_context(paragraph.text)
            loader.helper.insert_trans(
                paragraph,
                translation,
                loader.translation_style,
                single_translate,
            )

    chapter_helper = ChapterHelper()

    for idx, paragraph in enumerate(p_list):
        temp_p = copy(paragraph)

        for excluded in loader.exclude_translate_tags.split(","):
            if isinstance(paragraph, NavigableString):
                continue
            for tag in temp_p.find_all(excluded):
                tag.extract()

        if any([not paragraph.text, is_special_text(temp_p.text), not_trans(temp_p.text)]):
            if idx == len(p_list) - 1:
                chapter_helper.deal_old(wait_p_list, loader.single_translate)
            continue

        length = num_tokens_from_text(temp_p.text)
        if length > send_num:
            chapter_helper.deal_new(paragraph, wait_p_list, loader.single_translate)
            continue

        if idx == len(p_list) - 1:
            if count + length < send_num:
                wait_p_list.append(paragraph)
                chapter_helper.deal_old(wait_p_list, loader.single_translate)
            else:
                chapter_helper.deal_new(paragraph, wait_p_list, loader.single_translate)
            break

        if count + length < send_num:
            count += length
            wait_p_list.append(paragraph)
        else:
            chapter_helper.deal_old(wait_p_list, loader.single_translate)
            wait_p_list.append(paragraph)
            count = length


def process_chapter_parallel(loader, chapter_data):
    item, trans_taglist, _p_to_save_len, chapter_start = chapter_data
    chapter_result = {
        "item": item,
        "processed_content": None,
        "success": False,
        "error": None,
    }

    try:
        thread_translator = create_chapter_translator(loader)
        soup = bs(item.content, "html.parser")
        p_list = collect_translatable_nodes(
            soup,
            trans_taglist,
            allow_navigable_strings=loader.allow_navigable_strings,
        )

        chapter_context_list = []
        chapter_translated_list = []

        if loader.accumulated_num > 1:
            translate_paragraphs_acc_parallel(
                loader,
                p_list,
                loader.accumulated_num,
                thread_translator,
                chapter_context_list,
                chapter_translated_list,
            )
        else:
            local_index = 0
            for paragraph in p_list:
                if not paragraph.text or is_special_text(paragraph.text):
                    continue

                save_index = chapter_start + local_index
                local_index += 1
                new_p = extract_paragraph(copy(paragraph), loader.exclude_translate_tags)

                if loader._is_saved_index(save_index):
                    translated_text = loader._get_saved_value(save_index)
                else:
                    translated_text = translate_with_chapter_context(
                        loader,
                        thread_translator,
                        new_p.text,
                        chapter_context_list,
                        chapter_translated_list,
                    )
                    translated_text = "" if translated_text is None else translated_text
                    with loader._progress_lock:
                        loader._set_saved_value(save_index, translated_text)

                if isinstance(paragraph, NavigableString):
                    translated_node = NavigableString(translated_text)
                    paragraph.insert_after(translated_node)
                    if loader.single_translate:
                        paragraph.extract()
                else:
                    loader.helper.insert_trans(
                        paragraph,
                        translated_text,
                        loader.translation_style,
                        loader.single_translate,
                    )

                with loader._progress_lock:
                    if save_index % 20 == 0:
                        loader._save_progress()

        if soup:
            chapter_result["processed_content"] = soup.encode(encoding="utf-8")
        chapter_result["success"] = True
    except Exception as exc:
        chapter_result["error"] = str(exc)
        print(f"Error processing chapter {item.file_name}: {exc}")

    return chapter_result
