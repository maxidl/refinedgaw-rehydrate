from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
import urllib.parse
from dataclasses import dataclass, field


class UnsafeUrlError(ValueError):
    """Raised when a URL is not safe for unattended retrieval."""


class DnsResolutionError(ConnectionError):
    """Raised when a public hostname cannot be resolved."""


def normalize_url(url: str, *, allow_nonstandard_ports: bool) -> str:
    value = url.strip()
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise UnsafeUrlError(f"Malformed URL: {error}") from error
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise UnsafeUrlError("Only HTTP and HTTPS URLs are supported")
    if not parsed.hostname:
        raise UnsafeUrlError("URL has no hostname")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeUrlError("Credentials in URLs are not allowed")
    expected_port = 443 if scheme == "https" else 80
    if port is not None and port != expected_port and not allow_nonstandard_ports:
        raise UnsafeUrlError("Nonstandard ports are disabled")
    hostname = parsed.hostname.encode("idna").decode("ascii").lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise UnsafeUrlError("Localhost destinations are not allowed")
    if hostname.endswith(".local"):
        raise UnsafeUrlError("Local-network destinations are not allowed")
    host = hostname
    if ":" in hostname:
        host = f"[{hostname}]"
    if port is not None:
        host = f"{host}:{port}"
    path = parsed.path or "/"
    return urllib.parse.urlunsplit((scheme, host, path, parsed.query, ""))


@dataclass
class DestinationValidator:
    allow_private_network: bool = False
    cache_ttl_seconds: float = 300.0
    _cache: dict[tuple[str, int], float] = field(default_factory=dict)
    _locks: dict[tuple[str, int], asyncio.Lock] = field(default_factory=dict)

    async def validate(self, url: str) -> None:
        if self.allow_private_network:
            return
        parsed = urllib.parse.urlsplit(url)
        hostname = parsed.hostname
        if hostname is None:
            raise UnsafeUrlError("URL has no hostname")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        key = (hostname, port)
        now = time.monotonic()
        if self._cache.get(key, 0.0) > now:
            return
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            if self._cache.get(key, 0.0) > now:
                return
            loop = asyncio.get_running_loop()
            try:
                addresses = await loop.run_in_executor(
                    None,
                    lambda: socket.getaddrinfo(
                        hostname,
                        port,
                        type=socket.SOCK_STREAM,
                    ),
                )
            except socket.gaierror as error:
                raise DnsResolutionError(
                    f"Hostname resolution failed: {error}"
                ) from error
            if not addresses:
                raise DnsResolutionError("Hostname resolved to no addresses")
            for *_, socket_address in addresses:
                address = ipaddress.ip_address(socket_address[0])
                if not address.is_global:
                    raise UnsafeUrlError(
                        f"Destination resolves to a non-public address: {address}"
                    )
            self._cache[key] = time.monotonic() + self.cache_ttl_seconds
