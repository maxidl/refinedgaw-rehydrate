from __future__ import annotations

import json
from pathlib import Path

import pytest

from refinedgaw_pdf.cli import downloaded_pdf_rows


def test_downloaded_pdf_path_is_relative_to_results_file(tmp_path: Path) -> None:
    content = tmp_path / "content" / "aa" / "document.pdf"
    content.parent.mkdir(parents=True)
    content.write_bytes(b"pdf")
    results = tmp_path / "results.jsonl"
    results.write_text(
        json.dumps(
            {
                "record_id": "record",
                "format": "pdf",
                "status": "downloaded",
                "selected": {"local_path": "content/aa/document.pdf"},
            }
        )
        + "\n"
    )
    row = downloaded_pdf_rows(results)[0]
    assert row["selected"]["local_path"] == str(content)


def test_downloaded_pdf_rows_are_partitioned_into_stable_shards(
    tmp_path: Path,
) -> None:
    results = tmp_path / "results.jsonl"
    rows = [
        {
            "record_id": f"record-{index}",
            "format": "pdf",
            "status": "downloaded",
            "selected": {"local_path": f"content/{index}.pdf"},
        }
        for index in range(7)
    ]
    results.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )

    shards = [
        downloaded_pdf_rows(results, shard_index=index, shard_count=3)
        for index in range(3)
    ]
    assert [[row["record_id"] for row in shard] for shard in shards] == [
        ["record-0", "record-3", "record-6"],
        ["record-1", "record-4"],
        ["record-2", "record-5"],
    ]
    assert {row["record_id"] for shard in shards for row in shard} == {
        row["record_id"] for row in rows
    }


@pytest.mark.parametrize("shard_index,shard_count", [(-1, 2), (2, 2), (0, 0)])
def test_downloaded_pdf_rows_reject_invalid_shards(
    tmp_path: Path, shard_index: int, shard_count: int
) -> None:
    results = tmp_path / "results.jsonl"
    results.write_text("", encoding="utf-8")
    with pytest.raises(ValueError):
        downloaded_pdf_rows(results, shard_index=shard_index, shard_count=shard_count)
