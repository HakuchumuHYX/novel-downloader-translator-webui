from __future__ import annotations

import json
from pathlib import Path

from ..config import AppConfig
from ..option_registry import DOWNLOADER_CLI_OPTIONS, TRANSLATOR_CLI_OPTIONS, parse_bool
from ..task_models import TaskPayload


def build_downloader_command(
    cfg: AppConfig,
    payload: TaskPayload,
    settings: dict[str, str],
    download_root: Path,
    *,
    cookie_header: str = "",
) -> tuple[list[str], bool]:
    site_map = {
        "syosetu": "syosetu",
        "syosetu-r18": "novel18",
        "kakuyomu": "kakuyomu",
    }
    site = site_map[payload.source_type]
    save_format = payload.save_format or settings.get("save_format", "txt")
    command = [
        cfg.downloader_python,
        str(cfg.downloader_entry),
        "--site",
        site,
        "--backend",
        payload.backend or settings.get("backend", "auto"),
        "--paid-policy",
        payload.paid_policy or settings.get("paid_policy", "skip"),
        "--save-format",
        save_format,
        "--output-dir",
        str(download_root),
        "--merged-name",
        payload.merged_name or settings.get("merged_name", ""),
    ]

    for key, flag in DOWNLOADER_CLI_OPTIONS:
        if key in {"proxy", "backend", "paid_policy", "save_format", "merged_name"}:
            continue
        value = settings.get(key, "")
        if value != "":
            command.extend([flag, value])

    merge_all_enabled = payload.merge_all if payload.merge_all is not None else parse_bool(
        settings.get("merge_all", "true"),
        default=True,
    )
    translating_task = payload.mode == "download_and_translate"
    effective_merge_all = merge_all_enabled or translating_task
    if effective_merge_all:
        command.append("--merge-all")

    if payload.record_chapter_number:
        command.append("--record-chapter-number")

    source_input = payload.source_input.strip()
    if source_input.startswith("http://") or source_input.startswith("https://"):
        command.extend(["--url", source_input])
    else:
        command.extend(["--novel_id", source_input])

    if cookie_header:
        command.extend(["--cookie", cookie_header])

    return command, translating_task and not merge_all_enabled


def build_translator_command(
    cfg: AppConfig,
    source_path: Path,
    payload: TaskPayload,
    settings: dict[str, str],
    *,
    force_resume: bool = False,
    has_resume_state: bool = False,
) -> list[str]:
    command = [
        cfg.translator_python,
        str(cfg.translator_entry),
        "--book_name",
        str(source_path),
        "--model",
        settings.get("model", "openai"),
        "--language",
        settings.get("language", "zh-hans"),
    ]

    for key, flag in TRANSLATOR_CLI_OPTIONS:
        if key in {
            "model",
            "language",
            "prompt_file",
            "prompt_text",
            "prompt_system",
            "prompt_user",
            "use_context",
            "resume",
            "allow_navigable_strings",
            "test",
            "test_num",
        }:
            continue
        value = settings.get(key, "")
        if value != "":
            command.extend([flag, value])

    prompt_file = settings.get("prompt_file", "")
    prompt_text = settings.get("prompt_text", "")
    prompt_system = settings.get("prompt_system", "")
    prompt_user = settings.get("prompt_user", "")
    if prompt_file:
        command.extend(["--prompt", prompt_file])
    elif prompt_text:
        command.extend(["--prompt", prompt_text])
    elif prompt_user:
        prompt_payload = {"user": prompt_user}
        if prompt_system:
            prompt_payload["system"] = prompt_system
        command.extend(["--prompt", json.dumps(prompt_payload, ensure_ascii=False)])

    if parse_bool(settings.get("use_context", "false"), default=False):
        command.append("--use_context")

    if (force_resume or parse_bool(settings.get("resume", "false"), default=False)) and has_resume_state:
        command.append("--resume")

    if parse_bool(settings.get("allow_navigable_strings", "false"), default=False):
        command.append("--allow_navigable_strings")

    if payload.translate_mode == "preview":
        command.append("--test")
        command.extend(["--test_num", str(payload.test_num or settings.get("test_num", "80"))])

    if payload.translation_output_mode == "translated_only":
        command.append("--single_translate")

    return command
