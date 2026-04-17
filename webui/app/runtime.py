from __future__ import annotations

from typing import TYPE_CHECKING

from .config import AppConfig, get_config

if TYPE_CHECKING:
    from .services.worker import TaskWorker


_cfg: AppConfig | None = None
_worker: TaskWorker | None = None


def get_app_config() -> AppConfig:
    global _cfg
    if _cfg is None:
        _cfg = get_config()
    return _cfg


def set_app_config(cfg: AppConfig | None) -> None:
    global _cfg
    _cfg = cfg


def get_worker() -> TaskWorker | None:
    return _worker


def set_worker(worker: TaskWorker | None) -> None:
    global _worker
    _worker = worker
