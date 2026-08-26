from __future__ import annotations

import argparse
import collections
import json
import os
import random
import re
from pathlib import Path
from typing import Any

DEFAULT_SEED = "refinedgaw-source-availability-bootstrap-v1"
SAMPLE_NAMES = ("top_decile", "remainder")
TIMESTAMP_PATTERN = re.compile(r"^(\d{4})-(\d{2})")


def percentile(sorted_values: list[float], probability: float) -> float:
    if not sorted_values:
        raise ValueError("Cannot take a percentile of an empty list")
    position = probability * (len(sorted_values) - 1)
    lower = int(position)
    upper = min(lower + 1, len(sorted_values) - 1)
    fraction = position - lower
    return sorted_values[lower] * (1 - fraction) + sorted_values[upper] * fraction


def binomial_draw(rng: random.Random, trials: int, probability: float) -> int:
    method = getattr(rng, "binomialvariate", None)
    if method is not None:
        return int(method(trials, probability))
    return sum(rng.random() < probability for _ in range(trials))


def bootstrap_interval(
    *,
    successes: int,
    trials: int,
    replicates: int,
    rng: random.Random,
) -> tuple[float, float]:
    probability = successes / trials
    draws = sorted(
        binomial_draw(rng, trials, probability) / trials for _ in range(replicates)
    )
    return percentile(draws, 0.025), percentile(draws, 0.975)


def summarize_temporal(
    rows: list[dict[str, Any]], *, top_weight: float
) -> dict[str, Any]:
    sample_weights = {"top_decile": top_weight, "remainder": 1.0 - top_weight}
    counts: dict[tuple[str, str, str, str], dict[str, int]] = collections.defaultdict(
        lambda: {"records": 0, "downloaded": 0}
    )
    sample_totals: collections.Counter[tuple[str, str]] = collections.Counter()
    for row in rows:
        timestamp = str(row.get("source_timestamp") or "")
        match = TIMESTAMP_PATTERN.match(timestamp)
        if match is None:
            record = f"{row.get('format')}/{row.get('record_id')}"
            raise ValueError(f"Invalid source_timestamp for {record}: {timestamp!r}")
        year, month = match.groups()
        if not 1 <= int(month) <= 12:
            record = f"{row.get('format')}/{row.get('record_id')}"
            raise ValueError(f"Invalid source_timestamp for {record}: {timestamp!r}")
        format_name = str(row["format"])
        sample_name = str(row["stratum"])
        sample_totals[(format_name, sample_name)] += 1
        for resolution, bucket in (("years", year), ("months", f"{year}-{month}")):
            bucket_counts = counts[(format_name, resolution, bucket, sample_name)]
            bucket_counts["records"] += 1
            bucket_counts["downloaded"] += row.get("status") == "downloaded"

    result: dict[str, Any] = {}
    for format_name in ("html", "pdf"):
        format_result: dict[str, Any] = {}
        for resolution in ("years", "months"):
            buckets = sorted(
                {
                    key[2]
                    for key in counts
                    if key[0] == format_name and key[1] == resolution
                }
            )
            resolution_result: dict[str, Any] = {}
            for bucket in buckets:
                estimated_share = 0.0
                estimated_downloaded_share = 0.0
                samples: dict[str, Any] = {}
                for sample_name in SAMPLE_NAMES:
                    bucket_counts = counts[
                        (format_name, resolution, bucket, sample_name)
                    ]
                    records = bucket_counts["records"]
                    downloaded = bucket_counts["downloaded"]
                    total = sample_totals[(format_name, sample_name)]
                    sample_mass = sample_weights[sample_name] * records / total
                    estimated_share += sample_mass
                    estimated_downloaded_share += (
                        sample_weights[sample_name] * downloaded / total
                    )
                    samples[sample_name] = {
                        "records": records,
                        "downloaded": downloaded,
                        "downloaded_rate": downloaded / records if records else None,
                    }
                resolution_result[bucket] = {
                    "estimated_index_share": estimated_share,
                    "weighted_downloaded_rate": (
                        estimated_downloaded_share / estimated_share
                    ),
                    "samples": samples,
                }
            format_result[resolution] = resolution_result

        standardized_rates: dict[str, float] = {}
        raw_rates: dict[str, float] = {}
        for sample_name in SAMPLE_NAMES:
            usable_years = [
                year
                for year, values in format_result["years"].items()
                if values["samples"][sample_name]["records"] > 0
            ]
            normalization = sum(
                format_result["years"][year]["estimated_index_share"]
                for year in usable_years
            )
            standardized_rates[sample_name] = (
                sum(
                    format_result["years"][year]["estimated_index_share"]
                    * format_result["years"][year]["samples"][sample_name][
                        "downloaded_rate"
                    ]
                    for year in usable_years
                )
                / normalization
            )
            total = sample_totals[(format_name, sample_name)]
            raw_rates[sample_name] = (
                sum(
                    counts[(format_name, "years", year, sample_name)]["downloaded"]
                    for year in format_result["years"]
                )
                / total
            )
        format_result["crawl_year_standardization"] = {
            "target_distribution": "estimated_full_index_crawl_year_distribution",
            "unadjusted_rates": raw_rates,
            "standardized_rates": standardized_rates,
            "unadjusted_gap": raw_rates["top_decile"] - raw_rates["remainder"],
            "standardized_gap": (
                standardized_rates["top_decile"] - standardized_rates["remainder"]
            ),
        }
        result[format_name] = format_result
    return result


def summarize(
    results_path: Path,
    *,
    expected_per_stratum: int,
    top_weight: float,
    replicates: int,
    seed: str,
) -> dict[str, Any]:
    rows = [
        json.loads(line)
        for line in results_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = collections.defaultdict(list)
    seen: set[tuple[str, str]] = set()
    for row in rows:
        key = (str(row.get("format")), str(row.get("record_id")))
        if key in seen:
            raise ValueError(f"Duplicate result: {key}")
        seen.add(key)
        format_name = str(row.get("format"))
        stratum = str(row.get("stratum"))
        if format_name not in {"html", "pdf"}:
            raise ValueError(f"Unexpected format: {format_name}")
        if stratum not in {"top_decile", "remainder"}:
            raise ValueError(f"Unexpected stratum: {stratum}")
        grouped[(format_name, stratum)].append(row)
    rng = random.Random(seed)
    groups: dict[str, dict[str, Any]] = {}
    for format_name in ("html", "pdf"):
        for stratum in ("top_decile", "remainder"):
            group_rows = grouped[(format_name, stratum)]
            if len(group_rows) != expected_per_stratum:
                raise ValueError(
                    f"{format_name}/{stratum} has {len(group_rows)} results; "
                    f"expected {expected_per_stratum}"
                )
            downloaded = sum(row.get("status") == "downloaded" for row in group_rows)
            lower, upper = bootstrap_interval(
                successes=downloaded,
                trials=len(group_rows),
                replicates=replicates,
                rng=rng,
            )
            failure_counts = collections.Counter(
                str(row.get("status"))
                for row in group_rows
                if row.get("status") != "downloaded"
            )
            groups[f"{format_name}:{stratum}"] = {
                "records": len(group_rows),
                "downloaded": downloaded,
                "downloaded_rate": downloaded / len(group_rows),
                "bootstrap_95_ci": [lower, upper],
                "failure_counts": dict(sorted(failure_counts.items())),
            }
    formats: dict[str, dict[str, Any]] = {}
    remainder_weight = 1.0 - top_weight
    for format_name in ("html", "pdf"):
        top = groups[f"{format_name}:top_decile"]
        remainder = groups[f"{format_name}:remainder"]
        top_probability = float(top["downloaded_rate"])
        remainder_probability = float(remainder["downloaded_rate"])
        status_names = sorted(
            {
                "downloaded",
                *top["failure_counts"],
                *remainder["failure_counts"],
            }
        )
        weighted_status_rates: dict[str, float] = {}
        for status in status_names:
            if status == "downloaded":
                top_count = int(top["downloaded"])
                remainder_count = int(remainder["downloaded"])
            else:
                top_count = int(top["failure_counts"].get(status, 0))
                remainder_count = int(remainder["failure_counts"].get(status, 0))
            weighted_status_rates[status] = top_weight * top_count / int(
                top["records"]
            ) + remainder_weight * remainder_count / int(remainder["records"])
        estimates = []
        for _ in range(replicates):
            top_draw = binomial_draw(
                rng,
                int(top["records"]),
                top_probability,
            ) / int(top["records"])
            remainder_draw = binomial_draw(
                rng,
                int(remainder["records"]),
                remainder_probability,
            ) / int(remainder["records"])
            estimates.append(top_weight * top_draw + remainder_weight * remainder_draw)
        estimates.sort()
        formats[format_name] = {
            "top_weight": top_weight,
            "remainder_weight": remainder_weight,
            "weighted_downloaded_rate": (
                top_weight * top_probability + remainder_weight * remainder_probability
            ),
            "weighted_status_rates": weighted_status_rates,
            "bootstrap_95_ci": [
                percentile(estimates, 0.025),
                percentile(estimates, 0.975),
            ],
        }
    return {
        "schema_version": 2,
        "results": str(results_path),
        "records": len(rows),
        "bootstrap_replicates": replicates,
        "bootstrap_seed": seed,
        "groups": groups,
        "formats": formats,
        "temporal": summarize_temporal(rows, top_weight=top_weight),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("results", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-per-stratum", type=int, default=5_000)
    parser.add_argument("--top-weight", type=float, default=0.1)
    parser.add_argument("--replicates", type=int, default=10_000)
    parser.add_argument("--seed", default=DEFAULT_SEED)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    summary = summarize(
        args.results,
        expected_per_stratum=args.expected_per_stratum,
        top_weight=args.top_weight,
        replicates=args.replicates,
        seed=args.seed,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(f".{args.output.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, args.output)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
