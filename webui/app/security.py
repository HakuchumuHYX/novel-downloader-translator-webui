from __future__ import annotations

import base64
import hashlib
import hmac
import re
from typing import Optional

from cryptography.fernet import Fernet
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from .config import get_config


security = HTTPBasic(auto_error=True)


def _fallback_key_material() -> str:
    digest = hashlib.sha256(b"webui-insecure-fallback-key").digest()
    return base64.urlsafe_b64encode(digest).decode("ascii")


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
    if key and cfg.require_secret_key:
        raise RuntimeError("Invalid WEBUI_SECRET_KEY format (must be a valid Fernet key)")
    fallback = _fallback_key_material()
    return Fernet(fallback.encode("utf-8"))


def encryption_configured() -> bool:
    key = get_config().secret_key.strip() if get_config().secret_key else ""
    return bool(key) and _is_valid_fernet_key(key)


def encrypt_text(plain_text: str) -> str:
    return get_fernet().encrypt(plain_text.encode("utf-8")).decode("utf-8")


def _get_fallback_fernet() -> Fernet:
    fallback = _fallback_key_material()
    return Fernet(fallback.encode("ascii"))


def decrypt_text(cipher_text: str) -> str:
    """
    Decrypt text with best-effort backward compatibility.

    If WEBUI_SECRET_KEY is configured, we decrypt with that key first.
    If it fails (e.g. data was encrypted earlier using the fallback key),
    we try the fallback key as a second chance to avoid "locking" users out
    of previously saved secrets after configuring WEBUI_SECRET_KEY.
    """
    token = cipher_text.encode("utf-8")
    fernet = get_fernet()
    try:
        return fernet.decrypt(token).decode("utf-8")
    except Exception:
        # If a valid secret key is configured, data may have been created
        # before the key existed (fallback). Try fallback for migration.
        if encryption_configured():
            return _get_fallback_fernet().decrypt(token).decode("utf-8")
        raise


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
