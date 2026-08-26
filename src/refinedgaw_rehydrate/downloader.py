from __future__ import annotations

import asyncio
import datetime as dt
import email.utils
import hashlib
import json
import os
import random
import tempfile
import time
import urllib.parse
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import httpx

from . import __version__
from .models import DownloadConfig, FetchOutcome, SourceRecord, utc_now
from .rate_limit import DomainRateLimiter
from .robots import RobotsDecision, RobotsFetch, RobotsPolicy
from .safety import (
    DestinationValidator,
    DnsResolutionError,
    UnsafeUrlError,
    normalize_url,
)
from .state import StateStore

TRANSIENT_STATUSES = {408, 425, 429, 500, 502, 503, 504}
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
ROBOTS_MAX_BYTES = 1024 * 1024


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _status_category(status: int) -> str:
    if status in {401, 403}:
        return "access_denied"
    if status in {404, 410}:
        return "missing"
    if status == 429:
        return "rate_limited"
    if 400 <= status < 500:
        return "client_error"
    if status >= 500:
        return "server_error"
    return "http_error"


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            parsed = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.UTC)
        return max(0.0, (parsed - dt.datetime.now(dt.UTC)).total_seconds())


def _backoff(attempt: int) -> float:
    base = min(30.0, 0.5 * (2**attempt))
    return base + random.uniform(0.0, base * 0.25)


def _retry_delay(value: str | None, attempt: int, maximum: float) -> float:
    requested = _retry_after(value)
    return min(requested, maximum) if requested is not None else _backoff(attempt)


class Downloader:
    def __init__(self, config: DownloadConfig, state: StateStore) -> None:
        self.config = config
        self.state = state
        self.validator = DestinationValidator(
            allow_private_network=config.allow_private_network
        )
        self.limiter = DomainRateLimiter(
            requests_per_minute=config.requests_per_minute,
            min_interval_seconds=config.min_interval_seconds,
        )
        timeout = httpx.Timeout(
            connect=config.connect_timeout_seconds,
            read=config.read_timeout_seconds,
            write=config.read_timeout_seconds,
            pool=config.connect_timeout_seconds,
        )
        limits = httpx.Limits(
            max_connections=config.concurrency,
            max_keepalive_connections=config.concurrency,
        )
        self.client = httpx.AsyncClient(
            timeout=timeout,
            limits=limits,
            follow_redirects=False,
            trust_env=False,
            headers={
                "User-Agent": config.user_agent,
                "Accept-Encoding": "identity",
            },
        )
        self.robots = RobotsPolicy(
            state=state,
            fetcher=self._fetch_robots,
            user_agent=config.robots_user_agent,
            ttl_seconds=config.robots_ttl_seconds,
            failure_ttl_seconds=config.robots_failure_ttl_seconds,
        )
        (config.output_dir / "content").mkdir(parents=True, exist_ok=True)
        (config.output_dir / "tmp").mkdir(parents=True, exist_ok=True)

    async def close(self) -> None:
        await self.client.aclose()

    async def _fetch_robots(self, url: str) -> RobotsFetch:
        outcome, body = await self._fetch(
            url,
            max_bytes=ROBOTS_MAX_BYTES,
            obey_robots=False,
            keep_file=False,
            file_extension=None,
            accept="text/plain,*/*;q=0.1",
        )
        return RobotsFetch(
            status_code=outcome.status_code,
            body=body if outcome.success else None,
            error=outcome.error,
        )

    async def _prepare_url(self, url: str) -> str:
        normalized = normalize_url(
            url,
            allow_nonstandard_ports=self.config.allow_nonstandard_ports,
        )
        await self.validator.validate(normalized)
        return normalized

    async def _robots_decision(self, url: str) -> RobotsDecision:
        decision = await self.robots.allowed(url)
        await self.limiter.apply_robots_policy(
            url,
            crawl_delay=decision.crawl_delay,
            request_rate=decision.request_rate,
        )
        return decision

    async def _send(self, url: str, accept: str) -> httpx.Response:
        await self.limiter.acquire(url)
        request = self.client.build_request("GET", url, headers={"Accept": accept})
        return await self.client.send(request, stream=True)

    async def _fetch(
        self,
        requested_url: str,
        *,
        max_bytes: int,
        obey_robots: bool,
        keep_file: bool,
        file_extension: str | None,
        accept: str,
    ) -> tuple[FetchOutcome, bytes | None]:
        started = time.monotonic()
        current = requested_url
        redirects = 0
        redirected = False
        robots_reason: str | None = None
        while True:
            try:
                current = await self._prepare_url(current)
            except DnsResolutionError as error:
                return (
                    FetchOutcome(
                        False,
                        "dns_error",
                        requested_url,
                        final_url=current,
                        redirected=redirected,
                        redirect_count=redirects,
                        elapsed_seconds=round(time.monotonic() - started, 3),
                        error=str(error),
                    ),
                    None,
                )
            except UnsafeUrlError as error:
                return (
                    FetchOutcome(
                        False,
                        "unsafe_url",
                        requested_url,
                        final_url=current,
                        redirected=redirected,
                        redirect_count=redirects,
                        elapsed_seconds=round(time.monotonic() - started, 3),
                        error=str(error),
                    ),
                    None,
                )
            if obey_robots:
                decision = await self._robots_decision(current)
                robots_reason = decision.reason
                if not decision.allowed:
                    return (
                        FetchOutcome(
                            False,
                            "robots_denied",
                            requested_url,
                            final_url=current,
                            redirected=redirected,
                            redirect_count=redirects,
                            elapsed_seconds=round(time.monotonic() - started, 3),
                            robots_reason=decision.reason,
                        ),
                        None,
                    )
            follow_redirect = False
            for attempt in range(self.config.retries + 1):
                response: httpx.Response | None = None
                temporary_path: Path | None = None
                try:
                    response = await self._send(current, accept)
                    status = response.status_code
                    if status in TRANSIENT_STATUSES and attempt < self.config.retries:
                        delay = _retry_delay(
                            response.headers.get("Retry-After"),
                            attempt,
                            self.config.max_retry_delay_seconds,
                        )
                        await response.aclose()
                        await self.limiter.defer(current, delay)
                        continue
                    if status in REDIRECT_STATUSES:
                        location = response.headers.get("Location")
                        await response.aclose()
                        if not location:
                            return (
                                FetchOutcome(
                                    False,
                                    "redirect_without_location",
                                    requested_url,
                                    final_url=current,
                                    status_code=status,
                                    redirected=redirected,
                                    redirect_count=redirects,
                                    elapsed_seconds=round(
                                        time.monotonic() - started, 3
                                    ),
                                    robots_reason=robots_reason,
                                ),
                                None,
                            )
                        redirects += 1
                        redirected = True
                        if redirects > self.config.max_redirects:
                            return (
                                FetchOutcome(
                                    False,
                                    "too_many_redirects",
                                    requested_url,
                                    final_url=current,
                                    status_code=status,
                                    redirected=True,
                                    redirect_count=redirects,
                                    elapsed_seconds=round(
                                        time.monotonic() - started, 3
                                    ),
                                    robots_reason=robots_reason,
                                ),
                                None,
                            )
                        current = urllib.parse.urljoin(current, location)
                        follow_redirect = True
                        break
                    if not 200 <= status < 300:
                        await response.aclose()
                        return (
                            FetchOutcome(
                                False,
                                _status_category(status),
                                requested_url,
                                final_url=current,
                                status_code=status,
                                content_type=response.headers.get("Content-Type"),
                                redirected=redirected,
                                redirect_count=redirects,
                                elapsed_seconds=round(time.monotonic() - started, 3),
                                robots_reason=robots_reason,
                            ),
                            None,
                        )
                    length_header: int | None = None
                    raw_length = response.headers.get("Content-Length")
                    if raw_length:
                        try:
                            length_header = int(raw_length)
                        except ValueError:
                            length_header = None
                    if length_header is not None and length_header > max_bytes:
                        await response.aclose()
                        return (
                            FetchOutcome(
                                False,
                                "too_large",
                                requested_url,
                                final_url=current,
                                status_code=status,
                                content_type=response.headers.get("Content-Type"),
                                content_length_header=length_header,
                                redirected=redirected,
                                redirect_count=redirects,
                                elapsed_seconds=round(time.monotonic() - started, 3),
                                robots_reason=robots_reason,
                            ),
                            None,
                        )
                    buffer = bytearray() if not keep_file else None
                    digest = hashlib.sha256()
                    downloaded = 0
                    if keep_file:
                        descriptor, name = tempfile.mkstemp(
                            prefix="download-",
                            suffix=".part",
                            dir=self.config.output_dir / "tmp",
                        )
                        os.close(descriptor)
                        temporary_path = Path(name)
                        handle = temporary_path.open("wb")
                    else:
                        handle = None
                    try:
                        async for chunk in response.aiter_bytes():
                            downloaded += len(chunk)
                            if downloaded > max_bytes:
                                raise OverflowError("Response exceeded download limit")
                            digest.update(chunk)
                            if handle is not None:
                                handle.write(chunk)
                            else:
                                assert buffer is not None
                                buffer.extend(chunk)
                    except OverflowError as error:
                        if handle is not None:
                            handle.close()
                        if temporary_path is not None:
                            temporary_path.unlink(missing_ok=True)
                        await response.aclose()
                        return (
                            FetchOutcome(
                                False,
                                "too_large",
                                requested_url,
                                final_url=current,
                                status_code=status,
                                content_type=response.headers.get("Content-Type"),
                                content_length_header=length_header,
                                downloaded_bytes=downloaded,
                                redirected=redirected,
                                redirect_count=redirects,
                                elapsed_seconds=round(time.monotonic() - started, 3),
                                error=str(error),
                                robots_reason=robots_reason,
                            ),
                            None,
                        )
                    finally:
                        if handle is not None and not handle.closed:
                            handle.close()
                    await response.aclose()
                    if downloaded == 0:
                        if temporary_path is not None:
                            temporary_path.unlink(missing_ok=True)
                        return (
                            FetchOutcome(
                                False,
                                "empty_response",
                                requested_url,
                                final_url=current,
                                status_code=status,
                                content_type=response.headers.get("Content-Type"),
                                content_length_header=length_header,
                                redirected=redirected,
                                redirect_count=redirects,
                                elapsed_seconds=round(time.monotonic() - started, 3),
                                robots_reason=robots_reason,
                            ),
                            None,
                        )
                    sha256 = digest.hexdigest()
                    local_path = None
                    if temporary_path is not None:
                        extension = file_extension or ".bin"
                        destination = (
                            self.config.output_dir
                            / "content"
                            / sha256[:2]
                            / f"{sha256}{extension}"
                        )
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        if destination.exists():
                            temporary_path.unlink(missing_ok=True)
                        else:
                            os.replace(temporary_path, destination)
                        local_path = str(
                            destination.relative_to(self.config.output_dir)
                        )
                    return (
                        FetchOutcome(
                            True,
                            "downloaded",
                            requested_url,
                            final_url=current,
                            status_code=status,
                            content_type=response.headers.get("Content-Type"),
                            content_length_header=length_header,
                            downloaded_bytes=downloaded,
                            sha256=sha256,
                            local_path=local_path,
                            redirected=redirected,
                            redirect_count=redirects,
                            elapsed_seconds=round(time.monotonic() - started, 3),
                            robots_reason=robots_reason,
                        ),
                        bytes(buffer) if buffer is not None else None,
                    )
                except (httpx.TimeoutException, httpx.TransportError) as error:
                    if response is not None:
                        await response.aclose()
                    if temporary_path is not None:
                        temporary_path.unlink(missing_ok=True)
                    if attempt < self.config.retries:
                        await self.limiter.defer(current, _backoff(attempt))
                        continue
                    category = (
                        "timeout"
                        if isinstance(error, httpx.TimeoutException)
                        else "transport_error"
                    )
                    return (
                        FetchOutcome(
                            False,
                            category,
                            requested_url,
                            final_url=current,
                            redirected=redirected,
                            redirect_count=redirects,
                            elapsed_seconds=round(time.monotonic() - started, 3),
                            error=f"{type(error).__name__}: {error}",
                            robots_reason=robots_reason,
                        ),
                        None,
                    )
            if follow_redirect:
                continue
            raise AssertionError("Request retry loop exited unexpectedly")

    async def fetch_record_url(self, record: SourceRecord, url: str) -> FetchOutcome:
        accept = (
            "application/pdf,*/*;q=0.1"
            if record.format == "pdf"
            else "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1"
        )
        outcome, _ = await self._fetch(
            url,
            max_bytes=self.config.max_bytes(record.format),
            obey_robots=True,
            keep_file=True,
            file_extension=".pdf" if record.format == "pdf" else ".html",
            accept=accept,
        )
        return outcome

    @staticmethod
    def _failure_status(outcomes: list[dict[str, Any]]) -> str:
        categories = [str(outcome.get("category")) for outcome in outcomes]
        if categories and all(category == "missing" for category in categories):
            return "missing"
        for category in (
            "robots_denied",
            "access_denied",
            "too_large",
            "rate_limited",
            "timeout",
            "transport_error",
            "dns_error",
            "unsafe_url",
            "server_error",
            "client_error",
        ):
            if category in categories:
                return category
        return categories[-1] if categories else "no_candidates"

    async def process_record(self, record: SourceRecord) -> str:
        self.state.mark_in_progress(record)
        attempts: list[dict[str, Any]] = []
        selected: dict[str, Any] | None = None
        first_download: dict[str, Any] | None = None
        for candidate_index, url in enumerate(record.url_candidates):
            outcome = await self.fetch_record_url(record, url)
            attempt = outcome.as_dict()
            attempt["candidate_index"] = candidate_index
            if outcome.success and record.expected_pdf_sha256:
                attempt["pdf_byte_match"] = outcome.sha256 == record.expected_pdf_sha256
            else:
                attempt["pdf_byte_match"] = None
            attempts.append(attempt)
            self.state.add_attempt(record, candidate_index, attempt)
            if not outcome.success:
                continue
            if first_download is None:
                first_download = attempt
            if record.expected_pdf_sha256:
                if attempt["pdf_byte_match"]:
                    selected = attempt
                    break
                continue
            selected = attempt
            break
        if selected is None:
            selected = first_download
        status = (
            "downloaded" if selected is not None else self._failure_status(attempts)
        )
        result = {
            "record_id": record.record_id,
            "format": record.format,
            "expected_text_id": record.record_id,
            "expected_pdf_sha256": record.expected_pdf_sha256,
            "source_timestamp": record.source_timestamp,
            "quality_score": record.quality_score,
            "stratum": record.stratum,
            "release_rank": record.release_rank,
            "sample_rank": record.sample_rank,
            "status": status,
            "selected": selected,
            "attempts": attempts,
            "completed_at": utc_now(),
        }
        self.state.complete(record, status=status, result=result)
        return status


async def run_download(
    records: Iterable[SourceRecord],
    config: DownloadConfig,
    *,
    input_description: str | list[str],
    limit: int | None = None,
) -> dict[str, Any]:
    config.output_dir.mkdir(parents=True, exist_ok=True)
    started_at = utc_now()
    state = StateStore(config.output_dir / "state.sqlite3")
    downloader = Downloader(config, state)
    queue: asyncio.Queue[SourceRecord | None] = asyncio.Queue(
        maxsize=max(1, config.concurrency * 4)
    )
    run_counts: Counter[str] = Counter()
    completed = 0

    async def producer() -> None:
        for ordinal, record in enumerate(records, start=1):
            if limit is not None and ordinal > limit:
                break
            state.register(record, ordinal)
            await queue.put(record)
        for _ in range(config.concurrency):
            await queue.put(None)

    async def worker() -> None:
        nonlocal completed
        while True:
            record = await queue.get()
            try:
                if record is None:
                    return
                if state.should_skip(
                    record,
                    retry_failures=config.retry_failures,
                ):
                    run_counts["skipped"] += 1
                    continue
                try:
                    status = await downloader.process_record(record)
                except Exception as error:
                    status = "internal_error"
                    result = {
                        "record_id": record.record_id,
                        "format": record.format,
                        "expected_text_id": record.record_id,
                        "expected_pdf_sha256": record.expected_pdf_sha256,
                        "source_timestamp": record.source_timestamp,
                        "quality_score": record.quality_score,
                        "stratum": record.stratum,
                        "release_rank": record.release_rank,
                        "sample_rank": record.sample_rank,
                        "status": status,
                        "error": f"{type(error).__name__}: {error}",
                        "completed_at": utc_now(),
                    }
                    state.complete(record, status=status, result=result)
                run_counts[status] += 1
                completed += 1
                if completed == 1 or completed % 100 == 0:
                    print(f"Processed {completed:,} records", flush=True)
            finally:
                queue.task_done()

    try:
        tasks = [asyncio.create_task(worker()) for _ in range(config.concurrency)]
        await asyncio.gather(producer(), *tasks)
        state.export_results(config.output_dir / "results.jsonl")
        manifest = {
            "schema_version": 1,
            "tool": "refinedgaw-rehydrate",
            "tool_version": __version__,
            "started_at": started_at,
            "completed_at": utc_now(),
            "input": input_description,
            "limit": limit,
            "configuration": config.public_dict(),
            "this_run": dict(sorted(run_counts.items())),
            "state": state.summary(),
        }
        atomic_json(config.output_dir / "run-manifest.json", manifest)
        return manifest
    finally:
        await downloader.close()
        state.close()
