# RefinedGAW Rehydrate

Download current source files for records in the [RefinedGAW dataset on Hugging Face](https://huggingface.co/datasets/ellamind/RefinedGAW).
Optional extractors turn HTML and PDF files into text and check whether it matches the archived extraction.

The dataset contains metadata and annotations. This tool retrieves files from their current URLs, so some sources may be unavailable or changed.

## Install

Requires [uv](https://docs.astral.sh/uv/getting-started/installation/) and Python 3.11 or later.
`uv` can install Python if needed.

```bash
git clone https://github.com/maxidl/refinedgaw-rehydrate.git
cd refinedgaw-rehydrate
uv sync --frozen
```

## Select records

Use JSONL, Parquet, or a directory of Parquet files.
Each record needs an `id` and a `url`. Include `alt_urls` to try alternative sources.
For PDFs, also include `pdf_sha256` to check whether downloaded bytes match the archived file.

For example, save this as `select_records.py` to select ten HTML records:

```python
import json

from datasets import load_dataset

records = load_dataset(
    "ellamind/RefinedGAW",
    "html",
    split="train",
    streaming=True,
    columns=["id", "url", "alt_urls"],
)

with open("records.jsonl", "w", encoding="utf-8") as output:
    for record in records.take(10):
        output.write(json.dumps(record, ensure_ascii=False) + "\n")
```

```bash
uv run --with datasets select_records.py
```

Filter the records before `take()` to select a subset. The [dataset card](https://huggingface.co/datasets/ellamind/RefinedGAW#dataset-structure) describes the available fields.
For PDFs, change the configuration to `pdf` and add `pdf_sha256` to `columns`.

## Download

```bash
uv run --frozen refinedgaw download records.jsonl \
  --format html \
  --output downloads/
```

Use `--format pdf` for PDF records. You can omit `--format` if every record has a `format` field.
The downloader tries the primary URL and up to two alternatives, newest first.
It respects current `robots.txt` rules and limits requests to 30 per minute per registrable domain.

Run the same command to resume. Add `--retry-failures` to retry failed records.
Use `--limit N` for a small trial or `--concurrency N` to set the number of download workers.
Run `uv run --frozen refinedgaw download --help` for all options and defaults.

Results are written to:

- `content/` for downloaded files
- `results.jsonl` for each record's status, URL attempts, and selected file
- `state.sqlite3` for resume state
- `run-manifest.json` for settings and outcome counts

Successful records have status `downloaded`. Their `selected.local_path` points to the file, and `selected.pdf_byte_match` reports PDF byte equality when a reference hash is available.
For PDFs with a reference hash, the downloader tries alternatives until it finds a byte match. If none matches, it keeps the first successful download.
Failures remain in the results with statuses such as `missing`, `robots_denied`, or `timeout`.

## Extract text

HTML extraction runs on CPU with Trafilatura 2.0.0:

```bash
cd extractors/html
uv sync --frozen
uv run --frozen refinedgaw-extract-html \
  ../../downloads/results.jsonl \
  --output ../../downloads/html-extraction/
```

PDF extraction requires a vLLM server running the pinned Unlimited-OCR model.
See the [PDF extractor README](extractors/pdf/README.md) for setup and Slurm usage.
From the repository root:

```bash
cd extractors/pdf
uv sync --frozen
uv run --frozen refinedgaw-extract-pdf \
  ../../downloads/results.jsonl \
  --output ../../downloads/pdf-extraction/ \
  --api-base http://127.0.0.1:8103/v1
```

Both extractors write text to `texts/` and report identifier matches in `results.jsonl`.
See the [HTML extractor README](extractors/html/README.md) for its settings.
PDF OCR can vary across hardware and batching configurations, even when the source file is unchanged.
Annotations in the dataset describe the archived text and may not describe a changed live page.

## Development

Run these commands in the repository root or either extractor directory:

```bash
uv sync --frozen
uv run --frozen pytest
uv run --frozen ruff check .
```

## License

The code is licensed under [Apache 2.0](LICENSE).
Source documents retain their own rights and licenses. The tool does not grant permission to use them or provide access to the GAW archive.
