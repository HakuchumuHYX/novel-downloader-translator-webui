from __future__ import annotations

from importlib import import_module


__all__ = ["DownloadJob", "DownloadOptions"]


def __getattr__(name: str):
    if name == "DownloadJob":
        return import_module(".job", __name__).DownloadJob
    if name == "DownloadOptions":
        return import_module(".models", __name__).DownloadOptions
    raise AttributeError(name)
