import ast
import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from app.config import get_config
from app.config import AppConfig, _normalize_translator_entry
from app.db import _repair_legacy_settings
from app.main import create_app
from app.option_registry import DEFAULT_SETTINGS as REGISTRY_DEFAULT_SETTINGS, ENV_TO_SETTING, SETTING_TO_ENV
from app.security import decrypt_text, encrypt_text, encryption_configured, sanitize_log
from app.services.command_builder import build_downloader_command, build_translator_command
from app.services.cookie_service import cookie_header_from_json_text, infer_site_from_json_text
from app.services.env_service import export_settings_to_env, import_env_to_settings
from app.services.preview_service import preview_text_file
from app.services.settings_service import DEFAULT_SETTINGS, load_settings, merged_settings, save_settings, validate_task_payload, validate_translation_settings
from app.services.worker_command_service import classify_worker_error, redact_command
from app.services.worker_file_service import artifact_kind, list_source_candidates, resolve_source_file, resolve_translated_file
from app.services.task_management_service import delete_task_records, row_to_dict
from app.services.task_payload_service import build_task_payload
from app.services.task_service import set_task_finished
from app.task_models import TaskPayload
from book_maker.loader.epub_resume import load_resume_state, save_resume_state
from book_maker.loader.epub_support import count_translatable_nodes
from book_maker.translator.openai_translator import OpenAITranslator
from book_maker.translator.qwen_translator import QwenTranslator
from cli_support import build_download_options
from downloader.utils import extract_syosetu_novel_id, is_content_txt, sanitize_filename

VALID_FERNET_KEY = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="


def make_test_config() -> AppConfig:
    return AppConfig(
        base_dir=Path("/tmp/webui"),
        data_dir=Path("/tmp/data"),
        db_path=Path("/tmp/data/webui.sqlite3"),
        task_root=Path("/tmp/data/tasks"),
        upload_root=Path("/tmp/data/uploads"),
        worker_interval_seconds=1.0,
        cleanup_days=14,
        app_env="dev",
        enforce_secure_defaults=False,
        basic_auth_user="user",
        basic_auth_password="pass",
        secret_key="",
        require_secret_key=False,
        process_timeout_seconds=7200,
        stop_grace_seconds=8,
        max_upload_bytes=104857600,
        downloader_python="python",
        downloader_entry=Path("/tmp/downloader.py"),
        translator_python="python",
        translator_entry=Path("/tmp/bookmaker"),
    )


def test_settings_override_priority():
    base = dict(DEFAULT_SETTINGS)
    final = merged_settings(base, {"model": "openai", "language": "ja"})
    assert final["model"] == "openai"
    assert final["language"] == "ja"


def test_registry_defaults_match_settings_defaults():
    assert DEFAULT_SETTINGS == REGISTRY_DEFAULT_SETTINGS
    assert ENV_TO_SETTING["BBM_PARALLEL_WORKERS"] == "parallel_workers"
    assert SETTING_TO_ENV["parallel_workers"] == "BBM_PARALLEL_WORKERS"


def test_bookmaker_registries_are_lazy():
    import book_maker.loader as loader_registry
    import book_maker.translator as translator_registry

    assert "md" in loader_registry.BOOK_LOADER_DICT
    assert "openai" in translator_registry.MODEL_DICT


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


def test_validate_translation_settings_rejects_bad_numbers():
    settings = dict(DEFAULT_SETTINGS)
    settings["process_timeout"] = "ten"
    result = validate_translation_settings(settings)
    assert result.ok is False
    assert "process_timeout" in result.message

    settings["process_timeout"] = "59"
    result = validate_translation_settings(settings)
    assert result.ok is False
    assert ">= 60" in result.message


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


def test_invalid_secret_key_disables_encryption(monkeypatch):
    monkeypatch.setenv("WEBUI_SECRET_KEY", "test")
    monkeypatch.setenv("WEBUI_REQUIRE_SECRET_KEY", "false")
    get_config.cache_clear()

    assert encryption_configured() is False
    try:
        encrypt_text("hello")
    except RuntimeError as exc:
        assert "WEBUI_SECRET_KEY" in str(exc)
    else:
        raise AssertionError("encrypt_text should require a valid WEBUI_SECRET_KEY")

    get_config.cache_clear()


def test_preview_pagination(tmp_path: Path):
    f = tmp_path / "a.txt"
    f.write_text("\n".join([f"line{i}" for i in range(260)]), encoding="utf-8")
    page = preview_text_file(f, page=2, per_page=100)
    assert page.page == 2
    assert page.total_pages == 3
    assert page.lines[0] == "line100"


def test_preview_page_clamps_to_last_page(tmp_path: Path):
    f = tmp_path / "a.txt"
    f.write_text("\n".join([f"line{i}" for i in range(260)]), encoding="utf-8")
    page = preview_text_file(f, page=99, per_page=100)
    assert page.page == 3
    assert page.total_pages == 3
    assert page.lines[0] == "line200"
    assert page.lines[-1] == "line259"


def test_cookie_json_parse_and_infer_site():
    raw = """[
      {"domain":".kakuyomu.jp","name":"dis_session_r","value":"abc"},
      {"domain":".kakuyomu.jp","name":"pbid","value":"def"}
    ]"""
    header = cookie_header_from_json_text(raw)
    assert "dis_session_r=abc" in header
    assert "pbid=def" in header
    assert infer_site_from_json_text(raw) == "kakuyomu"


def test_build_download_options_uses_cookie_env_fallback(monkeypatch):
    monkeypatch.setenv("DOWNLOADER_COOKIE", "ses=from-env")
    args = argparse.Namespace(
        url="https://novel18.syosetu.com/n2954di/",
        site="auto",
        backend="native",
        proxy="",
        output_dir="/tmp/downloads",
        save_format="txt",
        record_chapter_number=False,
        merge_all=False,
        merged_name="",
        cookie="",
        cookie_file="",
        paid_policy="skip",
        rate_limit=1.0,
        retries=3,
        timeout=60,
    )

    options = build_download_options(args)
    assert options.cookie == "ses=from-env"


def test_import_env_settings_uses_current_keys():
    raw = """
    BBM_MODEL=openai
    BBM_LANGUAGE=zh-hans
    BBM_OPENAI_API_KEY=current_openai_key
    BBM_PROMPT_SYSTEM='current system'
    """
    mapped = import_env_to_settings(raw)
    assert mapped["model"] == "openai"
    assert mapped["language"] == "zh-hans"
    assert mapped["openai_key"] == "current_openai_key"
    assert mapped["prompt_system"] == "current system"


def test_export_env_settings_uses_bbm_keys():
    settings = dict(DEFAULT_SETTINGS)
    settings["model"] = "openai"
    settings["prompt_system"] = "sys"
    settings["prompt_user"] = "user"

    exported = export_settings_to_env(settings)
    assert "BBM_MODEL=openai" in exported
    assert "BBM_PROMPT_SYSTEM=sys" in exported
    assert "BBM_PROMPT_USER=user" in exported


def test_build_task_payload_normalizes_parallel_workers():
    payload = build_task_payload(
        {
            "mode": "download_and_translate",
            "source_type": "upload",
            "override__parallel_workers": "0",
            "merge_all": "true",
        },
        {},
        "/tmp/demo.txt",
    )
    assert payload.source_input == ""
    assert payload.upload_path == "/tmp/demo.txt"
    assert payload.settings_overrides["parallel_workers"] == "5"


def test_build_task_payload_unchecked_merge_all_false():
    payload = build_task_payload(
        {
            "mode": "download_only",
            "source_type": "syosetu",
            "source_input": "https://ncode.syosetu.com/n1234ab/",
            "merge_all": "false",
        },
        {"merge_all": True},
        "",
    )
    assert payload.merge_all is False


def test_new_task_template_submits_false_for_unchecked_checkboxes():
    template = Path("/opt/translator_webui/webui/app/templates/new_task.html").read_text(encoding="utf-8")
    assert 'type="hidden" name="merge_all" value="false"' in template
    assert 'type="hidden" name="record_chapter_number" value="false"' in template


def test_build_task_payload_empty_override_keeps_template_value():
    payload = build_task_payload(
        {
            "mode": "download_and_translate",
            "source_type": "upload",
            "override__proxy": "",
            "merge_all": "true",
        },
        {"settings_overrides": {"proxy": "http://127.0.0.1:7890"}},
        "/tmp/book.txt",
    )
    assert payload.settings_overrides["proxy"] == "http://127.0.0.1:7890"


def test_normalize_translator_entry_accepts_legacy_script_path():
    project_root = Path("/opt/translator_webui")
    normalized = _normalize_translator_entry("/app/bilingual_book_maker/make_book.py", project_root)
    assert normalized == Path("/app/bilingual_book_maker")


def test_downloader_url_and_content_txt_helpers():
    assert extract_syosetu_novel_id("https://ncode.syosetu.com/n1234ab/1/?p=2") == "n1234ab"
    assert extract_syosetu_novel_id("https://ncode.syosetu.com/n1234ab/?p=2") == "n1234ab"
    assert is_content_txt("001.txt") is True
    assert is_content_txt("README.txt") is False
    assert is_content_txt("metadata.txt") is False
    assert sanitize_filename("a/b:c<d>") == "a_b_c_d_"


def test_init_db_does_not_override_user_parallel_workers():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            is_secret INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO settings(key, value, is_secret, updated_at) VALUES(?, ?, 0, ?)",
        ("parallel_workers", "1", "2026-03-06T04:05:45.451433+00:00"),
    )
    conn.execute(
        "INSERT INTO settings(key, value, is_secret, updated_at) VALUES(?, ?, 0, ?)",
        ("accumulated_num", "1", "2026-03-06T04:05:45.451433+00:00"),
    )

    _repair_legacy_settings(conn)

    row = conn.execute("SELECT value FROM settings WHERE key = 'parallel_workers'").fetchone()
    assert row["value"] == "1"


def test_qwen_base_model_uses_qwen_key(monkeypatch):
    from book_maker.cli_support import resolve_api_key

    monkeypatch.setenv("BBM_QWEN_API_KEY", "qwen-secret")
    args = argparse.Namespace(
        model="qwen",
        openai_key="",
        caiyun_key="",
        deepl_key="",
        claude_key="",
        custom_api="",
        gemini_key="",
        groq_key="",
        xai_key="",
        qwen_key="",
    )
    assert resolve_api_key(args) == "qwen-secret"


def test_build_translator_command_uses_prompt_and_resume():
    cfg = AppConfig(
        base_dir=Path("/tmp/webui"),
        data_dir=Path("/tmp/data"),
        db_path=Path("/tmp/data/webui.sqlite3"),
        task_root=Path("/tmp/data/tasks"),
        upload_root=Path("/tmp/data/uploads"),
        worker_interval_seconds=1.0,
        cleanup_days=14,
        app_env="dev",
        enforce_secure_defaults=False,
        basic_auth_user="user",
        basic_auth_password="pass",
        secret_key="",
        require_secret_key=False,
        process_timeout_seconds=7200,
        stop_grace_seconds=8,
        max_upload_bytes=104857600,
        downloader_python="python",
        downloader_entry=Path("/tmp/downloader.py"),
        translator_python="python",
        translator_entry=Path("/tmp/bookmaker"),
    )
    payload = TaskPayload(
        translate_mode="preview",
        translation_output_mode="translated_only",
        test_num="12",
    )
    settings = dict(DEFAULT_SETTINGS)
    settings["prompt_system"] = "sys"
    settings["prompt_user"] = "user {text}"
    settings["use_context"] = "true"
    settings["resume"] = "true"
    settings["model_list"] = ""

    command, extra_env = build_translator_command(
        cfg,
        Path("/tmp/book.txt"),
        payload,
        settings,
        force_resume=False,
        has_resume_state=True,
    )
    assert extra_env == {}
    assert "--prompt" in command
    assert "--resume" in command
    assert "--use_context" in command
    assert "--test" in command
    assert "--single_translate" in command
    assert command[:3] == ["python", "-m", "book_maker"]
    assert "--model_list" in command
    assert "gpt-5.2" in command


def test_build_translator_command_puts_secret_settings_in_env():
    command, extra_env = build_translator_command(
        make_test_config(),
        Path("/tmp/book.txt"),
        TaskPayload(),
        {**DEFAULT_SETTINGS, "model": "openai", "openai_key": "sk-test-secret"},
    )

    assert "--openai_key" not in command
    assert "sk-test-secret" not in command
    assert extra_env["BBM_OPENAI_API_KEY"] == "sk-test-secret"
    assert extra_env["BBM_PROMPT_USER"] == ""
    assert extra_env["BBM_PROMPT_SYSTEM"] == ""
    assert extra_env["BBM_GEMINI_PROMPT_USER"] == ""
    assert extra_env["BBM_GEMINI_PROMPT_SYSTEM"] == ""


def test_build_downloader_command_puts_cookie_in_env():
    command, _, extra_env = build_downloader_command(
        make_test_config(),
        TaskPayload(
            mode="download_only",
            source_type="syosetu",
            source_input="https://ncode.syosetu.com/n1234ab/1/",
        ),
        {},
        Path("/tmp/downloads"),
        cookie_header="ses=secret-cookie",
    )

    assert "--cookie" not in command
    assert "ses=secret-cookie" not in command
    assert extra_env == {"DOWNLOADER_COOKIE": "ses=secret-cookie"}


def test_build_downloader_command_passes_proxy():
    payload = TaskPayload(
        mode="download_only",
        source_type="syosetu",
        source_input="https://ncode.syosetu.com/n1234ab/",
    )
    command, _, _ = build_downloader_command(
        make_test_config(),
        payload,
        {**DEFAULT_SETTINGS, "proxy": "http://127.0.0.1:7890"},
        Path("/tmp/downloads"),
    )
    assert "--proxy" in command
    assert "http://127.0.0.1:7890" in command


def test_list_source_candidates_ignores_downloader_temp_dirs(tmp_path: Path):
    good = tmp_path / "book.txt"
    good.write_text("complete", encoding="utf-8")
    temp_dir = tmp_path / "_node_job_abcd"
    temp_dir.mkdir()
    bad = temp_dir / "001.txt"
    bad.write_text("fragment larger than complete", encoding="utf-8")

    assert list_source_candidates(tmp_path, save_format="txt") == [good]


def test_resolve_translated_file_ignores_temp_output(tmp_path: Path):
    source = tmp_path / "book.txt"
    source.write_text("source", encoding="utf-8")
    temp = tmp_path / "book_翻译_temp.txt"
    temp.write_text("partial", encoding="utf-8")

    assert resolve_translated_file(source) is None


def test_cleanup_download_work_dirs_removes_downloader_temp_dirs(tmp_path: Path):
    from app.services import worker as worker_module

    assert hasattr(worker_module, "_cleanup_download_work_dirs")
    keep = tmp_path / "book"
    keep.mkdir()
    stale = tmp_path / "_node_job_abcd"
    stale.mkdir()
    cookie = tmp_path / "_cookie_xyz"
    cookie.mkdir()

    worker_module._cleanup_download_work_dirs(tmp_path)

    assert keep.exists()
    assert not stale.exists()
    assert not cookie.exists()


def test_worker_command_local_pause_reason_wins_over_exit_code(monkeypatch, tmp_path: Path):
    from app.services.worker import TaskWorker

    worker = TaskWorker()
    monkeypatch.setattr(worker, "_register_process", lambda task_id, process: None)
    monkeypatch.setattr(worker, "_unregister_process", lambda task_id: None)
    monkeypatch.setattr(worker, "_flush_latest_progress", lambda task_id: None)
    with worker._proc_lock:
        worker._terminate_reason[123] = "paused"

    try:
        worker._run_command(
            123,
            [sys.executable, "-c", "import sys; sys.exit(130)"],
            str(tmp_path),
            60,
        )
    except RuntimeError as exc:
        assert str(exc) == "__TASK_PAUSED__"
    else:
        raise AssertionError("paused terminate reason should raise __TASK_PAUSED__")


def test_set_task_finished_preserves_existing_paths_when_failed_without_paths():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY,
            status TEXT NOT NULL,
            finished_at TEXT,
            download_output_dir TEXT NOT NULL DEFAULT '',
            source_output_path TEXT NOT NULL DEFAULT '',
            translated_output_path TEXT NOT NULL DEFAULT '',
            error_message TEXT NOT NULL DEFAULT '',
            error_code TEXT NOT NULL DEFAULT '',
            running_pid INTEGER
        )
        """
    )
    conn.execute(
        """
        INSERT INTO tasks(id, status, download_output_dir, source_output_path, translated_output_path, running_pid)
        VALUES(1, 'running', '/tmp/downloads', '/tmp/source.txt', '/tmp/translated.txt', 99)
        """
    )

    set_task_finished(conn, 1, status="failed", error_message="boom", error_code="UNKNOWN")

    row = conn.execute("SELECT * FROM tasks WHERE id = 1").fetchone()
    assert row["download_output_dir"] == "/tmp/downloads"
    assert row["source_output_path"] == "/tmp/source.txt"
    assert row["translated_output_path"] == "/tmp/translated.txt"
    assert row["running_pid"] is None


def test_update_task_outputs_updates_source_path_without_finishing():
    from app.services import task_service

    assert hasattr(task_service, "update_task_outputs")
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY,
            status TEXT NOT NULL,
            finished_at TEXT,
            download_output_dir TEXT NOT NULL DEFAULT '',
            source_output_path TEXT NOT NULL DEFAULT '',
            translated_output_path TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute("INSERT INTO tasks(id, status) VALUES(1, 'running')")

    task_service.update_task_outputs(
        conn,
        1,
        download_output_dir="/tmp/downloads",
        source_output_path="/tmp/source.txt",
    )

    row = conn.execute("SELECT * FROM tasks WHERE id = 1").fetchone()
    assert row["status"] == "running"
    assert row["finished_at"] is None
    assert row["download_output_dir"] == "/tmp/downloads"
    assert row["source_output_path"] == "/tmp/source.txt"


def test_upload_suffix_allowlist_is_explicit():
    from app.routers import tasks

    assert tasks.ALLOWED_UPLOAD_SUFFIXES == {".txt", ".epub", ".md", ".pdf", ".srt"}


def test_row_to_dict_sanitizes_task_payload_secret_overrides():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO tasks(id, payload_json, created_at) VALUES(1, ?, ?)",
        (
            json.dumps(
                {
                    "settings_overrides": {
                        "openai_key": "sk-secret",
                        "proxy": "http://127.0.0.1:7890",
                    }
                }
            ),
            "2026-06-11T00:00:00+00:00",
        ),
    )

    row = conn.execute("SELECT * FROM tasks WHERE id = 1").fetchone()
    data = row_to_dict(row)

    assert "payload_json" not in data
    assert data["payload"]["settings_overrides"]["openai_key"] == "***"
    assert data["payload"]["settings_overrides"]["proxy"] == "http://127.0.0.1:7890"


def test_task_template_delete_removes_template():
    from app.services import task_service

    assert hasattr(task_service, "delete_task_template")
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE task_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    template_id = task_service.create_task_template(conn, "demo", {"mode": "download_only"})

    assert task_service.delete_task_template(conn, template_id) is True
    assert task_service.get_task_template(conn, template_id) is None


def test_validate_translation_settings_rejects_unknown_model():
    settings = dict(DEFAULT_SETTINGS)
    settings["model"] = "not-a-model"

    result = validate_translation_settings(settings)

    assert result.ok is False
    assert "model" in result.message


def test_validate_translation_settings_requires_text_placeholder_in_prompt_user():
    settings = dict(DEFAULT_SETTINGS)
    settings["prompt_user"] = "translate this"

    result = validate_translation_settings(settings)

    assert result.ok is False
    assert "{text}" in result.message


def test_new_task_template_has_no_secret_override_inputs():
    template = Path("/opt/translator_webui/webui/app/templates/new_task.html").read_text(encoding="utf-8")
    for key in [
        "openai_key",
        "claude_key",
        "gemini_key",
        "groq_key",
        "xai_key",
        "qwen_key",
        "caiyun_key",
        "deepl_key",
        "custom_api",
    ]:
        assert f"override__{key}" not in template


def test_build_translator_command_skips_default_openai_model_list_for_other_models():
    cfg = AppConfig(
        base_dir=Path("/tmp/webui"),
        data_dir=Path("/tmp/data"),
        db_path=Path("/tmp/data/webui.sqlite3"),
        task_root=Path("/tmp/data/tasks"),
        upload_root=Path("/tmp/data/uploads"),
        worker_interval_seconds=1.0,
        cleanup_days=14,
        app_env="dev",
        enforce_secure_defaults=False,
        basic_auth_user="user",
        basic_auth_password="pass",
        secret_key="",
        require_secret_key=False,
        process_timeout_seconds=7200,
        stop_grace_seconds=8,
        max_upload_bytes=104857600,
        downloader_python="python",
        downloader_entry=Path("/tmp/downloader.py"),
        translator_python="python",
        translator_entry=Path("/tmp/bookmaker"),
    )
    payload = TaskPayload(
        translate_mode="full",
        translation_output_mode="bilingual",
    )
    settings = dict(DEFAULT_SETTINGS)
    settings["model"] = "groq"
    settings["model_list"] = ""

    command, extra_env = build_translator_command(
        cfg,
        Path("/tmp/book.txt"),
        payload,
        settings,
    )
    assert set(extra_env) == {
        "BBM_PROMPT_USER",
        "BBM_PROMPT_SYSTEM",
        "BBM_GEMINI_PROMPT_USER",
        "BBM_GEMINI_PROMPT_SYSTEM",
    }
    assert "--model_list" not in command


def test_build_translator_command_clears_ambient_prompt_env_when_prompt_unset():
    payload = TaskPayload(translate_mode="full", translation_output_mode="translated_only")
    command, extra_env = build_translator_command(
        make_test_config(),
        Path("/tmp/book.txt"),
        payload,
        dict(DEFAULT_SETTINGS),
    )

    assert "--prompt" not in command
    assert extra_env["BBM_PROMPT_USER"] == ""
    assert extra_env["BBM_PROMPT_SYSTEM"] == ""
    assert extra_env["BBM_GEMINI_PROMPT_USER"] == ""
    assert extra_env["BBM_GEMINI_PROMPT_SYSTEM"] == ""


def test_common_create_translator_passes_context_settings():
    from book_maker.loader.common import create_translator

    class DummyTranslator:
        def __init__(self, key, language, **kwargs):
            self.key = key
            self.language = language
            self.kwargs = kwargs

    translator = create_translator(
        DummyTranslator,
        key="key",
        language="zh-hans",
        context_flag=True,
        context_paragraph_limit=3,
    )

    assert translator.kwargs["context_flag"] is True
    assert translator.kwargs["context_paragraph_limit"] == 3


def test_txt_loader_disables_context_for_parallel_workers(tmp_path: Path):
    from book_maker.loader.txt_loader import TXTBookLoader

    class DummyTranslator:
        def __init__(self, key, language, **kwargs):
            self.kwargs = kwargs

        def translate(self, text, context_flag=False):
            return text

    source = tmp_path / "book.txt"
    source.write_text("本文", encoding="utf-8")

    loader = TXTBookLoader(
        str(source),
        DummyTranslator,
        key="",
        resume=False,
        language="zh-hans",
        context_flag=True,
        context_paragraph_limit=5,
        parallel_workers=2,
    )

    assert loader.context_enabled is False
    assert loader.translate_model.kwargs["context_flag"] is False
    assert loader.translate_model.kwargs["context_paragraph_limit"] == 5


def test_txt_loader_preserves_special_batches(tmp_path: Path):
    from book_maker.loader.txt_loader import TXTBookLoader

    class DummyTranslator:
        def __init__(self, key, language, **kwargs):
            pass

        def translate(self, text, context_flag=False):
            return f"译文:{text}"

    source = tmp_path / "book.txt"
    source.write_text(("\n" * 10) + "本文", encoding="utf-8")
    loader = TXTBookLoader(str(source), DummyTranslator, key="", resume=False, language="zh-hans")

    batches = loader._build_batches()

    assert len(batches) == 2
    assert batches[0].translatable is False
    assert batches[0].text.count("\n") >= 9
    assert batches[1].translatable is True
    assert batches[1].text == "本文"


def test_txt_loader_batches_by_chapter_and_blank_paragraphs(tmp_path: Path):
    from book_maker.loader.txt_loader import TXTBookLoader

    class DummyTranslator:
        def __init__(self, key, language, **kwargs):
            pass

        def translate(self, text, context_flag=False):
            return text

    source = tmp_path / "book.txt"
    source.write_text("● 第一章\n第一段\n\n第二段\n● 第二章\n第三段", encoding="utf-8")
    loader = TXTBookLoader(str(source), DummyTranslator, key="", resume=False, language="zh-hans")

    batches = loader._build_batches()

    assert [batch.text for batch in batches] == ["● 第一章\n第一段", "\n第二段", "● 第二章\n第三段"]


def test_txt_resume_state_rejects_batch_fingerprint_mismatch(tmp_path: Path):
    from book_maker.loader.txt_loader import TXTBookLoader

    class DummyTranslator:
        def __init__(self, key, language, **kwargs):
            pass

        def translate(self, text, context_flag=False):
            return text

    source = tmp_path / "book.txt"
    source.write_text("第一段", encoding="utf-8")
    state = tmp_path / ".book.temp.bin"
    state.write_text('{"version": 3, "batch_hashes": ["wrong"], "p_to_save": ["旧译文"]}', encoding="utf-8")

    loader = TXTBookLoader(str(source), DummyTranslator, key="", resume=True, language="zh-hans")

    assert loader.p_to_save == []


def test_translation_quality_scan_flags_refusal_text(tmp_path: Path):
    from app.services.worker_file_service import scan_translation_quality

    output = tmp_path / "book_翻译.txt"
    output.write_text("I'm sorry, I cannot translate this content.", encoding="utf-8")

    warnings = scan_translation_quality(output)

    assert any("refusal" in warning for warning in warnings)


def test_translation_quality_scan_flags_line_count_and_missing_chapter_marker(tmp_path: Path):
    from app.services.worker_file_service import scan_translation_quality

    source = tmp_path / "book.txt"
    source.write_text("● 第一章\n第一段\n第二段\n第三段", encoding="utf-8")
    output = tmp_path / "book_翻译.txt"
    output.write_text("只有一行译文", encoding="utf-8")

    warnings = scan_translation_quality(output, source)

    assert any("line count" in warning for warning in warnings)
    assert any("chapter marker" in warning for warning in warnings)


def test_txt_bilingual_output_uses_blank_line_separators(tmp_path: Path):
    from book_maker.loader.txt_loader import TXTBookLoader

    class DummyTranslator:
        def __init__(self, key, language, **kwargs):
            pass

        def translate(self, text, context_flag=False):
            return f"译文:{text}"

    source = tmp_path / "book.txt"
    source.write_text("原文一\n原文二", encoding="utf-8")
    loader = TXTBookLoader(
        str(source),
        DummyTranslator,
        key="",
        resume=False,
        language="zh-hans",
        single_translate=False,
    )

    loader.build_book()

    output = (tmp_path / "book_翻译.txt").read_text(encoding="utf-8")
    assert "原文一\n原文二\n\n译文:原文一" in output
    assert not output.endswith("\n\n")


def test_txt2epub_splits_only_line_marked_chapters():
    from syosetu_novel_downloader.converters import txt2epub

    chapters = txt2epub.split_marked_chapters("● 第一章\n正文里的 ● 不是章节\n● 第二章\n结尾")

    assert chapters == [
        ("第一章", "正文里的 ● 不是章节"),
        ("第二章", "结尾"),
    ]


def test_txt2epub_body_to_html_keeps_paragraphs_and_line_breaks():
    from syosetu_novel_downloader.converters import txt2epub

    html = txt2epub.body_to_html("第一行\n第二行\n\n第三行")

    assert html.count("<p>") == 2
    assert "第一行<br/>第二行" in html
    assert "<p>第三行</p>" in html


def test_epub_node_text_preserves_br_as_newline():
    from bs4 import BeautifulSoup
    from book_maker.loader.epub_support import node_text

    soup = BeautifulSoup("<p>a<br/>b</p>", "html.parser")

    assert node_text(soup.p) == "a\nb"


def test_epub_insert_trans_rebuilds_br_nodes():
    from bs4 import BeautifulSoup
    from book_maker.loader.helper import EPUBBookLoaderHelper

    soup = BeautifulSoup("<body><p>a<br/>b</p></body>", "html.parser")
    helper = EPUBBookLoaderHelper(translate_model=None, accumulated_num=1, translation_style="", context_flag=False)

    helper.insert_trans(soup.p, "甲\n乙")

    translated = soup.find_all("p")[1]
    assert translated.get_text("\n") == "甲\n乙"
    assert "<br" in str(translated)


def test_verify_fetch_request_requires_requested_with_header():
    from fastapi import HTTPException
    from starlette.requests import Request

    from app.security import verify_fetch_request

    def make_request(headers: list[tuple[bytes, bytes]]) -> Request:
        return Request({"type": "http", "method": "POST", "path": "/api/tasks", "headers": headers})

    try:
        verify_fetch_request(make_request([]))
    except HTTPException as exc:
        assert exc.status_code == 403
    else:
        raise AssertionError("missing X-Requested-With header should be rejected")

    assert verify_fetch_request(make_request([(b"x-requested-with", b"fetch")])) is None


def test_safe_path_allows_upload_root_outside_data_dir(tmp_path: Path):
    cfg = AppConfig(
        base_dir=tmp_path,
        data_dir=tmp_path / "data",
        db_path=tmp_path / "data" / "webui.sqlite3",
        task_root=tmp_path / "task-root",
        upload_root=tmp_path / "upload-root",
        worker_interval_seconds=1.0,
        cleanup_days=14,
        app_env="dev",
        enforce_secure_defaults=False,
        basic_auth_user="user",
        basic_auth_password="pass",
        secret_key="",
        require_secret_key=False,
        process_timeout_seconds=7200,
        stop_grace_seconds=8,
        max_upload_bytes=104857600,
        downloader_python="python",
        downloader_entry=Path("/tmp/downloader.py"),
        translator_python="python",
        translator_entry=Path("/tmp/bookmaker"),
    )
    target = cfg.upload_root / "book.txt"
    target.parent.mkdir(parents=True)
    target.write_text("book", encoding="utf-8")

    from app.services.task_management_service import safe_path

    assert safe_path(str(target), cfg) == target.resolve()


def test_retry_upload_source_validation_rejects_missing_file(tmp_path: Path):
    from fastapi import HTTPException
    from app.services.task_management_service import validate_retry_source_available

    missing = tmp_path / "missing.txt"
    payload = {"source_type": "upload", "upload_path": str(missing)}

    try:
        validate_retry_source_available(payload)
    except HTTPException as exc:
        assert exc.status_code == 409
        assert "Uploaded file is missing" in str(exc.detail)
    else:
        raise AssertionError("missing upload retry source should be rejected")


def test_format_local_timestamp_uses_display_timezone_env(monkeypatch):
    from app import time_utils

    monkeypatch.setenv("WEBUI_DISPLAY_TZ", "UTC")

    assert time_utils.format_local_timestamp("2026-06-11T00:00:00+00:00") == "2026-06-11 00:00:00"


def test_log_prune_state_bound_helper_limits_entries():
    from app.services import task_service

    assert hasattr(task_service, "bound_log_prune_state")
    task_service._LOG_PRUNE_STATE.clear()
    for task_id in range(1005):
        task_service._LOG_PRUNE_STATE[task_id] = (float(task_id), 1)

    task_service.bound_log_prune_state(max_entries=1000)

    assert len(task_service._LOG_PRUNE_STATE) == 1000
    assert 0 not in task_service._LOG_PRUNE_STATE


def test_task_worker_uses_time_based_cleanup_state():
    from app.services.worker import TaskWorker

    worker = TaskWorker()

    assert hasattr(worker, "_last_cleanup_ts")
    assert not hasattr(worker, "_cleanup_tick")


def assert_init_has_parallel_workers(path: Path, class_name: str):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                    arg_names = [arg.arg for arg in item.args.args]
                    assert "parallel_workers" in arg_names
                    return
    raise AssertionError(f"could not find {class_name}.__init__ in {path}")


def assert_class_has_method(path: Path, class_name: str, method_name: str):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == method_name:
                    return
    raise AssertionError(f"could not find {class_name}.{method_name} in {path}")


def test_markdown_loader_accepts_parallel_workers():
    assert_init_has_parallel_workers(
        Path("/opt/translator_webui/bilingual_book_maker/book_maker/loader/md_loader.py"),
        "MarkdownBookLoader",
    )


def test_srt_loader_accepts_parallel_workers():
    assert_init_has_parallel_workers(
        Path("/opt/translator_webui/bilingual_book_maker/book_maker/loader/srt_loader.py"),
        "SRTBookLoader",
    )


def test_epub_loader_implements_make_new_book():
    assert_class_has_method(
        Path("/opt/translator_webui/bilingual_book_maker/book_maker/loader/epub_loader.py"),
        "EPUBBookLoader",
        "_make_new_book",
    )


def test_epub_count_translatable_nodes_falls_back_to_body_text():
    class DummyItem:
        file_name = "chap_1.xhtml"
        content = (
            b"<?xml version='1.0' encoding='utf-8'?><html><body><h1>\xe7\xab\xa0\xe8\x8a\x82</h1>"
            b"\xe3\x81\x93\xe3\x82\x8c\xe3\x81\xaf\xe6\x9c\xac\xe6\x96\x87\xe3\x81\xa7\xe3\x81\x99\xe3\x80\x82"
            b"</body></html>"
        )

    assert (
        count_translatable_nodes(
            DummyItem(),
            ["p"],
            allow_navigable_strings=False,
            only_filelist="",
            exclude_filelist="",
        )
        == 2
    )


def test_openai_translator_model_list_sets_immediate_model():
    translator = OpenAITranslator("test-key", "zh-hans", api_base="https://example.com/v1")
    translator.set_model_list(["deepseek-ai/DeepSeek-V3.2", "gpt-5.2"])
    assert translator._model_list_values == ["deepseek-ai/DeepSeek-V3.2", "gpt-5.2"]
    assert translator.model == "deepseek-ai/DeepSeek-V3.2"


def test_qwen_translator_model_setter_returns_selected_model():
    translator = QwenTranslator("test-key", "chinese", model="qwen-mt-plus")
    assert translator.model == "qwen-mt-plus"
    assert translator.set_qwen_model("bad-model") == "qwen-mt-turbo"


def test_epub_resume_state_round_trips_json(tmp_path: Path):
    path = tmp_path / ".book.temp.bin"
    save_resume_state(str(path), ["one", "two"])

    saved = path.read_text(encoding="utf-8")
    assert load_resume_state(str(path)) == ["one", "two"]
    assert '"version": 2' in saved
    assert '"p_to_save"' in saved


def test_import_env_unescapes_newlines():
    raw = 'BBM_PROMPT_TEXT="line1\\nline2"\n'
    mapped = import_env_to_settings(raw)
    assert mapped["prompt_text"] == "line1\nline2"


def test_save_settings_rejects_secret_without_key(monkeypatch):
    monkeypatch.delenv("WEBUI_SECRET_KEY", raising=False)
    monkeypatch.setenv("WEBUI_REQUIRE_SECRET_KEY", "false")
    get_config.cache_clear()

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            is_secret INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        )
        """
    )

    try:
        try:
            save_settings(conn, {"openai_key": "secret"})
        except RuntimeError as exc:
            assert "WEBUI_SECRET_KEY" in str(exc)
        else:
            raise AssertionError("save_settings should reject secret values without WEBUI_SECRET_KEY")
    finally:
        conn.close()
        get_config.cache_clear()


def test_save_settings_can_clear_secret(monkeypatch):
    monkeypatch.setenv("WEBUI_SECRET_KEY", VALID_FERNET_KEY)
    monkeypatch.setenv("WEBUI_REQUIRE_SECRET_KEY", "false")
    get_config.cache_clear()

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            is_secret INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO settings(key, value, is_secret, updated_at) VALUES(?, ?, ?, ?)",
        ("openai_key", encrypt_text("old-secret"), 1, "now"),
    )

    try:
        save_settings(conn, {"openai_key": ""}, clear_keys={"openai_key"})
        loaded = load_settings(conn)
        assert loaded["openai_key"] == ""
    finally:
        conn.close()
        get_config.cache_clear()


def test_create_app_registers_split_routes():
    app = create_app()
    paths = {route.path for route in app.routes}
    assert "/" in paths
    assert "/settings" in paths
    assert "/api/tasks" in paths
    assert "/api/system/status" in paths


def test_delete_task_records_cascade_removes_descendants():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY,
            status TEXT NOT NULL,
            upload_path TEXT NOT NULL DEFAULT '',
            parent_task_id INTEGER
        );
        INSERT INTO tasks(id, status, upload_path, parent_task_id) VALUES
            (1, 'failed', '/tmp/one.txt', NULL),
            (2, 'failed', '/tmp/two.txt', 1),
            (3, 'failed', '/tmp/three.txt', 2);
        """
    )

    try:
        deleted_ids, upload_paths = delete_task_records(conn, 1, force=True, cascade=True)
        assert deleted_ids == [1, 2, 3]
        assert set(upload_paths) == {"/tmp/one.txt", "/tmp/two.txt", "/tmp/three.txt"}
        remaining = conn.execute("SELECT COUNT(*) AS total FROM tasks").fetchone()
        assert int(remaining["total"]) == 0
    finally:
        conn.close()


def test_build_download_options_uses_cli_namespace():
    args = argparse.Namespace(
        url="https://kakuyomu.jp/works/abc123",
        site="auto",
        backend="native",
        proxy="http://localhost:7890",
        output_dir="/tmp/downloads",
        save_format="txt",
        record_chapter_number=True,
        merge_all=False,
        merged_name="",
        cookie="a=b",
        cookie_file="",
        paid_policy="skip",
        rate_limit=1.5,
        retries=3,
        timeout=60,
    )

    options = build_download_options(args)
    assert str(options.output_dir) == "/tmp/downloads"
    assert options.backend == "native"
    assert options.record_chapter_number is True


def test_worker_error_classification_and_redaction():
    status, code = classify_worker_error(RuntimeError("TRANSLATE_STAGE: boom"))
    assert (status, code) == ("failed", "TRANSLATE_FAILED")

    redacted = redact_command(["python", "x.py", "--openai_key", "secret", "--cookie", "a=b"])
    assert "secret" not in redacted
    assert "a=b" not in redacted
    assert "***" in redacted


def test_safe_delete_upload_file_removes_resume_artifacts(tmp_path: Path):
    from dataclasses import replace

    from app.services.task_management_service import safe_delete_upload_file

    cfg = replace(make_test_config(), upload_root=tmp_path)
    upload = tmp_path / "book.txt"
    upload.write_text("book", encoding="utf-8")
    resume = tmp_path / ".book.temp.bin"
    resume.write_text("state", encoding="utf-8")
    temp = tmp_path / "book_翻译_temp.txt"
    temp.write_text("partial", encoding="utf-8")

    assert safe_delete_upload_file(str(upload), cfg) is True
    assert not upload.exists()
    assert not resume.exists()
    assert not temp.exists()


def test_classify_worker_error_does_not_treat_any_cookie_word_as_auth_failed():
    status, code = classify_worker_error(RuntimeError("cookie jar path is unavailable"))

    assert code != "AUTH_FAILED"


def test_get_cookie_profile_for_edit_returns_metadata_only():
    from app.services.task_service import get_cookie_profile_for_edit

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE cookie_profiles (id INTEGER PRIMARY KEY, name TEXT, site TEXT, cookie_enc TEXT, created_at TEXT)")
    conn.execute("INSERT INTO cookie_profiles VALUES (1, 'demo', 'syosetu', 'encrypted-cookie', '2026-06-12T00:00:00+00:00')")

    profile = get_cookie_profile_for_edit(conn, 1)

    assert profile["name"] == "demo"
    assert profile["site"] == "syosetu"
    assert "cookie_enc" not in profile


def test_worker_success_rc_wins_over_late_pause_reason():
    from app.services.worker import classify_command_completion

    result = classify_command_completion(
        rc=0,
        local_reason="paused",
        paused=False,
        stopped=False,
        timed_out=False,
    )

    assert result == "success"


def test_preview_limit_help_text_names_txt_and_epub_units():
    from app.services.task_payload_service import preview_limit_help_text

    text = preview_limit_help_text()

    assert "TXT=行" in text
    assert "EPUB=段" in text


def test_resolve_source_file_prefers_named_merge(tmp_path: Path):
    root = tmp_path / "downloads"
    root.mkdir()
    (root / "foo.txt").write_text("a", encoding="utf-8")
    (root / "bar.txt").write_text("b", encoding="utf-8")

    resolved = resolve_source_file(root, merged_name="bar", save_format="txt")
    assert resolved.name == "bar.txt"
    assert artifact_kind(tmp_path / "demo_翻译.txt") == "translated"


def test_epub_helper_modules_define_planning_and_resume_functions():
    plan_tree = ast.parse(
        Path("/opt/translator_webui/bilingual_book_maker/book_maker/loader/epub_plan.py").read_text(encoding="utf-8")
    )
    resume_tree = ast.parse(
        Path("/opt/translator_webui/bilingual_book_maker/book_maker/loader/epub_resume.py").read_text(encoding="utf-8")
    )

    plan_functions = {node.name for node in plan_tree.body if isinstance(node, ast.FunctionDef)}
    resume_functions = {node.name for node in resume_tree.body if isinstance(node, ast.FunctionDef)}

    assert {"total_paragraph_count", "build_chapter_plan"} <= plan_functions
    assert {"load_resume_state", "save_resume_state", "save_temp_book"} <= resume_functions


def test_epub_accumulated_translation_updates_resume_slots():
    from book_maker.loader.epub_loader import EPUBBookLoader

    loader = object.__new__(EPUBBookLoader)
    loader.p_to_save = []

    EPUBBookLoader._set_accumulated_saved_values(loader, start_index=2, values=["甲", "乙"])

    assert loader.p_to_save == ["", "", "甲", "乙"]


def test_epub_accumulated_result_application_records_resume_values():
    from bs4 import BeautifulSoup
    from book_maker.loader.epub_loader import EPUBBookLoader

    soup = BeautifulSoup("<p>A</p><p>B</p>", "html.parser")
    nodes = soup.find_all("p")

    class DummyHelper:
        def __init__(self):
            self.inserted = []

        def insert_trans(self, node, text, translation_style, single_translate):
            self.inserted.append((node.text, text, translation_style, single_translate))

    loader = object.__new__(EPUBBookLoader)
    loader.p_to_save = []
    loader.helper = DummyHelper()
    loader.translation_style = ""
    loader.single_translate = False

    EPUBBookLoader._apply_accumulated_results(loader, [(2, nodes[0]), (3, nodes[1])], ["甲", "乙"])

    assert loader.p_to_save == ["", "", "甲", "乙"]
    assert loader.helper.inserted == [("A", "甲", "", False), ("B", "乙", "", False)]


def test_txt2epub_language_can_be_configured(tmp_path: Path):
    from syosetu_novel_downloader.converters.txt2epub import create_epub_from_txt

    source = tmp_path / "book.txt"
    source.write_text("● 第一章\n本文", encoding="utf-8")

    path = create_epub_from_txt(str(source), str(tmp_path), language="zh-hans")

    assert path.endswith("book.epub")


def test_node_txt_parser_preserves_body_when_title_marker_is_absent(tmp_path: Path):
    from downloader.adapters.node_adapter import _parse_node_txt_chapters

    source = tmp_path / "0001.txt"
    source.write_text("本文第一行\n本文第二行", encoding="utf-8")

    chapters = _parse_node_txt_chapters(tmp_path, [source])

    assert chapters[0].title == "0001"
    assert chapters[0].content == "本文第一行\n本文第二行"


def test_merge_txt_files_fallback_uses_natural_filename_order(tmp_path: Path):
    import os
    from syosetu_novel_downloader.converters.txt2epub import merge_txt_files

    for name, content in (("10.txt", "ten"), ("2.txt", "two"), ("1.txt", "one")):
        path = tmp_path / name
        path.write_text(content, encoding="utf-8")
        os.utime(path, (1000, 1000))

    merged = Path(merge_txt_files(str(tmp_path), "book.txt"))

    assert merged.read_text(encoding="utf-8") == "one\n\ntwo\n\nten"


def test_default_translate_tags_include_epub_titles():
    from app.option_registry import DEFAULT_SETTINGS

    assert DEFAULT_SETTINGS["translate_tags"] == "p,h1"


def test_download_retry_delay_increases_between_attempts():
    from downloader.job import retry_delay_seconds

    assert retry_delay_seconds(0, base=1.0) == 1.0
    assert retry_delay_seconds(1, base=1.0) == 2.0
    assert retry_delay_seconds(2, base=1.0) == 4.0


def test_syosetu_native_resume_skips_completed_nonempty_chapters(tmp_path: Path):
    from syosetu import _filter_pending_chapter_jobs

    book_dir = tmp_path / "Book"
    book_dir.mkdir()
    (book_dir / "Part.txt").write_text("● First\nbody\n", encoding="utf-8")
    (book_dir / "Empty.txt").write_text("", encoding="utf-8")
    (book_dir / "_chapter_manifest.json").write_text(
        json.dumps(
            {
                "chapters": [
                    {"index": 1, "status": "ok", "file_path": "Part.txt"},
                    {"index": 3, "status": "ok", "file_path": "Empty.txt"},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    jobs = [
        (1, str(book_dir / "Part")),
        (2, str(book_dir / "Part")),
        (3, str(book_dir / "Empty")),
    ]

    pending, skipped = _filter_pending_chapter_jobs(book_dir, jobs)

    assert pending == [(2, str(book_dir / "Part")), (3, str(book_dir / "Empty"))]
    assert skipped == [1]


def test_sanitize_task_payload_dict_masks_secret_overrides():
    from app.services.task_management_service import sanitize_task_payload_dict_for_api

    payload = {
        "mode": "download_only",
        "settings_overrides": {
            "openai_key": "sk-secret",
            "proxy": "http://127.0.0.1:7890",
        },
    }

    sanitized = sanitize_task_payload_dict_for_api(payload)

    assert sanitized["settings_overrides"]["openai_key"] == "***"
    assert sanitized["settings_overrides"]["proxy"] == "http://127.0.0.1:7890"
    assert payload["settings_overrides"]["openai_key"] == "sk-secret"


def test_strip_secret_overrides_for_template_removes_secret_keys():
    from app.services.task_management_service import strip_secret_overrides_for_template

    payload = {
        "mode": "download_only",
        "settings_overrides": {
            "openai_key": "sk-secret",
            "proxy": "http://127.0.0.1:7890",
        },
    }

    cleaned = strip_secret_overrides_for_template(payload)

    assert "openai_key" not in cleaned["settings_overrides"]
    assert cleaned["settings_overrides"]["proxy"] == "http://127.0.0.1:7890"
    assert payload["settings_overrides"]["openai_key"] == "sk-secret"


def test_validate_settings_update_rejects_imported_unknown_model():
    from app.services.settings_service import validate_settings_update

    result = validate_settings_update(dict(DEFAULT_SETTINGS), {"model": "not-a-model"})

    assert result.ok is False
    assert "model" in result.message


def test_validate_settings_update_accepts_valid_imported_model():
    from app.services.settings_service import validate_settings_update

    result = validate_settings_update(dict(DEFAULT_SETTINGS), {"model": "openai"})

    assert result.ok is True


def test_validate_translation_request_rejects_block_size_with_bilingual_payload():
    from app.services.settings_service import validate_translation_request

    settings = dict(DEFAULT_SETTINGS)
    settings["block_size"] = "1000"
    payload = {"translation_output_mode": "bilingual"}

    result = validate_translation_request(settings, payload)

    assert result.ok is False
    assert "block_size" in result.message
    assert "translated_only" in result.message


def test_preview_pdf_missing_dependency_returns_http_400(monkeypatch, tmp_path: Path):
    from fastapi import HTTPException
    from app.services import preview_service
    from app.services.task_management_service import preview_file

    pdf = tmp_path / "sample.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    monkeypatch.setattr(preview_service, "fitz", None)

    try:
        preview_file(pdf, page=1)
    except HTTPException as exc:
        assert exc.status_code == 400
        assert "PyMuPDF" in str(exc.detail)
    else:
        raise AssertionError("missing PyMuPDF should be returned as HTTP 400")


def test_env_defaults_fill_missing_settings(monkeypatch):
    from app.services.settings_service import merge_env_defaults

    monkeypatch.setenv("BBM_MODEL", "qwen")
    monkeypatch.setenv("BBM_TEMPERATURE", "0.6")
    settings = merge_env_defaults({"model": "openai", "temperature": ""})

    assert settings["model"] == "openai"
    assert settings["temperature"] == "0.6"


def test_env_defaults_do_not_override_db_values(monkeypatch):
    from app.services.settings_service import merge_env_defaults

    monkeypatch.setenv("BBM_TEMPERATURE", "0.6")
    settings = merge_env_defaults({"temperature": "0.3"})

    assert settings["temperature"] == "0.3"


def test_load_settings_uses_env_defaults_when_db_value_is_missing(monkeypatch):
    from app.services.settings_service import load_settings

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT, is_secret INTEGER)")
    monkeypatch.setenv("BBM_TEMPERATURE", "0.6")

    settings = load_settings(conn)

    assert settings["temperature"] == "0.6"


def test_default_temperature_is_translation_safe():
    from app.option_registry import DEFAULT_SETTINGS

    assert DEFAULT_SETTINGS["temperature"] == "0.6"


def test_glossary_is_appended_to_prompt_system():
    from app.services.settings_service import build_prompt_config_from_settings

    settings = dict(DEFAULT_SETTINGS)
    settings["prompt_system"] = "系统提示"
    settings["prompt_user"] = "翻译：{text}"
    settings["glossary"] = "太郎=太郎\n王都=王都"

    config = build_prompt_config_from_settings(settings)

    assert "术语表" in config["system"]
    assert "逐行对应" in config["system"]
    assert "● " in config["system"]
    assert "太郎=太郎" in config["system"]
    assert config["user"] == "翻译：{text}"
