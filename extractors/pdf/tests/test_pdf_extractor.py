from __future__ import annotations

import hashlib
from pathlib import Path

import fitz

from refinedgaw_pdf.cli import (
    assemble_document,
    clean_output,
    render_document,
    text_hash,
)


def test_cleanup_matches_production_rules() -> None:
    raw = "<|ref|>Titel<|/ref|><|det|>box<|/det|>\nText<PAGE>Weiter"
    assert clean_output(raw) == "Titel\nText\n\n<!-- PAGE -->\n\nWeiter"


def test_text_hash_is_utf8_sha256() -> None:
    text = "Deutscher OCR-Text"
    assert text_hash(text) == hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_render_and_assemble_exact_match(tmp_path: Path) -> None:
    pdf_path = tmp_path / "sample.pdf"
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "RefinedGAW PDF fixture")
    document.save(pdf_path)
    document.close()
    rendered = render_document(str(pdf_path), dpi=72, jpeg_quality=90)
    assert len(rendered) == 1
    assert rendered[0]["image_url"].startswith("data:image/jpeg;base64,")

    text = "Erste Seite\n\nZweite Seite"
    expected_id = text_hash(text)[:32]
    row = {
        "record_id": expected_id,
        "expected_text_id": expected_id,
        "expected_pdf_sha256": "a" * 64,
        "stratum": "top_decile",
        "release_rank": 1,
        "sample_rank": 1,
        "selected": {"local_path": str(pdf_path), "pdf_byte_match": True},
    }
    result = assemble_document(
        row,
        [
            {"page": 2, "status": "ok", "text": "Zweite Seite"},
            {"page": 1, "status": "ok", "text": "Erste Seite"},
        ],
        tmp_path / "output",
    )
    assert result["status"] == "extracted"
    assert result["exact_text_match"] is True
    assert Path(result["text_path"]).read_text() == text
