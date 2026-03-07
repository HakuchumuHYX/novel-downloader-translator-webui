import builtins
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from book_maker.utils import prompt_config_to_kwargs

from .base_loader import BaseBookLoader


def emit_progress(stage: str, current: int, total: int, unit: str) -> None:
    payload = {
        "stage": str(stage),
        "current": int(current),
        "total": int(total),
        "unit": str(unit),
    }
    builtins.print("__WEBUI_PROGRESS__ " + json.dumps(payload, ensure_ascii=False), flush=True)


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
        self.translate_model = model(
            key=key,
            language=language,
            api_base=model_api_base,
            temperature=temperature,
            source_lang=source_lang,
            **prompt_config_to_kwargs(prompt_config),
        )

        self.is_test = is_test
        self.p_to_save = []
        self.bilingual_result = []
        self.bilingual_temp_result = []
        self.test_num = test_num
        self.batch_size = 10
        self.single_translate = single_translate
        self.parallel_workers = max(1, parallel_workers)

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
        # cli.py post-init model configuration (set_model_list/set_gpt* models ...)
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

    def make_bilingual_book(self):
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
        try:
            payload: dict[str, Any] = {
                "version": 2,
                "p_to_save": self.p_to_save,
            }
            with open(self.bin_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
        except Exception as e:
            raise Exception("can not save resume file") from e

    def load_state(self):
        try:
            raw = Path(self.bin_path).read_text(encoding="utf-8")
            data = json.loads(raw)

            if isinstance(data, dict) and isinstance(data.get("p_to_save"), list):
                self.p_to_save = [str(x) for x in data.get("p_to_save", [])]
                return

            # Backward compatibility: very old format may be a plain JSON array.
            if isinstance(data, list):
                self.p_to_save = [str(x) for x in data]
                return

            # Unexpected JSON payload format.
            raise ValueError("invalid resume json format")
        except json.JSONDecodeError:
            # Backward compatibility with legacy newline-joined format.
            try:
                with open(self.bin_path, encoding="utf-8") as f:
                    self.p_to_save = f.read().splitlines()
            except Exception as e:
                raise Exception("can not load resume file") from e
        except Exception as e:
            raise Exception("can not load resume file") from e

    def save_file(self, book_path, content):
        try:
            with open(book_path, "w", encoding="utf-8") as f:
                f.write("\n".join(content))
        except Exception as e:
            raise Exception("can not save file") from e
