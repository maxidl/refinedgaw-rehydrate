from __future__ import annotations

import argparse
import asyncio
import itertools
import json
from pathlib import Path

from .downloader import run_download
from .models import DEFAULT_USER_AGENT, DownloadConfig
from .records import iter_records


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def nonnegative_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must be non-negative")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="refinedgaw")
    subparsers = parser.add_subparsers(dest="command", required=True)
    download = subparsers.add_parser(
        "download",
        help="Download source content referenced by RefinedGAW records",
    )
    download.add_argument("input", type=Path, nargs="+")
    download.add_argument("--output", type=Path, required=True)
    download.add_argument("--format", choices=("html", "pdf"))
    download.add_argument("--limit", type=positive_int)
    download.add_argument("--concurrency", type=positive_int, default=16)
    download.add_argument(
        "--requests-per-minute",
        type=positive_int,
        default=30,
        help="Maximum requests per registrable domain, including robots.txt",
    )
    download.add_argument(
        "--min-interval",
        type=nonnegative_float,
        default=1.0,
        help="Minimum seconds between requests to a registrable domain",
    )
    download.add_argument("--max-urls-per-record", type=positive_int, default=3)
    download.add_argument("--connect-timeout", type=positive_int, default=10)
    download.add_argument("--read-timeout", type=positive_int, default=60)
    download.add_argument("--retries", type=int, choices=range(0, 6), default=2)
    download.add_argument(
        "--max-retry-delay",
        type=positive_int,
        default=60,
        help="Maximum seconds to honor a server-supplied Retry-After value",
    )
    download.add_argument("--max-redirects", type=positive_int, default=8)
    download.add_argument("--max-html-mb", type=positive_int, default=20)
    download.add_argument("--max-pdf-mb", type=positive_int, default=200)
    download.add_argument("--robots-cache-hours", type=positive_int, default=24)
    download.add_argument(
        "--robots-failure-cache-minutes",
        type=positive_int,
        default=15,
    )
    download.add_argument(
        "--user-agent",
        default=DEFAULT_USER_AGENT,
        help="Identify your deployment and preferably include contact information",
    )
    download.add_argument(
        "--retry-failures",
        action="store_true",
        help="Retry previously completed failures when resuming",
    )
    download.add_argument(
        "--allow-private-network",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    download.add_argument(
        "--allow-nonstandard-ports",
        action="store_true",
        help="Allow public HTTP(S) destinations on ports other than 80 and 443",
    )
    return parser


def run_download_command(args: argparse.Namespace) -> int:
    config = DownloadConfig(
        output_dir=args.output,
        concurrency=args.concurrency,
        requests_per_minute=args.requests_per_minute,
        min_interval_seconds=args.min_interval,
        max_urls_per_record=args.max_urls_per_record,
        connect_timeout_seconds=float(args.connect_timeout),
        read_timeout_seconds=float(args.read_timeout),
        retries=args.retries,
        max_retry_delay_seconds=float(args.max_retry_delay),
        max_redirects=args.max_redirects,
        max_html_bytes=args.max_html_mb * 1024 * 1024,
        max_pdf_bytes=args.max_pdf_mb * 1024 * 1024,
        robots_ttl_seconds=args.robots_cache_hours * 60 * 60,
        robots_failure_ttl_seconds=args.robots_failure_cache_minutes * 60,
        user_agent=args.user_agent,
        allow_private_network=args.allow_private_network,
        allow_nonstandard_ports=args.allow_nonstandard_ports,
        retry_failures=args.retry_failures,
    )
    records = itertools.chain.from_iterable(
        iter_records(
            path,
            format_override=args.format,
            max_urls_per_record=args.max_urls_per_record,
        )
        for path in args.input
    )
    manifest = asyncio.run(
        run_download(
            records,
            config,
            input_description=[str(path) for path in args.input],
            limit=args.limit,
        )
    )
    print(json.dumps(manifest["state"], indent=2, sort_keys=True))
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command == "download":
            return run_download_command(args)
    except KeyboardInterrupt:
        parser.exit(130, "Interrupted; progress remains resumable.\n")
    except (FileNotFoundError, ValueError, RuntimeError) as error:
        parser.exit(2, f"error: {error}\n")
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
