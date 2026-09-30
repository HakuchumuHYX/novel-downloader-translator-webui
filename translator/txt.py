from __future__ import annotations

from pathlib import Path

from .llm import Translator
from .txt2epub import language_code, write_epub

MAX_LINES = 80
MAX_CHARS = 12000


def split_chunks(text: str) -> list[str]:
    """按「● 章节标记」和行数/字数上限切块；块内保留原有换行交给模型逐行对应。"""
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.splitlines():
        if current and (line.startswith("● ") or len(current) >= MAX_LINES or size + len(line) > MAX_CHARS):
            chunks.append("\n".join(current).strip("\n"))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    chunks.append("\n".join(current).strip("\n"))
    return [chunk for chunk in chunks if chunk.strip()]


def _merge(original: str, translated: str, bilingual: bool) -> str:
    # 模型偶尔丢掉章节标记，补回来才能正确分章
    if original.startswith("● ") and not translated.startswith("● "):
        translated = "● " + translated
    if not bilingual:
        return translated
    if translated.startswith("● "):
        # 双语时只保留译文标题作为分章标记，原文标题降为普通行
        title_line, _, body = translated.partition("\n")
        return f"{title_line}\n{original.removeprefix('● ')}\n\n{body}"
    return f"{original}\n\n{translated}"


def translate_txt(
    src: Path,
    out_dir: Path,
    translator: Translator,
    *,
    bilingual: bool,
    test_blocks: int,
    as_epub: bool,
) -> Path:
    chunks = split_chunks(src.read_text(encoding="utf-8"))
    if test_blocks:
        chunks = chunks[:test_blocks]
    translations = translator.translate_all(chunks)
    text = "\n\n".join(_merge(o, t, bilingual) for o, t in zip(chunks, translations)) + "\n"

    if as_epub:
        result = out_dir / f"{src.stem}_译.epub"
        write_epub(text, result, title=src.stem, language=language_code(translator.language))
    else:
        result = out_dir / f"{src.stem}_译.txt"
        result.write_text(text, encoding="utf-8")
    return result
