from __future__ import annotations

import dataclasses
import datetime as dt
from pathlib import Path
from typing import Any

from . import __version__

DEFAULT_USER_AGENT = f"RefinedGAW-Rehydrate/{__version__}"


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


@dataclasses.dataclass(frozen=True)
class SourceRecord:
    record_id: str
    format: str
    url_candidates: tuple[str, ...]
    expected_pdf_sha256: str | None = None
    source_timestamp: str | None = None
    quality_score: int | None = None
    stratum: str | None = None
    release_rank: int | None = None
    sample_rank: int | None = None

    @property
    def key(self) -> str:
        return f"{self.format}:{self.record_id}"


@dataclasses.dataclass(frozen=True)
class DownloadConfig:
    output_dir: Path
    concurrency: int = 16
    requests_per_minute: int = 30
    min_interval_seconds: float = 1.0
    max_urls_per_record: int = 3
    connect_timeout_seconds: float = 10.0
    read_timeout_seconds: float = 60.0
    retries: int = 2
    max_retry_delay_seconds: float = 60.0
    max_redirects: int = 8
    max_html_bytes: int = 20 * 1024 * 1024
    max_pdf_bytes: int = 200 * 1024 * 1024
    robots_ttl_seconds: int = 24 * 60 * 60
    robots_failure_ttl_seconds: int = 15 * 60
    user_agent: str = DEFAULT_USER_AGENT
    robots_user_agent: str = "RefinedGAW-Rehydrate"
    allow_private_network: bool = False
    allow_nonstandard_ports: bool = False
    retry_failures: bool = False

    def max_bytes(self, format_name: str) -> int:
        if format_name == "pdf":
            return self.max_pdf_bytes
        return self.max_html_bytes

    def public_dict(self) -> dict[str, Any]:
        value = dataclasses.asdict(self)
        value["output_dir"] = str(self.output_dir)
        return value


@dataclasses.dataclass
class FetchOutcome:
    success: bool
    category: str
    requested_url: str
    final_url: str | None = None
    status_code: int | None = None
    content_type: str | None = None
    content_length_header: int | None = None
    downloaded_bytes: int = 0
    sha256: str | None = None
    local_path: str | None = None
    redirected: bool = False
    redirect_count: int = 0
    elapsed_seconds: float = 0.0
    error: str | None = None
    robots_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)
