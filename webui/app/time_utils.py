from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo


DEFAULT_DISPLAY_TZ = "Asia/Shanghai"


def display_timezone():
    name = os.getenv("WEBUI_DISPLAY_TZ", DEFAULT_DISPLAY_TZ).strip() or DEFAULT_DISPLAY_TZ
    try:
        return ZoneInfo(name)
    except Exception:
        return timezone.utc


def format_local_timestamp(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""

    try:
        normalized = text.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
    except Exception:
        return text

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(display_timezone()).strftime("%Y-%m-%d %H:%M:%S")
