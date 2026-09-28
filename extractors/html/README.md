# RefinedGAW HTML extractor

Extract text from downloaded HTML using the RefinedGAW settings and compare its identifier with the archived extraction.
Run from `extractors/html`:

```bash
uv sync --frozen
uv run --frozen refinedgaw-extract-html \
  ../../downloads/results.jsonl \
  --output ../../downloads/html-extraction
```

The input is the downloader's `results.jsonl` file.
The output directory contains extracted UTF-8 text under `texts/`, one result per downloaded HTML record in `results.jsonl`, and a reproducibility manifest in `run-manifest.json`.

Extraction settings:

- Trafilatura 2.0.0
- `output_format="txt"`
- `favor_precision=True`
- `include_comments=False`
- `deduplicate=True`
- extracted text longer than 300 Unicode characters.
