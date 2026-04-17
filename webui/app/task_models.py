from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .option_registry import (
    DOWNLOADER_BACKENDS,
    PAID_POLICIES,
    SAVE_FORMATS,
    SOURCE_TYPES,
    TASK_MODES,
    TRANSLATE_MODES,
    TRANSLATION_OUTPUT_MODES,
    normalize_parallel_workers,
    normalize_string_map,
)


class TaskPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    mode: Literal["download_only", "download_and_translate"] = "download_and_translate"
    source_type: Literal["upload", "kakuyomu", "syosetu", "syosetu-r18"] = "upload"
    source_input: str = ""
    upload_path: str = ""
    cookie_profile_id: int | None = None
    backend: Literal["auto", "node", "native"] = "auto"
    paid_policy: Literal["skip", "fail", "metadata"] = "skip"
    save_format: Literal["txt", "epub"] = "txt"
    merge_all: bool = True
    merged_name: str = ""
    record_chapter_number: bool = False
    translate_mode: Literal["preview", "full"] = "preview"
    translation_output_mode: Literal["translated_only", "bilingual"] = "translated_only"
    test_num: str = "80"
    process_timeout: str = ""
    settings_overrides: dict[str, str] = Field(default_factory=dict)

    @field_validator("source_input", "upload_path", "merged_name", "test_num", "process_timeout", mode="before")
    @classmethod
    def _strip_strings(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("settings_overrides", mode="before")
    @classmethod
    def _normalize_overrides(cls, value: Any) -> dict[str, str]:
        data = normalize_string_map(value if isinstance(value, dict) else {})
        parallel_workers = data.get("parallel_workers", "")
        if parallel_workers:
            data["parallel_workers"] = normalize_parallel_workers(parallel_workers, fallback="5")
        else:
            data.pop("parallel_workers", None)
        return data

    def to_record(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def validate_task_payload_model(payload: TaskPayload) -> tuple[bool, str]:
    if payload.mode not in TASK_MODES:
        return False, "Unsupported mode"
    if payload.source_type not in SOURCE_TYPES:
        return False, "Unsupported source_type"
    if payload.backend not in DOWNLOADER_BACKENDS:
        return False, "backend must be auto/node/native"
    if payload.paid_policy not in PAID_POLICIES:
        return False, "paid_policy must be skip/fail/metadata"
    if payload.save_format not in SAVE_FORMATS:
        return False, "save_format must be txt or epub"
    if payload.translate_mode not in TRANSLATE_MODES:
        return False, "translate_mode must be preview/full"
    if payload.translation_output_mode not in TRANSLATION_OUTPUT_MODES:
        return False, "translation_output_mode must be translated_only/bilingual"
    if payload.source_type == "upload" and not payload.upload_path:
        return False, "Upload source requires file"
    if payload.source_type != "upload" and not payload.source_input:
        return False, "URL is required"
    if payload.source_type != "upload" and not (
        payload.source_input.startswith("http://") or payload.source_input.startswith("https://")
    ):
        return False, "source_input must be a full URL"
    if payload.source_type == "syosetu-r18" and not payload.cookie_profile_id:
        return False, "syosetu-r18 requires cookie profile"
    return True, ""
