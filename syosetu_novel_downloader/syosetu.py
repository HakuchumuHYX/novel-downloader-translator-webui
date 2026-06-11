import json
import os
import re
from asyncio import Semaphore, gather
from collections.abc import Callable
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import aiohttp
from bs4 import BeautifulSoup  # type: ignore

from custom_typing import ChapterContent, ChapterRange, ChapterTitle, NovelTitle, PartTitle
from downloader.async_support import DEFAULT_HEADERS, collect_results, prepare_output_dir, write_chapter_text
from downloader.utils import sanitize_filename


MAIN_URL: str = "https://ncode.syosetu.com"
CHAPTER_MANIFEST_NAME = "_chapter_manifest.json"


def _chapter_output_path(file_path: str) -> Path:
    return Path(f"{file_path}.txt")


def _relative_output_file_path(book_dir: Path, file_path: str) -> str:
    output_path = _chapter_output_path(file_path)
    try:
        return str(output_path.relative_to(book_dir))
    except ValueError:
        return output_path.name


def _load_chapter_manifest(book_dir: Path) -> dict[int, dict[str, str]]:
    manifest_path = book_dir / CHAPTER_MANIFEST_NAME
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return {}

    chapters = raw.get("chapters")
    if not isinstance(chapters, list):
        return {}

    entries: dict[int, dict[str, str]] = {}
    for entry in chapters:
        if not isinstance(entry, dict):
            continue
        try:
            chapter_index = int(entry.get("index"))
        except (TypeError, ValueError):
            continue
        entries[chapter_index] = {
            "status": str(entry.get("status") or ""),
            "file_path": str(entry.get("file_path") or ""),
        }
    return entries


def _write_chapter_manifest_entry(book_dir: Path, chapter_index: int, file_path: str, status: str) -> None:
    entries = _load_chapter_manifest(book_dir)
    entries[int(chapter_index)] = {
        "status": status,
        "file_path": _relative_output_file_path(book_dir, file_path),
    }
    payload = {
        "version": 1,
        "chapters": [
            {"index": index, **entry}
            for index, entry in sorted(entries.items())
        ],
    }
    (book_dir / CHAPTER_MANIFEST_NAME).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _chapter_output_has_content(book_dir: Path, file_path: str) -> bool:
    path = Path(file_path)
    if not path.is_absolute():
        path = book_dir / path
    return path.is_file() and bool(path.read_text(encoding="utf-8", errors="ignore").strip())


def _filter_pending_chapter_jobs(book_dir: str | Path, jobs: list[tuple[int, str]]) -> tuple[list[tuple[int, str]], list[int]]:
    book_path = Path(book_dir)
    entries = _load_chapter_manifest(book_path)
    pending: list[tuple[int, str]] = []
    skipped: list[int] = []

    for chapter_index, file_path in jobs:
        entry = entries.get(int(chapter_index), {})
        status = str(entry.get("status") or "").lower()
        entry_path = str(entry.get("file_path") or _relative_output_file_path(book_path, file_path))
        if status in {"ok", "completed"} and _chapter_output_has_content(book_path, entry_path):
            skipped.append(chapter_index)
            continue
        pending.append((chapter_index, file_path))

    return pending, skipped


class Syosetu:
    def __init__(
        self,
        novel_id: str,
        proxy: str = "",
        base_url: str = MAIN_URL,
        over18: bool = False,
        cookie_header: str = "",
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> None:
        self.novel_id = novel_id
        self.proxy = proxy
        self.base_url = base_url.rstrip("/")
        self.over18 = over18
        self.cookie_header = cookie_header.strip()
        self.novel_title: NovelTitle = ""

        self.record_chapter_index = False
        self.progress_callback = progress_callback
        self.total_chapters = 0

        self.__semaphore = Semaphore(8)
        self.__novel_info_soups: list[BeautifulSoup] = []
        self.__session: aiohttp.ClientSession | None = None

    async def async_init(self):
        cookies = _parse_cookie_header(self.cookie_header)
        if self.over18:
            cookies["over18"] = "yes"
        self.__session: aiohttp.ClientSession = aiohttp.ClientSession(
            cookies=cookies or None,
        )

        self.__novel_info_soups = await self.__fetch_all_novel_info_pages()
        if not self.__novel_info_soups:
            raise RuntimeError("Could not fetch novel info pages")

        self.__novel_info_soup = self.__novel_info_soups[0]
        self.novel_title = self.__get_novel_title()
        self.author = self.__get_novel_author()

    async def async_close(self):
        if self.__session:
            await self.__session.close()
            self.__session = None

    async def __fetch_novel_info(self, page: int = 1) -> BeautifulSoup:
        suffix = f"/{self.novel_id}"
        if page > 1:
            suffix = f"{suffix}/?p={page}"

        async with self.__session.get(
            f"{self.base_url}{suffix}",
            headers=DEFAULT_HEADERS,
            proxy=self.proxy,
        ) as response:
            return BeautifulSoup(await response.text(), "html.parser")

    async def __fetch_all_novel_info_pages(self) -> list[BeautifulSoup]:
        first = await self.__fetch_novel_info(page=1)
        max_page = self.__extract_max_page(first)
        if max_page <= 1:
            return [first]

        remaining_pages = await gather(
            *(self.__fetch_novel_info(page=i) for i in range(2, max_page + 1))
        )
        return [first, *remaining_pages]

    def __extract_max_page(self, soup: BeautifulSoup) -> int:
        max_page = 1
        for a_tag in soup.find_all("a", href=True):
            href = str(a_tag.get("href") or "")
            parsed = urlparse(href)
            page_values = parse_qs(parsed.query).get("p", [])
            for page in page_values:
                if page.isdigit():
                    max_page = max(max_page, int(page))
        return max_page

    async def __fetch_chapters_info(self, chapter: int) -> BeautifulSoup:
        async with self.__session.get(
            f"{self.base_url}/{self.novel_id}/{chapter}",
            headers=DEFAULT_HEADERS,
            proxy=self.proxy,
        ) as response:
            return BeautifulSoup(await response.text(), "html.parser")

    def __extract_chapter_numbers(self) -> list[int]:
        numbers: set[int] = set()
        pattern = re.compile(rf"/{re.escape(self.novel_id)}/(\d+)/?$")

        soups = self.__novel_info_soups or [self.__novel_info_soup]
        for soup in soups:
            for a_tag in soup.find_all("a", href=True):
                href = str(a_tag.get("href") or "")
                matched = pattern.search(href)
                if matched:
                    numbers.add(int(matched.group(1)))
        return sorted(numbers)

    async def __get_novel_parts(self) -> dict[NovelTitle, ChapterRange]:
        part_numbers: dict[PartTitle, list[int]] = {}
        current_part: PartTitle = self.novel_title

        soups = self.__novel_info_soups or [self.__novel_info_soup]
        chapter_pattern = re.compile(rf"/{re.escape(self.novel_id)}/(\d+)/?$")

        for soup in soups:
            blocks = soup.find_all("div", class_=["p-eplist__chapter-title", "p-eplist__sublist"])
            for block in blocks:
                classes = block.get("class", [])
                if "p-eplist__chapter-title" in classes:
                    current_part = block.get_text(strip=True) or self.novel_title
                    continue

                if "p-eplist__sublist" not in classes:
                    continue

                a_tag = block.find("a", href=True)
                if not a_tag:
                    continue

                href = str(a_tag.get("href") or "")
                matched = chapter_pattern.search(href)
                if not matched:
                    continue

                chapter_number = int(matched.group(1))
                key = current_part or self.novel_title
                part_numbers.setdefault(key, [])
                part_numbers[key].append(chapter_number)

        if part_numbers:
            chapters: dict[NovelTitle, ChapterRange] = {}
            for title, nums in part_numbers.items():
                unique_sorted = sorted(set(nums))
                if unique_sorted:
                    chapters[title] = unique_sorted
            if chapters:
                return chapters

        chapter_numbers = self.__extract_chapter_numbers()
        if chapter_numbers:
            return {self.novel_title: chapter_numbers}

        return {}

    async def get_novel_part_titles(self) -> list[PartTitle]:
        parts = await self.__get_novel_parts()
        return list(parts.keys())

    def __get_novel_title(self) -> NovelTitle:
        node = self.__novel_info_soup.find("h1", class_="p-novel__title")
        if node is None:
            raise RuntimeError("Could not find novel title")
        return node.text

    def __get_novel_author(self) -> str:
        node = self.__novel_info_soup.find("a", href=True)
        return "" if node is None else node.text

    def __get_chapters_range(self) -> ChapterRange:
        chapter_numbers = self.__extract_chapter_numbers()
        if chapter_numbers:
            return chapter_numbers

        chapters = self.__novel_info_soup.find_all("dd")
        return range(1, len(chapters) + 1)

    async def __get_chapter_title_content(self, chapter: int) -> tuple[ChapterTitle, ChapterContent]:
        soup: BeautifulSoup = await self.__fetch_chapters_info(chapter)
        title_node = soup.find("h1", class_="p-novel__title")
        content_node = soup.find("div", class_="p-novel__body")
        if title_node is None or content_node is None:
            raise RuntimeError(f"Could not parse chapter {chapter}")
        title = title_node.text.replace("\u3000", " ")
        content = content_node.text.replace("\u3000", "")
        return title, content

    async def __async_save_txt(self, title: ChapterTitle | PartTitle, content: ChapterContent, chapter_index, file_path: str) -> None:
        chapter_suffix = f"[総第{chapter_index}話]" if self.record_chapter_index else ""
        await write_chapter_text(file_path, str(title), str(content), chapter_suffix=chapter_suffix)
        _write_chapter_manifest_entry(_chapter_output_path(file_path).parent, int(chapter_index), file_path, "ok")

    async def async_fetch(self, chapter_index: int, file_path: str) -> tuple[int, str, ChapterTitle, ChapterContent]:
        async with self.__semaphore:
            title, content = await self.__get_chapter_title_content(chapter_index)
            return chapter_index, file_path, title, content

    async def async_download(self, output_dir) -> str:
        output_dir = prepare_output_dir(output_dir, self.novel_title, clean=False)
        parts: dict[PartTitle, ChapterRange] = await self.__get_novel_parts()
        print((len(parts) == 0) and "No part\n" or f"All parts:\n{chr(10).join(list(parts.keys()))}\n")

        if len(parts) != 0:
            self.total_chapters = sum(len(v) for v in parts.values())
        else:
            chapter_range = self.__get_chapters_range()
            self.total_chapters = len(chapter_range)

        downloaded = 0
        if self.progress_callback:
            self.progress_callback(downloaded, self.total_chapters)

        async def _run_jobs(jobs):
            nonlocal downloaded
            jobs, skipped = _filter_pending_chapter_jobs(output_dir, list(jobs))
            downloaded += len(skipped)
            if self.progress_callback and skipped:
                self.progress_callback(downloaded, self.total_chapters)
            if not jobs:
                return

            def _on_progress(current: int, _: int) -> None:
                if self.progress_callback:
                    self.progress_callback(downloaded + current, self.total_chapters)

            results = await collect_results(
                [self.async_fetch(chapter_index, file_path) for chapter_index, file_path in jobs],
                total=len(jobs),
                progress_callback=_on_progress,
            )
            downloaded += len(results)
            for chapter_index, file_path, title, content in sorted(results, key=lambda item: item[0]):
                await self.__async_save_txt(title, content, chapter_index, file_path)

        if len(parts) != 0:
            for k, v in parts.items():
                print(f"Start download part: {k}")
                await _run_jobs([(chapter_index, os.path.join(output_dir, sanitize_filename(k))) for chapter_index in v])
        else:
            print(f"Start download novel: {self.novel_title}")
            await _run_jobs(
                [
                    (chapter_index, os.path.join(output_dir, sanitize_filename(self.novel_title)))
                    for chapter_index in self.__get_chapters_range()
                ]
            )
        return output_dir


def _parse_cookie_header(cookie_header: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for chunk in str(cookie_header or "").split(";"):
        text = chunk.strip()
        if not text or "=" not in text:
            continue
        key, value = text.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key:
            cookies[key] = value
    return cookies
