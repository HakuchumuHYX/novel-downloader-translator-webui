from __future__ import annotations

import os
import shutil
from asyncio import as_completed, create_task
from collections.abc import Awaitable, Callable, Iterable
from typing import TypeVar

import aiofiles


DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.114 Safari/537.36"
}

ResultT = TypeVar("ResultT")


def prepare_output_dir(output_dir: str, title: str) -> str:
    path = os.path.join(output_dir, title)
    if os.path.exists(path):
        shutil.rmtree(path)
    os.makedirs(path, exist_ok=True)
    return path


async def write_chapter_text(file_path: str, title: str, content: str, *, chapter_suffix: str = "") -> None:
    async with aiofiles.open(f"{file_path}.txt", "a+", encoding="utf-8") as file_obj:
        header = f"● {title}"
        if chapter_suffix:
            header = f"{header} {chapter_suffix}"
        await file_obj.write(f"{header}\n")
        await file_obj.write(f"{content}\n")


async def collect_results(
    awaitables: Iterable[Awaitable[ResultT]],
    *,
    total: int,
    progress_callback: Callable[[int, int], None] | None = None,
) -> list[ResultT]:
    completed = 0
    if progress_callback:
        progress_callback(completed, total)

    tasks = [create_task(awaitable) for awaitable in awaitables]
    results: list[ResultT] = []
    for task in as_completed(tasks):
        results.append(await task)
        completed += 1
        if progress_callback:
            progress_callback(completed, total)
    return results
