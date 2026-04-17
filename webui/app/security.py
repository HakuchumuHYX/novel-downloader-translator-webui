from __future__ import annotations

import hmac
import re
from typing import Optional

from cryptography.fernet import Fernet
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from .config import get_config


security = HTTPBasic(auto_error=True)


def _is_valid_fernet_key(key: str) -> bool:
    try:
        Fernet(key.encode("utf-8"))
        return True
    except Exception:
        return False


def get_fernet() -> Fernet:
    cfg = get_config()
    key = cfg.secret_key.strip() if cfg.secret_key else ""
    if key and _is_valid_fernet_key(key):
        return Fernet(key.encode("utf-8"))
    if key:
        raise RuntimeError("Invalid WEBUI_SECRET_KEY format (must be a valid Fernet key)")
    raise RuntimeError("Valid WEBUI_SECRET_KEY is required for encryption/decryption")


def encryption_configured() -> bool:
    key = get_config().secret_key.strip() if get_config().secret_key else ""
    return bool(key) and _is_valid_fernet_key(key)


def encrypt_text(plain_text: str) -> str:
    return get_fernet().encrypt(plain_text.encode("utf-8")).decode("utf-8")


def decrypt_text(cipher_text: str) -> str:
    token = cipher_text.encode("utf-8")
    return get_fernet().decrypt(token).decode("utf-8")


def verify_basic_auth(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    cfg = get_config()
    valid_user = hmac.compare_digest(credentials.username, cfg.basic_auth_user)
    valid_pass = hmac.compare_digest(credentials.password, cfg.basic_auth_password)
    if not (valid_user and valid_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


_LOG_PATTERNS = [
    re.compile(r"(ses=)[^;\s]+", re.IGNORECASE),
    re.compile(r"(dis_session_r=)[^;\s]+", re.IGNORECASE),
    re.compile(r"(pbid=)[^;\s]+", re.IGNORECASE),
    re.compile(r"(ks2=)[^;\s]+", re.IGNORECASE),
    re.compile(r"(over18=)[^;\s]+", re.IGNORECASE),
    re.compile(r"(lineheight=)[^;\s]+", re.IGNORECASE),
    re.compile(r"(OPENAI_API_KEY=)[^\s]+", re.IGNORECASE),
    re.compile(r"(BBM_OPENAI_API_KEY=)[^\s]+", re.IGNORECASE),
    re.compile(r"(claude_key=)[^\s]+", re.IGNORECASE),
    re.compile(r"(gemini_key=)[^\s]+", re.IGNORECASE),
    re.compile(r"(qwen_key=)[^\s]+", re.IGNORECASE),
    re.compile(r"(xai_key=)[^\s]+", re.IGNORECASE),
    re.compile(r"(groq_key=)[^\s]+", re.IGNORECASE),
    re.compile(r"(deepl_key=)[^\s]+", re.IGNORECASE),
    re.compile(r"(caiyun_key=)[^\s]+", re.IGNORECASE),
    re.compile(r"(custom_api=)[^\s]+", re.IGNORECASE),
    re.compile(r"(Cookie:\s*)[^\r\n]+", re.IGNORECASE),
    re.compile(r"(Authorization:\s*Bearer\s+)[^\s]+", re.IGNORECASE),
    re.compile(
        r"((?:[\"'])?(?:api[_-]?key|openai_api_key|bbm_openai_api_key)(?:[\"'])?\s*[:=]\s*(?:[\"'])?)[^\"'\s,;}{]+",
        re.IGNORECASE,
    ),
    re.compile(
        r"((?:[\"'])?(?:authorization|cookie)(?:[\"'])?\s*[:=]\s*(?:[\"'])?)[^\"'\r\n]+",
        re.IGNORECASE,
    ),
]


def sanitize_log(text: Optional[str]) -> str:
    if not text:
        return ""
    redacted = text
    for pattern in _LOG_PATTERNS:
        redacted = pattern.sub(r"\1***", redacted)
    return redacted
