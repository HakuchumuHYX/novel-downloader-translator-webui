from pathlib import Path

from app.config import get_config
from app.security import decrypt_text, encrypt_text, encryption_configured, sanitize_log
from app.services.cookie_service import cookie_header_from_json_text, infer_site_from_json_text
from app.services.env_service import export_settings_to_env, import_env_to_settings
from app.services.preview_service import preview_text_file
from app.services.settings_service import DEFAULT_SETTINGS, merged_settings, validate_task_payload


def test_settings_override_priority():
    base = dict(DEFAULT_SETTINGS)
    final = merged_settings(base, {"model": "gpt4o", "language": "ja"})
    assert final["model"] == "gpt4o"
    assert final["language"] == "ja"


def test_validate_r18_requires_cookie():
    payload = {
        "source_type": "syosetu-r18",
        "source_input": "https://novel18.syosetu.com/n2954di/",
        "cookie_profile_id": None,
        "save_format": "txt",
        "paid_policy": "skip",
        "upload_path": "",
    }
    result = validate_task_payload(payload)
    assert result.ok is False


def test_validate_upload_requires_file():
    payload = {
        "source_type": "upload",
        "source_input": "",
        "cookie_profile_id": None,
        "save_format": "txt",
        "paid_policy": "skip",
        "upload_path": "",
    }
    result = validate_task_payload(payload)
    assert result.ok is False


def test_log_sanitize():
    raw = (
        "ses=abcdef dis_session_r=12345 OPENAI_API_KEY=secret "
        "Authorization: Bearer token123 "
        "{\"api_key\":\"abcxyz\"} "
        "Cookie: ses=abcdef; pbid=hello"
    )
    clean = sanitize_log(raw)
    assert "abcdef" not in clean
    assert "12345" not in clean
    assert "secret" not in clean
    assert "token123" not in clean
    assert "abcxyz" not in clean
    assert "hello" not in clean


def test_invalid_secret_key_fallback(monkeypatch):
    monkeypatch.setenv("WEBUI_SECRET_KEY", "test")
    monkeypatch.setenv("WEBUI_REQUIRE_SECRET_KEY", "false")
    get_config.cache_clear()

    assert encryption_configured() is False
    cipher = encrypt_text("hello")
    assert decrypt_text(cipher) == "hello"

    get_config.cache_clear()


def test_preview_pagination(tmp_path: Path):
    f = tmp_path / "a.txt"
    f.write_text("\n".join([f"line{i}" for i in range(260)]), encoding="utf-8")
    page = preview_text_file(f, page=2, per_page=100)
    assert page.page == 2
    assert page.total_pages == 3
    assert page.lines[0] == "line100"


def test_cookie_json_parse_and_infer_site():
    raw = """[
      {"domain":".kakuyomu.jp","name":"dis_session_r","value":"abc"},
      {"domain":".kakuyomu.jp","name":"pbid","value":"def"}
    ]"""
    header = cookie_header_from_json_text(raw)
    assert "dis_session_r=abc" in header
    assert "pbid=def" in header
    assert infer_site_from_json_text(raw) == "kakuyomu"


def test_import_env_settings_with_aliases():
    raw = """
    BBM_MODEL=openai
    BBM_LANGUAGE=zh-hans
    OPENAI_API_KEY=legacy_openai_key
    OPENAI_API_SYS_MSG='legacy system'
    """
    mapped = import_env_to_settings(raw)
    assert mapped["model"] == "openai"
    assert mapped["language"] == "zh-hans"
    assert mapped["openai_key"] == "legacy_openai_key"
    assert mapped["prompt_system"] == "legacy system"


def test_export_env_settings_uses_bbm_keys():
    settings = dict(DEFAULT_SETTINGS)
    settings["model"] = "openai"
    settings["prompt_system"] = "sys"
    settings["prompt_user"] = "user"

    exported = export_settings_to_env(settings)
    assert "BBM_MODEL=openai" in exported
    assert "BBM_CHATGPTAPI_SYS_MSG=sys" in exported
    assert "BBM_CHATGPTAPI_USER_MSG_TEMPLATE=user" in exported
