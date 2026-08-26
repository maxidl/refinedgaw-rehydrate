# RefinedGAW PDF extractor

This independent uv project reproduces the page-level Unlimited-OCR pipeline used to build RefinedGAW.
The client renders PDF pages at 300 dpi with PyMuPDF, submits them to an OpenAI-compatible vLLM server, applies the production grounding-tag cleanup, joins successful pages in page order, and computes the archived text identifier.

The model is fixed to `baidu/Unlimited-OCR` revision `27a5997fa0524f9adcf9e2f3d5e7d3f784434fa5`.
The accompanying Slurm script launches the model with its required logits processor.

```bash
uv sync --frozen
uv run --frozen refinedgaw-extract-pdf \
  ../../downloads/results.jsonl \
  --output ../../downloads/pdf-extraction \
  --api-base http://127.0.0.1:8103/v1
```

Completed documents are checkpointed atomically and skipped on restart.
The final `results.jsonl` contains one extraction outcome for every downloaded PDF result.

For a Slurm deployment, set `PDF_RESULTS`, `PDF_OUTPUT`, and `VLLM_IMAGE`, then submit `run_slurm.sh`.
The container must provide the pinned Unlimited-OCR-compatible vLLM runtime.
Pass cluster-specific partition, account, QoS, and GPU constraints to `sbatch`.
The reusable script does not set cluster-specific scheduling options.

Large runs can be distributed as a Slurm array without pre-splitting the downloader output.
Set `SHARD_COUNT` to the array size and submit zero-based array indices, for example `sbatch --array=0-7 --export=ALL,SHARD_COUNT=8,... run_slurm.sh`.
Each task writes to its own `shard-I-of-N` directory, and the experiment summarizer accepts all shard `results.jsonl` files as separate extraction inputs.

The output records whether exact text reproduction occurs.
Small numerical differences between inference backends or batching patterns can change equally plausible OCR formatting choices even with the same model revision and greedy decoding.
Changing the shard count changes request batching and can change exact identifiers.
The experimental protocol includes both the shard count and server configuration.
Use the same GPU class and vLLM backend as the original build when exact identifiers matter.
