"""python -m downloader --url URL --work DIR --out DIR [--epub]

章节缓存在 <work>/chapters/<key>.txt，重跑时跳过已下载的章节；成品写到 <out>/<书名>.txt 或 .epub。
Cookie 从环境变量 DOWNLOADER_COOKIE 读取，不走命令行参数，免得出现在进程列表和日志里。
"""
from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

import aiohttp

from translator.txt2epub import write_epub

from . import kakuyomu, syosetu
from .common import HEADERS, detect_site, emit, parse_cookie, sanitize_filename

SITE_MODULES = {"syosetu": syosetu, "novel18": syosetu, "kakuyomu": kakuyomu}


async def download(url: str, work_dir: Path, proxy: str | None, cookie: str) -> tuple[str, list[Path]]:
    site = detect_site(url)
    module = SITE_MODULES[site]
    cookies = parse_cookie(cookie)
    if site == "novel18":
        cookies["over18"] = "yes"

    chapter_dir = work_dir / "chapters"
    chapter_dir.mkdir(parents=True, exist_ok=True)
    timeout = aiohttp.ClientTimeout(total=120)
    async with aiohttp.ClientSession(headers=HEADERS, cookies=cookies, timeout=timeout) as session:
        title, chapters = await module.fetch_index(session, url, proxy)
        if not chapters:
            raise RuntimeError("目录为空")
        print(f"《{title}》共 {len(chapters)} 章", flush=True)

        paths = [chapter_dir / f"{key}.txt" for key, _ in chapters]
        pending = [(path, chapter_url) for path, (_, chapter_url) in zip(paths, chapters) if not _has_content(path)]
        done = len(chapters) - len(pending)
        if done:
            print(f"已缓存 {done} 章，跳过", flush=True)
        emit("PROGRESS", {"stage": "download", "current": done, "total": len(chapters)})

        semaphore = asyncio.Semaphore(8)

        async def fetch_one(path: Path, chapter_url: str) -> None:
            nonlocal done
            async with semaphore:
                chapter_title, content = await module.fetch_chapter(session, chapter_url, proxy)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(f"● {chapter_title}\n{content}\n", encoding="utf-8")
            tmp.replace(path)
            done += 1
            emit("PROGRESS", {"stage": "download", "current": done, "total": len(chapters)})

        await asyncio.gather(*(fetch_one(path, chapter_url) for path, chapter_url in pending))
    return title, paths


def _has_content(path: Path) -> bool:
    return path.exists() and path.stat().st_size > 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="downloader")
    parser.add_argument("--url", required=True)
    parser.add_argument("--work", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--proxy", default="")
    parser.add_argument("--epub", action="store_true")
    args = parser.parse_args()

    title, paths = asyncio.run(
        download(args.url.strip(), args.work, args.proxy or None, os.getenv("DOWNLOADER_COOKIE", ""))
    )
    text = "\n\n".join(path.read_text(encoding="utf-8").strip() for path in paths) + "\n"

    args.out.mkdir(parents=True, exist_ok=True)
    name = sanitize_filename(title)
    if args.epub:
        result = args.out / f"{name}.epub"
        write_epub(text, result, title=title, language="ja")
    else:
        result = args.out / f"{name}.txt"
        result.write_text(text, encoding="utf-8")
    print(f"下载完成：{result.name}", flush=True)
    emit("RESULT", str(result))


if __name__ == "__main__":
    main()
