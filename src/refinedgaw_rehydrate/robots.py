from __future__ import annotations

import asyncio
import time
import urllib.parse
import urllib.robotparser
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .state import StateStore


@dataclass(frozen=True)
class RobotsFetch:
    status_code: int | None
    body: bytes | None
    error: str | None = None


@dataclass(frozen=True)
class RobotsDecision:
    allowed: bool
    reason: str
    crawl_delay: float | None = None
    request_rate: tuple[int, int] | None = None


RobotsFetcher = Callable[[str], Awaitable[RobotsFetch]]


class RobotsPolicy:
    def __init__(
        self,
        *,
        state: StateStore,
        fetcher: RobotsFetcher,
        user_agent: str,
        ttl_seconds: int,
        failure_ttl_seconds: int,
    ) -> None:
        self.state = state
        self.fetcher = fetcher
        self.user_agent = user_agent
        self.ttl_seconds = ttl_seconds
        self.failure_ttl_seconds = failure_ttl_seconds
        self._inflight: dict[str, asyncio.Task[None]] = {}
        self._guard = asyncio.Lock()

    @staticmethod
    def _origin(url: str) -> str:
        parsed = urllib.parse.urlsplit(url)
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))

    async def _refresh(self, origin: str) -> None:
        robots_url = f"{origin}/robots.txt"
        result = await self.fetcher(robots_url)
        fetched_at = time.time()
        status = result.status_code
        body: str | None = None
        error = result.error
        if status == 200 and result.body is not None:
            policy = "parsed"
            body = result.body.decode("utf-8", errors="replace")
            ttl = self.ttl_seconds
        elif status in {404, 410}:
            policy = "allow_all"
            ttl = self.ttl_seconds
        elif status in {401, 403}:
            policy = "disallow_all"
            ttl = self.ttl_seconds
        else:
            policy = "disallow_all"
            ttl = self.failure_ttl_seconds
        self.state.put_robots(
            origin=origin,
            fetched_at=fetched_at,
            expires_at=fetched_at + ttl,
            status_code=status,
            policy=policy,
            body=body,
            error=error,
        )

    async def _ensure_current(self, origin: str) -> None:
        cached = self.state.get_robots(origin)
        if cached is not None and float(cached["expires_at"]) > time.time():
            return
        async with self._guard:
            task = self._inflight.get(origin)
            if task is None:
                task = asyncio.create_task(self._refresh(origin))
                self._inflight[origin] = task
        try:
            await task
        finally:
            async with self._guard:
                if self._inflight.get(origin) is task:
                    self._inflight.pop(origin, None)

    async def allowed(self, url: str) -> RobotsDecision:
        origin = self._origin(url)
        await self._ensure_current(origin)
        cached = self.state.get_robots(origin)
        if cached is None:
            return RobotsDecision(False, "robots_cache_error")
        policy = str(cached["policy"])
        if policy == "allow_all":
            return RobotsDecision(True, "robots_not_present")
        if policy == "disallow_all":
            status = cached["status_code"]
            reason = f"robots_unavailable_{status}" if status else "robots_unavailable"
            return RobotsDecision(False, reason)
        parser = urllib.robotparser.RobotFileParser()
        parser.set_url(f"{origin}/robots.txt")
        parser.parse((cached["body"] or "").splitlines())
        allowed = parser.can_fetch(self.user_agent, url)
        crawl_delay = parser.crawl_delay(self.user_agent)
        if crawl_delay is None:
            crawl_delay = parser.crawl_delay("*")
        request_rate = parser.request_rate(self.user_agent)
        if request_rate is None:
            request_rate = parser.request_rate("*")
        rate_tuple = None
        if request_rate is not None:
            rate_tuple = (request_rate.requests, request_rate.seconds)
        return RobotsDecision(
            allowed,
            "robots_allowed" if allowed else "robots_disallowed",
            crawl_delay=float(crawl_delay) if crawl_delay is not None else None,
            request_rate=rate_tuple,
        )
