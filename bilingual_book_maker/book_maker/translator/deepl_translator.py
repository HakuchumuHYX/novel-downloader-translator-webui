import json
import time

import requests
import re

from book_maker.utils import LANGUAGES, TO_LANGUAGE_CODE

from .base_translator import Base
from rich import print

REQUEST_TIMEOUT = 30
MAX_RETRIES = 3


class DeepL(Base):
    """
    DeepL translator
    """

    def __init__(self, key, language, **kwargs) -> None:
        super().__init__(key, language)
        self.api_url = "https://dpl-translator.p.rapidapi.com/translate"
        self.headers = {
            "content-type": "application/json",
            "X-RapidAPI-Key": "",
            "X-RapidAPI-Host": "dpl-translator.p.rapidapi.com",
        }
        l = language if language in LANGUAGES else TO_LANGUAGE_CODE.get(language)
        if l not in [
            "bg",
            "zh",
            "cs",
            "da",
            "nl",
            "en-US",
            "en-GB",
            "et",
            "fi",
            "fr",
            "de",
            "el",
            "hu",
            "id",
            "it",
            "ja",
            "lv",
            "lt",
            "pl",
            "pt-PT",
            "pt-BR",
            "ro",
            "ru",
            "sk",
            "sl",
            "es",
            "sv",
            "tr",
            "uk",
            "ko",
            "nb",
        ]:
            raise Exception(f"DeepL do not support {l}")
        self.language = l

    def rotate_key(self):
        self.headers["X-RapidAPI-Key"] = f"{next(self.keys)}"

    def translate(self, text):
        self.rotate_key()
        print(text)
        payload = {"text": text, "source": "EN", "target": self.language}
        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                response = requests.request(
                    "POST",
                    self.api_url,
                    data=json.dumps(payload),
                    headers=self.headers,
                    timeout=REQUEST_TIMEOUT,
                )
                if response.status_code in {429, 500, 502, 503, 504}:
                    raise requests.HTTPError(f"HTTP {response.status_code}: {response.text}", response=response)
                response.raise_for_status()
                t_text = response.json().get("text", "")
                break
            except Exception as exc:
                last_error = exc
                if attempt == MAX_RETRIES - 1:
                    raise
                print(exc)
                time.sleep(2 ** attempt)
        else:
            raise last_error or RuntimeError("DeepL request failed")
        print("[bold green]" + re.sub("\n{3,}", "\n\n", t_text) + "[/bold green]")
        return t_text
