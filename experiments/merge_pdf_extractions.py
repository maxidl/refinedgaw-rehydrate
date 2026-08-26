from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import os
from pathlib import Path
from typing import Any


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as source:
        return [json.loads(line) for line in source if line.strip()]


def atomic_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(value)
    os.replace(temporary, path)


def atomic_json(path: Path, value: object) -> None:
    atomic_bytes(
        path,
        (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )


def identical_value(manifests: list[dict[str, Any]], key: str) -> Any:
    values = {json.dumps(manifest.get(key), sort_keys=True) for manifest in manifests}
    if len(values) != 1:
        raise ValueError(f"Shard manifests disagree on {key}: {sorted(values)}")
    return manifests[0].get(key)


def merge(
    downloads_path: Path,
    shard_dirs: list[Path],
    *,
    output_path: Path,
    manifest_output: Path,
    expected_shards: int,
    resumed_checkpoints: int,
) -> dict[str, Any]:
    if len(shard_dirs) != expected_shards:
        raise ValueError(
            f"Received {len(shard_dirs)} shard directories; expected {expected_shards}"
        )

    download_rows = [
        row
        for row in read_jsonl(downloads_path)
        if row.get("format") == "pdf" and row.get("status") == "downloaded"
    ]
    expected_ids = [str(row["record_id"]) for row in download_rows]
    if len(expected_ids) != len(set(expected_ids)):
        raise ValueError(
            "Downloader results contain duplicate downloaded PDF record IDs"
        )

    manifests: list[dict[str, Any]] = []
    results_by_id: dict[str, dict[str, Any]] = {}
    observed_indices: set[int] = set()
    for shard_dir in shard_dirs:
        manifest_path = shard_dir / "run-manifest.json"
        results_path = shard_dir / "results.jsonl"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        configuration = manifest.get("configuration", {})
        shard_count = int(configuration.get("shard_count", -1))
        shard_index = int(configuration.get("shard_index", -1))
        if shard_count != expected_shards:
            raise ValueError(
                f"{manifest_path} reports shard_count={shard_count}; "
                f"expected {expected_shards}"
            )
        if not 0 <= shard_index < expected_shards or shard_index in observed_indices:
            raise ValueError(f"Invalid or duplicate shard index in {manifest_path}")
        observed_indices.add(shard_index)
        rows = read_jsonl(results_path)
        if len(rows) != int(manifest.get("downloaded_pdf_records", -1)):
            raise ValueError(f"Result count disagrees with manifest for {shard_dir}")
        for row in rows:
            record_id = str(row.get("record_id"))
            if row.get("format") != "pdf":
                raise ValueError(f"Non-PDF extraction result in {results_path}")
            if record_id in results_by_id:
                raise ValueError(f"Duplicate extraction result for {record_id}")
            if row.get("exact_text_match") is True and row.get("status") != "extracted":
                raise ValueError(f"Exact match without extracted text for {record_id}")
            text_path = row.get("text_path")
            if row.get("status") == "extracted" and (
                not isinstance(text_path, str) or not Path(text_path).is_file()
            ):
                raise ValueError(f"Missing extracted text for {record_id}: {text_path}")
            results_by_id[record_id] = row
        manifests.append(manifest)

    if observed_indices != set(range(expected_shards)):
        raise ValueError(f"Incomplete shard indices: {sorted(observed_indices)}")
    expected_set = set(expected_ids)
    observed_set = set(results_by_id)
    if observed_set != expected_set:
        missing = sorted(expected_set - observed_set)[:5]
        extra = sorted(observed_set - expected_set)[:5]
        raise ValueError(
            f"Extraction coverage mismatch: missing={missing}, extra={extra}"
        )

    ordered_results = [results_by_id[record_id] for record_id in expected_ids]
    counts = collections.Counter(str(row.get("status")) for row in ordered_results)
    atomic_bytes(
        output_path,
        b"".join(
            (json.dumps(row, sort_keys=True) + "\n").encode("utf-8")
            for row in ordered_results
        ),
    )

    tool_version = identical_value(manifests, "tool_version")
    model = identical_value(manifests, "model")
    model_revision = identical_value(manifests, "model_revision")
    comparable_configuration = []
    for manifest in manifests:
        configuration = dict(manifest.get("configuration", {}))
        configuration.pop("api_base", None)
        configuration.pop("shard_index", None)
        comparable_configuration.append(configuration)
    if any(
        value != comparable_configuration[0]
        for value in comparable_configuration[1:]
    ):
        raise ValueError("Shard manifests disagree on extraction configuration")

    aggregate = {
        "schema_version": 1,
        "tool": "refinedgaw-pdf-extractor-shard-merge",
        "tool_version": tool_version,
        "model": model,
        "model_revision": model_revision,
        "started_at": min(str(manifest["started_at"]) for manifest in manifests),
        "completed_at": dt.datetime.now(dt.UTC).isoformat(),
        "input": str(downloads_path),
        "downloaded_pdf_records": len(ordered_results),
        "configuration": {
            **comparable_configuration[0],
            "shard_count": expected_shards,
            "shared_checkpoint_resume": True,
        },
        "outcome_counts": dict(sorted(counts.items())),
        "exact_text_matches": sum(
            row.get("exact_text_match") is True for row in ordered_results
        ),
        "resumed_checkpoints": resumed_checkpoints,
        "shard_manifests": [
            str(shard_dir / "run-manifest.json") for shard_dir in shard_dirs
        ],
        "results": str(output_path),
    }
    atomic_json(manifest_output, aggregate)
    return aggregate


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("downloads", type=Path)
    parser.add_argument("shards", type=Path, nargs="+")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest-output", type=Path, required=True)
    parser.add_argument("--expected-shards", type=int, required=True)
    parser.add_argument("--resumed-checkpoints", type=int, default=0)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    aggregate = merge(
        args.downloads,
        args.shards,
        output_path=args.output,
        manifest_output=args.manifest_output,
        expected_shards=args.expected_shards,
        resumed_checkpoints=args.resumed_checkpoints,
    )
    print(json.dumps(aggregate, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
