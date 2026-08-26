# RefinedGAW HTML extractor

This independent uv project reproduces the HTML text extraction used to build RefinedGAW.
It uses the production Trafilatura version and arguments, applies the production minimum-length criterion, and computes the same 128-bit truncated SHA-256 text identifier.

```bash
uv sync --frozen
uv run --frozen refinedgaw-extract-html \
  ../../downloads/results.jsonl \
  --output ../../downloads/html-extraction
```

The input is the downloader's `results.jsonl` file.
The output directory contains extracted UTF-8 text under `texts/`, one result per downloaded HTML record in `results.jsonl`, and a reproducibility manifest in `run-manifest.json`.

The extractor uses these production settings:

- Trafilatura 2.0.0
- `output_format="txt"`
- `favor_precision=True`
- `include_comments=False`
- `deduplicate=True`
- extracted text longer than 300 Unicode characters.
