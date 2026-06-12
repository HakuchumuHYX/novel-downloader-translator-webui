from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException

from app.config import get_config
from app.db import get_conn, init_db
from app.main import create_app
from app.runtime import set_app_config, set_worker
from app.services.settings_service import save_settings
from app.services.task_service import append_log, create_task, create_task_template
from app.task_models import TaskPayload
from app.routers.system import healthz


REPO_ROOT = Path(__file__).resolve().parents[2]
AUTH = ("user", "pass")
FETCH_HEADERS = {"X-Requested-With": "fetch"}
VALID_FERNET_KEY = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def api_client(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("WEBUI_DATA_DIR", str(data_dir))
    monkeypatch.setenv("WEBUI_DB_PATH", str(data_dir / "webui.sqlite3"))
    monkeypatch.setenv("WEBUI_TASK_ROOT", str(data_dir / "tasks"))
    monkeypatch.setenv("WEBUI_UPLOAD_ROOT", str(data_dir / "uploads"))
    monkeypatch.setenv("WEBUI_BASIC_AUTH_USER", AUTH[0])
    monkeypatch.setenv("WEBUI_BASIC_AUTH_PASSWORD", AUTH[1])
    monkeypatch.setenv("WEBUI_ENFORCE_SECURE_DEFAULTS", "false")
    monkeypatch.setenv("WEBUI_REQUIRE_SECRET_KEY", "false")
    get_config.cache_clear()
    set_app_config(get_config())
    init_db()

    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client

    set_worker(None)
    set_app_config(None)
    get_config.cache_clear()


def _seed_task(payload: dict) -> int:
    with get_conn() as conn:
        return create_task(conn, payload)


def _valid_syosetu_payload(**overrides) -> dict:
    payload = TaskPayload(
        mode="download_and_translate",
        source_type="syosetu",
        source_input="https://ncode.syosetu.com/n1234ab/",
        translation_output_mode="translated_only",
    ).to_record()
    payload.update(overrides)
    return payload


def _seed_paused_task() -> int:
    with get_conn() as conn:
        task_id = create_task(conn, _valid_syosetu_payload())
        append_log(conn, task_id, "paused log")
        conn.execute("UPDATE tasks SET status = 'paused' WHERE id = ?", (task_id,))
        return task_id


def _seed_template(payload: dict, name: str = "contract-template") -> int:
    with get_conn() as conn:
        return create_task_template(conn, name, payload)


def test_healthz_route_returns_ok():
    assert healthz() == {"status": "ok"}


def test_webui_app_imports_without_test_path_injection():
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, "-c", "import webui.app.main"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_log_stream_end_states_include_paused():
    from app.routers.tasks import LOG_STREAM_END_STATES

    assert "paused" in LOG_STREAM_END_STATES


def test_mutating_routes_keep_fetch_guard_dependency():
    from app.routers import cookies, settings, tasks

    guarded_functions = [
        tasks.api_create_task,
        tasks.api_purge_task,
        tasks.api_delete_task,
        tasks.api_batch_purge_tasks,
        tasks.api_batch_delete_tasks,
        tasks.api_retry_task,
        tasks.api_run_full_task,
        tasks.api_cancel_task,
        tasks.api_pause_task,
        tasks.api_resume_task,
        tasks.api_stop_task,
        tasks.api_save_template,
        tasks.api_delete_template,
        settings.api_save_settings,
        settings.api_import_env,
        cookies.api_parse_cookie_json,
        cookies.api_create_cookie_profile,
        cookies.api_delete_cookie_profile,
    ]

    for func in guarded_functions:
        assert "verify_fetch_request" in inspect.getsource(func), func.__name__


@pytest.mark.anyio
async def test_mutating_route_without_fetch_header_returns_403(api_client):
    task_id = _seed_task(_valid_syosetu_payload())

    response = await api_client.post(f"/api/tasks/{task_id}/retry", auth=AUTH)

    assert response.status_code == 403


@pytest.mark.anyio
async def test_settings_save_invalid_model_returns_400(api_client):
    response = await api_client.post(
        "/api/settings",
        data={"model": "not-a-model"},
        headers=FETCH_HEADERS,
        auth=AUTH,
    )

    assert response.status_code == 400


@pytest.mark.anyio
async def test_retry_revalidates_current_settings(api_client):
    task_id = _seed_task(_valid_syosetu_payload(translation_output_mode="bilingual"))
    with get_conn() as conn:
        save_settings(conn, {"block_size": "100"})

    from app.routers.tasks import api_retry_task

    with pytest.raises(HTTPException) as exc_info:
        api_retry_task(task_id, _=AUTH[0], _csrf=None)

    assert exc_info.value.status_code == 400
    assert "block_size" in str(exc_info.value.detail)


@pytest.mark.anyio
async def test_run_full_revalidates_current_settings(api_client):
    task_id = _seed_task(_valid_syosetu_payload(translation_output_mode="bilingual"))
    with get_conn() as conn:
        save_settings(conn, {"block_size": "100"})

    from app.routers.tasks import api_run_full_task

    with pytest.raises(HTTPException) as exc_info:
        api_run_full_task(task_id, _=AUTH[0], _csrf=None)

    assert exc_info.value.status_code == 400
    assert "block_size" in str(exc_info.value.detail)


@pytest.mark.anyio
async def test_task_detail_sanitizes_payload_json(api_client):
    payload = _valid_syosetu_payload(settings_overrides={"openai_key": "sk-secret", "proxy": "http://proxy"})
    task_id = _seed_task(payload)

    from app.routers.tasks import api_get_task

    response = api_get_task(task_id, _=AUTH[0])
    data = json.loads(response.body)
    assert "payload_json" not in data
    assert data["payload"]["settings_overrides"]["openai_key"] == "***"
    assert data["payload"]["settings_overrides"]["proxy"] == "http://proxy"


@pytest.mark.anyio
async def test_template_get_sanitizes_secret_overrides(api_client):
    payload = _valid_syosetu_payload(settings_overrides={"openai_key": "sk-secret", "proxy": "http://proxy"})
    template_id = _seed_template(payload)

    from app.routers.tasks import api_get_template

    response = api_get_template(template_id, _=AUTH[0])
    data = json.loads(response.body)
    assert data["payload"]["settings_overrides"]["openai_key"] == "***"
    assert data["payload"]["settings_overrides"]["proxy"] == "http://proxy"


@pytest.mark.anyio
async def test_import_env_rejects_invalid_model_and_accepts_valid_model(api_client):
    invalid = await api_client.post(
        "/api/settings/import-env",
        json={"env_text": "BBM_MODEL=not-a-model\n"},
        headers=FETCH_HEADERS,
        auth=AUTH,
    )
    assert invalid.status_code == 400

    valid = await api_client.post(
        "/api/settings/import-env",
        json={"env_text": "BBM_MODEL=openai\n"},
        headers=FETCH_HEADERS,
        auth=AUTH,
    )
    assert valid.status_code == 200
    assert valid.json()["imported_keys"] == ["model"]


@pytest.mark.anyio
async def test_cookie_edit_can_update_metadata_without_resubmitting_cookie(api_client, monkeypatch):
    from app.config import get_config
    from app.runtime import set_app_config
    from app.security import decrypt_text, encrypt_text

    monkeypatch.setenv("WEBUI_SECRET_KEY", VALID_FERNET_KEY)
    get_config.cache_clear()
    set_app_config(get_config())

    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO cookie_profiles(name, site, cookie_enc, created_at, updated_at)
            VALUES(?, ?, ?, ?, ?)
            """,
            (
                "old",
                "kakuyomu",
                encrypt_text("a=b"),
                "2026-06-12T00:00:00+00:00",
                "2026-06-12T00:00:00+00:00",
            ),
        )
        profile_id = int(conn.execute("SELECT id FROM cookie_profiles WHERE name = ?", ("old",)).fetchone()["id"])

    response = await api_client.post(
        "/api/cookies",
        data={"profile_id": str(profile_id), "name": "new", "site": "syosetu", "cookie": ""},
        headers=FETCH_HEADERS,
        auth=AUTH,
    )

    assert response.status_code == 200
    with get_conn() as conn:
        row = conn.execute("SELECT name, site, cookie_enc FROM cookie_profiles WHERE id = ?", (profile_id,)).fetchone()
    assert row["name"] == "new"
    assert row["site"] == "syosetu"
    assert decrypt_text(row["cookie_enc"]) == "a=b"


@pytest.mark.anyio
async def test_log_stream_returns_end_for_paused_task(api_client, monkeypatch):
    from app.routers import tasks as tasks_router

    async def inline_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(tasks_router.asyncio, "to_thread", inline_to_thread)
    task_id = _seed_paused_task()

    async with api_client.stream("GET", f"/api/tasks/{task_id}/logs/stream", auth=AUTH) as response:
        body = await response.aread()

    assert response.status_code == 200
    assert b"event: end" in body
