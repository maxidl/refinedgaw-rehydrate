import asyncio
import socket

import pytest

from refinedgaw_rehydrate.rate_limit import DomainRateLimiter, registrable_domain
from refinedgaw_rehydrate.safety import (
    DestinationValidator,
    DnsResolutionError,
    UnsafeUrlError,
    normalize_url,
)


def test_url_safety_rejects_credentials_and_localhost() -> None:
    with pytest.raises(UnsafeUrlError):
        normalize_url(
            "https://user:secret@example.org/path",
            allow_nonstandard_ports=False,
        )
    with pytest.raises(UnsafeUrlError):
        normalize_url("http://localhost/path", allow_nonstandard_ports=False)


def test_destination_validator_rejects_private_address() -> None:
    validator = DestinationValidator()
    with pytest.raises(UnsafeUrlError):
        asyncio.run(validator.validate("http://127.0.0.1/"))


def test_destination_validator_distinguishes_dns_failure(monkeypatch) -> None:
    def fail_resolution(*_args, **_kwargs):
        raise socket.gaierror("test failure")

    monkeypatch.setattr(socket, "getaddrinfo", fail_resolution)
    validator = DestinationValidator()
    with pytest.raises(DnsResolutionError):
        asyncio.run(validator.validate("https://example.invalid/"))


def test_rate_limit_key_is_shared_by_subdomains() -> None:
    assert registrable_domain("https://a.example.co.uk/x") == "example.co.uk"
    assert registrable_domain("https://b.example.co.uk/y") == "example.co.uk"


def test_robots_delay_is_not_shared_by_sibling_origins() -> None:
    async def exercise() -> None:
        limiter = DomainRateLimiter(
            requests_per_minute=100,
            min_interval_seconds=0,
        )
        slow = "https://slow.example.org/a"
        sibling = "https://fast.example.org/b"
        await limiter.apply_robots_policy(
            slow,
            crawl_delay=60,
            request_rate=None,
        )
        await limiter.acquire(slow)
        await asyncio.wait_for(limiter.acquire(sibling), timeout=0.1)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(limiter.acquire(slow), timeout=0.01)

    asyncio.run(exercise())


def test_general_rate_limit_is_shared_by_sibling_origins() -> None:
    async def exercise() -> None:
        limiter = DomainRateLimiter(
            requests_per_minute=1,
            min_interval_seconds=0,
        )
        await limiter.acquire("https://a.example.org/a")
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(
                limiter.acquire("https://b.example.org/b"),
                timeout=0.01,
            )

    asyncio.run(exercise())
