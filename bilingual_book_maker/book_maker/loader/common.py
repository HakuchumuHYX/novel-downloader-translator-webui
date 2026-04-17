from __future__ import annotations

import builtins
import json
import os
from pathlib import Path

from book_maker.utils import prompt_config_to_kwargs


def emit_progress(stage: str, current: int, total: int, unit: str) -> None:
    payload = {
        "stage": str(stage),
        "current": int(current),
        "total": int(total),
        "unit": str(unit),
    }
    builtins.print("__WEBUI_PROGRESS__ " + json.dumps(payload, ensure_ascii=False), flush=True)


def create_translator(
    model,
    key,
    language,
    *,
    model_api_base=None,
    prompt_config=None,
    temperature=1.0,
    source_lang="auto",
):
    return model(
        key,
        language,
        api_base=model_api_base,
        temperature=temperature,
        source_lang=source_lang,
        **prompt_config_to_kwargs(prompt_config),
    )


def save_text_output(book_path: str | Path, content: list[str], *, joiner: str = "\n") -> None:
    try:
        with open(book_path, "w", encoding="utf-8") as handle:
            handle.write(joiner.join(content))
    except Exception as exc:
        raise Exception("can not save file") from exc


def save_resume_entries(
    path: str | Path,
    entries: list[str],
    *,
    mode: str = "json",
    delimiter: str = "\n",
    atomic: bool = False,
) -> None:
    target = Path(path)
    try:
        if atomic:
            temp_path = target.with_suffix(target.suffix + ".tmp")
            _write_resume(temp_path, entries, mode=mode, delimiter=delimiter)
            temp_path.replace(target)
            return
        _write_resume(target, entries, mode=mode, delimiter=delimiter)
    except Exception as exc:
        raise Exception("can not save resume file") from exc


def _write_resume(path: Path, entries: list[str], *, mode: str, delimiter: str) -> None:
    if mode == "json":
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"version": 2, "p_to_save": entries}, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        return
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(delimiter.join(entries))


def load_resume_entries(path: str | Path, *, mode: str = "json", delimiter: str = "\n") -> list[str]:
    try:
        raw = Path(path).read_text(encoding="utf-8")
        if mode == "json":
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                return raw.splitlines()
            if isinstance(data, dict) and isinstance(data.get("p_to_save"), list):
                return [str(item) for item in data.get("p_to_save", [])]
            if isinstance(data, list):
                return [str(item) for item in data]
            raise ValueError("invalid resume json format")
        if not raw:
            return []
        return raw.split(delimiter)
    except Exception as exc:
        raise Exception("can not load resume file") from exc
