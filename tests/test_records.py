from refinedgaw_rehydrate.records import record_from_mapping


def test_record_candidates_are_deduplicated_and_recent_alternatives_first() -> None:
    record = record_from_mapping(
        {
            "id": "abc",
            "url": "https://example.org/primary",
            "alt_urls": [
                {"url": "https://example.org/old", "timestamp": "2020-01-01"},
                {"url": "https://example.org/new", "timestamp": "2024-01-01"},
                {"url": "https://example.org/primary", "timestamp": "2025-01-01"},
            ],
        },
        format_override="html",
        max_urls_per_record=3,
    )

    assert record.url_candidates == (
        "https://example.org/primary",
        "https://example.org/new",
        "https://example.org/old",
    )


def test_pdf_hash_is_validated() -> None:
    record = record_from_mapping(
        {
            "id": "abc",
            "url": "https://example.org/file.pdf",
            "pdf_sha256": "a" * 64,
        },
        format_override="pdf",
        max_urls_per_record=1,
    )

    assert record.expected_pdf_sha256 == "a" * 64
