from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any

DEFAULT_SEED = "refinedgaw-source-availability-interleave-v1"


def order_key(row: dict[str, Any], seed: str) -> bytes:
    format_name = str(row.get("format"))
    record_id = str(row.get("id") or row.get("record_id"))
    return hashlib.sha256(f"{seed}\0{format_name}\0{record_id}".encode()).digest()


def interleave(inputs: list[Path], output: Path, seed: str) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for path in inputs:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            key = (str(row.get("format")), str(row.get("id") or row.get("record_id")))
            if key in seen:
                raise ValueError(f"Duplicate record: {key}")
            seen.add(key)
            rows.append(row)
    rows.sort(key=lambda row: order_key(row, seed))
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    os.replace(temporary, output)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    manifest = {
        "algorithm": "sha256-record-interleave-v1",
        "seed": seed,
        "inputs": [str(path) for path in inputs],
        "records": len(rows),
        "output": str(output),
        "output_sha256": digest,
    }
    output.with_suffix(".manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--seed", default=DEFAULT_SEED)
    args = parser.parse_args()
    manifest = interleave(args.inputs, args.output, args.seed)
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
