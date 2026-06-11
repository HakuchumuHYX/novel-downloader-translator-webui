from __future__ import annotations

from typing import Any

from fastapi import UploadFile

from ..option_registry import normalize_parallel_workers, parse_bool
from ..task_models import TaskPayload


def preview_limit_help_text() -> str:
    return "预览数量单位：TXT=行，MD=行，SRT=行，PDF=行，EPUB=段。"


def _optional_positive_int(value: Any, field_name: str) -> int | None:
    text = str(value or "").strip()
    if not text or text == "0":
        return None
    try:
        number = int(text)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an integer") from exc
    if number < 1:
        raise ValueError(f"{field_name} must be >= 1")
    return number


def _form_value(form: Any, key: str, default: Any = "") -> Any:
    if hasattr(form, "getlist"):
        values = [item for item in form.getlist(key) if not isinstance(item, UploadFile)]
        if values:
            return values[-1]
    return form.get(key, default)


def _non_empty_form_value(form: Any, key: str, default: Any = "") -> Any:
    value = _form_value(form, key, default)
    if str(value or "").strip() == "":
        return default
    return value


def build_task_payload(form: Any, template_payload: dict[str, Any], upload_path: str) -> TaskPayload:
    settings_overrides: dict[str, str] = dict(template_payload.get("settings_overrides", {}))
    for key, value in form.items():
        if isinstance(value, UploadFile):
            continue
        if key.startswith("override__"):
            text = str(value).strip()
            if text != "":
                settings_overrides[key.replace("override__", "", 1)] = text

    parallel_workers_override = settings_overrides.get("parallel_workers", "")
    if parallel_workers_override:
        settings_overrides["parallel_workers"] = normalize_parallel_workers(parallel_workers_override, fallback="5")
    else:
        settings_overrides.pop("parallel_workers", None)

    payload = TaskPayload.model_validate(
        {
            "mode": str(_non_empty_form_value(form, "mode", template_payload.get("mode", "download_and_translate"))).strip(),
            "source_type": str(_non_empty_form_value(form, "source_type", template_payload.get("source_type", "upload"))).strip(),
            "source_input": str(_non_empty_form_value(form, "source_input", template_payload.get("source_input", ""))).strip(),
            "upload_path": upload_path or template_payload.get("upload_path", ""),
            "cookie_profile_id": _optional_positive_int(
                _form_value(form, "cookie_profile_id", template_payload.get("cookie_profile_id", "0")),
                "cookie_profile_id",
            ),
            "backend": str(_non_empty_form_value(form, "backend", template_payload.get("backend", "auto"))).strip(),
            "paid_policy": str(_non_empty_form_value(form, "paid_policy", template_payload.get("paid_policy", "skip"))).strip(),
            "save_format": str(_non_empty_form_value(form, "save_format", template_payload.get("save_format", "txt"))).strip(),
            "merge_all": parse_bool(_form_value(form, "merge_all", template_payload.get("merge_all", "true")), default=True),
            "merged_name": str(_non_empty_form_value(form, "merged_name", template_payload.get("merged_name", ""))).strip(),
            "record_chapter_number": parse_bool(
                _form_value(form, "record_chapter_number", template_payload.get("record_chapter_number", "false")),
                default=False,
            ),
            "translate_mode": str(_non_empty_form_value(form, "translate_mode", template_payload.get("translate_mode", "preview"))).strip(),
            "translation_output_mode": str(
                _non_empty_form_value(
                    form,
                    "translation_output_mode",
                    template_payload.get("translation_output_mode", "translated_only"),
                )
            ).strip(),
            "test_num": str(_non_empty_form_value(form, "test_num", template_payload.get("test_num", "80"))).strip(),
            "process_timeout": str(_non_empty_form_value(form, "process_timeout", template_payload.get("process_timeout", ""))).strip(),
            "settings_overrides": settings_overrides,
        }
    )

    if payload.source_type == "upload":
        payload.source_input = ""

    return payload


def task_parallel_workers(task_row: Any, base_settings: dict[str, str] | None = None) -> str:
    fallback = normalize_parallel_workers((base_settings or {}).get("parallel_workers", "5"), fallback="5")
    try:
        payload = TaskPayload.model_validate_json(task_row["payload_json"])
    except Exception:
        return fallback
    return normalize_parallel_workers(payload.settings_overrides.get("parallel_workers", ""), fallback=fallback)
