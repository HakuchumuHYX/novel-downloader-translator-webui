from __future__ import annotations

import json
from typing import Any


def _cookie_pairs_from_obj(obj: Any) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    if isinstance(obj, list):
        for item in obj:
            if isinstance(item, dict):
                name = str(item.get("name", "")).strip()
                value = str(item.get("value", "")).strip()
                if name:
                    pairs.append((name, value))
        return pairs

    if isinstance(obj, dict):
        if isinstance(obj.get("cookies"), list):
            return _cookie_pairs_from_obj(obj["cookies"])
        for k, v in obj.items():
            if isinstance(v, (str, int, float, bool)):
                pairs.append((str(k).strip(), str(v).strip()))
        return pairs

    return pairs


def cookie_pairs_from_json_text(raw_text: str) -> list[tuple[str, str]]:
    obj = json.loads(raw_text)
    pairs = _cookie_pairs_from_obj(obj)
    dedup: list[tuple[str, str]] = []
    seen = set()
    for key, value in pairs:
        if not key or key in seen:
            continue
        seen.add(key)
        dedup.append((key, value))
    if not dedup:
        raise ValueError("No cookie pairs found in JSON")
    return dedup


def cookie_header_from_json_text(raw_text: str) -> str:
    pairs = cookie_pairs_from_json_text(raw_text)
    return "; ".join(f"{k}={v}" for k, v in pairs)


def infer_site_from_json_text(raw_text: str) -> str:
    try:
        obj = json.loads(raw_text)
    except Exception:
        return ""

    domains: list[str] = []
    names: list[str] = []

    if isinstance(obj, list):
        for item in obj:
            if not isinstance(item, dict):
                continue
            domains.append(str(item.get("domain", "")).lower())
            names.append(str(item.get("name", "")).lower())
    elif isinstance(obj, dict) and isinstance(obj.get("cookies"), list):
        for item in obj["cookies"]:
            if not isinstance(item, dict):
                continue
            domains.append(str(item.get("domain", "")).lower())
            names.append(str(item.get("name", "")).lower())

    merged = " ".join(domains)
    if "kakuyomu.jp" in merged:
        return "kakuyomu"
    if "novel18.syosetu.com" in merged:
        return "novel18"
    if "ncode.syosetu.com" in merged:
        return "syosetu"

    if "syosetu.com" in merged:
        if "over18" in names:
            return "novel18"
        return "syosetu"

    return ""
