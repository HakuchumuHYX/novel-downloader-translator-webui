from __future__ import annotations

import json
from typing import Iterable


_ENV_TO_SETTING = {
    "BBM_MODEL": "model",
    "BBM_MODEL_LIST": "model_list",
    "BBM_API_BASE": "api_base",
    "BBM_LANGUAGE": "language",
    "BBM_SOURCE_LANG": "source_lang",
    "BBM_TEMPERATURE": "temperature",
    "BBM_PROMPT_FILE": "prompt_file",
    "BBM_PROMPT_TEXT": "prompt_text",
    "BBM_OPENAI_API_KEY": "openai_key",
    "BBM_CLAUDE_API_KEY": "claude_key",
    "BBM_GOOGLE_GEMINI_KEY": "gemini_key",
    "BBM_GROQ_API_KEY": "groq_key",
    "BBM_XAI_API_KEY": "xai_key",
    "BBM_QWEN_API_KEY": "qwen_key",
    "BBM_CAIYUN_API_KEY": "caiyun_key",
    "BBM_DEEPL_API_KEY": "deepl_key",
    "BBM_CUSTOM_API": "custom_api",
    "BBM_TEST": "test",
    "BBM_TEST_NUM": "test_num",
    "BBM_RESUME": "resume",
    "BBM_USE_CONTEXT": "use_context",
    "BBM_CONTEXT_PARAGRAPH_LIMIT": "context_paragraph_limit",
    "BBM_ACCUMULATED_NUM": "accumulated_num",
    "BBM_PARALLEL_WORKERS": "parallel_workers",
    "BBM_BLOCK_SIZE": "block_size",
    "BBM_TRANSLATION_STYLE": "translation_style",
    "BBM_BATCH_SIZE": "batch_size",
    "BBM_INTERVAL": "interval",
    "BBM_PROXY": "proxy",
    "BBM_DEPLOYMENT_ID": "deployment_id",
    "BBM_TRANSLATE_TAGS": "translate_tags",
    "BBM_EXCLUDE_TRANSLATE_TAGS": "exclude_translate_tags",
    "BBM_ALLOW_NAVIGABLE_STRINGS": "allow_navigable_strings",
    "BBM_CHATGPTAPI_SYS_MSG": "prompt_system",
    "BBM_CHATGPTAPI_USER_MSG_TEMPLATE": "prompt_user",
}

# Import-only aliases for compatibility with older or custom .env files.
_IMPORT_ALIASES = {
    "OPENAI_API_KEY": "openai_key",
    "OPENAI_API_SYS_MSG": "prompt_system",
}

_SETTING_TO_ENV = {v: k for k, v in _ENV_TO_SETTING.items()}


def parse_env_text(raw_text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in raw_text.splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        if "=" not in text:
            continue
        key, value = text.split("=", 1)
        key = key.strip()
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        value = value.replace("\\n", "\n")
        result[key] = value
    return result


def import_env_to_settings(raw_text: str) -> dict[str, str]:
    env_data = parse_env_text(raw_text)
    settings: dict[str, str] = {}

    for env_key, setting_key in _ENV_TO_SETTING.items():
        if env_key in env_data:
            settings[setting_key] = env_data[env_key]

    for env_key, setting_key in _IMPORT_ALIASES.items():
        if env_key in env_data and setting_key not in settings:
            settings[setting_key] = env_data[env_key]

    return settings


def export_settings_to_env(settings: dict[str, str], keys: Iterable[str] | None = None) -> str:
    if keys is None:
        keys = _SETTING_TO_ENV.keys()

    lines: list[str] = ["# Exported by webui"]
    for setting_key in keys:
        env_key = _SETTING_TO_ENV.get(setting_key)
        if not env_key:
            continue
        value = str(settings.get(setting_key, ""))
        if value == "" or any(ch in value for ch in (' ', '#', '"', "'", "\n", "\t")):
            value = json.dumps(value, ensure_ascii=False)
        lines.append(f"{env_key}={value}")
    return "\n".join(lines) + "\n"
