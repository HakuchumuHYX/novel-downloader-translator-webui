from __future__ import annotations

import sys
from pathlib import Path


WEBUI_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = WEBUI_ROOT.parent
BOOKMAKER_ROOT = REPO_ROOT / "bilingual_book_maker"
DOWNLOADER_ROOT = REPO_ROOT / "syosetu_novel_downloader"

for path in (WEBUI_ROOT, REPO_ROOT, BOOKMAKER_ROOT, DOWNLOADER_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
