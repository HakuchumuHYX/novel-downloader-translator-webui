"""小説家になろう / ノクターン（novel18）。novel18 需要登录 Cookie + over18=yes。"""
from __future__ import annotations

import asyncio
import re
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

from .common import get_html


async def fetch_index(session, url: str, proxy: str | None) -> tuple[str, list[tuple[str, str]]]:
    parsed = urlparse(url)
    base = f"https://{parsed.hostname}"
    novel_id = next(seg for seg in parsed.path.split("/") if seg).lower()
    index_url = f"{base}/{novel_id}/"

    first = BeautifulSoup(await get_html(session, index_url, proxy), "html.parser")
    title_node = first.find("h1", class_="p-novel__title")
    if title_node is None:
        raise RuntimeError("找不到作品标题，novel18 请检查 Cookie 是否有效")
    title = title_node.get_text(strip=True)

    # 目录分页：?p=2, ?p=3 ...
    max_page = 1
    for a in first.find_all("a", href=True):
        for page in parse_qs(urlparse(a["href"]).query).get("p", []):
            if page.isdigit():
                max_page = max(max_page, int(page))
    rest = await asyncio.gather(
        *(get_html(session, f"{index_url}?p={page}", proxy) for page in range(2, max_page + 1))
    )
    soups = [first, *(BeautifulSoup(html, "html.parser") for html in rest)]

    pattern = re.compile(rf"/{re.escape(novel_id)}/(\d+)/?$")
    numbers = sorted(
        {int(m.group(1)) for soup in soups for a in soup.find_all("a", href=True) if (m := pattern.search(a["href"]))}
    )
    if not numbers:
        # 短篇没有目录，正文直接在作品首页
        return title, [("1", index_url)]
    return title, [(str(n), f"{base}/{novel_id}/{n}/") for n in numbers]


async def fetch_chapter(session, url: str, proxy: str | None) -> tuple[str, str]:
    soup = BeautifulSoup(await get_html(session, url, proxy), "html.parser")
    title_node = soup.find("h1", class_="p-novel__title")
    body_node = soup.find("div", class_="p-novel__body")
    if title_node is None or body_node is None:
        raise RuntimeError(f"解析章节失败：{url}")
    title = title_node.get_text().replace("　", " ").strip()
    # 全角空格是段首缩进，去掉后交给译文自己排版
    content = body_node.get_text().replace("　", "").strip()
    return title, content
