from __future__ import annotations

from importlib import import_module


class LazyTranslatorRegistry(dict):
    def _resolve(self, key):
        value = dict.__getitem__(self, key)
        if not isinstance(value, tuple):
            return value
        module_name, attr_name = value
        translator = getattr(import_module(module_name), attr_name)
        dict.__setitem__(self, key, translator)
        return translator

    def __getitem__(self, key):
        return self._resolve(key)

    def get(self, key, default=None):
        if key not in self:
            return default
        return self._resolve(key)

    def items(self):
        for key in dict.keys(self):
            yield key, self._resolve(key)

    def values(self):
        for key in dict.keys(self):
            yield self._resolve(key)


MODEL_DICT = LazyTranslatorRegistry(
    {
        "openai": ("book_maker.translator.chatgptapi_translator", "ChatGPTAPI"),
        "google": ("book_maker.translator.google_translator", "Google"),
        "caiyun": ("book_maker.translator.caiyun_translator", "Caiyun"),
        "deepl": ("book_maker.translator.deepl_translator", "DeepL"),
        "deepl_free": ("book_maker.translator.deepl_free_translator", "DeepLFree"),
        "claude": ("book_maker.translator.claude_translator", "Claude"),
        "claude-sonnet-4-6": ("book_maker.translator.claude_translator", "Claude"),
        "claude-opus-4-6": ("book_maker.translator.claude_translator", "Claude"),
        "claude-opus-4-5-20251101": ("book_maker.translator.claude_translator", "Claude"),
        "claude-haiku-4-5-20251001": ("book_maker.translator.claude_translator", "Claude"),
        "claude-sonnet-4-5-20250929": ("book_maker.translator.claude_translator", "Claude"),
        "claude-opus-4-1-20250805": ("book_maker.translator.claude_translator", "Claude"),
        "claude-opus-4-20250514": ("book_maker.translator.claude_translator", "Claude"),
        "claude-sonnet-4-20250514": ("book_maker.translator.claude_translator", "Claude"),
        "gemini": ("book_maker.translator.gemini_translator", "Gemini"),
        "groq": ("book_maker.translator.groq_translator", "GroqClient"),
        "tencent_transmart": ("book_maker.translator.tencent_transmart_translator", "TencentTranSmart"),
        "custom_api": ("book_maker.translator.custom_api_translator", "CustomAPI"),
        "xai": ("book_maker.translator.xai_translator", "XAIClient"),
        "qwen": ("book_maker.translator.qwen_translator", "QwenTranslator"),
        "qwen-mt-turbo": ("book_maker.translator.qwen_translator", "QwenTranslator"),
        "qwen-mt-plus": ("book_maker.translator.qwen_translator", "QwenTranslator"),
    }
)
