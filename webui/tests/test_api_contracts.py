from __future__ import annotations

import inspect
import os
import subprocess
import sys
from pathlib import Path

from app.routers.system import healthz


REPO_ROOT = Path(__file__).resolve().parents[2]


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
