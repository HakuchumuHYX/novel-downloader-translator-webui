import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .base_loader import BaseBookLoader
from .common import create_translator, emit_progress, load_resume_entries, save_resume_entries, save_text_output


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
        self.translate_model = create_translator(
            model,
            key=key,
            language=language,
            model_api_base=model_api_base,
            prompt_config=prompt_config,
            temperature=temperature,
            source_lang=source_lang,
        )

        self.is_test = is_test
        self.p_to_save = []
        self.bilingual_result = []
        self.bilingual_temp_result = []
        self.test_num = test_num
        self.batch_size = 10
        self.single_translate = single_translate
        self.parallel_workers = max(1, parallel_workers)
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

    def _build_batches(self) -> list[tuple[int, str]]:
        batches: list[tuple[int, str]] = []
        for start in range(0, len(self.origin_book), self.batch_size):
            if self.is_test and start >= self.test_num:
                break
            chunk = self.origin_book[start : start + self.batch_size]
            batch_text = "\n".join(chunk)
            if self._is_special_text(batch_text):
                continue
            batches.append((len(batches), batch_text))
        return batches

    def _translate_batch(self, batch_text: str) -> str:
        # Keep using the configured loader-level translator instance so that
        # cli.py post-init model configuration (set_model_list/set_default_models ...)
        # is preserved for TXT mode.
        result = self.translate_model.translate(batch_text)
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

        completed = 0
        if self.resume:
            completed = sum(1 for i in range(total_batches) if self.p_to_save[i] != "")
        emit_progress("translate", completed, total_batches, "batch")

        try:
            pending = [(idx, text) for idx, text in batches if self.p_to_save[idx] == ""]

            if self.parallel_workers <= 1 or len(pending) <= 1:
                for idx, batch_text in pending:
                    try:
                        translated = self._translate_batch(batch_text)
                    except Exception as e:
                        print(e)
                        raise Exception("Something is wrong when translate") from e
                    self.p_to_save[idx] = translated
                    completed += 1
                    emit_progress("translate", completed, total_batches, "batch")
                    self._maybe_checkpoint(completed, total_batches)
            else:
                with ThreadPoolExecutor(max_workers=self.parallel_workers) as executor:
                    future_map = {
                        executor.submit(self._translate_batch, batch_text): idx
                        for idx, batch_text in pending
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
            for idx, batch_text in batches:
                translated = self.p_to_save[idx]
                if not self.single_translate:
                    self.bilingual_result.append(batch_text)
                self.bilingual_result.append(translated)

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
        for idx, batch_text in batches:
            if not self.single_translate:
                self.bilingual_temp_result.append(batch_text)
            if idx < len(self.p_to_save) and self.p_to_save[idx] != "":
                self.bilingual_temp_result.append(self.p_to_save[idx])

        self.save_file(
            f"{Path(self.txt_name).parent}/{Path(self.txt_name).stem}_翻译_temp.txt",
            self.bilingual_temp_result,
        )

    def _save_progress(self):
        save_resume_entries(self.bin_path, self.p_to_save, mode="json", atomic=True)

    def load_state(self):
        self.p_to_save = load_resume_entries(self.bin_path, mode="json")

    def save_file(self, book_path, content):
        save_text_output(book_path, content)
