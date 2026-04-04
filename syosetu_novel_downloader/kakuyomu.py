import os
import re
import shutil
from asyncio import Semaphore, as_completed, create_task
from collections.abc import Callable
from urllib.parse import urlparse

import aiofiles
import aiohttp
from bs4 import BeautifulSoup  # type: ignore

from custom_typing import ChapterContent, ChapterTitle, NovelTitle


HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.114 Safari/537.36"
}


class Kakuyomu:
    def __init__(
        self,
        work_url: str,
        proxy: str = "",
        cookie: str = "",
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> None:
        self.work_url = self.__normalize_work_url(work_url)
        self.work_id = self.__extract_work_id(self.work_url)
        self.proxy = proxy
        self.cookie = cookie.strip()
        self.progress_callback = progress_callback
        self.novel_title: NovelTitle = ""
        self.total_chapters = 0

        self.__semaphore = Semaphore(8)
        self.__episode_urls: list[str] = []

    def __normalize_work_url(self, url: str) -> str:
        base = url.strip().split("?", 1)[0].rstrip("/")
        return base

    def __extract_work_id(self, url: str) -> str:
        parsed = urlparse(url)
        matched = re.search(r"/works/([^/]+)$", parsed.path.rstrip("/"))
        if not matched:
            raise ValueError(f"Invalid Kakuyomu work URL: {url}")
        return matched.group(1)

    async def async_init(self) -> None:
        headers = dict(HEADERS)
        if self.cookie:
            headers["Cookie"] = self.cookie

        self.__session: aiohttp.ClientSession = aiohttp.ClientSession(
            headers=headers,
        )

        self.__episode_urls = await self.__collect_episode_urls()
        if not self.novel_title:
            self.novel_title = f"kakuyomu_{self.work_id}"

    async def async_close(self) -> None:
        if self.__session:
            await self.__session.close()
            self.__session = None

    async def __fetch_html(self, url: str) -> str:
        async with self.__session.get(url, proxy=self.proxy) as response:
            return await response.text()

    def __extract_title(self, soup: BeautifulSoup) -> str:
        node = soup.find("h1")
        if node and node.get_text(strip=True):
            return node.get_text(strip=True)

        meta = soup.find("meta", property="og:title")
        if meta and meta.get("content"):
            title = str(meta.get("content")).strip()
            return title.replace(" - カクヨム", "").strip()

        return ""

    async def __collect_episode_urls(self) -> list[str]:
        seen_pages: set[int] = set()
        seen_episodes: set[str] = set()
        episode_urls: list[str] = []
        max_page = 1
        page = 1

        while page <= max_page:
            if page in seen_pages:
                page += 1
                continue
            seen_pages.add(page)

            page_url = self.work_url if page == 1 else f"{self.work_url}?page={page}"
            html = await self.__fetch_html(page_url)
            soup = BeautifulSoup(html, "html.parser")

            if page == 1:
                self.novel_title = self.__extract_title(soup) or self.novel_title

            for a_tag in soup.find_all("a", href=True):
                href = str(a_tag.get("href") or "")
                episode_match = re.search(rf"/works/{re.escape(self.work_id)}/episodes/([^/?#]+)", href)
                if episode_match:
                    abs_url = f"https://kakuyomu.jp/works/{self.work_id}/episodes/{episode_match.group(1)}"
                    if abs_url not in seen_episodes:
                        seen_episodes.add(abs_url)
                        episode_urls.append(abs_url)

                page_match = re.search(r"[?&]page=(\d+)", href)
                if page_match:
                    max_page = max(max_page, int(page_match.group(1)))

            page += 1

        return episode_urls

    async def __get_chapter_title_content(self, episode_url: str) -> tuple[ChapterTitle, ChapterContent]:
        html = await self.__fetch_html(episode_url)
        soup = BeautifulSoup(html, "html.parser")

        title = ""
        title_selectors = [
            "h1#contentMain-header",
            "p.widget-episodeTitle",
            "h1",
        ]
        for selector in title_selectors:
            node = soup.select_one(selector)
            if node and node.get_text(strip=True):
                title = node.get_text(strip=True)
                break
        if not title:
            title = episode_url.rsplit("/", 1)[-1]

        content = ""
        body_selectors = [
            "div.widget-episodeBody",
            "div#contentMain-inner",
            "div.js-episode-body",
            "div[itemprop='articleBody']",
        ]
        for selector in body_selectors:
            node = soup.select_one(selector)
            if node and node.get_text(strip=True):
                content = node.get_text("\n", strip=True)
                break

        if not content:
            raise RuntimeError(f"Could not parse kakuyomu episode body: {episode_url}")

        return title, content

    async def __async_save_txt(self, title: ChapterTitle, content: ChapterContent, chapter_index: int, file_path: str) -> None:
        async with aiofiles.open(f"{file_path}.txt", "a+", encoding="utf-8") as f:
            await f.write(f"● {title} [第{chapter_index}話]\n")
            await f.write(f"{content}\n")

    async def async_fetch(self, chapter_index: int, episode_url: str, file_path: str) -> tuple[int, str, ChapterTitle, ChapterContent]:
        async with self.__semaphore:
            title, content = await self.__get_chapter_title_content(episode_url)
            return chapter_index, file_path, title, content

    async def async_download(self, output_dir: str) -> None:
        output_dir = os.path.join(output_dir, self.novel_title)
        if os.path.exists(output_dir):
            shutil.rmtree(output_dir)
        os.makedirs(output_dir, exist_ok=True)

        self.total_chapters = len(self.__episode_urls)
        downloaded = 0
        if self.progress_callback:
            self.progress_callback(downloaded, self.total_chapters)

        tasks = [
            create_task(self.async_fetch(idx, episode_url, os.path.join(output_dir, self.novel_title)))
            for idx, episode_url in enumerate(self.__episode_urls, start=1)
        ]
        results: list[tuple[int, str, ChapterTitle, ChapterContent]] = []
        for task in as_completed(tasks):
            results.append(await task)
            downloaded += 1
            if self.progress_callback:
                self.progress_callback(downloaded, self.total_chapters)

        for chapter_index, file_path, title, content in sorted(results, key=lambda item: item[0]):
            await self.__async_save_txt(title, content, chapter_index, file_path)
