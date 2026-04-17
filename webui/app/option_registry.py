from __future__ import annotations

from dataclasses import dataclass
from typing import Any


TRUE_VALUES = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class SettingDefinition:
    key: str
    default: str
    env_key: str | None = None
    translator_flag: str | None = None
    downloader_flag: str | None = None
    is_secret: bool = False


SETTING_DEFINITIONS: tuple[SettingDefinition, ...] = (
    SettingDefinition("model", "openai", env_key="BBM_MODEL"),
    SettingDefinition("model_list", "", env_key="BBM_MODEL_LIST", translator_flag="--model_list"),
    SettingDefinition("api_base", "", env_key="BBM_API_BASE", translator_flag="--api_base"),
    SettingDefinition("language", "zh-hans", env_key="BBM_LANGUAGE"),
    SettingDefinition("source_lang", "auto", env_key="BBM_SOURCE_LANG", translator_flag="--source_lang"),
    SettingDefinition("temperature", "1.0", env_key="BBM_TEMPERATURE", translator_flag="--temperature"),
    SettingDefinition("prompt_file", "", env_key="BBM_PROMPT_FILE"),
    SettingDefinition("prompt_text", "", env_key="BBM_PROMPT_TEXT"),
    SettingDefinition("prompt_system", "", env_key="BBM_PROMPT_SYSTEM"),
    SettingDefinition("prompt_user", "", env_key="BBM_PROMPT_USER"),
    SettingDefinition("test", "true", env_key="BBM_TEST"),
    SettingDefinition("test_num", "80", env_key="BBM_TEST_NUM"),
    SettingDefinition("resume", "false", env_key="BBM_RESUME"),
    SettingDefinition("accumulated_num", "1", env_key="BBM_ACCUMULATED_NUM", translator_flag="--accumulated_num"),
    SettingDefinition(
        "parallel_workers",
        "5",
        env_key="BBM_PARALLEL_WORKERS",
        translator_flag="--parallel-workers",
    ),
    SettingDefinition("use_context", "false", env_key="BBM_USE_CONTEXT"),
    SettingDefinition(
        "context_paragraph_limit",
        "0",
        env_key="BBM_CONTEXT_PARAGRAPH_LIMIT",
        translator_flag="--context_paragraph_limit",
    ),
    SettingDefinition("block_size", "-1", env_key="BBM_BLOCK_SIZE", translator_flag="--block_size"),
    SettingDefinition(
        "translation_style",
        "",
        env_key="BBM_TRANSLATION_STYLE",
        translator_flag="--translation_style",
    ),
    SettingDefinition("batch_size", "", env_key="BBM_BATCH_SIZE", translator_flag="--batch_size"),
    SettingDefinition("translate_tags", "p", env_key="BBM_TRANSLATE_TAGS", translator_flag="--translate-tags"),
    SettingDefinition(
        "exclude_translate_tags",
        "sup",
        env_key="BBM_EXCLUDE_TRANSLATE_TAGS",
        translator_flag="--exclude_translate-tags",
    ),
    SettingDefinition(
        "allow_navigable_strings",
        "false",
        env_key="BBM_ALLOW_NAVIGABLE_STRINGS",
    ),
    SettingDefinition("interval", "0.01", env_key="BBM_INTERVAL", translator_flag="--interval"),
    SettingDefinition("deployment_id", "", env_key="BBM_DEPLOYMENT_ID", translator_flag="--deployment_id"),
    SettingDefinition("proxy", "", env_key="BBM_PROXY", translator_flag="--proxy", downloader_flag="--proxy"),
    SettingDefinition("timeout", "240", downloader_flag="--timeout"),
    SettingDefinition("retries", "2", downloader_flag="--retries"),
    SettingDefinition("rate_limit", "1.0", downloader_flag="--rate-limit"),
    SettingDefinition("backend", "auto"),
    SettingDefinition("paid_policy", "skip"),
    SettingDefinition("save_format", "txt"),
    SettingDefinition("merge_all", "true"),
    SettingDefinition("merged_name", ""),
    SettingDefinition("record_chapter_number", "false"),
    SettingDefinition("cleanup_days", "14"),
    SettingDefinition("cleanup_statuses", "succeeded,failed,canceled"),
    SettingDefinition("process_timeout", "7200"),
    SettingDefinition("openai_key", "", env_key="BBM_OPENAI_API_KEY", translator_flag="--openai_key", is_secret=True),
    SettingDefinition("claude_key", "", env_key="BBM_CLAUDE_API_KEY", translator_flag="--claude_key", is_secret=True),
    SettingDefinition(
        "gemini_key",
        "",
        env_key="BBM_GOOGLE_GEMINI_KEY",
        translator_flag="--gemini_key",
        is_secret=True,
    ),
    SettingDefinition("groq_key", "", env_key="BBM_GROQ_API_KEY", translator_flag="--groq_key", is_secret=True),
    SettingDefinition("xai_key", "", env_key="BBM_XAI_API_KEY", translator_flag="--xai_key", is_secret=True),
    SettingDefinition("qwen_key", "", env_key="BBM_QWEN_API_KEY", translator_flag="--qwen_key", is_secret=True),
    SettingDefinition(
        "caiyun_key",
        "",
        env_key="BBM_CAIYUN_API_KEY",
        translator_flag="--caiyun_key",
        is_secret=True,
    ),
    SettingDefinition("deepl_key", "", env_key="BBM_DEEPL_API_KEY", translator_flag="--deepl_key", is_secret=True),
    SettingDefinition("custom_api", "", env_key="BBM_CUSTOM_API", translator_flag="--custom_api", is_secret=True),
)


SETTING_DEFINITION_MAP = {item.key: item for item in SETTING_DEFINITIONS}
DEFAULT_SETTINGS: dict[str, str] = {item.key: item.default for item in SETTING_DEFINITIONS}
SECRET_SETTING_KEYS = {item.key for item in SETTING_DEFINITIONS if item.is_secret}
ENV_TO_SETTING = {item.env_key: item.key for item in SETTING_DEFINITIONS if item.env_key}
SETTING_TO_ENV = {setting_key: env_key for env_key, setting_key in ENV_TO_SETTING.items()}
TRANSLATOR_CLI_OPTIONS = tuple(
    (item.key, item.translator_flag) for item in SETTING_DEFINITIONS if item.translator_flag
)
DOWNLOADER_CLI_OPTIONS = tuple(
    (item.key, item.downloader_flag) for item in SETTING_DEFINITIONS if item.downloader_flag
)
SOURCE_TYPES = {"upload", "kakuyomu", "syosetu", "syosetu-r18"}
TRANSLATE_MODES = {"preview", "full"}
TASK_MODES = {"download_only", "download_and_translate"}
TRANSLATION_OUTPUT_MODES = {"translated_only", "bilingual"}
DOWNLOADER_BACKENDS = {"auto", "node", "native"}
SAVE_FORMATS = {"txt", "epub"}
PAID_POLICIES = {"skip", "fail", "metadata"}


def parse_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in TRUE_VALUES


def normalize_string_map(values: dict[str, Any] | None) -> dict[str, str]:
    if not values:
        return {}
    return {
        str(key).strip(): str(value).strip()
        for key, value in values.items()
        if str(key).strip()
    }


def normalize_parallel_workers(value: Any, fallback: str = "5") -> str:
    try:
        number = int(str(value).strip())
    except Exception:
        return fallback
    if number < 1:
        return fallback
    return str(number)
