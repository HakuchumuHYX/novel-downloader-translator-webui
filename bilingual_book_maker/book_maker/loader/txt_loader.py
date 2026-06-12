import hashlib
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from .base_loader import BaseBookLoader
from .common import (
    create_translator,
    emit_progress,
    load_resume_state_with_metadata,
    save_resume_entries,
    save_text_output,
)
from .helper import translate_model_with_backoff


@dataclass(frozen=True)
class TextBatch:
    index: int
    text: str
    translatable: bool = True


MAX_NATURAL_CHUNK_LINES = max(1, int(os.getenv("BBM_TXT_MAX_CHUNK_LINES", "80")))
MAX_NATURAL_CHUNK_CHARS = max(1000, int(os.getenv("BBM_TXT_MAX_CHUNK_CHARS", "12000")))


def _would_exceed_chunk_limit(current: list[str], next_line: str) -> bool:
    if not current:
        return False
    if len(current) >= MAX_NATURAL_CHUNK_LINES:
        return True
    current_chars = sum(len(line) + 1 for line in current)
    return current_chars + len(next_line) + 1 > MAX_NATURAL_CHUNK_CHARS


def _split_oversized_line(line: str) -> list[str]:
    limit = MAX_NATURAL_CHUNK_CHARS
    if len(line) <= limit:
        return [line]
    return [line[start : start + limit] for start in range(0, len(line), limit)]


def _build_natural_chunks(lines: list[str]) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    blank_run: list[str] = []

    def flush_current() -> None:
        nonlocal current
        if current:
            chunks.append("\n".join(current).rstrip())
            current = []

    def flush_blank_run() -> None:
        nonlocal blank_run
        if blank_run:
            chunks.append("\n".join(blank_run))
            blank_run = []

    def append_line(line: str) -> None:
        for part in _split_oversized_line(line):
            if _would_exceed_chunk_limit(current, part):
                flush_current()
            current.append(part)

    for line in lines:
        starts_chapter = line.startswith("● ")
        blank = line.strip() == ""

        if starts_chapter:
            flush_current()
            flush_blank_run()
            append_line(line)
            continue

        if blank:
            blank_run.append(line)
            continue

        if blank_run:
            if len(blank_run) > 1:
                flush_current()
                flush_blank_run()
                append_line(line)
            else:
                flush_current()
                current = blank_run
                append_line(line)
                blank_run = []
            continue

        append_line(line)

    flush_current()
    flush_blank_run()
    return [chunk for chunk in chunks if chunk != ""]


class TXTBookLoader(BaseBookLoader):
    def __init__(
        self,
        txt_name,
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
        parallel_workers=1,
    ) -> None:
        self.txt_name = txt_name
        try:
            self.parallel_workers = max(1, int(parallel_workers or 1))
        except Exception:
            self.parallel_workers = 1
        self.context_enabled = bool(context_flag) and self.parallel_workers <= 1
        if context_flag and not self.context_enabled:
            print("warning: TXT context is disabled when parallel_workers > 1")
        self.translate_model = create_translator(
            model,
            key=key,
            language=language,
            model_api_base=model_api_base,
            prompt_config=prompt_config,
            temperature=temperature,
            source_lang=source_lang,
            context_flag=self.context_enabled,
            context_paragraph_limit=context_paragraph_limit,
        )

        self.is_test = is_test
        self.p_to_save = []
        self.bilingual_result = []
        self.bilingual_temp_result = []
        self.test_num = test_num
        self.batch_size = 10
        self.single_translate = single_translate
        self._checkpoint_interval_seconds = max(
            0.0,
            float(os.getenv("BBM_TXT_CHECKPOINT_INTERVAL_SECONDS", "2.0")),
        )
        self._last_checkpoint_ts = 0.0
        self._last_checkpoint_completed = -1

        try:
            with open(f"{txt_name}", encoding="utf-8") as f:
                self.origin_book = f.read().splitlines()

        except Exception as e:
            raise Exception("can not load file") from e

        self.resume = resume
        self.bin_path = f"{Path(txt_name).parent}/.{Path(txt_name).stem}.temp.bin"
        if self.resume:
            self.load_state()

    @staticmethod
    def _is_special_text(text):
        return text.isdigit() or text.isspace() or len(text) == 0

    def _make_new_book(self, book):
        pass

    def _build_batches(self) -> list[TextBatch]:
        batches: list[TextBatch] = []
        for chunk in _build_natural_chunks(self.origin_book):
            if self.is_test and len(batches) >= self.test_num:
                break
            if self._is_special_text(chunk):
                batches.append(TextBatch(index=len(batches), text=chunk, translatable=False))
                continue
            batches.append(TextBatch(index=len(batches), text=chunk))
        return batches

    def _batch_hashes(self) -> list[str]:
        return [hashlib.sha256(batch.text.encode("utf-8")).hexdigest() for batch in self._build_batches()]

    def _translate_batch(self, batch_text: str) -> str:
        # Keep using the configured loader-level translator instance so that
        # cli.py post-init model configuration (set_model_list/set_default_models ...)
        # is preserved for TXT mode.
        result = translate_model_with_backoff(self.translate_model, batch_text)
        if result is None:
            raise RuntimeError("translate returned None")
        return result

    def _normalize_saved_progress(self, total_batches: int) -> None:
        if len(self.p_to_save) > total_batches:
            self.p_to_save = self.p_to_save[:total_batches]
        if len(self.p_to_save) < total_batches:
            self.p_to_save.extend([""] * (total_batches - len(self.p_to_save)))

    def _maybe_checkpoint(self, completed: int, total_batches: int, force: bool = False) -> None:
        if total_batches <= 0:
            return

        now = time.monotonic()
        if not force and self._checkpoint_interval_seconds > 0:
            if completed == self._last_checkpoint_completed:
                return
            if (now - self._last_checkpoint_ts) < self._checkpoint_interval_seconds:
                return

        try:
            self._save_progress()
            self._last_checkpoint_ts = now
            self._last_checkpoint_completed = completed
        except Exception as e:
            # Best effort: checkpoint failure should not abort the whole translation task.
            print(f"warning: failed to checkpoint resume state: {e}")

    def build_book(self):
        batches = self._build_batches()
        total_batches = len(batches)
        self._normalize_saved_progress(total_batches)
        for batch in batches:
            if not batch.translatable and self.p_to_save[batch.index] == "":
                self.p_to_save[batch.index] = batch.text

        completed = sum(
            1
            for batch in batches
            if not batch.translatable or self.p_to_save[batch.index] != ""
        )
        emit_progress("translate", completed, total_batches, "batch")

        try:
            pending = [batch for batch in batches if batch.translatable and self.p_to_save[batch.index] == ""]

            if self.parallel_workers <= 1 or len(pending) <= 1:
                for batch in pending:
                    try:
                        translated = self._translate_batch(batch.text)
                    except Exception as e:
                        print(e)
                        raise Exception("Something is wrong when translate") from e
                    self.p_to_save[batch.index] = translated
                    completed += 1
                    emit_progress("translate", completed, total_batches, "batch")
                    self._maybe_checkpoint(completed, total_batches)
            else:
                with ThreadPoolExecutor(max_workers=self.parallel_workers) as executor:
                    future_map = {
                        executor.submit(self._translate_batch, batch.text): batch.index
                        for batch in pending
                    }

                    for future in as_completed(future_map):
                        idx = future_map[future]
                        try:
                            translated = future.result()
                        except Exception as e:
                            print(e)
                            raise Exception("Something is wrong when translate") from e
                        self.p_to_save[idx] = translated
                        completed += 1
                        emit_progress("translate", completed, total_batches, "batch")
                        self._maybe_checkpoint(completed, total_batches)

            self._maybe_checkpoint(completed, total_batches, force=True)

            self.bilingual_result = []
            for batch in batches:
                translated = self.p_to_save[batch.index]
                if batch.translatable and not self.single_translate:
                    self.bilingual_result.append(batch.text)
                    self.bilingual_result.append("")
                self.bilingual_result.append(translated)
                self.bilingual_result.append("")
            while self.bilingual_result and self.bilingual_result[-1] == "":
                self.bilingual_result.pop()

            self.save_file(
                f"{Path(self.txt_name).parent}/{Path(self.txt_name).stem}_翻译.txt",
                self.bilingual_result,
            )

        except KeyboardInterrupt as e:
            print(e)
            print("you can resume it next time")
            self._save_progress()
            self._save_temp_book()
            raise
        except Exception as e:
            print(e)
            print("you can resume it next time")
            self._save_progress()
            self._save_temp_book()
            raise

    def _save_temp_book(self):
        batches = self._build_batches()
        self.bilingual_temp_result = []
        for batch in batches:
            if batch.translatable and not self.single_translate:
                self.bilingual_temp_result.append(batch.text)
                self.bilingual_temp_result.append("")
            if batch.index < len(self.p_to_save) and (
                self.p_to_save[batch.index] != "" or not batch.translatable
            ):
                self.bilingual_temp_result.append(self.p_to_save[batch.index] if batch.index < len(self.p_to_save) else batch.text)
                self.bilingual_temp_result.append("")
        while self.bilingual_temp_result and self.bilingual_temp_result[-1] == "":
            self.bilingual_temp_result.pop()

        self.save_file(
            f"{Path(self.txt_name).parent}/{Path(self.txt_name).stem}_翻译_temp.txt",
            self.bilingual_temp_result,
        )

    def _save_progress(self):
        save_resume_entries(
            self.bin_path,
            self.p_to_save,
            mode="json",
            atomic=True,
            metadata={"version": 3, "batch_hashes": self._batch_hashes()},
        )

    def load_state(self):
        state = load_resume_state_with_metadata(self.bin_path)
        if state.get("batch_hashes") != self._batch_hashes():
            print("warning: resume state batch layout changed; ignoring old TXT resume state")
            self.p_to_save = []
            return
        self.p_to_save = [str(item) for item in state.get("p_to_save", [])]

    def save_file(self, book_path, content):
        save_text_output(book_path, content)
