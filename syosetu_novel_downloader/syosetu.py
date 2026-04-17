import os
import re
from asyncio import Semaphore, gather
from collections.abc import Callable
from urllib.parse import parse_qs, urlparse

import aiohttp
from bs4 import BeautifulSoup  # type: ignore

from custom_typing import ChapterContent, ChapterRange, ChapterTitle, NovelTitle, PartTitle
from downloader.legacy_async_support import DEFAULT_HEADERS, collect_results, prepare_output_dir, write_chapter_text


MAIN_URL: str = "https://ncode.syosetu.com"


class Syosetu:
    def __init__(
        self,
        novel_id: str,
        proxy: str = "",
        base_url: str = MAIN_URL,
        over18: bool = False,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> None:
        self.novel_id = novel_id
        self.proxy = proxy
        self.base_url = base_url.rstrip("/")
        self.over18 = over18
        self.novel_title: NovelTitle = ""

        self.record_chapter_index = False
        self.progress_callback = progress_callback
        self.total_chapters = 0

        self.__semaphore = Semaphore(8)
        self.__novel_info_soups: list[BeautifulSoup] = []
        self.__session: aiohttp.ClientSession | None = None

    async def async_init(self):
        cookies = {"over18": "yes"} if self.over18 else None
        self.__session: aiohttp.ClientSession = aiohttp.ClientSession(
            cookies=cookies,
        )

        self.__novel_info_soups = await self.__fetch_all_novel_info_pages()
        if not self.__novel_info_soups:
            raise RuntimeError("Could not fetch novel info pages")

        # Keep page1 soup for compatibility with existing helpers.
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
                    chapters[title] = range(min(unique_sorted), max(unique_sorted) + 1)
            if chapters:
                return chapters

        chapter_numbers = self.__extract_chapter_numbers()
        if chapter_numbers:
            return {self.novel_title: range(min(chapter_numbers), max(chapter_numbers) + 1)}

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
            return range(min(chapter_numbers), max(chapter_numbers) + 1)

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

    async def async_fetch(self, chapter_index: int, file_path: str) -> tuple[int, str, ChapterTitle, ChapterContent]:
        async with self.__semaphore:
            title, content = await self.__get_chapter_title_content(chapter_index)
            return chapter_index, file_path, title, content

    async def async_download(self, output_dir) -> None:
        output_dir = prepare_output_dir(output_dir, self.novel_title)
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
                await _run_jobs([(chapter_index, os.path.join(output_dir, k)) for chapter_index in v])
        else:
            print(f"Start download novel: {self.novel_title}")
            await _run_jobs(
                [
                    (chapter_index, os.path.join(output_dir, self.novel_title))
                    for chapter_index in self.__get_chapters_range()
                ]
            )
