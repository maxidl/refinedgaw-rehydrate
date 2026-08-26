from __future__ import annotations

import json
from pathlib import Path

import pytest

from experiments.summarize_rehydration import summarize


def write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def test_pipeline_summary_uses_full_stratum_denominator(tmp_path: Path) -> None:
    downloads: list[dict[str, object]] = []
    extractions: list[dict[str, object]] = []
    for format_name in ("html", "pdf"):
        for stratum in ("top_decile", "remainder"):
            for index in range(2):
                record_id = f"{format_name}-{stratum}-{index}"
                downloaded = index == 0
                downloads.append(
                    {
                        "format": format_name,
                        "stratum": stratum,
                        "record_id": record_id,
                        "status": "downloaded" if downloaded else "missing",
                    }
                )
                if downloaded:
                    extractions.append(
                        {
                            "format": format_name,
                            "record_id": record_id,
                            "status": "extracted",
                            "exact_text_match": format_name == "html",
                        }
                    )
    download_path = tmp_path / "downloads.jsonl"
    extraction_path = tmp_path / "extractions.jsonl"
    write_jsonl(download_path, downloads)
    write_jsonl(extraction_path, extractions)
    result = summarize(
        download_path,
        [extraction_path],
        expected_per_stratum=2,
        top_weight=0.1,
        replicates=100,
        seed="test",
    )
    assert result["groups"]["html:top_decile"]["rates"] == {
        "downloaded": 0.5,
        "extracted": 0.5,
        "exact_match": 0.5,
    }
    assert result["groups"]["pdf:top_decile"]["rates"] == {
        "downloaded": 0.5,
        "extracted": 0.5,
        "exact_match": 0.0,
        "pdf_byte_match": 0.0,
    }


def test_pipeline_summary_computes_fuzzy_similarity(tmp_path: Path) -> None:
    downloads: list[dict[str, object]] = []
    extractions: list[dict[str, object]] = []
    expected: list[dict[str, object]] = []
    words = "one two three four five six seven eight nine ten"
    for format_name in ("html", "pdf"):
        for stratum in ("top_decile", "remainder"):
            record_id = f"{format_name}-{stratum}"
            text_path = tmp_path / f"{record_id}.txt"
            text_path.write_text(words)
            downloads.append(
                {
                    "format": format_name,
                    "stratum": stratum,
                    "record_id": record_id,
                    "status": "downloaded",
                }
            )
            extractions.append(
                {
                    "format": format_name,
                    "record_id": record_id,
                    "status": "extracted",
                    "exact_text_match": True,
                    "downloaded_pdf_byte_match": format_name == "pdf",
                    "text_path": str(text_path),
                }
            )
            expected.append(
                {
                    "format": format_name,
                    "id": record_id,
                    "text": words,
                }
            )
    download_path = tmp_path / "downloads.jsonl"
    extraction_path = tmp_path / "extractions.jsonl"
    expected_path = tmp_path / "expected.jsonl"
    write_jsonl(download_path, downloads)
    write_jsonl(extraction_path, extractions)
    write_jsonl(expected_path, expected)

    result = summarize(
        download_path,
        [extraction_path],
        expected_per_stratum=1,
        top_weight=0.1,
        replicates=100,
        seed="test",
        expected_record_paths=[expected_path],
    )

    assert result["groups"]["html:top_decile"]["rates"]["similarity_0_8"] == 1
    assert result["formats"]["pdf"]["pdf_byte_match"]["weighted_rate"] == 1


def test_pipeline_summary_rejects_incomplete_extraction_results(tmp_path: Path) -> None:
    downloads = tmp_path / "downloads.jsonl"
    extractions = tmp_path / "extractions.jsonl"
    write_jsonl(
        downloads,
        [
            {
                "format": "html",
                "stratum": "top_decile",
                "record_id": "x",
                "status": "downloaded",
            }
        ],
    )
    write_jsonl(extractions, [])
    with pytest.raises(ValueError, match="cover every and only"):
        summarize(
            downloads,
            [extractions],
            expected_per_stratum=1,
            top_weight=0.1,
            replicates=10,
            seed="test",
        )


def test_pipeline_summary_rejects_exact_match_without_extraction(
    tmp_path: Path,
) -> None:
    downloads: list[dict[str, object]] = []
    extractions: list[dict[str, object]] = []
    for format_name in ("html", "pdf"):
        for stratum in ("top_decile", "remainder"):
            record_id = f"{format_name}-{stratum}"
            downloads.append(
                {
                    "format": format_name,
                    "stratum": stratum,
                    "record_id": record_id,
                    "status": "downloaded",
                }
            )
            extractions.append(
                {
                    "format": format_name,
                    "record_id": record_id,
                    "status": "extraction_error"
                    if record_id == "pdf-remainder"
                    else "extracted",
                    "exact_text_match": record_id == "pdf-remainder",
                }
            )
    download_path = tmp_path / "downloads.jsonl"
    extraction_path = tmp_path / "extractions.jsonl"
    write_jsonl(download_path, downloads)
    write_jsonl(extraction_path, extractions)
    with pytest.raises(ValueError, match="Exact match without extracted text"):
        summarize(
            download_path,
            [extraction_path],
            expected_per_stratum=1,
            top_weight=0.1,
            replicates=10,
            seed="test",
        )
