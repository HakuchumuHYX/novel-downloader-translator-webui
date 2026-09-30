from __future__ import annotations

import asyncio
import json
import re
from urllib.parse import urlparse

import aiohttp

SITES = {
    "ncode.syosetu.com": "syosetu",
    "novel18.syosetu.com": "novel18",
    "kakuyomu.jp": "kakuyomu",
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
}


def detect_site(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if host not in SITES:
        raise ValueError(f"不支持的站点：{host or url}")
    return SITES[host]


def parse_cookie(header: str) -> dict[str, str]:
    cookies = {}
    for pair in header.split(";"):
        key, sep, value = pair.strip().partition("=")
        if sep and key.strip():
            cookies[key.strip()] = value.strip()
    return cookies


def sanitize_filename(name: str) -> str:
    text = re.sub(r'[\\/:*?"<>|]+', "_", name)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:120] or "book"


def emit(tag: str, payload) -> None:
    # webui 的 worker 按这两个前缀解析子进程输出
    print(f"__{tag}__ " + json.dumps(payload, ensure_ascii=False), flush=True)


async def get_html(session: aiohttp.ClientSession, url: str, proxy: str | None) -> str:
    # 几百章并发抓取时偶发断连/5xx 很常见，单请求重试比整本重跑便宜
    error = ""
    for attempt in range(4):
        if attempt:
            await asyncio.sleep(2 * 2**attempt)
        try:
            async with session.get(url, proxy=proxy) as resp:
                if resp.status < 400:
                    return await resp.text()
                if resp.status < 500 and resp.status != 429:
                    raise RuntimeError(f"HTTP {resp.status}：{url}")
                error = f"HTTP {resp.status}"
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            error = repr(exc)
    raise RuntimeError(f"请求失败（{error}）：{url}")
