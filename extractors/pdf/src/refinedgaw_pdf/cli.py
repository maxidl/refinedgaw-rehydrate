from __future__ import annotations

import argparse
import asyncio
import base64
import concurrent.futures
import datetime as dt
import hashlib
import json
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any

import fitz
import httpx
from openai import AsyncOpenAI

from . import __version__

MODEL = "baidu/Unlimited-OCR"
MODEL_REVISION = "27a5997fa0524f9adcf9e2f3d5e7d3f784434fa5"
DET_RE = re.compile(r"<\|det\|>.*?<\|/det\|>", re.DOTALL)
REF_RE = re.compile(r"<\|ref\|>(.*?)<\|/ref\|>", re.DOTALL)

fitz.TOOLS.mupdf_display_errors(False)


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def clean_output(text: str) -> str:
    text = DET_RE.sub("", text)
    text = REF_RE.sub(r"\1", text)
    return text.replace("<PAGE>", "\n\n<!-- PAGE -->\n\n").strip()


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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


def render_document(
    pdf_path_string: str, dpi: int, jpeg_quality: int
) -> list[dict[str, Any]]:
    pdf_path = Path(pdf_path_string)
    rendered: list[dict[str, Any]] = []
    with fitz.open(pdf_path) as document:
        matrix = fitz.Matrix(dpi / 72, dpi / 72)
        for page_number, page in enumerate(document, start=1):
            pixmap = page.get_pixmap(matrix=matrix, alpha=False)
            encoded = base64.b64encode(
                pixmap.tobytes("jpeg", jpg_quality=jpeg_quality)
            ).decode("ascii")
            rendered.append(
                {
                    "page": page_number,
                    "image_url": f"data:image/jpeg;base64,{encoded}",
                }
            )
    return rendered


async def infer_page(
    client: AsyncOpenAI,
    semaphore: asyncio.Semaphore,
    page: dict[str, Any],
    *,
    max_tokens: int,
    retries: int,
) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            async with semaphore:
                response = await client.chat.completions.create(
                    model=MODEL,
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "<image>document parsing."},
                                {
                                    "type": "image_url",
                                    "image_url": {"url": page["image_url"]},
                                },
                            ],
                        }
                    ],
                    max_tokens=max_tokens,
                    temperature=0.0,
                    extra_body={
                        "skip_special_tokens": False,
                        "vllm_xargs": {"ngram_size": 35, "window_size": 128},
                    },
                )
            raw = response.choices[0].message.content or ""
            return {
                "page": page["page"],
                "status": "ok",
                "text": clean_output(raw),
                "attempts": attempt,
                "finish_reason": response.choices[0].finish_reason,
            }
        except Exception as error:
            last_error = error
            if attempt < retries:
                await asyncio.sleep(min(2 ** (attempt - 1), 8))
    return {
        "page": page["page"],
        "status": "inference_error",
        "text": None,
        "attempts": retries,
        "error": f"{type(last_error).__name__}: {last_error}",
    }


def assemble_document(
    row: dict[str, Any],
    page_results: list[dict[str, Any]],
    output_dir: Path,
) -> dict[str, Any]:
    ordered = sorted(page_results, key=lambda page: int(page["page"]))
    texts = [
        str(page["text"]).strip()
        for page in ordered
        if page.get("status") == "ok" and page.get("text")
    ]
    text = "\n\n".join(part for part in texts if part)
    full_hash = text_hash(text) if text else None
    extracted_id = full_hash[:32] if full_hash else None
    text_path: Path | None = None
    if text and extracted_id:
        text_path = output_dir / "texts" / extracted_id[:2] / f"{extracted_id}.txt"
        atomic_bytes(text_path, text.encode("utf-8"))
    page_counts = Counter(str(page.get("status")) for page in ordered)
    expected_id = str(row.get("expected_text_id") or row.get("record_id"))
    return {
        "record_id": row.get("record_id"),
        "format": "pdf",
        "stratum": row.get("stratum"),
        "release_rank": row.get("release_rank"),
        "sample_rank": row.get("sample_rank"),
        "source_path": row["selected"]["local_path"],
        "expected_text_id": expected_id,
        "expected_pdf_sha256": row.get("expected_pdf_sha256"),
        "downloaded_pdf_byte_match": row["selected"].get("pdf_byte_match"),
        "status": "extracted" if text else "extraction_error",
        "pages": len(ordered),
        "page_status_counts": dict(sorted(page_counts.items())),
        "extracted_chars": len(text) if text else None,
        "extracted_text_sha256": full_hash,
        "extracted_text_id": extracted_id,
        "exact_text_match": extracted_id == expected_id if extracted_id else False,
        "text_path": str(text_path) if text_path else None,
        "completed_at": utc_now(),
    }


def downloaded_pdf_rows(
    path: Path, *, shard_index: int = 0, shard_count: int = 1
) -> list[dict[str, Any]]:
    if shard_count < 1:
        raise ValueError("shard_count must be positive")
    if not 0 <= shard_index < shard_count:
        raise ValueError("shard_index must be in [0, shard_count)")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("format") != "pdf" or row.get("status") != "downloaded":
                continue
            record_id = str(row.get("record_id"))
            if record_id in seen:
                raise ValueError(f"Duplicate PDF record_id on line {line_number}")
            seen.add(record_id)
            selected = row.get("selected")
            if not isinstance(selected, dict) or not selected.get("local_path"):
                raise ValueError(
                    f"Downloaded PDF result on line {line_number} lacks local_path"
                )
            resolved = Path(selected["local_path"])
            if not resolved.is_absolute():
                resolved = path.parent / resolved
            row = dict(row)
            row["selected"] = {**selected, "local_path": str(resolved)}
            rows.append(row)
    return rows[shard_index::shard_count]


async def run_async(args: argparse.Namespace) -> dict[str, Any]:
    rows = downloaded_pdf_rows(
        args.results,
        shard_index=args.shard_index,
        shard_count=args.shard_count,
    )
    args.output.mkdir(parents=True, exist_ok=True)
    checkpoints = args.output / "checkpoints"
    checkpoints.mkdir(parents=True, exist_ok=True)
    limits = httpx.Limits(
        max_connections=args.concurrency,
        max_keepalive_connections=args.concurrency,
    )
    http_client = httpx.AsyncClient(limits=limits, timeout=args.timeout)
    client = AsyncOpenAI(
        api_key="EMPTY",
        base_url=args.api_base,
        timeout=args.timeout,
        http_client=http_client,
    )
    inference_semaphore = asyncio.Semaphore(args.concurrency)
    document_semaphore = asyncio.Semaphore(args.render_workers * 2)
    loop = asyncio.get_running_loop()
    process_pool = concurrent.futures.ProcessPoolExecutor(
        max_workers=args.render_workers
    )

    async def process(row: dict[str, Any]) -> dict[str, Any]:
        record_id = str(row["record_id"])
        checkpoint = checkpoints / record_id[:2] / f"{record_id}.json"
        if checkpoint.is_file():
            value = json.loads(checkpoint.read_text(encoding="utf-8"))
            if value.get("record_id") != record_id:
                raise ValueError(f"Invalid checkpoint: {checkpoint}")
            return value
        async with document_semaphore:
            try:
                pages = await loop.run_in_executor(
                    process_pool,
                    render_document,
                    row["selected"]["local_path"],
                    args.dpi,
                    args.jpeg_quality,
                )
            except Exception as error:
                result = {
                    "record_id": record_id,
                    "format": "pdf",
                    "stratum": row.get("stratum"),
                    "release_rank": row.get("release_rank"),
                    "sample_rank": row.get("sample_rank"),
                    "source_path": row["selected"]["local_path"],
                    "expected_text_id": row.get("expected_text_id"),
                    "expected_pdf_sha256": row.get("expected_pdf_sha256"),
                    "status": "render_error",
                    "error": f"{type(error).__name__}: {error}",
                    "exact_text_match": False,
                    "completed_at": utc_now(),
                }
                atomic_json(checkpoint, result)
                return result
            page_results = await asyncio.gather(
                *(
                    infer_page(
                        client,
                        inference_semaphore,
                        page,
                        max_tokens=args.max_tokens,
                        retries=args.retries,
                    )
                    for page in pages
                )
            )
            result = assemble_document(row, page_results, args.output)
            atomic_json(checkpoint, result)
            return result

    started_at = utc_now()
    try:
        tasks = [asyncio.create_task(process(row)) for row in rows]
        results: list[dict[str, Any]] = []
        for completed, task in enumerate(tasks, start=1):
            results.append(await task)
            if completed == 1 or completed % 25 == 0:
                print(f"Processed {completed:,}/{len(tasks):,} downloaded PDFs")
    finally:
        process_pool.shutdown(wait=True, cancel_futures=True)
        await client.close()
    results_output = args.output / "results.jsonl"
    atomic_bytes(
        results_output,
        b"".join(
            (json.dumps(result, sort_keys=True) + "\n").encode("utf-8")
            for result in results
        ),
    )
    counts = Counter(str(result.get("status")) for result in results)
    manifest = {
        "schema_version": 1,
        "tool": "refinedgaw-pdf-extractor",
        "tool_version": __version__,
        "model": MODEL,
        "model_revision": MODEL_REVISION,
        "started_at": started_at,
        "completed_at": utc_now(),
        "input": str(args.results),
        "downloaded_pdf_records": len(rows),
        "configuration": {
            "api_base": args.api_base,
            "concurrency": args.concurrency,
            "render_workers": args.render_workers,
            "dpi": args.dpi,
            "jpeg_quality": args.jpeg_quality,
            "max_tokens": args.max_tokens,
            "retries": args.retries,
            "timeout": args.timeout,
            "shard_index": args.shard_index,
            "shard_count": args.shard_count,
        },
        "outcome_counts": dict(sorted(counts.items())),
        "exact_text_matches": sum(
            result.get("exact_text_match") is True for result in results
        ),
    }
    atomic_json(args.output / "run-manifest.json", manifest)
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Reproduce RefinedGAW Unlimited-OCR PDF extraction"
    )
    parser.add_argument("results", type=Path, help="Downloader results.jsonl")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--api-base", required=True)
    parser.add_argument("--concurrency", type=int, default=256)
    parser.add_argument("--render-workers", type=int, default=16)
    parser.add_argument("--dpi", type=int, default=300)
    parser.add_argument("--jpeg-quality", type=int, default=90)
    parser.add_argument("--max-tokens", type=int, default=8192)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=1200)
    parser.add_argument(
        "--shard-count",
        type=int,
        default=1,
        help="Split downloaded PDFs into this many deterministic input-order shards",
    )
    parser.add_argument(
        "--shard-index",
        type=int,
        default=0,
        help="Zero-based shard to process",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    for name in ("concurrency", "render_workers", "dpi", "retries", "shard_count"):
        if getattr(args, name) < 1:
            raise SystemExit(f"--{name.replace('_', '-')} must be positive")
    if not 0 <= args.shard_index < args.shard_count:
        raise SystemExit("--shard-index must be in [0, --shard-count)")
    manifest = asyncio.run(run_async(args))
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
