# RefinedGAW Rehydrate

This repository contains tools for retrieving current public source documents referenced by the [RefinedGAW](https://hf.co/datasets/ellamind/RefinedGAW) metadata index.
Optional extractors can process downloaded HTML pages and PDFs using the methods applied when RefinedGAW was built.

The repository does not contain archived HTML, PDF files, or extracted source text from the German Academic Web.
The downloader accesses only current public URLs.

## Requirements

- Git
- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- Python 3.11 or later, which `uv` can install automatically
- Internet access for document retrieval

Downloading HTML and PDF files does not require a GPU.
HTML extraction runs on CPU.
PDF extraction requires an OpenAI-compatible vLLM server running the pinned Unlimited-OCR model.

## Installation

```bash
git clone https://github.com/maxidl/refinedgaw-rehydrate.git
cd refinedgaw-rehydrate
uv sync --frozen
uv run --frozen refinedgaw --help
```

The project supports macOS and Linux.

## Retrieve documents

### 1. Select records

The downloader accepts JSONL files, Parquet files, and directories of Parquet files.
Each record must contain an `id` and at least one source URL.

This example selects two HTML records from the RefinedGAW metadata index.
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

Run the script with the `datasets` package in a temporary uv environment.

```bash
uv run --with datasets select_records.py
```

For PDFs, use the `pdf` configuration and include `pdf_sha256` in the selected columns and output records.
The downloader uses this digest to report whether a retrieved PDF is byte-identical to the archived file.

You can filter the streamed dataset before `take()` to select records by language, content annotation, document length, or another metadata field.
See the RefinedGAW dataset card for the complete dataset schema.

### 2. Download current sources

```bash
uv run --frozen refinedgaw download records.jsonl \
  --format html \
  --output downloads/
```

Use `--format pdf` for records selected from the PDF configuration.
The format override is required when the input does not contain a `format` field.

The downloader attempts the primary URL first, followed by alternative URLs in reverse timestamp order.
It tries at most three URLs per record by default.

### 3. Read the results

The output directory contains:

- `content/` with content-addressed downloaded files
- `results.jsonl` with one final result for each processed record
- `state.sqlite3` with resumable item, attempt, and `robots.txt` state
- `run-manifest.json` with the tool version, configuration, and outcome counts

For a successful record, `selected.local_path` in `results.jsonl` points to the downloaded file under `content/`.
The selected attempt also records the final URL, HTTP status, content type, byte count, SHA-256 digest, redirects, and elapsed time.
For PDFs with an expected digest, `selected.pdf_byte_match` reports whether the downloaded bytes match the archived file.

Failed records remain in `results.jsonl` with statuses such as `missing`, `robots_denied`, `dns_error`, or `timeout`.
The `attempts` field records the outcome for every URL that was tried.

## Resume or retry a run

Run the same command again with the same output directory to resume.
Completed records are skipped.

Add `--retry-failures` to retry completed failures.

```bash
uv run --frozen refinedgaw download records.jsonl \
  --format html \
  --output downloads/ \
  --retry-failures
```

An interrupted record is processed again when the command resumes.

## Input fields

| Field | Required | Description |
|:---|:---:|:---|
| `id` or `record_id` | Yes | RefinedGAW text identifier |
| `format` | Unless `--format` is used | `html` or `pdf` |
| `url` | Unless `url_candidates` is present | Primary source URL |
| `alt_urls` | No | Alternative URLs as strings or objects with `url` and `timestamp` |
| `url_candidates` | No | Precomputed URL list used instead of `url` and `alt_urls` |
| `pdf_sha256` | No | Expected PDF byte digest |
| `timestamp` or `source_timestamp` | No | Source capture timestamp retained in the result |

Duplicate candidate URLs are removed.
The downloader rejects records without an identifier or a valid format.

## Download options

Run `uv run --frozen refinedgaw download --help` for the complete command reference.
The main options are:

| Option | Default | Purpose |
|:---|---:|:---|
| `--format` | Input value | Set every input record to `html` or `pdf` |
| `--limit` | All records | Process only the first N records |
| `--concurrency` | 16 | Set the maximum number of concurrent record workers |
| `--requests-per-minute` | 30 | Limit requests per registrable domain, including `robots.txt` |
| `--min-interval` | 1 second | Set the minimum delay between requests to a registrable domain |
| `--max-urls-per-record` | 3 | Limit the candidate URLs tried for each record |
| `--connect-timeout` | 10 seconds | Set the connection timeout |
| `--read-timeout` | 60 seconds | Set the response read timeout |
| `--retries` | 2 | Set transient-failure retries from 0 to 5 |
| `--max-retry-delay` | 60 seconds | Bound server-supplied `Retry-After` delays |
| `--max-redirects` | 8 | Limit redirects for one request |
| `--max-html-mb` | 20 MB | Limit downloaded HTML size |
| `--max-pdf-mb` | 200 MB | Limit downloaded PDF size |
| `--robots-cache-hours` | 24 hours | Cache successful `robots.txt` checks |
| `--robots-failure-cache-minutes` | 15 minutes | Cache failed `robots.txt` checks |
| `--user-agent` | Tool name and version | Set the request user agent |
| `--retry-failures` | Off | Retry failures already recorded in the output directory |
| `--allow-nonstandard-ports` | Off | Allow public HTTP or HTTPS destinations outside ports 80 and 443 |

Multiple input paths can be supplied in one command.
They share rate limits, downloader state, and the `robots.txt` cache.

## Downloader access rules

The downloader applies these rules:

- Only public HTTP and HTTPS destinations are allowed
- DNS results and every redirect target are checked before access
- Current `robots.txt` rules are checked and cached per origin
- Request limits are shared by subdomains of the same registrable domain
- `Crawl-delay`, `Request-rate`, and bounded `Retry-After` values are honored
- Downloads are streamed with configurable size and timeout limits
- Cookies, credentials, authentication, environment proxies, and automatic redirects are disabled
- PDF byte hashes are checked when `pdf_sha256` is present

Use `--user-agent` to identify your deployment and provide contact information when appropriate.

## Optional HTML extraction

Users who only need current source files can stop after downloading them.

The HTML extractor uses Trafilatura 2.0.0 with the RefinedGAW extraction settings.
It runs on CPU.

```bash
cd extractors/html
uv sync --frozen

uv run --frozen refinedgaw-extract-html \
  ../../downloads/results.jsonl \
  --output ../../downloads/html-extraction/
```

The output contains extracted UTF-8 text under `texts/`, one result per downloaded HTML record in `results.jsonl`, and a reproducibility manifest in `run-manifest.json`.
Each result reports whether the newly extracted text identifier matches the identifier stored in RefinedGAW.
See `extractors/html/README.md` for the exact settings.

## Optional PDF extraction

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

Completed documents are checkpointed and skipped when the command resumes.
The output contains extracted text under `texts/`, one result per downloaded PDF in `results.jsonl`, and a reproducibility manifest in `run-manifest.json`.
Character-for-character reproduction can vary across GPU classes, vLLM versions, and batching configurations.
The result records the observed identifier match.

The repository includes `extractors/pdf/run_slurm.sh` for launching the pinned model and extractor on Slurm.
See `extractors/pdf/README.md` for its requirements and usage.

## Development checks

Run the downloader checks from the repository root.

```bash
uv sync --frozen
uv run --frozen pytest
uv run --frozen ruff check .
```

The HTML and PDF extractor projects have separate environments and test suites.

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
A RefinedGAW record may no longer be retrievable.
A retrieved page may differ from the archived capture.

The tool does not grant rights to source documents and does not bypass access controls.
Users are responsible for ensuring that retrieval and downstream use comply with applicable law, source licenses, website terms, and privacy interests.

## License

The code in this repository is licensed under the Apache License 2.0.
The license does not apply to the RefinedGAW metadata index or to source documents retrieved with the tool.
See `LICENSE` for the complete terms.
