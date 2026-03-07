from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from ..db import utcnow_iso
from ..security import decrypt_text, encrypt_text


SECRET_SETTING_KEYS = {
    "openai_key",
    "claude_key",
    "gemini_key",
    "groq_key",
    "xai_key",
    "qwen_key",
    "caiyun_key",
    "deepl_key",
    "custom_api",
}


DEFAULT_SETTINGS: dict[str, str] = {
    "model": "openai",
    "model_list": "gpt-5.2",
    "api_base": "",
    "language": "zh-hans",
    "source_lang": "auto",
    "temperature": "1.0",
    "prompt_file": "",
    "prompt_text": "",
    "prompt_system": "",
    "prompt_user": "",
    "test": "true",
    "test_num": "80",
    "resume": "false",
    "accumulated_num": "1",
    "parallel_workers": "5",
    "use_context": "false",
    "context_paragraph_limit": "0",
    "block_size": "-1",
    "translation_style": "",
    "batch_size": "",
    "translate_tags": "p",
    "exclude_translate_tags": "sup",
    "allow_navigable_strings": "false",
    "interval": "0.01",
    "deployment_id": "",
    "proxy": "",
    "timeout": "240",
    "retries": "2",
    "rate_limit": "1.0",
    "backend": "auto",
    "paid_policy": "skip",
    "save_format": "txt",
    "merge_all": "true",
    "merged_name": "",
    "record_chapter_number": "false",
    "cleanup_days": "14",
    "cleanup_statuses": "succeeded,failed,canceled",
    "process_timeout": "7200",
    "openai_key": "",
    "claude_key": "",
    "gemini_key": "",
    "groq_key": "",
    "xai_key": "",
    "qwen_key": "",
    "caiyun_key": "",
    "deepl_key": "",
    "custom_api": "",
}


SOURCE_TYPES = {"upload", "kakuyomu", "syosetu", "syosetu-r18"}


@dataclass
class ValidationResult:
    ok: bool
    message: str = ""


def _row_to_setting_value(row: sqlite3.Row) -> str:
    raw_value = row["value"]
    if int(row["is_secret"]) == 1:
        try:
            return decrypt_text(raw_value)
        except Exception:
            return ""
    return raw_value


def load_settings(conn: sqlite3.Connection) -> dict[str, str]:
    data = dict(DEFAULT_SETTINGS)
    rows = conn.execute("SELECT key, value, is_secret FROM settings").fetchall()
    for row in rows:
        data[row["key"]] = _row_to_setting_value(row)
    return data


def save_settings(conn: sqlite3.Connection, incoming: dict[str, str]) -> None:
    current = load_settings(conn)
    now = utcnow_iso()

    for key, value in incoming.items():
        if key not in DEFAULT_SETTINGS:
            continue

        value = (value or "").strip()
        if key in SECRET_SETTING_KEYS and value == "":
            value = current.get(key, "")

        is_secret = 1 if key in SECRET_SETTING_KEYS else 0
        stored = encrypt_text(value) if is_secret else value

        conn.execute(
            """
            INSERT INTO settings(key, value, is_secret, updated_at)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                is_secret = excluded.is_secret,
                updated_at = excluded.updated_at
            """,
            (key, stored, is_secret, now),
        )


def merged_settings(base: dict[str, str], overrides: dict[str, str] | None) -> dict[str, str]:
    final = dict(base)
    if not overrides:
        return final

    for key, value in overrides.items():
        if key in DEFAULT_SETTINGS and str(value).strip() != "":
            final[key] = str(value).strip()
    return final


def mask_for_display(settings: dict[str, str]) -> dict[str, str]:
    masked = dict(settings)
    for key in SECRET_SETTING_KEYS:
        value = masked.get(key, "")
        if value:
            masked[key] = "***"
    return masked


def validate_task_payload(payload: dict[str, Any]) -> ValidationResult:
    source_type = payload.get("source_type", "")
    if source_type not in SOURCE_TYPES:
        return ValidationResult(False, "Unsupported source_type")

    if source_type == "upload":
        if not payload.get("upload_path"):
            return ValidationResult(False, "Upload source requires file")
    else:
        if not str(payload.get("source_input", "")).strip():
            return ValidationResult(False, "URL or novel id is required")

    if source_type == "syosetu-r18" and not payload.get("cookie_profile_id"):
        return ValidationResult(False, "syosetu-r18 requires cookie profile")

    output_format = payload.get("save_format", "txt")
    if output_format not in {"txt", "epub"}:
        return ValidationResult(False, "save_format must be txt or epub")

    paid_policy = payload.get("paid_policy", "skip")
    if paid_policy not in {"skip", "fail", "metadata"}:
        return ValidationResult(False, "paid_policy must be skip/fail/metadata")

    backend = payload.get("backend", "auto")
    if backend not in {"auto", "node", "native"}:
        return ValidationResult(False, "backend must be auto/node/native")

    translation_output_mode = payload.get("translation_output_mode", "translated_only")
    if translation_output_mode not in {"translated_only", "bilingual"}:
        return ValidationResult(False, "translation_output_mode must be translated_only/bilingual")

    return ValidationResult(True)


def validate_translation_settings(settings: dict[str, str]) -> ValidationResult:
    model = str(settings.get("model", "")).strip()
    model_list = str(settings.get("model_list", "")).strip()
    deployment_id = str(settings.get("deployment_id", "")).strip()
    api_base = str(settings.get("api_base", "")).strip()
    interval = str(settings.get("interval", "")).strip()

    if model == "openai" and not model_list:
        return ValidationResult(False, "model=openai requires model_list")

    if deployment_id and not api_base:
        return ValidationResult(False, "deployment_id requires api_base")

    if interval:
        try:
            if float(interval) < 0:
                return ValidationResult(False, "interval must be >= 0")
        except ValueError:
            return ValidationResult(False, "interval must be a number")

    return ValidationResult(True)


def load_task_payload(row: sqlite3.Row) -> dict[str, Any]:
    return json.loads(row["payload_json"])
