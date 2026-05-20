from .base_translator import Base
import re
import json
import requests
import time
from rich import print

REQUEST_TIMEOUT = 10
MAX_RETRIES = 3


class CustomAPI(Base):
    """
    Custom API translator
    """

    def __init__(self, custom_api, language, **kwargs) -> None:
        super().__init__(custom_api, language)
        self.language = language
        self.custom_api = custom_api

    def rotate_key(self):
        pass

    def translate(self, text):
        print(text)
        custom_api = self.custom_api
        data = {"text": text, "source_lang": "auto", "target_lang": self.language}
        post_data = json.dumps(data)
        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                response = requests.post(url=custom_api, data=post_data, timeout=REQUEST_TIMEOUT)
                if response.status_code in {429, 500, 502, 503, 504}:
                    raise requests.HTTPError(f"HTTP {response.status_code}: {response.text}", response=response)
                response.raise_for_status()
                t_text = response.json()["data"]
                break
            except Exception as exc:
                last_error = exc
                if attempt == MAX_RETRIES - 1:
                    raise
                time.sleep(2 ** attempt)
        else:
            raise last_error or RuntimeError("Custom API request failed")
        print("[bold green]" + re.sub("\n{3,}", "\n\n", t_text) + "[/bold green]")
        time.sleep(5)
        return t_text
