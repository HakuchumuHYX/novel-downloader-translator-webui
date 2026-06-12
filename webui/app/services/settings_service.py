from __future__ import annotations

import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from typing import Any

from ..db import utcnow_iso
from ..option_registry import (
    DEFAULT_SETTINGS,
    DOWNLOADER_BACKENDS,
    ENV_TO_SETTING,
    PAID_POLICIES,
    SAVE_FORMATS,
    SECRET_SETTING_KEYS,
)
from ..security import decrypt_text, encrypt_text, encryption_configured
from ..task_models import TaskPayload, validate_task_payload_model
logger = logging.getLogger(__name__)


FALLBACK_MODEL_OPTIONS = {
    "openai",
    "claude",
    "claude-sonnet-4-6",
    "claude-opus-4-6",
    "claude-opus-4-5-20251101",
    "claude-haiku-4-5-20251001",
    "claude-sonnet-4-5-20250929",
    "claude-opus-4-1-20250805",
    "claude-opus-4-20250514",
    "claude-sonnet-4-20250514",
    "gemini",
    "google",
    "groq",
    "xai",
    "qwen",
    "qwen-mt-plus",
    "qwen-mt-turbo",
    "caiyun",
    "deepl",
    "deepl_free",
    "custom_api",
    "tencent_transmart",
}

NOVEL_PROMPT_SYSTEM = (
    "你是一位专业中文小说译者。保持叙事语气、人物称呼和段落结构；"
    "逐行对应输出，保留空行与以 ● 开头的章节标记行；"
    "只输出译文，不添加解释、警告或省略标记。"
)
NOVEL_PROMPT_USER = "目标语言：{language}\n请逐行对应翻译并保持段落结构：\n\n{text}"
NOVEL_FORMAT_GUARD = "逐行对应输出，保留空行与以 ● 开头的章节标记行。"


@dataclass
class ValidationResult:
    ok: bool
    message: str = ""


def supported_model_names() -> set[str]:
    try:
        from book_maker.translator import MODEL_DICT
    except ModuleNotFoundError:
        logger.warning("book_maker is not importable in the WebUI process; using fallback model registry.")
        return set(FALLBACK_MODEL_OPTIONS)
    return {str(key) for key in MODEL_DICT.keys()}


def _row_to_setting_value(row: sqlite3.Row) -> str:
    raw_value = row["value"]
    if int(row["is_secret"]) == 1:
        try:
            return decrypt_text(raw_value)
        except Exception:
            logger.warning("Failed to decrypt secret setting '%s'; treating it as empty.", row["key"])
            return ""
    return raw_value


def merge_env_defaults(settings: dict[str, str]) -> dict[str, str]:
    merged = dict(settings)
    for env_key, setting_key in ENV_TO_SETTING.items():
        if setting_key in SECRET_SETTING_KEYS:
            continue
        env_value = os.getenv(env_key, "").strip()
        if not env_value:
            continue
        if setting_key not in merged or str(merged.get(setting_key, "")).strip() == "":
            merged[setting_key] = env_value.replace("\\n", "\n")
    return merged


def build_prompt_config_from_settings(settings: dict[str, str]) -> dict[str, str]:
    system = str(settings.get("prompt_system", "")).strip()
    user = str(settings.get("prompt_user", "")).strip()
    glossary = str(settings.get("glossary", "")).strip()

    system = system or NOVEL_PROMPT_SYSTEM
    if "逐行对应" not in system or "● " not in system:
        system = system.rstrip() + "\n\n" + NOVEL_FORMAT_GUARD
    if glossary:
        system = system.rstrip() + "\n\n术语表：\n" + glossary
    if not user:
        user = NOVEL_PROMPT_USER
    return {"system": system, "user": user}


def load_settings(conn: sqlite3.Connection) -> dict[str, str]:
    data = dict(DEFAULT_SETTINGS)
    data.update(merge_env_defaults({}))
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


def _validate_number(
    settings: dict[str, str],
    key: str,
    *,
    integer: bool = False,
    min_value: float | int | None = None,
) -> str:
    value = str(settings.get(key, "")).strip()
    if value == "":
        return ""
    try:
        number = int(value) if integer else float(value)
    except ValueError:
        return f"{key} must be {'an integer' if integer else 'a number'}"
    if min_value is not None and number < min_value:
        return f"{key} must be >= {min_value}"
    return ""


def validate_translation_settings(settings: dict[str, str]) -> ValidationResult:
    deployment_id = str(settings.get("deployment_id", "")).strip()
    api_base = str(settings.get("api_base", "")).strip()
    model = str(settings.get("model", "")).strip()
    backend = str(settings.get("backend", "")).strip()
    save_format = str(settings.get("save_format", "")).strip()
    paid_policy = str(settings.get("paid_policy", "")).strip()
    prompt_user = str(settings.get("prompt_user", "")).strip()

    if model not in supported_model_names():
        return ValidationResult(False, "model is not supported")
    if backend not in DOWNLOADER_BACKENDS:
        return ValidationResult(False, "backend must be auto/node/native")
    if save_format not in SAVE_FORMATS:
        return ValidationResult(False, "save_format must be txt or epub")
    if paid_policy not in PAID_POLICIES:
        return ValidationResult(False, "paid_policy must be skip/fail/metadata")

    if deployment_id and not api_base:
        return ValidationResult(False, "deployment_id requires api_base")
    if deployment_id and model != "openai":
        return ValidationResult(False, "deployment_id only supports model=openai")
    if prompt_user and "{text}" not in prompt_user:
        return ValidationResult(False, "prompt_user must contain {text}")

    checks = (
        ("interval", False, 0),
        ("timeout", True, 1),
        ("retries", True, 0),
        ("rate_limit", False, 0),
        ("cleanup_days", True, 0),
        ("process_timeout", True, 60),
        ("temperature", False, 0),
        ("test_num", True, 1),
        ("accumulated_num", True, 1),
        ("context_paragraph_limit", True, 0),
        ("block_size", True, -1),
        ("batch_size", True, 1),
    )
    for key, integer, min_value in checks:
        message = _validate_number(settings, key, integer=integer, min_value=min_value)
        if message:
            return ValidationResult(False, message)

    return ValidationResult(True)


def validate_settings_update(current: dict[str, str], incoming: dict[str, str]) -> ValidationResult:
    candidate = {**current, **{key: value for key, value in incoming.items() if key in DEFAULT_SETTINGS}}
    return validate_translation_settings(candidate)


def validate_translation_request(settings: dict[str, str], task_payload: dict[str, Any]) -> ValidationResult:
    base = validate_translation_settings(settings)
    if not base.ok:
        return base

    try:
        block_size = int(str(settings.get("block_size", "-1")).strip() or "-1")
    except ValueError:
        return ValidationResult(False, "block_size must be an integer")

    if block_size > 0 and task_payload.get("translation_output_mode") != "translated_only":
        return ValidationResult(False, "block_size > 0 requires translation_output_mode=translated_only")
    return ValidationResult(True)


def load_task_payload(row: sqlite3.Row) -> dict[str, Any]:
    payload = json.loads(row["payload_json"])
    return TaskPayload.model_validate(payload).to_record()
