"""カクヨム。作品页 https://kakuyomu.jp/works/<id>。"""
from __future__ import annotations

import json
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from .common import get_html

TITLE_SELECTORS = ["h1#contentMain-header", "p.widget-episodeTitle", "h1"]
BODY_SELECTORS = [
    "div.widget-episodeBody",
    "div#contentMain-inner",
    "div.js-episode-body",
    "div[itemprop='articleBody']",
]


async def fetch_index(session, url: str, proxy: str | None) -> tuple[str, list[tuple[str, str]]]:
    matched = re.search(r"/works/([^/?#]+)", urlparse(url).path)
    if not matched:
        raise ValueError(f"不是カクヨム作品链接：{url}")
    work_id = matched.group(1)
    work_url = f"https://kakuyomu.jp/works/{work_id}"

    # 作品页是 Next.js 渲染的，HTML 里的目录会折叠（180 话只露出 7 个链接），
    # 完整目录在 __NEXT_DATA__ 的 Apollo state 里：Work → tableOfContentsV2 → episodeUnions
    html = await get_html(session, work_url, proxy)
    data = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.S)
    if not data:
        raise RuntimeError("作品页里找不到 __NEXT_DATA__，カクヨム页面结构可能变了")
    state = json.loads(data.group(1))["props"]["pageProps"]["__APOLLO_STATE__"]
    work = state[f"Work:{work_id}"]
    episode_ids = [
        state[episode["__ref"]]["id"]
        for toc in work["tableOfContentsV2"]
        for episode in state[toc["__ref"]]["episodeUnions"]
    ]
    return work["title"], [(eid, f"{work_url}/episodes/{eid}") for eid in episode_ids]


async def fetch_chapter(session, url: str, proxy: str | None) -> tuple[str, str]:
    soup = BeautifulSoup(await get_html(session, url, proxy), "html.parser")
    title = next(
        (node.get_text(strip=True) for sel in TITLE_SELECTORS if (node := soup.select_one(sel)) and node.get_text(strip=True)),
        url.rsplit("/", 1)[-1],
    )
    content = next(
        (node.get_text("\n", strip=True) for sel in BODY_SELECTORS if (node := soup.select_one(sel)) and node.get_text(strip=True)),
        "",
    )
    if not content:
        raise RuntimeError(f"解析章节失败：{url}")
    return title, content
