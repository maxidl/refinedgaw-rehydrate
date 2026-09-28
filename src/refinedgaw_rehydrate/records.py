from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any

from .models import SourceRecord


def _clean_string(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    text = str(value).strip()
    return text or None


def _alternative_urls(value: object) -> list[tuple[str, str]]:
    if not isinstance(value, list):
        return []
    alternatives: list[tuple[str, str]] = []
    for item in value:
        if isinstance(item, str):
            url = _clean_string(item)
            if url:
                alternatives.append(("", url))
        elif isinstance(item, Mapping):
            url = _clean_string(item.get("url"))
            if url:
                timestamp = _clean_string(item.get("timestamp")) or ""
                alternatives.append((timestamp, url))
    alternatives.sort(key=lambda pair: (pair[0], pair[1]), reverse=True)
    return alternatives


def _candidate_urls(row: Mapping[str, Any], maximum: int) -> tuple[str, ...]:
    candidates: list[str] = []
    seen: set[str] = set()

    def add(value: object) -> None:
        url = _clean_string(value)
        if url and url not in seen:
            seen.add(url)
            candidates.append(url)

    supplied = row.get("url_candidates")
    if isinstance(supplied, list):
        for value in supplied:
            add(value)
    else:
        add(row.get("url"))
        for _, url in _alternative_urls(row.get("alt_urls")):
            add(url)
    return tuple(candidates[:maximum])


def record_from_mapping(
    row: Mapping[str, Any],
    *,
    format_override: str | None,
    max_urls_per_record: int,
) -> SourceRecord:
    record_id = _clean_string(row.get("id") or row.get("record_id"))
    if not record_id:
        raise ValueError("Input record has no id")
    format_name = format_override or _clean_string(row.get("format"))
    if format_name not in {"html", "pdf"}:
        raise ValueError(
            f"Record {record_id!r} has no valid format. Use --format html|pdf"
        )
    expected_pdf_sha256 = _clean_string(row.get("pdf_sha256"))
    if expected_pdf_sha256 is not None:
        expected_pdf_sha256 = expected_pdf_sha256.lower()
        if len(expected_pdf_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in expected_pdf_sha256
        ):
            raise ValueError(f"Record {record_id!r} has an invalid pdf_sha256")
    return SourceRecord(
        record_id=record_id,
        format=format_name,
        url_candidates=_candidate_urls(row, max_urls_per_record),
        expected_pdf_sha256=expected_pdf_sha256,
        source_timestamp=_clean_string(
            row.get("timestamp") or row.get("source_timestamp")
        ),
    )


def _jsonl_rows(path: Path) -> Iterator[Mapping[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"Invalid JSON at {path}:{line_number}") from error
            if not isinstance(value, Mapping):
                raise ValueError(f"Expected an object at {path}:{line_number}")
            yield value


def _parquet_rows(path: Path) -> Iterable[Mapping[str, Any]]:
    import pyarrow.dataset as ds

    dataset = ds.dataset(path, format="parquet")
    wanted = {
        "id",
        "record_id",
        "format",
        "url",
        "url_candidates",
        "alt_urls",
        "timestamp",
        "source_timestamp",
        "pdf_sha256",
    }
    columns = [name for name in dataset.schema.names if name in wanted]
    if not ({"id", "record_id"} & set(columns)):
        raise ValueError(f"Parquet input {path} has no id column")
    for batch in dataset.scanner(columns=columns, batch_size=65_536).to_batches():
        yield from batch.to_pylist()


def iter_records(
    path: Path,
    *,
    format_override: str | None,
    max_urls_per_record: int,
) -> Iterator[SourceRecord]:
    if not path.exists():
        raise FileNotFoundError(path)
    if path.is_dir() or path.suffix.lower() == ".parquet":
        rows = _parquet_rows(path)
    elif path.suffix.lower() in {".jsonl", ".ndjson"}:
        rows = _jsonl_rows(path)
    else:
        raise ValueError("Input must be JSONL, Parquet, or a Parquet directory")
    for row in rows:
        yield record_from_mapping(
            row,
            format_override=format_override,
            max_urls_per_record=max_urls_per_record,
        )
