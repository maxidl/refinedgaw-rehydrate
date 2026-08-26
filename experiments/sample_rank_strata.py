from __future__ import annotations

import argparse
import bisect
import collections
import dataclasses
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

ALGORITHM = "sha256-rank-sample-v1"
DEFAULT_SEED = "refinedgaw-source-availability-v1"


@dataclasses.dataclass(frozen=True)
class RowGroupSpan:
    path: Path
    row_group: int
    start: int
    stop: int


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def json_default(value: object) -> object:
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    raise TypeError(f"Cannot encode {type(value).__name__}")


def stable_ranks(start: int, stop: int, count: int, seed: str) -> list[int]:
    population = stop - start
    if count > population:
        raise ValueError("Sample is larger than its rank interval")
    selected: set[int] = set()
    counter = 0
    modulus_limit = (1 << 256) // population * population
    seed_bytes = seed.encode("utf-8")
    while len(selected) < count:
        digest = hashlib.sha256(
            seed_bytes + b"\0" + counter.to_bytes(16, "big")
        ).digest()
        counter += 1
        value = int.from_bytes(digest, "big")
        if value >= modulus_limit:
            continue
        selected.add(start + value % population)
    return sorted(selected)


def inventory(paths: list[Path]) -> tuple[list[RowGroupSpan], int]:
    spans: list[RowGroupSpan] = []
    offset = 0
    for path in paths:
        parquet = pq.ParquetFile(path)
        for row_group in range(parquet.num_row_groups):
            rows = parquet.metadata.row_group(row_group).num_rows
            spans.append(RowGroupSpan(path, row_group, offset, offset + rows))
            offset += rows
    return spans, offset


def selected_locations(
    spans: list[RowGroupSpan],
    ranks: dict[int, str],
) -> dict[tuple[Path, int], list[tuple[int, int, str]]]:
    stops = [span.stop for span in spans]
    locations: dict[tuple[Path, int], list[tuple[int, int, str]]] = (
        collections.defaultdict(list)
    )
    for rank, stratum in sorted(ranks.items()):
        span_index = bisect.bisect_right(stops, rank)
        span = spans[span_index]
        locations[(span.path, span.row_group)].append(
            (rank, rank - span.start, stratum)
        )
    return locations


def extract_rows(
    locations: dict[tuple[Path, int], list[tuple[int, int, str]]],
    *,
    format_name: str,
    include_text: bool = False,
) -> list[dict[str, Any]]:
    columns = ["id", "url", "timestamp", "alt_urls", "quality_score"]
    if include_text:
        columns.append("text")
    if format_name == "pdf":
        columns.append("pdf_sha256")
    by_path: dict[Path, list[tuple[int, list[tuple[int, int, str]]]]] = (
        collections.defaultdict(list)
    )
    for (path, row_group), selections in locations.items():
        by_path[path].append((row_group, selections))
    rows: list[dict[str, Any]] = []
    for path in sorted(by_path):
        parquet = pq.ParquetFile(path)
        missing = set(columns) - set(parquet.schema_arrow.names)
        if missing:
            raise ValueError(f"{path} is missing columns: {sorted(missing)}")
        for row_group, selections in sorted(by_path[path]):
            table = parquet.read_row_group(row_group, columns=columns)
            offsets = pa.array([selection[1] for selection in selections], pa.int64())
            selected = table.take(offsets).to_pylist()
            for values, (rank, _, stratum) in zip(selected, selections, strict=True):
                rows.append(
                    {
                        "format": format_name,
                        "stratum": stratum,
                        "release_rank": rank + 1,
                        **values,
                    }
                )
    rows.sort(key=lambda row: int(row["release_rank"]))
    stratum_counts: collections.Counter[str] = collections.Counter()
    for row in rows:
        stratum = str(row["stratum"])
        stratum_counts[stratum] += 1
        row["sample_rank"] = stratum_counts[stratum]
    return rows


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def sample_dataset(
    *,
    input_dir: Path,
    output: Path,
    format_name: str,
    per_stratum: int,
    top_fraction: float,
    seed: str,
    include_text: bool = False,
) -> dict[str, Any]:
    paths = sorted(input_dir.glob("*.parquet"))
    if not paths:
        raise ValueError(f"No Parquet files found in {input_dir}")
    spans, total_rows = inventory(paths)
    top_rows = int(total_rows * top_fraction)
    if not 0 < top_rows < total_rows:
        raise ValueError("Top fraction produces an empty stratum")
    top_ranks = stable_ranks(
        0,
        top_rows,
        per_stratum,
        f"{seed}:{format_name}:top",
    )
    remainder_ranks = stable_ranks(
        top_rows,
        total_rows,
        per_stratum,
        f"{seed}:{format_name}:remainder",
    )
    rank_strata = {rank: "top_decile" for rank in top_ranks}
    rank_strata.update({rank: "remainder" for rank in remainder_ranks})
    rows = extract_rows(
        selected_locations(spans, rank_strata),
        format_name=format_name,
        include_text=include_text,
    )
    if len(rows) != 2 * per_stratum:
        raise RuntimeError("Sampler did not recover every selected rank")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(
                json.dumps(row, ensure_ascii=False, default=json_default) + "\n"
            )
    os.replace(temporary, output)
    score_counts: dict[str, dict[str, int]] = {}
    for stratum in ("top_decile", "remainder"):
        counter = collections.Counter(
            int(row["quality_score"]) for row in rows if row["stratum"] == stratum
        )
        score_counts[stratum] = {
            str(score): count for score, count in sorted(counter.items(), reverse=True)
        }
    manifest = {
        "created_at": utc_now(),
        "algorithm": ALGORITHM,
        "seed": seed,
        "format": format_name,
        "input_dir": str(input_dir),
        "input_files": len(paths),
        "total_rows": total_rows,
        "top_fraction": top_fraction,
        "top_rows": top_rows,
        "remainder_rows": total_rows - top_rows,
        "per_stratum": per_stratum,
        "sample_rows": len(rows),
        "includes_text": include_text,
        "sampled_score_counts": score_counts,
        "output": str(output),
        "output_sha256": sha256_file(output),
    }
    manifest_path = output.with_suffix(".manifest.json")
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("input_dir", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--format", choices=("html", "pdf"), required=True)
    parser.add_argument("--per-stratum", type=int, default=5_000)
    parser.add_argument("--top-fraction", type=float, default=0.1)
    parser.add_argument("--seed", default=DEFAULT_SEED)
    parser.add_argument("--include-text", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    manifest = sample_dataset(
        input_dir=args.input_dir,
        output=args.output,
        format_name=args.format,
        per_stratum=args.per_stratum,
        top_fraction=args.top_fraction,
        seed=args.seed,
        include_text=args.include_text,
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
