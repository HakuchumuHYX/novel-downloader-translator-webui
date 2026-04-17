import ast
import argparse
import sqlite3
from pathlib import Path

from app.config import get_config
from app.config import AppConfig
from app.main import create_app
from app.option_registry import DEFAULT_SETTINGS as REGISTRY_DEFAULT_SETTINGS, ENV_TO_SETTING, SETTING_TO_ENV
from app.security import decrypt_text, encrypt_text, encryption_configured, sanitize_log
from app.services.command_builder import build_translator_command
from app.services.cookie_service import cookie_header_from_json_text, infer_site_from_json_text
from app.services.env_service import export_settings_to_env, import_env_to_settings
from app.services.preview_service import preview_text_file
from app.services.settings_service import DEFAULT_SETTINGS, load_settings, merged_settings, save_settings, validate_task_payload
from app.services.worker_command_service import classify_worker_error, redact_command
from app.services.worker_file_service import artifact_kind, resolve_source_file
from app.services.task_management_service import delete_task_records
from app.services.task_payload_service import build_task_payload
from app.task_models import TaskPayload
from cli_support import build_download_options

VALID_FERNET_KEY = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="


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

    command = build_translator_command(
        cfg,
        Path("/tmp/book.txt"),
        payload,
        settings,
        force_resume=False,
        has_resume_state=True,
    )
    assert "--prompt" in command
    assert "--resume" in command
    assert "--use_context" in command
    assert "--test" in command
    assert "--single_translate" in command
    assert command[:3] == ["python", "-m", "book_maker"]
    assert "--model_list" in command
    assert "gpt-5.2" in command


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

    command = build_translator_command(
        cfg,
        Path("/tmp/book.txt"),
        payload,
        settings,
    )
    assert "--model_list" not in command


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
