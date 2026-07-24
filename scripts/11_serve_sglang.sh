#!/usr/bin/env bash
# Launches OneRec-8B-Pro behind SGLang's OpenAI-compatible server, in Docker.
#
# Usage:
#   ./scripts/11_serve_sglang.sh --run-name sglang-tp1-bf16 --tp 1 --gpus 0 \
#       --dtype bfloat16 --max-model-len 8192 [--port 8001] [--extra-args "..."]
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

RUN_NAME="sglang-default"
TP=1
GPUS="0"
DTYPE="bfloat16"
MAX_MODEL_LEN=8192
PORT="$SGLANG_PORT"
EXTRA_ARGS=""
MEM_FRACTION_STATIC=0.85

while [[ $# -gt 0 ]]; do
  case "$1" in
    --run-name) RUN_NAME="$2"; shift 2;;
    --tp) TP="$2"; shift 2;;
    --gpus) GPUS="$2"; shift 2;;
    --dtype) DTYPE="$2"; shift 2;;
    --max-model-len) MAX_MODEL_LEN="$2"; shift 2;;
    --port) PORT="$2"; shift 2;;
    --mem-fraction-static) MEM_FRACTION_STATIC="$2"; shift 2;;
    --extra-args) EXTRA_ARGS="$2"; shift 2;;
    *) die "unknown arg: $1";;
  esac
done

NGPU_LIST=$(gpu_count_from_list "$GPUS")
if [[ "$NGPU_LIST" -ne "$TP" ]]; then
  die "--gpus lists ${NGPU_LIST} device(s) but --tp is ${TP}; they must match."
fi

CONTAINER_NAME="${CONTAINER_NAME_PREFIX}-sglang-${RUN_NAME}"
stop_container "$CONTAINER_NAME"

QUANT_ARGS=()
if [[ "$DTYPE" == "fp8" ]]; then
  QUANT_ARGS=(--quantization fp8)
  DTYPE_ARG="bfloat16"
else
  DTYPE_ARG="$DTYPE"
fi

mkdir -p "$LOG_DIR"
LOG_FILE="${LOG_DIR}/${CONTAINER_NAME}.log"

log_info "Starting SGLang server '${CONTAINER_NAME}' on GPU(s) ${GPUS} (TP=${TP}, dtype=${DTYPE}, max-model-len=${MAX_MODEL_LEN}, port=${PORT})"
log_info "Image: ${SGLANG_IMAGE}"

# shellcheck disable=SC2086
docker run -d \
  --name "$CONTAINER_NAME" \
  --gpus "device=${GPUS}" \
  --ipc=host \
  --shm-size 32g \
  -p "${PORT}:30000" \
  -v "${HF_HOME}:/root/.cache/huggingface" \
  -v "${MODEL_DIR}:/model:ro" \
  -e "HF_HOME=/root/.cache/huggingface" \
  --restart unless-stopped \
  "$SGLANG_IMAGE" \
  python3 -m sglang.launch_server \
  --model-path /model \
  --served-model-name onerec-8b-pro \
  --tp "$TP" \
  --dtype "$DTYPE_ARG" \
  "${QUANT_ARGS[@]}" \
  --context-length "$MAX_MODEL_LEN" \
  --mem-fraction-static "$MEM_FRACTION_STATIC" \
  --trust-remote-code \
  --host 0.0.0.0 \
  --port 30000 \
  $EXTRA_ARGS \
  > /dev/null

docker logs -f "$CONTAINER_NAME" > "$LOG_FILE" 2>&1 &

# SGLang exposes /health too.
if wait_for_http "http://localhost:${PORT}/health" 1800 "SGLang (${RUN_NAME})"; then
  log_ok "SGLang server '${RUN_NAME}' ready at http://localhost:${PORT} (log: ${LOG_FILE})"
  exit 0
else
  log_err "SGLang server '${RUN_NAME}' failed to become healthy. Last 60 log lines:"
  tail -n 60 "$LOG_FILE"
  exit 1
fi
