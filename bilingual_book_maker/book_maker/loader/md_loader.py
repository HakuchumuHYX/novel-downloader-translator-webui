from pathlib import Path

from .base_loader import BaseBookLoader
from .common import create_translator, load_resume_entries, save_resume_entries, save_text_output


class MarkdownBookLoader(BaseBookLoader):
    def __init__(
        self,
        md_name,
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
        self.md_name = md_name
        self.translate_model = create_translator(
            model,
            key,
            language,
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
        self.md_paragraphs = []

        try:
            with open(f"{md_name}", encoding="utf-8") as f:
                self.origin_book = f.read().splitlines()

        except Exception as e:
            raise Exception("can not load file") from e

        self.resume = resume
        self.bin_path = f"{Path(md_name).parent}/.{Path(md_name).stem}.temp.bin"
        if self.resume:
            self.load_state()

        self.process_markdown_content()

    def process_markdown_content(self):
        """将原始内容处理成 markdown 段落"""
        current_paragraph = []
        for line in self.origin_book:
            # 如果是空行且当前段落不为空，保存当前段落
            if not line.strip() and current_paragraph:
                self.md_paragraphs.append("\n".join(current_paragraph))
                current_paragraph = []
            # 如果是标题行，单独作为一个段落
            elif line.strip().startswith("#"):
                if current_paragraph:
                    self.md_paragraphs.append("\n".join(current_paragraph))
                    current_paragraph = []
                self.md_paragraphs.append(line)
            # 其他情况，添加到当前段落
            else:
                current_paragraph.append(line)

        # 处理最后一个段落
        if current_paragraph:
            self.md_paragraphs.append("\n".join(current_paragraph))

    @staticmethod
    def _is_special_text(text):
        return text.isdigit() or text.isspace() or len(text) == 0

    def _make_new_book(self, book):
        pass

    def build_book(self):
        try:
            sliced_list = [
                self.md_paragraphs[i : i + self.batch_size]
                for i in range(0, len(self.md_paragraphs), self.batch_size)
            ]
            self.bilingual_result = []

            for batch_index, paragraphs in enumerate(sliced_list):
                batch_text = "\n\n".join(paragraphs)
                if self._is_special_text(batch_text):
                    continue

                if self.resume and batch_index < len(self.p_to_save):
                    temp = self.p_to_save[batch_index]
                else:
                    try:
                        max_retries = 3
                        retry_count = 0
                        while retry_count < max_retries:
                            try:
                                temp = self.translate_model.translate(batch_text)
                                break
                            except AttributeError as ae:
                                print(f"翻译出错: {ae}")
                                retry_count += 1
                                if retry_count == max_retries:
                                    raise Exception("翻译模型初始化失败") from ae
                    except Exception as e:
                        print(f"翻译过程中出错: {e}")
                        raise Exception("翻译过程中出现错误") from e

                    if batch_index < len(self.p_to_save):
                        self.p_to_save[batch_index] = temp
                    else:
                        self.p_to_save.append(temp)

                if not self.single_translate:
                    self.bilingual_result.append(batch_text)
                self.bilingual_result.append(temp)

                processed_count = (batch_index + 1) * self.batch_size
                if self.is_test and processed_count > self.test_num:
                    break

            self.save_file(
                f"{Path(self.md_name).parent}/{Path(self.md_name).stem}_翻译.md",
                self.bilingual_result,
            )

        except (KeyboardInterrupt, Exception) as e:
            print(f"发生错误: {e}")
            print("程序将保存进度，您可以稍后继续")
            self._save_progress()
            self._save_temp_book()
            raise

    def _save_temp_book(self):
        sliced_list = [
            self.md_paragraphs[i : i + self.batch_size]
            for i in range(0, len(self.md_paragraphs), self.batch_size)
        ]

        self.bilingual_temp_result = []
        for batch_index, paragraphs in enumerate(sliced_list):
            batch_text = "\n\n".join(paragraphs)
            self.bilingual_temp_result.append(batch_text)
            if self._is_special_text(batch_text):
                continue
            if batch_index < len(self.p_to_save):
                self.bilingual_temp_result.append(self.p_to_save[batch_index])

        self.save_file(
            f"{Path(self.md_name).parent}/{Path(self.md_name).stem}_翻译_temp.txt",
            self.bilingual_temp_result,
        )

    def _save_progress(self):
        save_resume_entries(self.bin_path, self.p_to_save, mode="lines")

    def load_state(self):
        self.p_to_save = load_resume_entries(self.bin_path, mode="lines")

    def save_file(self, book_path, content):
        save_text_output(book_path, content)
