"""翻译上传的 epub。

直接按 zip 处理：只改正文 xhtml 里的段落/标题文本，图片、目录、元数据原样拷贝。
比用 ebooklib 读写整本书稳（ebooklib 回写会重建 nav/ncx，旧项目为此打过好几个补丁）。
"""
from __future__ import annotations

import copy
import re
import zipfile
from pathlib import Path

from bs4 import BeautifulSoup

from .llm import Translator
from .txt2epub import language_code

TAGS = ["p", "h1", "h2", "h3", "h4", "h5", "h6"]
BATCH_CHARS = 4000
BATCH_ITEMS = 40
NUMBERED_RULE = "输入的每一行以 [编号] 开头。逐行翻译，输出行数与输入相同，每行保留原来的 [编号] 前缀。"


def _text(node) -> str:
    node = copy.copy(node)
    for ruby in node.find_all(["rt", "rp"]):
        ruby.decompose()  # 去掉振假名，否则会变成「漢字かんじ」
    return re.sub(r"\s*\n\s*", " ", node.get_text()).strip()


def _batches(entries: list) -> list[list]:
    batches, current, size = [], [], 0
    for entry in entries:
        if current and (len(current) >= BATCH_ITEMS or size + len(entry[2]) > BATCH_CHARS):
            batches.append(current)
            current, size = [], 0
        current.append(entry)
        size += len(entry[2])
    if current:
        batches.append(current)
    return batches


def _numbered(batch: list) -> str:
    return "\n".join(f"[{i}] {text}" for i, (_, _, text) in enumerate(batch, start=1))


def _parse_numbered(result: str, count: int) -> list[str] | None:
    lines = {}
    for line in result.splitlines():
        if m := re.match(r"\s*\[(\d+)\]\s*(.*)", line):
            lines[int(m.group(1))] = m.group(2).strip()
    if set(lines) != set(range(1, count + 1)):
        return None
    return [lines[i] for i in range(1, count + 1)]


def _apply(soup, node, translated: str, bilingual: bool) -> None:
    if bilingual:
        attrs = {k: v for k, v in node.attrs.items() if k != "id"}  # 重复 id 会破坏目录跳转
        new = soup.new_tag(node.name, attrs=attrs)
        new.string = translated
        node.insert_after(new)
    else:
        node.clear()
        node.string = translated


def translate_epub(src: Path, out_dir: Path, translator: Translator, *, bilingual: bool, test_blocks: int) -> Path:
    with zipfile.ZipFile(src) as zin:
        docs = {}
        entries = []  # (soup, node, text)
        for name in zin.namelist():
            if not name.lower().endswith((".xhtml", ".html", ".htm")):
                continue
            soup = BeautifulSoup(zin.read(name), "lxml-xml")
            docs[name] = soup
            for node in soup.find_all(TAGS):
                if node.find(TAGS):
                    continue  # 嵌套时只翻最内层
                if text := _text(node):
                    entries.append((soup, node, text))

        batches = _batches(entries)
        if test_blocks:
            batches = batches[:test_blocks]

        results = translator.translate_all([_numbered(b) for b in batches], extra_system=NUMBERED_RULE)
        fallback = []
        for batch, result in zip(batches, results):
            lines = _parse_numbered(result, len(batch))
            if lines is None:
                fallback.extend(batch)
                continue
            for (soup, node, _), line in zip(batch, lines):
                _apply(soup, node, line, bilingual)

        if fallback:
            # 编号对不上的批次退回逐段翻译，慢但不会错位
            print(f"{len(fallback)} 段编号未对齐，改为逐段翻译", flush=True)
            for (soup, node, _), line in zip(fallback, translator.translate_all([text for _, _, text in fallback])):
                _apply(soup, node, line, bilingual)

        result_path = out_dir / f"{src.stem}_译.epub"
        lang = language_code(translator.language).encode()
        with zipfile.ZipFile(result_path, "w") as zout:
            # 原顺序写回，保证 mimetype 仍是第一个且不压缩
            for info in zin.infolist():
                data = zin.read(info.filename)
                if info.filename in docs:
                    data = str(docs[info.filename]).encode("utf-8")
                elif info.filename.lower().endswith(".opf"):
                    data = re.sub(rb"(<dc:language[^>]*>)[^<]*(</dc:language>)", rb"\g<1>" + lang + rb"\g<2>", data)
                compress = zipfile.ZIP_STORED if info.filename == "mimetype" else zipfile.ZIP_DEFLATED
                zout.writestr(info.filename, data, compress_type=compress)
    return result_path
