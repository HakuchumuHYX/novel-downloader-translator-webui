from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from typing import Any

from ..db import utcnow_iso
from ..option_registry import DEFAULT_SETTINGS, SECRET_SETTING_KEYS
from ..security import decrypt_text, encrypt_text, encryption_configured
from ..task_models import TaskPayload, validate_task_payload_model
logger = logging.getLogger(__name__)


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
            logger.warning("Failed to decrypt secret setting '%s'; treating it as empty.", row["key"])
            return ""
    return raw_value


def load_settings(conn: sqlite3.Connection) -> dict[str, str]:
    data = dict(DEFAULT_SETTINGS)
    rows = conn.execute("SELECT key, value, is_secret FROM settings").fetchall()
    for row in rows:
        data[row["key"]] = _row_to_setting_value(row)
    return data


def save_settings(
    conn: sqlite3.Connection,
    incoming: dict[str, str],
    *,
    clear_keys: set[str] | None = None,
) -> None:
    current = load_settings(conn)
    now = utcnow_iso()
    clear_keys = {key for key in (clear_keys or set()) if key in SECRET_SETTING_KEYS}

    for key, value in incoming.items():
        if key not in DEFAULT_SETTINGS:
            continue

        if key in clear_keys:
            conn.execute("DELETE FROM settings WHERE key = ?", (key,))
            continue

        value = (value or "").strip()
        if key in SECRET_SETTING_KEYS and value == "":
            value = current.get(key, "")

        is_secret = 1 if key in SECRET_SETTING_KEYS else 0
        if is_secret and value and not encryption_configured():
            raise RuntimeError("WEBUI_SECRET_KEY 未配置：默认禁止保存 API 密钥等敏感设置。请先配置有效密钥。")
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
    try:
        model = TaskPayload.model_validate(payload)
    except Exception as exc:
        return ValidationResult(False, str(exc))

    ok, message = validate_task_payload_model(model)
    return ValidationResult(ok, message)


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
    payload = json.loads(row["payload_json"])
    return TaskPayload.model_validate(payload).to_record()
