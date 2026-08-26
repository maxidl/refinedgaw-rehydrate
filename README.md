# RefinedGAW Rehydrate

This repository contains tools for retrieving currently public source documents referenced by the [RefinedGAW](https://hf.co/datasets/ellamind/RefinedGAW) metadata index.
It also contains the HTML and PDF extraction pipelines used to compare newly extracted text with the identifiers stored in RefinedGAW.

The repository does not contain archived HTML, PDF files, or extracted source text from the German Academic Web.
The downloader accesses only current public URLs.

## Requirements

- Git
- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- Python 3.11 or later, which `uv` can install automatically
- Internet access for document retrieval

Downloading HTML and PDF files does not require a GPU.
HTML text extraction runs on CPU.
PDF text extraction requires an OpenAI-compatible vLLM server running the pinned Unlimited-OCR model.

## Installation

```bash
git clone https://github.com/maxidl/refinedgaw-rehydrate.git
cd refinedgaw-rehydrate
uv sync --frozen
uv run --frozen refinedgaw --help
```

The project targets macOS and Linux with Python 3.11 or later.

## Retrieve a few RefinedGAW documents

### 1. Select records from the metadata index

The downloader accepts JSONL files, Parquet files, and directories containing Parquet files.
Each record must contain an `id` and at least one source URL.

The following example selects two HTML records and writes the fields needed by the downloader.
Save it as `select_records.py`.

```python
import json

from datasets import load_dataset


documents = load_dataset(
    "ellamind/RefinedGAW",
    "html",
    split="train",
    streaming=True,
    columns=["id", "url", "alt_urls"],
)

with open("records.jsonl", "w", encoding="utf-8") as output:
    for row in documents.take(2):
        record = {
            "id": row["id"],
            "url": row["url"],
            "alt_urls": row["alt_urls"],
        }
        output.write(json.dumps(record, ensure_ascii=False) + "\n")
```

Run the selection script with the `datasets` package in a temporary uv environment.

```bash
uv run --with datasets select_records.py
```

For PDFs, use the `pdf` configuration and include `pdf_sha256` in the selected columns and output records.
The downloader uses this digest to report whether a retrieved PDF is byte-identical to the archived file.

Annotations can be used before `take()` to select records by language, content property, length, or another metadata field.
See the RefinedGAW dataset card for the complete schema.

### 2. Download the selected sources

```bash
uv run --frozen refinedgaw download records.jsonl \
  --format html \
  --output downloads/
```

Use `--format pdf` for records selected from the PDF configuration.
The format override is required when the input does not contain a `format` column.

The output directory contains:

- `content/` with content-addressed downloaded files
- `results.jsonl` with one final result for each input record
- `state.sqlite3` with resumable item, attempt, and `robots.txt` state
- `run-manifest.json` with the tool version, configuration, and outcome counts

For a successful record, `selected.local_path` in `results.jsonl` points to its file under `content/`.
Failures remain in `results.jsonl` with categories such as `missing`, `robots_denied`, `dns_error`, or `timeout`.

Running the same command again with the same output directory resumes the run and skips completed records.
Add `--retry-failures` to attempt completed failures again.

```bash
uv run --frozen refinedgaw download records.jsonl \
  --format html \
  --output downloads/ \
  --retry-failures
```

Use `--limit N` to process only the first `N` input records.
Use `--max-urls-per-record N` to control how many primary and alternative URLs are attempted.
Multiple input files can be supplied in one command so that they share rate limits and the `robots.txt` cache.
Run `uv run --frozen refinedgaw download --help` for all downloader options.

## Downloader behavior

The downloader applies the following rules:

- Only public HTTP and HTTPS destinations are allowed
- DNS results and every redirect target are checked before access
- Current `robots.txt` rules are checked and cached per origin
- Request limits are shared by subdomains of the same registrable domain
- `Crawl-delay`, `Request-rate`, and bounded `Retry-After` values are honored
- Downloads are streamed with configurable size and timeout limits
- Cookies, credentials, authentication, environment proxies, and automatic redirects are disabled
- Primary and alternative URLs are attempted in deterministic order
- PDF byte hashes are checked when `pdf_sha256` is present

The default limit is 30 requests per minute per registrable domain, with at least one second between requests.
Requests for `robots.txt` count toward the same limit.
Use `--user-agent` to identify your deployment and provide contact information when appropriate.

## Extract HTML text

Downloading a source and reproducing the RefinedGAW text extraction are separate steps.
Users who only need the current source files can stop after downloading them.

The HTML extractor uses Trafilatura 2.0.0 with the production RefinedGAW settings.
It runs on CPU.

```bash
cd extractors/html
uv sync --frozen

uv run --frozen refinedgaw-extract-html \
  ../../downloads/results.jsonl \
  --output ../../downloads/html-extraction/
```

The output contains extracted UTF-8 text under `texts/`, one result per downloaded HTML record in `results.jsonl`, and a reproducibility manifest in `run-manifest.json`.
Each result reports whether the new text identifier matches the RefinedGAW identifier.

## Extract PDF text

The PDF extractor renders pages at 300 dpi and sends them to an OpenAI-compatible vLLM server.
It uses `baidu/Unlimited-OCR` revision `27a5997fa0524f9adcf9e2f3d5e7d3f784434fa5`.

```bash
cd extractors/pdf
uv sync --frozen

uv run --frozen refinedgaw-extract-pdf \
  ../../downloads/results.jsonl \
  --output ../../downloads/pdf-extraction/ \
  --api-base http://127.0.0.1:8103/v1
```

The repository includes `extractors/pdf/run_slurm.sh` for launching the pinned model and running extraction on Slurm.
The script requires `PDF_RESULTS`, `PDF_OUTPUT`, and `VLLM_IMAGE`.
Cluster-specific partition, account, QoS, and GPU options must be passed to `sbatch`.

PDF extraction supports deterministic input-order sharding with `--shard-count` and `--shard-index`.
Completed documents are checkpointed and skipped when a run resumes.
Use `experiments.merge_pdf_extractions` to combine completed shard outputs.

Character-for-character reproduction can change across GPU classes, vLLM versions, or batching configurations even with the same model revision.
The result files record the observed match.

## Input fields

The downloader reads the following fields from JSONL or Parquet records.

| Field | Required | Description |
|:---|:---:|:---|
| `id` or `record_id` | Yes | RefinedGAW text identifier |
| `format` | Unless `--format` is used | `html` or `pdf` |
| `url` | Yes, unless `url_candidates` is present | Primary source URL |
| `alt_urls` | No | Alternative URLs as strings or objects with `url` and `timestamp` |
| `url_candidates` | No | Precomputed URL list used instead of `url` and `alt_urls` |
| `pdf_sha256` | No | Expected PDF byte digest |
| `timestamp` | No | Source capture timestamp |
| `quality_score` | No | RefinedGAW convenience score retained in results |
| `release_rank` | No | Release rank retained in results |
| `sample_rank` | No | Experiment sample rank retained in results |

The downloader attempts at most three URLs per record by default.
Alternative URLs are ordered by timestamp and URL before this limit is applied.

## Experiment utilities

The `experiments` package contains the commands used for the source-availability analysis in the RefinedGAW paper.
They are not required for ordinary document retrieval.

### Sample the quality-ranking groups

```bash
uv run --frozen python -m experiments.sample_rank_strata release-data/html \
  --format html \
  --per-stratum 5000 \
  --top-fraction 0.1 \
  --seed refinedgaw-source-availability-v1 \
  --output html-sample.jsonl
```

Add `--include-text` only when archived text is available for internal evaluation.
The command writes a manifest next to the sample.

### Interleave samples

```bash
uv run --frozen python -m experiments.interleave_samples \
  html-sample.jsonl pdf-sample.jsonl \
  --output combined-sample.jsonl
```

The command produces a deterministic order from the supplied seed and rejects duplicate format and record identifier pairs.

### Summarize downloads

```bash
uv run --frozen python -m experiments.summarize_downloads \
  downloads/results.jsonl \
  --output download-summary.json
```

The summary validates the expected group sizes, reports group-specific and population-weighted outcomes, computes bootstrap confidence intervals, and reports recovery by crawl date.

### Summarize download and extraction outcomes

```bash
uv run --frozen python -m experiments.summarize_rehydration \
  downloads/results.jsonl \
  downloads/html-extraction/results.jsonl \
  downloads/pdf-extraction/results.jsonl \
  --output rehydration-summary.json
```

Pass internal evaluation samples with `--expected-records` to add normalized five-word-shingle Jaccard recovery and PDF byte-match results.

### Merge PDF extraction shards

```bash
uv run --frozen python -m experiments.merge_pdf_extractions \
  downloads/results.jsonl \
  downloads/pdf-shards/shard-* \
  --expected-shards 8 \
  --output downloads/pdf-extraction/results.jsonl \
  --manifest-output downloads/pdf-extraction/run-manifest.json
```

This command verifies shard count, extraction coverage, duplicate records, and extraction configuration before writing the merged result and manifest.

Run any experiment module with `--help` for its complete arguments.

## Development checks

Run the downloader checks from the repository root.

```bash
uv sync --frozen
uv run --frozen pytest
uv run --frozen ruff check .
```

The HTML and PDF extractor projects have separate locked environments and test suites.

```bash
cd extractors/html
uv sync --frozen
uv run --frozen pytest
uv run --frozen ruff check .
```

```bash
cd extractors/pdf
uv sync --frozen
uv run --frozen pytest
uv run --frozen ruff check .
```

## Limitations and user responsibilities

Retrieval depends on current source availability, DNS, `robots.txt`, rate limits, and server behavior.
A record in RefinedGAW may no longer be retrievable.
A retrieved page may differ from the archived capture.

The tool does not grant rights to source documents and does not bypass access controls.
Users are responsible for ensuring that retrieval and downstream use comply with applicable law, source licenses, website terms, and privacy interests.

## License

The code in this repository is licensed under the Apache License 2.0.
The license does not apply to the RefinedGAW metadata index or to source documents retrieved with the tool.
See `LICENSE` for the complete terms.
