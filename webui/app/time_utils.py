from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any


CN_TZ = timezone(timedelta(hours=8))


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

    return dt.astimezone(CN_TZ).strftime("%Y-%m-%d %H:%M:%S")
