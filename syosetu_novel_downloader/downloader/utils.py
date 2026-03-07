from __future__ import annotations

import json
import re
import sys
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
    cookies: list[tuple[str, str]] = []

    # Accept both:
    # 1) Netscape cookie format lines
    # 2) Header-style "a=b; c=d" (single or multi-line)
    for line in raw.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue

        if "\t" in s:
            parts = s.split("\t")
            if len(parts) >= 7:
                name = parts[5].strip()
                value = parts[6].strip()
                if name:
                    cookies.append((name, value))
            continue

        # Header-like or loose key-value syntax in the file
        # e.g. "a=b; c=d", or one cookie per line "a=b"
        for seg in s.split(";"):
            part = seg.strip()
            if not part or "=" not in part:
                continue
            k, v = part.split("=", 1)
            key = k.strip()
            if not key:
                continue
            cookies.append((key, v.strip()))

    dedup: list[str] = []
    seen = set()
    for key, value in cookies:
        if key in seen:
            continue
        seen.add(key)
        dedup.append(f"{key}={value}")
    return "; ".join(dedup)


def emit_progress(stage: str, current: int, total: int, unit: str) -> None:
    payload = {
        "stage": str(stage),
        "current": int(current),
        "total": int(total),
        "unit": str(unit),
    }
    print("__WEBUI_PROGRESS__ " + json.dumps(payload, ensure_ascii=False), flush=True)
    try:
        sys.stdout.flush()
    except Exception:
        pass


def write_manifest(path: Path, data: dict) -> None:
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
