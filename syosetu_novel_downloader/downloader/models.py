from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

SiteId = Literal["auto", "syosetu", "novel18", "kakuyomu"]
BackendId = Literal["auto", "node", "native"]
PaidPolicy = Literal["skip", "fail", "metadata"]


@dataclass
class DownloadOptions:
    url: str
    site: SiteId = "auto"
    backend: BackendId = "auto"
    proxy: str = ""
    output_dir: Path = Path("./downloads")
    save_format: str = "txt"
    record_chapter_number: bool = False
    merge_all: bool = False
    merged_name: str = ""
    cookie: str = ""
    cookie_file: str = ""
    paid_policy: PaidPolicy = "skip"
    rate_limit: float = 1.0
    retries: int = 2
    timeout: int = 120


@dataclass
class Chapter:
    index: int
    title: str
    content: str
    volume: str = ""
    source_path: str = ""


@dataclass
class BookMeta:
    title: str
    author: str = ""
    source_url: str = ""
    site: str = ""
    expected_chapter_count: int = 0


@dataclass
class DownloadResult:
    backend: str
    site: str
    meta: BookMeta
    chapters: list[Chapter] = field(default_factory=list)
    skipped_chapters: int = 0
    skipped_reasons: list[str] = field(default_factory=list)
    raw_metadata_path: str = ""


@dataclass
class RunManifest:
    status: str
    started_at: str
    finished_at: str
    duration_seconds: float
    backend_used: str
    site: str
    source_url: str
    output_dir: str
    title: str
    chapter_count: int
    expected_chapter_count: int
    skipped_chapters: int
    skipped_reasons: list[str]
    paid_policy: str
    errors: list[str]

    @classmethod
    def build(
        cls,
        *,
        status: str,
        backend_used: str,
        site: str,
        source_url: str,
        output_dir: Path,
        title: str,
        chapter_count: int,
        expected_chapter_count: int,
        skipped_chapters: int,
        skipped_reasons: list[str],
        paid_policy: str,
        errors: list[str],
        started: datetime,
        finished: datetime,
    ) -> "RunManifest":
        return cls(
            status=status,
            started_at=started.isoformat(),
            finished_at=finished.isoformat(),
            duration_seconds=(finished - started).total_seconds(),
            backend_used=backend_used,
            site=site,
            source_url=source_url,
            output_dir=str(output_dir),
            title=title,
            chapter_count=chapter_count,
            expected_chapter_count=expected_chapter_count,
            skipped_chapters=skipped_chapters,
            skipped_reasons=skipped_reasons,
            paid_policy=paid_policy,
            errors=errors,
        )
