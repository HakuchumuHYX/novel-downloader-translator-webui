from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import DownloadOptions, DownloadResult


class BackendAdapter(ABC):
    name = "base"

    @abstractmethod
    def supports(self, options: DownloadOptions) -> bool:
        raise NotImplementedError

    @abstractmethod
    def fetch(self, options: DownloadOptions) -> DownloadResult:
        raise NotImplementedError
