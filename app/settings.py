from __future__ import annotations

import sqlite3

DEFAULT_PROMPT_SYSTEM = (
    "你是一位专业中文小说译者。保持叙事语气、人物称呼和段落结构；"
    "逐行对应输出，保留空行与以 ● 开头的章节标记行；"
    "只输出译文，不添加解释、警告或省略标记。"
)
DEFAULT_PROMPT_USER = "目标语言：{language}\n请逐行对应翻译并保持段落结构：\n\n{text}"

DEFAULTS = {
    "api_base": "",
    "api_key": "",
    "model": "",
    "language": "简体中文",
    "prompt_system": DEFAULT_PROMPT_SYSTEM,
    "prompt_user": DEFAULT_PROMPT_USER,
    "parallel_workers": "5",
    "proxy": "",
    "cookie_syosetu": "",
    "cookie_novel18": "",
    "cookie_kakuyomu": "",
}
# 页面上只显示掩码，提交空值表示不修改
SECRETS = {"api_key", "cookie_syosetu", "cookie_novel18", "cookie_kakuyomu"}
TRANSLATOR_KEYS = ["api_base", "api_key", "model", "language", "prompt_system", "prompt_user", "parallel_workers", "proxy"]


def load(conn: sqlite3.Connection) -> dict[str, str]:
    values = dict(DEFAULTS)
    values.update({row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM settings")})
    return values


def mask(value: str) -> str:
    if not value:
        return ""
    return f"已设置（{value[:4]}…{value[-4:]}）" if len(value) > 12 else "已设置"


def save(conn: sqlite3.Connection, form: dict[str, str]) -> None:
    updates = {}
    for key in DEFAULTS:
        value = form.get(key, "").replace("\r\n", "\n").strip()
        if key in SECRETS:
            if form.get(f"clear_{key}"):
                value = ""
            elif not value:
                continue
        updates[key] = value

    if "{text}" not in updates["prompt_user"]:
        raise ValueError("用户提示词必须包含 {text}")
    if not updates["parallel_workers"].isdigit() or not 1 <= int(updates["parallel_workers"]) <= 32:
        raise ValueError("并发数须为 1–32 的整数")

    conn.executemany(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        updates.items(),
    )
