from __future__ import annotations

import json
import re
import sys
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


def normalize_input_url(url: str, site: SiteId) -> str:
    normalized = str(url or "").strip()
    if not normalized:
        raise ValueError("--url is required")

    detected_site = detect_site_from_url(normalized)
    if site != "auto" and site != detected_site:
        raise ValueError(f"--site={site} does not match URL host for {detected_site}")
    return normalized


def sanitize_filename(name: str, default: str = "book") -> str:
    text = (name or "").strip()
    if not text:
        text = default
    text = re.sub(r'[\\/:*?"<>|]+', "_", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:160] or default

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
