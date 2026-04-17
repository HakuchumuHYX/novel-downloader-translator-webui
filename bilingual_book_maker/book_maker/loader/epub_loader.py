import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import copy
from pathlib import Path
import traceback
from threading import Lock

from bs4 import BeautifulSoup as bs
from bs4.element import NavigableString
from ebooklib import ITEM_DOCUMENT, epub
from rich import print
from tqdm import tqdm

from book_maker.utils import num_tokens_from_text, prompt_config_to_kwargs

from .base_loader import BaseBookLoader
from .common import emit_progress
from .epub_compat import install_epub_compat_patches, install_epub_spine_fallback_patch
from .epub_parallel import (
    create_chapter_translator,
    process_chapter_parallel,
    translate_paragraphs_acc_parallel,
    translate_with_chapter_context,
)
from .epub_plan import build_chapter_plan, total_paragraph_count
from .epub_resume import load_resume_state, save_resume_state, save_temp_book
from .epub_support import (
    extract_paragraph,
    filter_nested_nodes,
    is_special_text,
    make_new_book,
    should_translate_item,
)
from .helper import EPUBBookLoaderHelper, not_trans


class EPUBBookLoader(BaseBookLoader):
    def __init__(
        self,
        epub_name,
        model,
        key,
        resume,
        language,
        model_api_base=None,
        is_test=False,
        test_num=5,
        prompt_config=None,
        single_translate=False,
        context_flag=False,
        context_paragraph_limit=0,
        temperature=1.0,
        source_lang="auto",
        parallel_workers=5,
    ):
        self.epub_name = epub_name
        self.new_epub = epub.EpubBook()
        self._translator_model_cls = model
        self._translator_init_args = (key, language)
        self._translator_init_kwargs = {
            "api_base": model_api_base,
            "context_flag": context_flag,
            "context_paragraph_limit": context_paragraph_limit,
            "temperature": temperature,
            "source_lang": source_lang,
            **prompt_config_to_kwargs(prompt_config),
        }
        self.translate_model = self._create_translator_instance()
        self.is_test = is_test
        self.test_num = test_num
        self.translate_tags = "p"
        self.exclude_translate_tags = "sup"
        self.allow_navigable_strings = False
        self.accumulated_num = 1
        self.translation_style = ""
        self.context_flag = context_flag
        self.helper = EPUBBookLoaderHelper(
            self.translate_model,
            self.accumulated_num,
            self.translation_style,
            self.context_flag,
        )
        self.retranslate = None
        self.exclude_filelist = ""
        self.only_filelist = ""
        self.single_translate = single_translate
        self.block_size = -1
        self.batch_use_flag = False
        self.batch_flag = False
        self.parallel_workers = 1
        self.enable_parallel = False
        self._progress_lock = Lock()
        self._translation_index = 0
        self.set_parallel_workers(parallel_workers)

        install_epub_compat_patches()

        try:
            self.origin_book = epub.read_epub(self.epub_name)
        except Exception:
            install_epub_spine_fallback_patch()
            self.origin_book = epub.read_epub(self.epub_name)

        self.p_to_save = []
        self.resume = resume
        self.bin_path = f"{Path(epub_name).parent}/.{Path(epub_name).stem}.temp.bin"
        if self.resume:
            self.load_state()

    def _create_translator_instance(self):
        return self._translator_model_cls(
            *self._translator_init_args,
            **self._translator_init_kwargs,
        )

    def _process_paragraph(self, p, new_p, index, p_to_save_len, thread_safe=False):
        if self._is_saved_index(index):
            cached = self._get_saved_value(index)
            p.string = cached
            new_p.string = cached
        else:
            t_text = ""
            if self.batch_flag:
                self.translate_model.add_to_batch_translate_queue(index, new_p.text)
            elif self.batch_use_flag:
                t_text = self.translate_model.batch_translate(index)
            else:
                t_text = self.translate_model.translate(new_p.text)
            if t_text is None:
                raise RuntimeError(
                    "`t_text` is None: your translation model is not working as expected. Please check your translation model configuration."
                )
            if type(p) is NavigableString:
                new_p = t_text
                self._set_saved_value(index, str(new_p))
            else:
                new_p.string = t_text
                self._set_saved_value(index, new_p.text)

        self.helper.insert_trans(
            p, new_p.string, self.translation_style, self.single_translate
        )
        index += 1

        if thread_safe:
            with self._progress_lock:
                if index % 20 == 0:
                    self._save_progress()
        else:
            if index % 20 == 0:
                self._save_progress()
        return index

    def _process_combined_paragraph(
        self, p_block, index, p_to_save_len, thread_safe=False
    ):
        text = []

        for p in p_block:
            if self.resume and index < p_to_save_len:
                p.string = self.p_to_save[index]
            else:
                p_text = p.text.rstrip()
                text.append(p_text)

            if self.is_test and index >= self.test_num:
                break

            index += 1

        if len(text) > 0:
            translated_text = self.translate_model.translate("\n".join(text))
            translated_text = translated_text.split("\n")
            text_len = len(translated_text)

            for i in range(text_len):
                t = translated_text[i]

                if i >= len(p_block):
                    p = p_block[-1]
                else:
                    p = p_block[i]

                if type(p) is NavigableString:
                    p = t
                else:
                    p.string = t

                self.helper.insert_trans(
                    p, p.string, self.translation_style, self.single_translate
                )

        if thread_safe:
            with self._progress_lock:
                self._save_progress()
        else:
            self._save_progress()
        return index

    def translate_paragraphs_acc(self, p_list, send_num):
        count = 0
        wait_p_list = []
        for i in range(len(p_list)):
            p = p_list[i]
            print(f"translating {i}/{len(p_list)}")
            temp_p = copy(p)

            for p_exclude in self.exclude_translate_tags.split(","):
                # for issue #280
                if type(p) is NavigableString:
                    continue
                for pt in temp_p.find_all(p_exclude):
                    pt.extract()

            if any(
                [not p.text, is_special_text(temp_p.text), not_trans(temp_p.text)]
            ):
                if i == len(p_list) - 1:
                    self.helper.deal_old(wait_p_list, self.single_translate)
                continue
            length = num_tokens_from_text(temp_p.text)
            if length > send_num:
                self.helper.deal_new(p, wait_p_list, self.single_translate)
                continue
            if i == len(p_list) - 1:
                if count + length < send_num:
                    wait_p_list.append(p)
                    self.helper.deal_old(wait_p_list, self.single_translate)
                else:
                    self.helper.deal_new(p, wait_p_list, self.single_translate)
                break
            if count + length < send_num:
                count += length
                wait_p_list.append(p)
            else:
                self.helper.deal_old(wait_p_list, self.single_translate)
                wait_p_list.append(p)
                count = length

    def get_item(self, book, name):
        for item in book.get_items():
            if item.file_name == name:
                return item

    def find_items_containing_string(self, book, search_string):
        matching_items = []

        for item in book.get_items_of_type(ITEM_DOCUMENT):
            content = item.get_content()
            soup = bs(content, "html.parser")
            if search_string in soup.get_text():
                matching_items.append(item)

        return matching_items

    def retranslate_book(self, index, p_to_save_len, pbar, trans_taglist, retranslate):
        complete_book_name = retranslate[0]
        fixname = retranslate[1]
        fixstart = retranslate[2]
        fixend = retranslate[3]

        if fixend == "":
            fixend = fixstart

        name_fix = complete_book_name

        complete_book = epub.read_epub(complete_book_name)

        if fixname == "":
            fixname = self.find_items_containing_string(complete_book, fixstart)[
                0
            ].file_name
            print(f"auto find fixname: {fixname}")

        new_book = make_new_book(complete_book)

        complete_item = self.get_item(complete_book, fixname)
        if complete_item is None:
            return

        ori_item = self.get_item(self.origin_book, fixname)
        if ori_item is None:
            return

        content_complete = complete_item.content
        content_ori = ori_item.content
        soup_complete = bs(content_complete, "html.parser")
        soup_ori = bs(content_ori, "html.parser")

        p_list_complete = soup_complete.findAll(trans_taglist)
        p_list_ori = soup_ori.findAll(trans_taglist)

        target = None
        tagl = []

        # extract from range
        find_end = False
        find_start = False
        for tag in p_list_complete:
            if find_end:
                tagl.append(tag)
                break

            if fixend in tag.text:
                find_end = True
            if fixstart in tag.text:
                find_start = True

            if find_start:
                if not target:
                    target = tag.previous_sibling
                tagl.append(tag)

        for t in tagl:
            t.extract()

        flag = False
        extract_p_list_ori = []
        for p in p_list_ori:
            if fixstart in p.text:
                flag = True
            if flag:
                extract_p_list_ori.append(p)
            if fixend in p.text:
                break

        for t in extract_p_list_ori:
            if target:
                target.insert_after(t)
                target = t

        for item in complete_book.get_items():
            if item.file_name != fixname:
                new_book.add_item(item)
        if soup_complete:
            complete_item.content = soup_complete.encode()

        index = self.process_item(
            complete_item,
            index,
            p_to_save_len,
            pbar,
            new_book,
            trans_taglist,
            fixstart,
            fixend,
        )
        epub.write_epub(f"{name_fix}", new_book, {})

    def _is_saved_index(self, index: int) -> bool:
        if index < 0 or index >= len(self.p_to_save):
            return False
        value = self.p_to_save[index]
        return isinstance(value, str) and value != ""

    def _get_saved_value(self, index: int) -> str:
        if 0 <= index < len(self.p_to_save):
            value = self.p_to_save[index]
            if isinstance(value, str):
                return value
        return ""

    def _set_saved_value(self, index: int, value: str) -> None:
        if index < 0:
            return
        if index >= len(self.p_to_save):
            self.p_to_save.extend([""] * (index - len(self.p_to_save) + 1))
        self.p_to_save[index] = value

    def process_item(
        self,
        item,
        index,
        p_to_save_len,
        pbar,
        new_book,
        trans_taglist,
        fixstart=None,
        fixend=None,
    ):
        if not should_translate_item(item, self.only_filelist, self.exclude_filelist):
            new_book.add_item(item)
            return index

        if not os.path.exists("log"):
            os.makedirs("log")

        content = item.content
        soup = bs(content, "html.parser")
        p_list = soup.findAll(trans_taglist)

        p_list = filter_nested_nodes(p_list, trans_taglist)

        if self.retranslate:
            new_p_list = []

            if fixstart is None or fixend is None:
                return

            start_append = False
            for p in p_list:
                text = p.get_text()
                if fixstart in text or fixend in text or start_append:
                    start_append = True
                    new_p_list.append(p)
                if fixend in text:
                    p_list = new_p_list
                    break

        if self.allow_navigable_strings:
            p_list.extend(soup.findAll(text=True))

        send_num = self.accumulated_num
        if send_num > 1:
            with open("log/buglog.txt", "a") as f:
                print(f"------------- {item.file_name} -------------", file=f)

            print("------------------------------------------------------")
            print(f"dealing {item.file_name} ...")
            self.translate_paragraphs_acc(p_list, send_num)
        else:
            is_test_done = self.is_test and index > self.test_num
            p_block = []
            block_len = 0
            for p in p_list:
                if is_test_done:
                    break
                if not p.text or is_special_text(p.text):
                    pbar.update(1)
                    continue

                new_p = extract_paragraph(copy(p), self.exclude_translate_tags)
                if self.single_translate and self.block_size > 0:
                    p_len = num_tokens_from_text(new_p.text)
                    block_len += p_len
                    if block_len > self.block_size:
                        index = self._process_combined_paragraph(
                            p_block, index, p_to_save_len, thread_safe=False
                        )
                        p_block = [p]
                        block_len = p_len
                        print()
                    else:
                        p_block.append(p)
                else:
                    index = self._process_paragraph(
                        p, new_p, index, p_to_save_len, thread_safe=False
                    )
                    print()

                # pbar.update(delta) not pbar.update(index)?
                pbar.update(1)

                if self.is_test and index >= self.test_num:
                    break
            if self.single_translate and self.block_size > 0 and len(p_block) > 0:
                index = self._process_combined_paragraph(
                    p_block, index, p_to_save_len, thread_safe=False
                )

        if soup:
            item.content = soup.encode(encoding="utf-8")
        new_book.add_item(item)

        return index

    def set_parallel_workers(self, workers):
        """Set number of parallel workers for chapter processing.

        Args:
            workers (int): Number of parallel workers. Will be automatically
                         optimized based on actual chapter count during processing.
        """
        self.parallel_workers = max(1, workers)
        self.enable_parallel = workers > 1

        if workers > 8:
            print(
                f"⚠️  Warning: {workers} workers is quite high. Consider using 2-8 workers for optimal performance."
            )

    def _get_next_translation_index(self):
        """Thread-safe method to get next translation index."""
        with self._progress_lock:
            index = self._translation_index
            self._translation_index += 1
            return index

    def _process_chapter_parallel(self, chapter_data):
        return process_chapter_parallel(self, chapter_data)

    def _create_chapter_translator(self):
        return create_chapter_translator(self)

    def _translate_with_chapter_context(
        self, translator, text, chapter_context_list, chapter_translated_list
    ):
        return translate_with_chapter_context(
            self,
            translator,
            text,
            chapter_context_list,
            chapter_translated_list,
        )

    def _translate_paragraphs_acc_parallel(
        self,
        p_list,
        send_num,
        translator,
        chapter_context_list,
        chapter_translated_list,
    ):
        translate_paragraphs_acc_parallel(
            self,
            p_list,
            send_num,
            translator,
            chapter_context_list,
            chapter_translated_list,
        )

    def batch_init_then_wait(self):
        name, _ = os.path.splitext(self.epub_name)
        if self.batch_flag or self.batch_use_flag:
            self.translate_model.batch_init(name)
            if self.batch_use_flag:
                start_time = time.time()
                while not self.translate_model.is_completed_batch():
                    print("Batch translation is not completed yet")
                    time.sleep(2)
                    if time.time() - start_time > 300:  # 5 minutes
                        raise Exception("Batch translation timed out after 5 minutes")

    def make_bilingual_book(self):
        self.helper = EPUBBookLoaderHelper(
            self.translate_model,
            self.accumulated_num,
            self.translation_style,
            self.context_flag,
        )
        self.batch_init_then_wait()
        new_book = make_new_book(self.origin_book)
        all_items = list(self.origin_book.get_items())
        trans_taglist = self.translate_tags.split(",")
        all_p_length = total_paragraph_count(
            all_items,
            trans_taglist,
            allow_navigable_strings=self.allow_navigable_strings,
            only_filelist=self.only_filelist,
            exclude_filelist=self.exclude_filelist,
        )
        pbar = tqdm(total=self.test_num) if self.is_test else tqdm(total=all_p_length)
        print()
        index = 0
        p_to_save_len = len(self.p_to_save)
        try:
            if self.retranslate:
                self.retranslate_book(
                    index, p_to_save_len, pbar, trans_taglist, self.retranslate
                )
                return
            # Add the things that don't need to be translated first, so that you can see the img after the interruption
            for item in self.origin_book.get_items():
                if item.get_type() != ITEM_DOCUMENT:
                    new_book.add_item(item)

            document_items = list(self.origin_book.get_items_of_type(ITEM_DOCUMENT))
            chapter_targets, chapter_offsets, completed_flags = build_chapter_plan(
                document_items,
                trans_taglist,
                allow_navigable_strings=self.allow_navigable_strings,
                only_filelist=self.only_filelist,
                exclude_filelist=self.exclude_filelist,
                is_saved_index=self._is_saved_index,
            )

            chapter_total = len(chapter_targets)
            chapter_current = sum(1 for done in completed_flags if done)
            emit_progress("translate", chapter_current, chapter_total, "chapter")

            if self.enable_parallel and len(chapter_targets) > 1:
                effective_workers = min(self.parallel_workers, len(chapter_targets))

                print(f"🚀 Parallel processing: {len(chapter_targets)} chapters")
                if effective_workers < self.parallel_workers:
                    print(
                        f"📊 Optimized workers: {effective_workers} (reduced from {self.parallel_workers})"
                    )
                else:
                    print(f"📊 Using {effective_workers} workers")

                if self.accumulated_num > 1:
                    print(
                        f"📝 Each chapter applies accumulated_num={self.accumulated_num} independently"
                    )

                if self.context_flag:
                    print(
                        f"🔗 Context enabled: each chapter maintains independent context (limit={self.translate_model.context_paragraph_limit})"
                    )
                else:
                    print(f"🚫 Context disabled for this translation")

                for item in document_items:
                    if not should_translate_item(item, self.only_filelist, self.exclude_filelist):
                        new_book.add_item(item)

                pbar.close()
                chapter_pbar = tqdm(
                    total=len(chapter_targets), desc="Chapters", unit="ch"
                )

                chapter_data_list = [
                    (item, trans_taglist, p_to_save_len, chapter_offsets[idx])
                    for idx, item in enumerate(chapter_targets)
                ]

                processed_by_file: dict[str, bytes] = {}

                with ThreadPoolExecutor(max_workers=effective_workers) as executor:
                    future_meta = {
                        executor.submit(
                            self._process_chapter_parallel, chapter_data
                        ): (chapter_data[0], completed_flags[idx])
                        for idx, chapter_data in enumerate(chapter_data_list)
                    }

                    for future in as_completed(future_meta):
                        item, was_completed = future_meta[future]
                        try:
                            result = future.result()
                            if result["success"] and result["processed_content"]:
                                processed_by_file[item.file_name] = result["processed_content"]
                            chapter_pbar.update(1)
                            chapter_pbar.set_postfix_str(
                                f"Latest: {item.file_name[:20]}..."
                            )
                            if not was_completed:
                                chapter_current += 1
                                emit_progress("translate", chapter_current, chapter_total, "chapter")

                        except Exception as e:
                            print(f"❌ Error processing {item.file_name}: {e}")
                            chapter_pbar.update(1)

                chapter_pbar.close()

                # Keep EPUB item insertion deterministic: always add translated document
                # items in their original book order, regardless of future completion order.
                for item in document_items:
                    if not should_translate_item(item, self.only_filelist, self.exclude_filelist):
                        continue
                    if item.file_name in processed_by_file:
                        item.content = processed_by_file[item.file_name]
                    new_book.add_item(item)

                print(f"✅ Completed all {len(chapter_targets)} chapters")
            else:
                if len(chapter_targets) == 1 and self.enable_parallel:
                    print(f"📄 Single chapter detected - using sequential processing")

                completed_map = {item.file_name: completed_flags[idx] for idx, item in enumerate(chapter_targets)}

                for item in document_items:
                    index = self.process_item(
                        item, index, p_to_save_len, pbar, new_book, trans_taglist
                    )
                    if should_translate_item(item, self.only_filelist, self.exclude_filelist) and not completed_map.get(item.file_name, False):
                        chapter_current += 1
                        emit_progress("translate", chapter_current, chapter_total, "chapter")

                if self.accumulated_num > 1:
                    name, _ = os.path.splitext(self.epub_name)
                    epub.write_epub(f"{name}_翻译.epub", new_book, {})
            name, _ = os.path.splitext(self.epub_name)
            if self.batch_flag:
                self.translate_model.batch()
            else:
                epub.write_epub(f"{name}_翻译.epub", new_book, {})
            if self.accumulated_num == 1:
                pbar.close()
        except KeyboardInterrupt as e:
            print(e)
            if self.accumulated_num == 1:
                print("you can resume it next time")
                self._save_progress()
                self._save_temp_book()
            raise
        except Exception:
            traceback.print_exc()
            raise

    def load_state(self):
        self.p_to_save = load_resume_state(self.bin_path)

    def _save_temp_book(self):
        save_temp_book(self)

    def _save_progress(self):
        save_resume_state(self.bin_path, self.p_to_save)
