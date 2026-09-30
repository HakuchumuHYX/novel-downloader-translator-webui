"""OpenAI 兼容接口 + 译文缓存 + 并发调度。

请求里不带 temperature：新模型大多忽略或直接拒绝这个参数。
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import openai
from openai import OpenAI

RETRYABLE = (openai.APIConnectionError, openai.RateLimitError, openai.InternalServerError)


def emit(tag: str, payload) -> None:
    # 与 downloader/common.py 的 emit 相同协议，webui 的 worker 按前缀解析
    print(f"__{tag}__ " + json.dumps(payload, ensure_ascii=False), flush=True)


class Translator:
    def __init__(self, cfg: dict, cache_path: Path) -> None:
        self.client = OpenAI(api_key=cfg["api_key"], base_url=cfg["api_base"] or None, timeout=300, max_retries=0)
        self.model = cfg["model"]
        self.language = cfg["language"]
        self.system = cfg["prompt_system"]
        self.user = cfg["prompt_user"]
        self.workers = max(1, int(cfg["parallel_workers"]))

        # 译文缓存按原文内容寻址：失败重试、试译转全文都直接复用已译部分
        self.cache_path = cache_path
        self.cache: dict[str, str] = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
        self.lock = threading.Lock()

    def _request(self, text: str, extra_system: str) -> str:
        system = f"{self.system}\n\n{extra_system}" if extra_system else self.system
        user = self.user.replace("{language}", self.language).replace("{text}", text)
        error = ""
        for attempt in range(6):
            if attempt:
                wait = min(60, 5 * 2 ** (attempt - 1))
                print(f"请求失败（{error}），{wait}s 后重试", flush=True)
                time.sleep(wait)
            try:
                resp = self.client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                )
            except RETRYABLE as exc:
                error = f"{type(exc).__name__}: {exc}"
                continue
            choice = resp.choices[0]
            content = (choice.message.content or "").strip()
            if not content:
                error = "空回复"
                continue
            if choice.finish_reason == "length":
                print("警告：输出被截断（finish_reason=length），可调小分块或换长输出模型", flush=True)
            return content
        raise RuntimeError(f"翻译失败：{error}")

    def _key(self, text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def _store(self, text: str, result: str) -> None:
        with self.lock:
            self.cache[self._key(text)] = result
            tmp = self.cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.cache, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.cache_path)

    def translate_all(self, texts: list[str], extra_system: str = "") -> list[str]:
        results: list[str | None] = [self.cache.get(self._key(text)) for text in texts]
        pending = [i for i, result in enumerate(results) if result is None]
        done = len(texts) - len(pending)
        print(f"共 {len(texts)} 块，缓存命中 {done} 块，待翻译 {len(pending)} 块", flush=True)
        emit("PROGRESS", {"stage": "translate", "current": done, "total": len(texts)})

        pool = ThreadPoolExecutor(self.workers)
        try:
            futures = {pool.submit(self._request, texts[i], extra_system): i for i in pending}
            for future in as_completed(futures):
                i = futures[future]
                results[i] = future.result()
                self._store(texts[i], results[i])
                done += 1
                print(f"第 {i + 1} 块完成（{len(texts[i])} → {len(results[i])} 字）", flush=True)
                emit("PROGRESS", {"stage": "translate", "current": done, "total": len(texts)})
        finally:
            # 出错时别让排队中的块继续发请求
            pool.shutdown(wait=False, cancel_futures=True)
        return results
