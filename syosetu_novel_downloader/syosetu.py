import os
import re
import shutil
import ssl
from asyncio import Semaphore
from collections.abc import Callable
from enum import Enum

import aiofiles
import aiohttp
from bs4 import BeautifulSoup, Tag  # type: ignore
from deprecated import deprecated
from pydantic import BaseModel

from custom_typing import ChapterContent, ChapterRange, ChapterTitle, NovelTitle, PartTitle


MAIN_URL: str = "https://ncode.syosetu.com"

headers = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.114 Safari/537.36"
}


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

    async def async_init(self):
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

        connector = aiohttp.TCPConnector(ssl=ssl_context)
        cookies = {"over18": "yes"} if self.over18 else None
        self.__session: aiohttp.ClientSession = aiohttp.ClientSession(
            connector=connector,
            cookies=cookies,
        )
        self.__novel_info_soup = await self.__fetch_novel_info()
        self.novel_title = self.__get_novel_title()
        self.author = self.__get_novel_author()

    async def async_close(self):
        if self.__session:
            await self.__session.close()
            self.__session = None

    async def __fetch_novel_info(self) -> BeautifulSoup:
        async with self.__session.get(
            f"{self.base_url}/{self.novel_id}",
            headers=headers,
            proxy=self.proxy,
        ) as response:
            return BeautifulSoup(await response.text(), "html.parser")

    async def __fetch_chapters_info(self, chapter: int) -> BeautifulSoup:
        async with self.__session.get(
            f"{self.base_url}/{self.novel_id}/{chapter}",
            headers=headers,
            proxy=self.proxy,
        ) as response:
            return BeautifulSoup(await response.text(), "html.parser")

    def __extract_chapter_numbers(self) -> list[int]:
        numbers: set[int] = set()
        pattern = re.compile(rf"/{re.escape(self.novel_id)}/(\d+)/?$")
        for a_tag in self.__novel_info_soup.find_all("a", href=True):
            href = str(a_tag.get("href") or "")
            matched = pattern.search(href)
            if matched:
                numbers.add(int(matched.group(1)))
        return sorted(numbers)

    async def __get_novel_parts(self) -> dict[NovelTitle, ChapterRange]:
        chapters = {}
        chapter_titles: Tag = self.__novel_info_soup.find_all("div", class_="p-eplist__chapter-title")
        for title in chapter_titles:
            chapter_title: Tag = title.get_text(strip=True)
            chapter_numbers = []
            next_element = title.find_next_sibling()
            while next_element and next_element.name == "div" and "p-eplist__sublist" in next_element.get("class", []):
                a_tag = next_element.find("a", href=True)
                if a_tag:
                    chapter_number = int(a_tag["href"].split("/")[-2])
                    chapter_numbers.append(chapter_number)
                next_element = next_element.find_next_sibling()

            if chapter_numbers:
                chapters[chapter_title] = range(min(chapter_numbers), max(chapter_numbers) + 1)

        if chapters:
            return chapters

        chapter_numbers = self.__extract_chapter_numbers()
        if chapter_numbers:
            return {self.novel_title: range(min(chapter_numbers), max(chapter_numbers) + 1)}

        return {}

    async def get_novel_part_titles(self) -> list[PartTitle]:
        parts = await self.__get_novel_parts()
        return list(parts.keys())

    @deprecated(version="0.1.0", reason="Feeling bad, so use __get_novel_parts instead")
    async def __get_novel_parts2(self) -> dict[NovelTitle, ChapterRange]:
        parts = {}
        start = 1
        current_title = None
        count = 0

        for element in self.__novel_info_soup.find_all(["div", "dl"], class_=["chapter_title", "novel_sublist2"]):
            element: Tag
            if element["class"][0] == "chapter_title":
                if current_title is not None:
                    parts[current_title] = range(start, start + count)
                current_title = element.get_text(strip=True)
                start += count
                count = 0
            elif element["class"][0] == "novel_sublist2":
                count += 1

        if current_title is not None:
            parts[current_title] = range(start, start + count)

        return parts

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
        async with aiofiles.open(f"{file_path}.txt", "a+", encoding="utf-8") as f:
            if self.record_chapter_index:
                await f.write(f"● {title} [総第{chapter_index}話]\n")
            else:
                await f.write(f"● {title}\n")

            await f.write(f"{content}\n")

    async def async_save(self, chapter_index: int, file_path) -> None:
        async with self.__semaphore:
            title, content = await self.__get_chapter_title_content(chapter_index)
            await self.__async_save_txt(title, content, chapter_index, file_path)

    async def async_download(self, output_dir) -> None:
        output_dir = os.path.join(output_dir, self.novel_title)
        print(output_dir)
        if os.path.exists(output_dir):
            shutil.rmtree(output_dir)
        os.makedirs(output_dir, exist_ok=True)
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

        if len(parts) != 0:
            for k, v in parts.items():
                print(f"Start download part: {k}")
                for chapter_index in v:
                    await self.async_save(chapter_index, os.path.join(output_dir, k))
                    downloaded += 1
                    if self.progress_callback:
                        self.progress_callback(downloaded, self.total_chapters)
        else:
            print(f"Start download novel: {self.novel_title}")
            for chapter_index in self.__get_chapters_range():
                await self.async_save(chapter_index, os.path.join(output_dir, self.novel_title))
                downloaded += 1
                if self.progress_callback:
                    self.progress_callback(downloaded, self.total_chapters)


class SaveFormat(Enum):
    TXT = "txt"
    EPUB = "epub"


class SyosuteArgs(BaseModel):
    novel_id: str
    proxy: str
    output_dir: str
    save_format: SaveFormat
    record_chapter_number: bool
