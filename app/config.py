from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("DATA_DIR", str(ROOT / "data"))).resolve()
DB_PATH = DATA_DIR / "app.sqlite3"
TASK_DIR = DATA_DIR / "tasks"

AUTH_USER = os.environ["WEBUI_BASIC_AUTH_USER"]
AUTH_PASSWORD = os.environ["WEBUI_BASIC_AUTH_PASSWORD"]
