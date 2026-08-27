# RefinedGAW PDF extractor

This uv project reproduces the page-level PDF extraction used to build RefinedGAW.
The client renders pages with PyMuPDF, submits them to an OpenAI-compatible vLLM server, removes grounding tags, joins successful pages in page order, and computes the RefinedGAW text identifier.

The model is fixed to `baidu/Unlimited-OCR` revision `27a5997fa0524f9adcf9e2f3d5e7d3f784434fa5`.
The default rendering resolution is 300 dpi.

## Run the extractor

Start a compatible vLLM server with the pinned model and its `NGramPerReqLogitsProcessor`.
Then install and run the client.

```bash
uv sync --frozen
uv run --frozen refinedgaw-extract-pdf \
  ../../downloads/results.jsonl \
  --output ../../downloads/pdf-extraction \
  --api-base http://127.0.0.1:8103/v1
```

The input must be the `results.jsonl` produced by the RefinedGAW downloader.
Only records with format `pdf`, status `downloaded`, and a valid `selected.local_path` are processed.

The output directory contains:

- `texts/` with extracted UTF-8 text
- `results.jsonl` with one extraction result for each downloaded PDF
- `checkpoints/` with one atomic checkpoint per processed PDF
- `run-manifest.json` with the model revision, configuration, and outcome counts

Running the same command again with the same output directory reuses completed checkpoints.
The extractor rejects duplicate PDF record identifiers.

Run `uv run --frozen refinedgaw-extract-pdf --help` for the complete option reference.
Options control request concurrency, rendering workers, rendering resolution, JPEG quality, maximum output tokens, retries, and request timeout.

## Run on Slurm

`run_slurm.sh` starts the pinned model in an Apptainer container, waits for its health endpoint, and then runs the extractor.
Set these variables before submission:

- `PDF_RESULTS` to the downloader `results.jsonl`
- `PDF_OUTPUT` to the extraction output directory
- `VLLM_IMAGE` to an Unlimited-OCR-compatible Apptainer image

Submit cluster-specific scheduling options through `sbatch`.
The script does not set a partition, account, QoS, or GPU model.

```bash
sbatch \
  --partition YOUR_PARTITION \
  --account YOUR_ACCOUNT \
  --export=ALL,PDF_RESULTS=/path/to/results.jsonl,PDF_OUTPUT=/path/to/output,VLLM_IMAGE=/path/to/vllm.sif \
  run_slurm.sh
```

The optional variables `PORT`, `CONCURRENCY`, `RENDER_WORKERS`, `HF_HOME`, `UV_BIN`, and `REFINEDGAW_PDF_DIR` override the script defaults.

## Reproduction limits

The result records whether the extracted text identifier exactly matches the identifier stored in RefinedGAW.
GPU class, vLLM version, and request batching can change OCR formatting even with the same model revision and greedy decoding.
Use the original runtime configuration when character-for-character reproduction is required.
