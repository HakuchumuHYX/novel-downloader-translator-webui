from __future__ import annotations

from pathlib import Path

from ..models import Chapter


def parse_native_volume_txt(path: Path, *, strip_index_suffix: bool = False) -> list[tuple[str, str]]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    lines = text.splitlines()

    parsed: list[tuple[str, str]] = []
    current_title = ""
    buffer: list[str] = []

    for line in lines:
        if line.startswith("● "):
            if current_title:
                parsed.append((current_title, "\n".join(buffer).strip()))
            current_title = line[2:].strip()
            if strip_index_suffix and current_title.endswith("]") and " [" in current_title:
                current_title = current_title.rsplit(" [", 1)[0]
            buffer = []
        else:
            buffer.append(line)

    if current_title:
        parsed.append((current_title, "\n".join(buffer).strip()))

    return parsed


def chapters_from_txt_files(
    book_dir: Path,
    txt_files: list[Path],
    *,
    strip_index_suffix: bool = False,
) -> list[Chapter]:
    chapters: list[Chapter] = []
    chapter_index = 1
    for txt in txt_files:
        for title, content in parse_native_volume_txt(txt, strip_index_suffix=strip_index_suffix):
            chapters.append(
                Chapter(
                    index=chapter_index,
                    title=title,
                    content=content,
                    volume=txt.stem,
                    source_path=str(txt.relative_to(book_dir)),
                )
            )
            chapter_index += 1
    return chapters
