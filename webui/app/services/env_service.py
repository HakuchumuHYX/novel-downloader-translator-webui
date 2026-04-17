from __future__ import annotations

import json
from typing import Iterable

from ..option_registry import ENV_TO_SETTING, SETTING_TO_ENV

# Import-only aliases for compatibility with older or custom .env files.
_IMPORT_ALIASES = {
    "OPENAI_API_KEY": "openai_key",
    "OPENAI_API_SYS_MSG": "prompt_system",
}


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

    for env_key, setting_key in ENV_TO_SETTING.items():
        if env_key in env_data:
            settings[setting_key] = env_data[env_key]

    for env_key, setting_key in _IMPORT_ALIASES.items():
        if env_key in env_data and setting_key not in settings:
            settings[setting_key] = env_data[env_key]

    return settings


def export_settings_to_env(settings: dict[str, str], keys: Iterable[str] | None = None) -> str:
    if keys is None:
        keys = SETTING_TO_ENV.keys()

    lines: list[str] = ["# Exported by webui"]
    for setting_key in keys:
        env_key = SETTING_TO_ENV.get(setting_key)
        if not env_key:
            continue
        value = str(settings.get(setting_key, ""))
        if value == "" or any(ch in value for ch in (' ', '#', '"', "'", "\n", "\t")):
            value = json.dumps(value, ensure_ascii=False)
        lines.append(f"{env_key}={value}")
    return "\n".join(lines) + "\n"
