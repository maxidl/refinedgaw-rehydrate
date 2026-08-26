from __future__ import annotations

import asyncio
import collections
import math
import time
import urllib.parse
from dataclasses import dataclass, field

import tldextract

_extract_domain = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)


def registrable_domain(url: str) -> str:
    hostname = urllib.parse.urlsplit(url).hostname or ""
    result = _extract_domain(hostname)
    return result.top_domain_under_public_suffix or hostname.lower()


@dataclass
class _RateBucket:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    timestamps: collections.deque[float] = field(default_factory=collections.deque)
    deferred_until: float = 0.0
    robots_min_interval: float = 0.0
    robots_requests_per_minute: int | None = None


@dataclass
class DomainRateLimiter:
    requests_per_minute: int
    min_interval_seconds: float
    _domain_buckets: dict[str, _RateBucket] = field(default_factory=dict)
    _origin_buckets: dict[str, _RateBucket] = field(default_factory=dict)

    def _domain_bucket(self, url: str) -> _RateBucket:
        return self._domain_buckets.setdefault(registrable_domain(url), _RateBucket())

    def _origin_bucket(self, url: str) -> _RateBucket:
        parsed = urllib.parse.urlsplit(url)
        origin = urllib.parse.urlunsplit(
            (parsed.scheme.lower(), parsed.netloc.lower(), "", "", "")
        )
        return self._origin_buckets.setdefault(origin, _RateBucket())

    @staticmethod
    def _trim(bucket: _RateBucket, now: float) -> None:
        while bucket.timestamps and bucket.timestamps[0] <= now - 60.0:
            bucket.timestamps.popleft()

    @staticmethod
    def _delay(
        bucket: _RateBucket,
        now: float,
        *,
        requests_per_minute: int | None,
        min_interval_seconds: float,
    ) -> float:
        delay = max(0.0, bucket.deferred_until - now)
        if bucket.timestamps:
            delay = max(
                delay,
                bucket.timestamps[-1] + min_interval_seconds - now,
            )
        if (
            requests_per_minute is not None
            and len(bucket.timestamps) >= requests_per_minute
        ):
            delay = max(delay, bucket.timestamps[0] + 60.0 - now)
        return delay

    async def acquire(self, url: str) -> None:
        domain_bucket = self._domain_bucket(url)
        origin_bucket = self._origin_bucket(url)
        while True:
            async with domain_bucket.lock, origin_bucket.lock:
                now = time.monotonic()
                self._trim(domain_bucket, now)
                self._trim(origin_bucket, now)
                domain_delay = self._delay(
                    domain_bucket,
                    now,
                    requests_per_minute=self.requests_per_minute,
                    min_interval_seconds=self.min_interval_seconds,
                )
                origin_delay = self._delay(
                    origin_bucket,
                    now,
                    requests_per_minute=origin_bucket.robots_requests_per_minute,
                    min_interval_seconds=origin_bucket.robots_min_interval,
                )
                delay = max(domain_delay, origin_delay)
                if delay <= 0:
                    domain_bucket.timestamps.append(now)
                    origin_bucket.timestamps.append(now)
                    return
            await asyncio.sleep(delay)

    async def defer(self, url: str, seconds: float) -> None:
        bucket = self._origin_bucket(url)
        async with bucket.lock:
            bucket.deferred_until = max(
                bucket.deferred_until,
                time.monotonic() + max(0.0, seconds),
            )

    async def apply_robots_policy(
        self,
        url: str,
        *,
        crawl_delay: float | None,
        request_rate: tuple[int, int] | None,
    ) -> None:
        bucket = self._origin_bucket(url)
        async with bucket.lock:
            if crawl_delay is not None and crawl_delay > 0:
                bucket.robots_min_interval = max(
                    bucket.robots_min_interval,
                    crawl_delay,
                )
            if request_rate is not None:
                requests, seconds = request_rate
                if requests > 0 and seconds > 0:
                    per_minute = max(1, math.floor(requests * 60 / seconds))
                    current = bucket.robots_requests_per_minute
                    bucket.robots_requests_per_minute = (
                        per_minute if current is None else min(current, per_minute)
                    )
