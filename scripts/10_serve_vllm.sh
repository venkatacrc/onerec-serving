#!/usr/bin/env bash
# Launches OneRec-8B-Pro behind vLLM's OpenAI-compatible server, in Docker.
#
# Usage:
#   ./scripts/10_serve_vllm.sh --run-name vllm-tp1-bf16 --tp 1 --gpus 0 \
#       --dtype bfloat16 --max-model-len 8192 [--port 8000] [--extra-args "..."]
#
# Blocks until the server answers /health, then returns. Use
# ./scripts/90_stop_serving.sh to tear it down.
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

RUN_NAME="vllm-default"
TP=1
GPUS="0"
DTYPE="bfloat16"
MAX_MODEL_LEN=8192
PORT="$VLLM_PORT"
EXTRA_ARGS=""
GPU_MEM_UTIL=0.90

while [[ $# -gt 0 ]]; do
  case "$1" in
    --run-name) RUN_NAME="$2"; shift 2;;
    --tp) TP="$2"; shift 2;;
    --gpus) GPUS="$2"; shift 2;;
    --dtype) DTYPE="$2"; shift 2;;
    --max-model-len) MAX_MODEL_LEN="$2"; shift 2;;
    --port) PORT="$2"; shift 2;;
    --gpu-mem-util) GPU_MEM_UTIL="$2"; shift 2;;
    --extra-args) EXTRA_ARGS="$2"; shift 2;;
    *) die "unknown arg: $1";;
  esac
done

NGPU_LIST=$(gpu_count_from_list "$GPUS")
if [[ "$NGPU_LIST" -ne "$TP" ]]; then
  die "--gpus lists ${NGPU_LIST} device(s) but --tp is ${TP}; they must match."
fi

CONTAINER_NAME="${CONTAINER_NAME_PREFIX}-vllm-${RUN_NAME}"
stop_container "$CONTAINER_NAME"

QUANT_ARGS=()
if [[ "$DTYPE" == "fp8" ]]; then
  # Online (weights-only-dynamic) FP8 quantization; the model ships as
  # bf16, so we ask vLLM to quantize on load rather than requiring a
  # pre-quantized checkpoint. Serve dtype stays bf16 for activations that
  # don't support fp8 kernels on this arch; --quantization drives the GEMMs.
  QUANT_ARGS=(--quantization fp8)
  DTYPE_ARG="bfloat16"
else
  DTYPE_ARG="$DTYPE"
fi

mkdir -p "$LOG_DIR"
LOG_FILE="${LOG_DIR}/${CONTAINER_NAME}.log"

log_info "Starting vLLM server '${CONTAINER_NAME}' on GPU(s) ${GPUS} (TP=${TP}, dtype=${DTYPE}, max-model-len=${MAX_MODEL_LEN}, port=${PORT})"
log_info "Image: ${VLLM_IMAGE}"

# shellcheck disable=SC2086
# NOTE: the embedded literal double-quotes around device=... are REQUIRED,
# not a typo. Docker's --gpus parser splits its argument on commas into
# separate device-request fields; an unquoted `device=0,1` gets parsed as
# TWO fields (`device=0` -> DeviceIDs, bare `1` -> Count), which Docker
# rejects with "cannot set both Count and DeviceIDs on device request".
# Wrapping the whole value in quotes makes it one atomic field. This broke
# every multi-GPU run the first time this toolkit was used end-to-end --
# see docs/RUNBOOK.md "Troubleshooting: docker --gpus multi-device bug".
docker run -d \
  --name "$CONTAINER_NAME" \
  --gpus "\"device=${GPUS}\"" \
  --ipc=host \
  -p "${PORT}:8000" \
  -v "${HF_HOME}:/root/.cache/huggingface" \
  -v "${MODEL_DIR}:/model:ro" \
  -e "HF_HOME=/root/.cache/huggingface" \
  --restart unless-stopped \
  "$VLLM_IMAGE" \
  --model /model \
  --served-model-name onerec-8b-pro \
  --tensor-parallel-size "$TP" \
  --dtype "$DTYPE_ARG" \
  "${QUANT_ARGS[@]}" \
  --max-model-len "$MAX_MODEL_LEN" \
  --gpu-memory-utilization "$GPU_MEM_UTIL" \
  --enable-chunked-prefill \
  --trust-remote-code \
  --port 8000 \
  $EXTRA_ARGS \
  > /dev/null

docker logs -f "$CONTAINER_NAME" > "$LOG_FILE" 2>&1 &

if wait_for_http "http://localhost:${PORT}/health" 1800 "vLLM (${RUN_NAME})"; then
  log_ok "vLLM server '${RUN_NAME}' ready at http://localhost:${PORT} (log: ${LOG_FILE})"
  exit 0
else
  log_err "vLLM server '${RUN_NAME}' failed to become healthy. Last 60 log lines:"
  tail -n 60 "$LOG_FILE"
  exit 1
fi
