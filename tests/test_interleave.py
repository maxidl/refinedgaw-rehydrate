import json

from experiments.interleave_samples import interleave


def test_interleave_preserves_records_and_is_reproducible(tmp_path) -> None:
    first_input = tmp_path / "first.jsonl"
    second_input = tmp_path / "second.jsonl"
    rows = [
        {"id": "a", "format": "html"},
        {"id": "b", "format": "html"},
        {"id": "c", "format": "pdf"},
        {"id": "d", "format": "pdf"},
    ]
    first_input.write_text("".join(json.dumps(row) + "\n" for row in rows[:2]))
    second_input.write_text("".join(json.dumps(row) + "\n" for row in rows[2:]))
    first_output = tmp_path / "first-output.jsonl"
    second_output = tmp_path / "second-output.jsonl"

    first_manifest = interleave([first_input, second_input], first_output, "test-seed")
    second_manifest = interleave(
        [first_input, second_input], second_output, "test-seed"
    )

    assert first_output.read_bytes() == second_output.read_bytes()
    assert first_manifest["output_sha256"] == second_manifest["output_sha256"]
    actual = [json.loads(line) for line in first_output.read_text().splitlines()]
    assert {(row["format"], row["id"]) for row in actual} == {
        (row["format"], row["id"]) for row in rows
    }
