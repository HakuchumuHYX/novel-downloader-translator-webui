from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlparse

from .models import SiteId


def detect_site_from_url(url: str) -> SiteId:
    host = (urlparse(url).hostname or "").lower()
    if host == "ncode.syosetu.com":
        return "syosetu"
    if host == "novel18.syosetu.com":
        return "novel18"
    if host == "kakuyomu.jp":
        return "kakuyomu"
    raise ValueError(f"Unsupported site hostname: {host or '<empty>'}")


def normalize_input_url(url: str, novel_id: str, site: SiteId) -> str:
    if url:
        return url.strip()

    nid = novel_id.strip().strip("/")
    if not nid:
        raise ValueError("Either --url or --novel_id is required")

    if site == "novel18":
        return f"https://novel18.syosetu.com/{nid}/"
    if site in ("auto", "syosetu"):
        return f"https://ncode.syosetu.com/{nid}/"

    raise ValueError("--novel_id is only supported for syosetu/novel18")


def sanitize_filename(name: str, default: str = "book") -> str:
    text = (name or "").strip()
    if not text:
        text = default
    text = re.sub(r'[\\/:*?"<>|]+', "_", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:160] or default


def parse_cookie_file(path: Path) -> str:
    raw = path.read_text(encoding="utf-8", errors="ignore")
    cookies = []

    # Netscape cookies format
    for line in raw.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if "\t" in s:
            parts = s.split("\t")
            if len(parts) >= 7:
                cookies.append(f"{parts[5]}={parts[6]}")
            continue
        if "=" in s:
            k, v = s.split("=", 1)
            cookies.append(f"{k.strip()}={v.strip()}")

    dedup = []
    seen = set()
    for item in cookies:
        key = item.split("=", 1)[0]
        if key in seen:
            continue
        seen.add(key)
        dedup.append(item)
    return "; ".join(dedup)


def write_manifest(path: Path, data: dict) -> None:
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
