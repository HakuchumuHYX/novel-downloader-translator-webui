"""python -m translator --input FILE --work DIR --out DIR [--epub] [--bilingual] [--test-blocks N]

配置（api_base / api_key / model / language / prompt / 并发 / 代理）从环境变量 TRANSLATOR_CONFIG（JSON）读取，
key 不出现在命令行。译文缓存在 <work>/translation_cache.json，重跑自动续传。
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .epub import translate_epub
from .llm import Translator, emit
from .txt import translate_txt


def main() -> None:
    parser = argparse.ArgumentParser(prog="translator")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--work", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--epub", action="store_true", help="txt 输入时输出 epub；epub 输入总是输出 epub")
    parser.add_argument("--bilingual", action="store_true")
    parser.add_argument("--test-blocks", type=int, default=0, help="只翻前 N 块，0 为全文")
    args = parser.parse_args()

    cfg = json.loads(os.environ["TRANSLATOR_CONFIG"])
    if cfg["proxy"]:
        os.environ["HTTP_PROXY"] = os.environ["HTTPS_PROXY"] = cfg["proxy"]

    args.work.mkdir(parents=True, exist_ok=True)
    args.out.mkdir(parents=True, exist_ok=True)
    translator = Translator(cfg, args.work / "translation_cache.json")
    print(f"模型 {translator.model}，并发 {translator.workers}", flush=True)

    if args.input.suffix.lower() == ".epub":
        result = translate_epub(args.input, args.out, translator, bilingual=args.bilingual, test_blocks=args.test_blocks)
    else:
        result = translate_txt(
            args.input,
            args.out,
            translator,
            bilingual=args.bilingual,
            test_blocks=args.test_blocks,
            as_epub=args.epub,
        )
    print(f"翻译完成：{result.name}", flush=True)
    emit("RESULT", str(result))


if __name__ == "__main__":
    main()
