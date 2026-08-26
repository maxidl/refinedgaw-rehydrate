import json

import pytest

from experiments.summarize_downloads import summarize


def test_download_summary_counts_groups_and_weights(tmp_path) -> None:
    results = tmp_path / "results.jsonl"
    rows = []
    outcomes = {
        ("html", "top_decile"): ["downloaded", "downloaded"],
        ("html", "remainder"): ["downloaded", "missing"],
        ("pdf", "top_decile"): ["downloaded", "missing"],
        ("pdf", "remainder"): ["missing", "missing"],
    }
    for (format_name, stratum), statuses in outcomes.items():
        for index, status in enumerate(statuses):
            rows.append(
                {
                    "record_id": f"{format_name}-{stratum}-{index}",
                    "format": format_name,
                    "stratum": stratum,
                    "status": status,
                    "source_timestamp": "2025-01-01T00:00:00Z",
                }
            )
    results.write_text("".join(json.dumps(row) + "\n" for row in rows))

    summary = summarize(
        results,
        expected_per_stratum=2,
        top_weight=0.1,
        replicates=100,
        seed="test-seed",
    )

    assert summary["groups"]["html:top_decile"]["downloaded"] == 2
    assert summary["groups"]["pdf:remainder"]["failure_counts"] == {"missing": 2}
    assert summary["formats"]["html"]["weighted_downloaded_rate"] == 0.55
    assert summary["formats"]["pdf"]["weighted_downloaded_rate"] == 0.05
    assert summary["formats"]["html"]["weighted_status_rates"] == {
        "downloaded": 0.55,
        "missing": 0.45,
    }
    assert summary["formats"]["pdf"]["weighted_status_rates"] == pytest.approx(
        {
            "downloaded": 0.05,
            "missing": 0.95,
        }
    )


def test_download_summary_standardizes_crawl_year_distribution(tmp_path) -> None:
    results = tmp_path / "results.jsonl"
    rows = []
    for format_name in ("html", "pdf"):
        for stratum in ("top_decile", "remainder"):
            recent_records = 3 if stratum == "top_decile" else 1
            for index in range(4):
                recent = index < recent_records
                rows.append(
                    {
                        "record_id": f"{format_name}-{stratum}-{index}",
                        "format": format_name,
                        "stratum": stratum,
                        "status": "downloaded" if recent else "missing",
                        "source_timestamp": (
                            "2025-06-01T00:00:00Z" if recent else "2015-06-01T00:00:00Z"
                        ),
                    }
                )
    results.write_text("".join(json.dumps(row) + "\n" for row in rows))

    summary = summarize(
        results,
        expected_per_stratum=4,
        top_weight=0.1,
        replicates=100,
        seed="test-seed",
    )

    temporal = summary["temporal"]["html"]
    assert temporal["years"]["2025"]["weighted_downloaded_rate"] == 1.0
    assert temporal["months"]["2015-06"]["weighted_downloaded_rate"] == 0.0
    adjustment = temporal["crawl_year_standardization"]
    assert adjustment["unadjusted_gap"] == 0.5
    assert adjustment["standardized_rates"] == pytest.approx(
        {"top_decile": 0.3, "remainder": 0.3}
    )
    assert adjustment["standardized_gap"] == pytest.approx(0.0)
