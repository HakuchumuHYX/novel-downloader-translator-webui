import re
import time
import uuid
import requests

from rich import print
from .base_translator import Base

REQUEST_TIMEOUT = 10
MAX_RETRIES = 3


class TencentTranSmart(Base):
    """
    Tencent TranSmart translator
    """

    def __init__(self, key, language, **kwargs) -> None:
        super().__init__(key, language)
        self.api_url = "https://transmart.qq.com/api/imt"
        self.header = {
            "authority": "transmart.qq.com",
            "content-type": "application/json",
            "origin": "https://transmart.qq.com",
            "referer": "https://transmart.qq.com/zh-CN/index",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        }
        self.uuid = str(uuid.uuid4())
        self.session = requests.Session()
        self.translate_type = "zh"
        if self.language == "english":
            self.translate_type = "en"

    def rotate_key(self):
        pass

    def _post(self, payload):
        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                response = self.session.post(
                    self.api_url,
                    json=payload,
                    headers=self.header,
                    timeout=REQUEST_TIMEOUT,
                )
                if response.status_code in {429, 500, 502, 503, 504}:
                    raise requests.HTTPError(f"HTTP {response.status_code}: {response.text}", response=response)
                response.raise_for_status()
                return response
            except Exception as exc:
                last_error = exc
                if attempt == MAX_RETRIES - 1:
                    raise
                time.sleep(2 ** attempt)
        raise last_error or RuntimeError("Tencent TranSmart request failed")

    def translate(self, text):
        print(text)
        source_language, text_list = self.text_analysis(text)
        client_key = self.get_client_key()
        api_form_data = {
            "header": {
                "fn": "auto_translation",
                "client_key": client_key,
            },
            "type": "plain",
            "model_category": "normal",
            "source": {
                "lang": source_language,
                "text_list": [""] + text_list + [""],
            },
            "target": {"lang": self.translate_type},
        }

        response = self._post(api_form_data)
        t_text = "".join(response.json()["auto_translation"])
        print("[bold green]" + re.sub("\n{3,}", "\n\n", t_text) + "[/bold green]")
        return t_text

    def text_analysis(self, text):
        client_key = self.get_client_key()
        self.header.update({"Cookie": "TSMT_CLIENT_KEY={}".format(client_key)})
        analysis_request_data = {
            "header": {
                "fn": "text_analysis",
                "session": "",
                "client_key": client_key,
                "user": "",
            },
            "text": text,
            "type": "plain",
            "normalize": {"merge_broken_line": "false"},
        }
        r = self._post(analysis_request_data)
        if not r.ok:
            return "auto", [text]
        response_json_data = r.json()
        text_list = [item["tgt_str"] for item in response_json_data["sentence_list"]]
        language = response_json_data["language"]
        return language, text_list

    def get_client_key(self):
        return "browser-chrome-121.0.0-Windows_10-{}-{}".format(
            self.uuid, int(time.time() * 1e3)
        )
