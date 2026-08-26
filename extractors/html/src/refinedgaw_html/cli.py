from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import hashlib
import json
import os
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import trafilatura

from . import __version__

MIN_TEXT_CHARS = 301
TRAFILATURA_SETTINGS = {
    "output_format": "txt",
    "favor_precision": True,
    "include_comments": False,
    "deduplicate": True,
}


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def extract_bytes(html: bytes) -> tuple[str | None, str | None]:
    """Apply the exact production extractor and identifier construction."""
    try:
        text = trafilatura.extract(html, **TRAFILATURA_SETTINGS)
    except Exception as error:
        return None, f"{type(error).__name__}: {error}"
    return text, None


def text_id(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:32]


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(content)
    os.replace(temporary, path)


def eligible_rows(results_path: Path) -> Iterator[dict[str, Any]]:
    with results_path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("format") != "html" or row.get("status") != "downloaded":
                continue
            selected = row.get("selected")
            local_path = (
                selected.get("local_path") if isinstance(selected, dict) else None
            )
            if not local_path:
                raise ValueError(
                    f"Downloaded HTML result on line {line_number} lacks local_path"
                )
            resolved = Path(local_path)
            if not resolved.is_absolute():
                resolved = results_path.parent / resolved
            row = dict(row)
            row["selected"] = {**selected, "local_path": str(resolved)}
            yield row


def process_row(task: tuple[dict[str, Any], str]) -> dict[str, Any]:
    row, output_dir_string = task
    output_dir = Path(output_dir_string)
    source_path = Path(row["selected"]["local_path"])
    base = {
        "record_id": row.get("record_id"),
        "format": "html",
        "stratum": row.get("stratum"),
        "release_rank": row.get("release_rank"),
        "sample_rank": row.get("sample_rank"),
        "source_path": str(source_path),
        "expected_text_id": row.get("expected_text_id"),
    }
    try:
        html = source_path.read_bytes()
    except OSError as error:
        return {
            **base,
            "status": "source_read_error",
            "error": f"{type(error).__name__}: {error}",
            "extracted_chars": None,
            "extracted_text_id": None,
            "exact_text_match": False,
            "text_path": None,
        }
    text, error = extract_bytes(html)
    if error is not None:
        return {
            **base,
            "status": "extraction_error",
            "error": error,
            "extracted_chars": None,
            "extracted_text_id": None,
            "exact_text_match": False,
            "text_path": None,
        }
    if text is None:
        return {
            **base,
            "status": "no_text",
            "error": None,
            "extracted_chars": None,
            "extracted_text_id": None,
            "exact_text_match": False,
            "text_path": None,
        }
    extracted_id = text_id(text)
    if len(text) < MIN_TEXT_CHARS:
        return {
            **base,
            "status": "too_short",
            "error": None,
            "extracted_chars": len(text),
            "extracted_text_id": extracted_id,
            "exact_text_match": False,
            "text_path": None,
        }
    text_path = output_dir / "texts" / extracted_id[:2] / f"{extracted_id}.txt"
    atomic_write(text_path, text.encode())
    return {
        **base,
        "status": "extracted",
        "error": None,
        "extracted_chars": len(text),
        "extracted_text_id": extracted_id,
        "exact_text_match": extracted_id == row.get("expected_text_id"),
        "text_path": str(text_path),
    }


def run(results_path: Path, output_dir: Path, workers: int) -> dict[str, Any]:
    if workers < 1:
        raise ValueError("workers must be at least 1")
    rows = list(eligible_rows(results_path))
    output_dir.mkdir(parents=True, exist_ok=True)
    results_output = output_dir / "results.jsonl"
    temporary = results_output.with_name(f".{results_output.name}.{os.getpid()}.tmp")
    counts: Counter[str] = Counter()
    exact_matches = 0
    tasks = ((row, str(output_dir)) for row in rows)
    with (
        temporary.open("w", encoding="utf-8") as sink,
        concurrent.futures.ProcessPoolExecutor(max_workers=workers) as pool,
    ):
        for index, result in enumerate(
            pool.map(process_row, tasks, chunksize=16), start=1
        ):
            counts[str(result["status"])] += 1
            exact_matches += bool(result["exact_text_match"])
            sink.write(json.dumps(result, sort_keys=True) + "\n")
            if index == 1 or index % 100 == 0:
                print(f"Processed {index:,}/{len(rows):,} downloaded HTML records")
    os.replace(temporary, results_output)
    manifest = {
        "schema_version": 1,
        "tool": "refinedgaw-html-extractor",
        "tool_version": __version__,
        "trafilatura_version": trafilatura.__version__,
        "completed_at": utc_now(),
        "input": str(results_path),
        "input_downloaded_html_records": len(rows),
        "workers": workers,
        "minimum_text_characters": MIN_TEXT_CHARS,
        "trafilatura_settings": TRAFILATURA_SETTINGS,
        "outcome_counts": dict(sorted(counts.items())),
        "exact_text_matches": exact_matches,
    }
    atomic_write(
        output_dir / "run-manifest.json",
        (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode(),
    )
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Reproduce RefinedGAW HTML text extraction"
    )
    parser.add_argument("results", type=Path, help="Downloader results.jsonl")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=max(1, os.cpu_count() or 1))
    return parser


def main() -> int:
    args = build_parser().parse_args()
    manifest = run(args.results, args.output, args.workers)
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
