#!/usr/bin/env bash
# Launches OneRec-8B-Pro behind TensorRT-LLM's OpenAI-compatible server
# (trtllm-serve), in Docker. Uses TensorRT-LLM's PyTorch backend, which can
# serve a HF checkpoint directly (no separate `trtllm-build` engine-building
# step needed for a plain dense Qwen3 architecture like this one).
#
# Usage:
#   ./scripts/12_serve_trtllm.sh --run-name trtllm-tp1-bf16 --tp 1 --gpus 0 \
#       --dtype bfloat16 --max-model-len 8192 [--port 8002] [--extra-args "..."]
#
# NOTE: this is the least "plug and play" of the three engines. If
# trtllm-serve rejects the flags below (CLI surface changes between
# TensorRT-LLM releases), see docs/RUNBOOK.md -> "TensorRT-LLM troubleshooting"
# for the manual `trtllm-build` fallback path.
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

RUN_NAME="trtllm-default"
TP=1
GPUS="0"
DTYPE="bfloat16"
MAX_MODEL_LEN=8192
PORT="$TRTLLM_PORT"
EXTRA_ARGS=""
KV_FRACTION=0.85

while [[ $# -gt 0 ]]; do
  case "$1" in
    --run-name) RUN_NAME="$2"; shift 2;;
    --tp) TP="$2"; shift 2;;
    --gpus) GPUS="$2"; shift 2;;
    --dtype) DTYPE="$2"; shift 2;;
    --max-model-len) MAX_MODEL_LEN="$2"; shift 2;;
    --port) PORT="$2"; shift 2;;
    --kv-fraction) KV_FRACTION="$2"; shift 2;;
    --extra-args) EXTRA_ARGS="$2"; shift 2;;
    *) die "unknown arg: $1";;
  esac
done

NGPU_LIST=$(gpu_count_from_list "$GPUS")
if [[ "$NGPU_LIST" -ne "$TP" ]]; then
  die "--gpus lists ${NGPU_LIST} device(s) but --tp is ${TP}; they must match."
fi

CONTAINER_NAME="${CONTAINER_NAME_PREFIX}-trtllm-${RUN_NAME}"
stop_container "$CONTAINER_NAME"

mkdir -p "$LOG_DIR" "$TRTLLM_ENGINE_DIR"
LOG_FILE="${LOG_DIR}/${CONTAINER_NAME}.log"

log_info "Starting TensorRT-LLM server '${CONTAINER_NAME}' on GPU(s) ${GPUS} (TP=${TP}, dtype=${DTYPE}, max-model-len=${MAX_MODEL_LEN}, port=${PORT})"
log_info "Image: ${TRTLLM_IMAGE}"
log_warn "First launch compiles/warms up CUDA graphs and can take several minutes longer than vLLM/SGLang -- this is expected, not a hang."

DTYPE_ARG="$DTYPE"
if [[ "$DTYPE" == "fp8" ]]; then
  DTYPE_ARG="bfloat16"   # TensorRT-LLM derives fp8 from --quantization, not --dtype
fi

QUANT_ARGS=()
if [[ "$DTYPE" == "fp8" ]]; then
  QUANT_ARGS=(--quantization FP8)
fi

# shellcheck disable=SC2086
docker run -d \
  --name "$CONTAINER_NAME" \
  --gpus "device=${GPUS}" \
  --ipc=host \
  --ulimit memlock=-1 \
  --ulimit stack=67108864 \
  -p "${PORT}:8000" \
  -v "${HF_HOME}:/root/.cache/huggingface" \
  -v "${MODEL_DIR}:/model:ro" \
  -v "${TRTLLM_ENGINE_DIR}:/engine_cache" \
  -e "HF_HOME=/root/.cache/huggingface" \
  --restart unless-stopped \
  "$TRTLLM_IMAGE" \
  trtllm-serve /model \
  --host 0.0.0.0 \
  --port 8000 \
  --tp_size "$TP" \
  --max_batch_size 256 \
  --max_num_tokens 16384 \
  --max_seq_len "$MAX_MODEL_LEN" \
  --kv_cache_free_gpu_memory_fraction "$KV_FRACTION" \
  "${QUANT_ARGS[@]}" \
  $EXTRA_ARGS \
  > /dev/null

docker logs -f "$CONTAINER_NAME" > "$LOG_FILE" 2>&1 &

# trtllm-serve exposes /health as well as an OpenAI-compatible /v1/* surface.
if wait_for_http "http://localhost:${PORT}/health" 2400 "TensorRT-LLM (${RUN_NAME})"; then
  log_ok "TensorRT-LLM server '${RUN_NAME}' ready at http://localhost:${PORT} (log: ${LOG_FILE})"
  exit 0
else
  log_err "TensorRT-LLM server '${RUN_NAME}' failed to become healthy. Last 100 log lines:"
  tail -n 100 "$LOG_FILE"
  log_err "See docs/RUNBOOK.md -> 'TensorRT-LLM troubleshooting' for the manual trtllm-build fallback."
  exit 1
fi
