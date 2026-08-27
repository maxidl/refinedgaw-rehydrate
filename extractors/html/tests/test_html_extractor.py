from __future__ import annotations

import hashlib
import json
from pathlib import Path

from refinedgaw_html.cli import extract_bytes, run, text_id


def sample_html() -> bytes:
    paragraphs = "".join(
        f"<p>Absatz {index}: Dies ist ein ausführlicher deutscher Beispielsatz "
        "für die reproduzierbare Extraktion wissenschaftlicher Webinhalte.</p>"
        for index in range(12)
    )
    return (
        "<!doctype html><html lang='de'><head><title>Test</title></head>"
        f"<body><nav>Navigation</nav><main><h1>Beispiel</h1>{paragraphs}"
        "</main><footer>Impressum</footer></body></html>"
    ).encode()


def test_identifier_is_first_128_bits_of_utf8_sha256() -> None:
    text = "Ein deutscher Text."
    assert text_id(text) == hashlib.sha256(text.encode()).hexdigest()[:32]


def test_extract_and_exact_match(tmp_path: Path) -> None:
    html = sample_html()
    expected_text, error = extract_bytes(html)
    assert error is None
    assert expected_text is not None
    assert len(expected_text) > 300
    expected_id = text_id(expected_text)
    source_path = tmp_path / "source.html"
    source_path.write_bytes(html)
    downloader_results = tmp_path / "download-results.jsonl"
    downloader_results.write_text(
        json.dumps(
            {
                "record_id": expected_id,
                "expected_text_id": expected_id,
                "format": "html",
                "status": "downloaded",
                "selected": {"local_path": source_path.name},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    output = tmp_path / "output"
    manifest = run(downloader_results, output, workers=1)
    assert manifest["outcome_counts"] == {"extracted": 1}
    assert manifest["exact_text_matches"] == 1
    result = json.loads((output / "results.jsonl").read_text())
    assert result["exact_text_match"] is True
    assert Path(result["text_path"]).read_text() == expected_text


def test_non_downloaded_and_pdf_rows_are_not_extracted(tmp_path: Path) -> None:
    downloader_results = tmp_path / "download-results.jsonl"
    rows = [
        {"record_id": "html", "format": "html", "status": "missing"},
        {"record_id": "pdf", "format": "pdf", "status": "downloaded"},
    ]
    downloader_results.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )
    manifest = run(downloader_results, tmp_path / "output", workers=1)
    assert manifest["input_downloaded_html_records"] == 0
    assert (tmp_path / "output" / "results.jsonl").read_text() == ""
