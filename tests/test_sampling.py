import json

import pyarrow as pa
import pyarrow.parquet as pq

from experiments.sample_rank_strata import sample_dataset, stable_ranks


def test_stable_ranks_are_unique_bounded_and_reproducible() -> None:
    first = stable_ranks(10, 100, 20, "seed")
    second = stable_ranks(10, 100, 20, "seed")

    assert first == second
    assert len(first) == len(set(first)) == 20
    assert min(first) >= 10
    assert max(first) < 100


def test_rank_stratified_sampler(tmp_path) -> None:
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    schema = pa.schema(
        [
            ("id", pa.string()),
            ("url", pa.string()),
            ("timestamp", pa.string()),
            ("alt_urls", pa.list_(pa.struct([("url", pa.string())]))),
            ("quality_score", pa.uint8()),
            ("text", pa.string()),
        ]
    )
    for file_index in range(2):
        start = file_index * 10
        table = pa.Table.from_pylist(
            [
                {
                    "id": f"id-{index:02d}",
                    "url": f"https://example.org/{index}",
                    "timestamp": "2026-01-01",
                    "alt_urls": [],
                    "quality_score": 100 - index,
                    "text": f"Document text {index}",
                }
                for index in range(start, start + 10)
            ],
            schema=schema,
        )
        pq.write_table(
            table,
            input_dir / f"part-{file_index:02d}.parquet",
            row_group_size=3,
        )
    output = tmp_path / "sample.jsonl"

    manifest = sample_dataset(
        input_dir=input_dir,
        output=output,
        format_name="html",
        per_stratum=2,
        top_fraction=0.5,
        seed="test-seed",
        include_text=True,
    )

    rows = [json.loads(line) for line in output.read_text().splitlines()]
    assert manifest["total_rows"] == 20
    assert len(rows) == 4
    assert sum(row["stratum"] == "top_decile" for row in rows) == 2
    assert sum(row["stratum"] == "remainder" for row in rows) == 2
    assert all(row["release_rank"] <= 10 for row in rows[:2])
    assert all(row["release_rank"] > 10 for row in rows[2:])
    assert all(row["text"].startswith("Document text") for row in rows)
    assert manifest["includes_text"] is True
