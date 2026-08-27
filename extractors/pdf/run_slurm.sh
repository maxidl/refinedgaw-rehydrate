#!/bin/bash
#SBATCH --job-name=refinedgaw-pdf
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=32
#SBATCH --mem=128G
#SBATCH --gres=gpu:1
#SBATCH --time=12:00:00
#SBATCH --signal=B:TERM@120
#SBATCH --output=refinedgaw-pdf-%j.log
#SBATCH --error=refinedgaw-pdf-%j.log

# Supply cluster-specific partition, account, QoS, and GPU constraints to sbatch.

set -euo pipefail

: "${PDF_RESULTS:?Set PDF_RESULTS to downloader results.jsonl}"
: "${PDF_OUTPUT:?Set PDF_OUTPUT to the extraction output directory}"
: "${VLLM_IMAGE:?Set VLLM_IMAGE to an Unlimited-OCR-compatible container}"

if [ -n "${SLURM_SUBMIT_DIR:-}" ]; then
    PROJECT_DIR=${REFINEDGAW_PDF_DIR:-${SLURM_SUBMIT_DIR}}
else
    PROJECT_DIR=${REFINEDGAW_PDF_DIR:-$(cd "$(dirname "$0")" && pwd)}
fi
UV_BIN=${UV_BIN:-uv}
PORT=${PORT:-8103}
CONCURRENCY=${CONCURRENCY:-256}
RENDER_WORKERS=${RENDER_WORKERS:-16}
HF_CACHE=${HF_HOME:-${HOME}/.cache/huggingface}
SERVER_LOG=${PDF_OUTPUT}/vllm.log
JOB_VENV=${SLURM_TMPDIR:-/tmp}/refinedgaw-pdf-venv
mkdir -p "${PDF_OUTPUT}" "${HF_CACHE}"

apptainer exec --nv \
    --bind "${HF_CACHE}:/root/.cache/huggingface" \
    --env HF_HOME=/root/.cache/huggingface \
    "${VLLM_IMAGE}" \
    vllm serve baidu/Unlimited-OCR \
    --revision 27a5997fa0524f9adcf9e2f3d5e7d3f784434fa5 \
    --host 127.0.0.1 \
    --port "${PORT}" \
    --served-model-name baidu/Unlimited-OCR \
    --api-server-count 4 \
    --trust-remote-code \
    --logits_processors vllm.model_executor.models.unlimited_ocr:NGramPerReqLogitsProcessor \
    --no-enable-prefix-caching \
    --mm-processor-cache-gb 0 \
    --max-num-seqs 256 \
    --max-num-batched-tokens 8192 \
    --gpu-memory-utilization 0.90 \
    >"${SERVER_LOG}" 2>&1 &
SERVER_PID=$!

cleanup() {
    kill "${SERVER_PID}" 2>/dev/null || true
    wait "${SERVER_PID}" 2>/dev/null || true
}
trap cleanup EXIT

for _ in $(seq 1 240); do
    if curl --fail --silent "http://127.0.0.1:${PORT}/health" >/dev/null; then
        break
    fi
    if ! kill -0 "${SERVER_PID}" 2>/dev/null; then
        tail -n 100 "${SERVER_LOG}" >&2
        exit 1
    fi
    sleep 5
done
curl --fail --silent "http://127.0.0.1:${PORT}/health" >/dev/null

cd "${PROJECT_DIR}"
UV_PROJECT_ENVIRONMENT="${JOB_VENV}" UV_LINK_MODE=copy \
    "${UV_BIN}" sync --frozen
UV_PROJECT_ENVIRONMENT="${JOB_VENV}" UV_LINK_MODE=copy \
    "${UV_BIN}" run --frozen refinedgaw-extract-pdf \
    "${PDF_RESULTS}" \
    --output "${PDF_OUTPUT}" \
    --api-base "http://127.0.0.1:${PORT}/v1" \
    --concurrency "${CONCURRENCY}" \
    --render-workers "${RENDER_WORKERS}"
