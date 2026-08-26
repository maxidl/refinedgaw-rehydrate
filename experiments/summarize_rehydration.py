from __future__ import annotations

import argparse
import collections
import json
import os
import random
import re
import unicodedata
from pathlib import Path
from typing import Any

from experiments.summarize_downloads import binomial_draw, percentile

DEFAULT_SEED = "refinedgaw-source-availability-pipeline-bootstrap-v1"
OUTCOMES = ("downloaded", "extracted", "similarity_0_8", "exact_match")
SIMILARITY_THRESHOLD = 0.8


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as source:
        return [json.loads(line) for line in source if line.strip()]


def unique_by_key(
    rows: list[dict[str, Any]], *, label: str
) -> dict[tuple[str, str], dict[str, Any]]:
    indexed: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (str(row.get("format")), str(row.get("record_id")))
        if key in indexed:
            raise ValueError(f"Duplicate {label} result: {key}")
        indexed[key] = row
    return indexed


def normalized_words(text: str) -> list[str]:
    characters = []
    for character in unicodedata.normalize("NFD", text.lower()):
        category = unicodedata.category(character)
        if category == "Mn" or category.startswith("P"):
            continue
        characters.append("0" if category.startswith("N") else character)
    normalized = re.sub(r"0+", "0", "".join(characters))
    return normalized.split()


def five_shingle_jaccard(left: str, right: str) -> float:
    def shingles(text: str) -> set[tuple[str, ...]]:
        words = normalized_words(text)
        if len(words) < 5:
            return {tuple(words)} if words else set()
        return set(zip(words, words[1:], words[2:], words[3:], words[4:], strict=False))

    left_shingles = shingles(left)
    right_shingles = shingles(right)
    if not left_shingles and not right_shingles:
        return 1.0
    return len(left_shingles & right_shingles) / len(left_shingles | right_shingles)


def read_expected_texts(paths: list[Path]) -> dict[tuple[str, str], str]:
    expected: dict[tuple[str, str], str] = {}
    for path in paths:
        for row in read_jsonl(path):
            format_name = str(row.get("format"))
            record_id = str(row.get("record_id") or row.get("id"))
            text = row.get("text")
            if not isinstance(text, str):
                key = (format_name, record_id)
                raise ValueError(f"Expected text is missing for {key}")
            key = (format_name, record_id)
            if key in expected:
                raise ValueError(f"Duplicate expected text: {key}")
            expected[key] = text
    return expected


def weighted_interval(
    *,
    top_successes: int,
    remainder_successes: int,
    trials: int,
    top_weight: float,
    replicates: int,
    rng: random.Random,
) -> tuple[float, float]:
    top_probability = top_successes / trials
    remainder_probability = remainder_successes / trials
    values = sorted(
        top_weight * binomial_draw(rng, trials, top_probability) / trials
        + (1 - top_weight) * binomial_draw(rng, trials, remainder_probability) / trials
        for _ in range(replicates)
    )
    return percentile(values, 0.025), percentile(values, 0.975)


def summarize(
    downloads_path: Path,
    extraction_paths: list[Path],
    *,
    expected_per_stratum: int,
    top_weight: float,
    replicates: int,
    seed: str,
    expected_record_paths: list[Path] | None = None,
) -> dict[str, Any]:
    downloads = unique_by_key(read_jsonl(downloads_path), label="download")
    extraction_rows = [row for path in extraction_paths for row in read_jsonl(path)]
    extractions = unique_by_key(extraction_rows, label="extraction")
    expected_texts = (
        read_expected_texts(expected_record_paths)
        if expected_record_paths is not None
        else None
    )
    downloaded_keys = {
        key for key, row in downloads.items() if row.get("status") == "downloaded"
    }
    extraction_keys = set(extractions)
    if extraction_keys != downloaded_keys:
        missing = sorted(downloaded_keys - extraction_keys)[:3]
        extra = sorted(extraction_keys - downloaded_keys)[:3]
        raise ValueError(
            "Extraction results must cover every and only downloaded record: "
            f"missing={missing}, extra={extra}"
        )

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = collections.defaultdict(list)
    for row in downloads.values():
        format_name = str(row.get("format"))
        stratum = str(row.get("stratum"))
        if format_name not in {"html", "pdf"}:
            raise ValueError(f"Unexpected format: {format_name}")
        if stratum not in {"top_decile", "remainder"}:
            raise ValueError(f"Unexpected stratum: {stratum}")
        grouped[(format_name, stratum)].append(row)

    groups: dict[str, dict[str, Any]] = {}
    for format_name in ("html", "pdf"):
        for stratum in ("top_decile", "remainder"):
            rows = grouped[(format_name, stratum)]
            if len(rows) != expected_per_stratum:
                raise ValueError(
                    f"{format_name}/{stratum} has {len(rows)} records; "
                    f"expected {expected_per_stratum}"
                )
            counts = dict.fromkeys(OUTCOMES, 0)
            pdf_byte_matches = 0
            similarities: list[float] = []
            extraction_statuses: collections.Counter[str] = collections.Counter()
            for row in rows:
                key = (format_name, str(row["record_id"]))
                if row.get("status") != "downloaded":
                    continue
                counts["downloaded"] += 1
                extraction = extractions[key]
                extraction_statuses[str(extraction.get("status"))] += 1
                if (
                    extraction.get("exact_text_match") is True
                    and extraction.get("status") != "extracted"
                ):
                    raise ValueError(f"Exact match without extracted text: {key}")
                if extraction.get("status") == "extracted":
                    counts["extracted"] += 1
                    if expected_texts is not None:
                        expected_text = expected_texts.get(key)
                        if expected_text is None:
                            raise ValueError(f"Missing expected text for {key}")
                        text_path = extraction.get("text_path")
                        if not isinstance(text_path, str):
                            raise ValueError(f"Missing extracted text path for {key}")
                        extracted_text = Path(text_path).read_text(encoding="utf-8")
                        similarity = five_shingle_jaccard(
                            expected_text,
                            extracted_text,
                        )
                        similarities.append(similarity)
                        if similarity >= SIMILARITY_THRESHOLD:
                            counts["similarity_0_8"] += 1
                if extraction.get("exact_text_match") is True:
                    counts["exact_match"] += 1
                if (
                    format_name == "pdf"
                    and extraction.get("downloaded_pdf_byte_match") is True
                ):
                    pdf_byte_matches += 1
            if expected_texts is None:
                counts.pop("similarity_0_8")
            elif counts["exact_match"] > counts["similarity_0_8"]:
                raise ValueError(
                    f"Exact matches must reach the similarity threshold for "
                    f"{format_name}/{stratum}"
                )
            if not (
                counts["exact_match"] <= counts["extracted"] <= counts["downloaded"]
            ):
                raise ValueError(f"Non-nested outcomes for {format_name}/{stratum}")
            group_summary = {
                "records": len(rows),
                "counts": counts,
                "rates": {
                    name: count / len(rows) for name, count in counts.items()
                },
                "extraction_status_counts": dict(sorted(extraction_statuses.items())),
            }
            if expected_texts is not None:
                ordered = sorted(similarities)
                group_summary["extracted_similarity_quantiles"] = {
                    "0.1": percentile(ordered, 0.1),
                    "0.5": percentile(ordered, 0.5),
                    "0.9": percentile(ordered, 0.9),
                }
            if format_name == "pdf":
                group_summary["counts"]["pdf_byte_match"] = pdf_byte_matches
                group_summary["rates"]["pdf_byte_match"] = (
                    pdf_byte_matches / len(rows)
                )
            groups[f"{format_name}:{stratum}"] = group_summary

    rng = random.Random(seed)
    formats: dict[str, dict[str, Any]] = {}
    for format_name in ("html", "pdf"):
        top = groups[f"{format_name}:top_decile"]
        remainder = groups[f"{format_name}:remainder"]
        format_outcomes: dict[str, Any] = {}
        format_outcome_names = list(OUTCOMES)
        if expected_texts is None:
            format_outcome_names.remove("similarity_0_8")
        if format_name == "pdf":
            format_outcome_names.append("pdf_byte_match")
        for outcome in format_outcome_names:
            top_count = int(top["counts"][outcome])
            remainder_count = int(remainder["counts"][outcome])
            lower, upper = weighted_interval(
                top_successes=top_count,
                remainder_successes=remainder_count,
                trials=expected_per_stratum,
                top_weight=top_weight,
                replicates=replicates,
                rng=rng,
            )
            estimate = (
                top_weight * top_count / expected_per_stratum
                + (1 - top_weight) * remainder_count / expected_per_stratum
            )
            format_outcomes[outcome] = {
                "weighted_rate": estimate,
                "bootstrap_95_ci": [lower, upper],
            }
        formats[format_name] = format_outcomes

    return {
        "schema_version": 1,
        "downloads": str(downloads_path),
        "extractions": [str(path) for path in extraction_paths],
        "records": len(downloads),
        "expected_per_stratum": expected_per_stratum,
        "top_weight": top_weight,
        "bootstrap_replicates": replicates,
        "bootstrap_seed": seed,
        "similarity": (
            {
                "normalization": "lowercase-number-punctuation-diacritic-whitespace-v1",
                "shingle_size": 5,
                "threshold": SIMILARITY_THRESHOLD,
                "expected_records": [str(path) for path in expected_record_paths],
            }
            if expected_record_paths is not None
            else None
        ),
        "groups": groups,
        "formats": formats,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("downloads", type=Path)
    parser.add_argument("extractions", type=Path, nargs="+")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-per-stratum", type=int, default=5_000)
    parser.add_argument("--top-weight", type=float, default=0.1)
    parser.add_argument("--replicates", type=int, default=10_000)
    parser.add_argument("--seed", default=DEFAULT_SEED)
    parser.add_argument("--expected-records", type=Path, nargs="+")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    summary = summarize(
        args.downloads,
        args.extractions,
        expected_per_stratum=args.expected_per_stratum,
        top_weight=args.top_weight,
        replicates=args.replicates,
        seed=args.seed,
        expected_record_paths=args.expected_records,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(f".{args.output.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, args.output)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
