from __future__ import annotations

import json
from pathlib import Path

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
