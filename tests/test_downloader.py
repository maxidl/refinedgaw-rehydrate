from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import threading
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from refinedgaw_rehydrate.downloader import Downloader, _retry_delay, run_download
from refinedgaw_rehydrate.models import DownloadConfig, SourceRecord


@pytest.fixture
def source_server():
    counts: Counter[str] = Counter()
    pdf_changed = b"%PDF-1.4\nchanged\n%%EOF\n"
    pdf_expected = b"%PDF-1.4\nexpected\n%%EOF\n"

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802
            counts[self.path] += 1
            if self.path == "/robots.txt":
                self._send(
                    200,
                    b"User-agent: RefinedGAW-Rehydrate\nDisallow: /blocked\n",
                    "text/plain",
                )
            elif self.path == "/ok":
                self._send(200, b"<html><body>ok</body></html>", "text/html")
            elif self.path == "/blocked":
                self._send(200, b"must not be fetched", "text/html")
            elif self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/ok")
                self.send_header("Content-Length", "0")
                self.end_headers()
            elif self.path == "/flaky":
                if counts[self.path] == 1:
                    self.send_response(503)
                    self.send_header("Retry-After", "0")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                else:
                    self._send(200, b"recovered", "text/plain")
            elif self.path == "/large":
                self._send(200, b"x" * 128, "text/plain")
            elif self.path == "/changed.pdf":
                self._send(200, pdf_changed, "application/pdf")
            elif self.path == "/expected.pdf":
                self._send(200, pdf_expected, "application/pdf")
            else:
                self._send(404, b"not found", "text/plain")

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, counts, pdf_expected
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _config(output: Path) -> DownloadConfig:
    return DownloadConfig(
        output_dir=output,
        concurrency=4,
        requests_per_minute=10_000,
        min_interval_seconds=0,
        retries=1,
        allow_private_network=True,
        allow_nonstandard_ports=True,
    )


def _results(path: Path) -> dict[str, dict]:
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    return {row["record_id"]: row for row in rows}


def test_server_retry_after_is_capped() -> None:
    assert _retry_delay("3600", attempt=0, maximum=60) == 60


def test_download_robots_redirect_hash_and_resume(tmp_path, source_server) -> None:
    server, counts, pdf_expected = source_server
    origin = f"http://127.0.0.1:{server.server_port}"
    records = [
        SourceRecord("html-ok", "html", (f"{origin}/ok",)),
        SourceRecord("html-blocked", "html", (f"{origin}/blocked",)),
        SourceRecord("html-redirect", "html", (f"{origin}/redirect",)),
        SourceRecord("html-flaky", "html", (f"{origin}/flaky",)),
        SourceRecord(
            "pdf-exact",
            "pdf",
            (f"{origin}/changed.pdf", f"{origin}/expected.pdf"),
            expected_pdf_sha256=hashlib.sha256(pdf_expected).hexdigest(),
        ),
    ]
    output = tmp_path / "run"

    asyncio.run(
        run_download(
            records,
            _config(output),
            input_description="test records",
        )
    )

    results = _results(output / "results.jsonl")
    assert results["html-ok"]["status"] == "downloaded"
    assert results["html-blocked"]["status"] == "robots_denied"
    assert results["html-redirect"]["selected"]["redirected"] is True
    assert results["html-flaky"]["status"] == "downloaded"
    assert counts["/flaky"] == 2
    assert results["pdf-exact"]["selected"]["pdf_byte_match"] is True
    assert len(results["pdf-exact"]["attempts"]) == 2
    assert counts["/robots.txt"] == 1
    assert counts["/blocked"] == 0

    before = counts.copy()
    asyncio.run(
        run_download(
            records,
            _config(output),
            input_description="test records",
        )
    )
    assert counts == before

    retry_config = dataclasses.replace(_config(output), retry_failures=True)
    asyncio.run(
        run_download(
            records,
            retry_config,
            input_description="test records",
        )
    )
    assert counts["/robots.txt"] == 1
    assert counts["/blocked"] == 0


def test_download_limit_is_enforced_before_body_read(tmp_path, source_server) -> None:
    server, _, _ = source_server
    origin = f"http://127.0.0.1:{server.server_port}"
    output = tmp_path / "limited"
    config = dataclasses.replace(_config(output), max_html_bytes=16)

    asyncio.run(
        run_download(
            [SourceRecord("large", "html", (f"{origin}/large",))],
            config,
            input_description="large test record",
        )
    )

    result = _results(output / "results.jsonl")["large"]
    assert result["status"] == "too_large"


def test_internal_error_preserves_experiment_metadata(tmp_path, monkeypatch) -> None:
    async def fail(*_args, **_kwargs):
        raise RuntimeError("test failure")

    monkeypatch.setattr(Downloader, "process_record", fail)
    record = SourceRecord(
        "record",
        "html",
        ("https://example.org/",),
        source_timestamp="2014-01-02T03:04:05Z",
        quality_score=7,
        stratum="top_decile",
        release_rank=11,
        sample_rank=12,
    )
    output = tmp_path / "internal-error"

    asyncio.run(
        run_download(
            [record],
            _config(output),
            input_description="internal error test",
        )
    )

    result = _results(output / "results.jsonl")["record"]
    assert result["status"] == "internal_error"
    assert result["source_timestamp"] == "2014-01-02T03:04:05Z"
    assert result["quality_score"] == 7
    assert result["stratum"] == "top_decile"
    assert result["release_rank"] == 11
    assert result["sample_rank"] == 12
