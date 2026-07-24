#!/usr/bin/env bash
# Central configuration for the OneRec-8B-Pro serving benchmark toolkit.
# Source this file from every script: `source "$(dirname "${BASH_SOURCE[0]}")/env.sh"`
# Every value can be overridden by exporting it before running a script, e.g.:
#   ONEREC_DATA_DIR=/mnt/nvme/onerec-data ./scripts/03_download_model.sh

# Resolve repo root regardless of caller's cwd.
export REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# --- Model ------------------------------------------------------------------
export MODEL_ID="${ONEREC_MODEL_ID:-OpenOneRec/OneRec-8B-pro}"
export MODEL_REVISION="${ONEREC_MODEL_REVISION:-main}"

# --- Filesystem layout -------------------------------------------------------
# Kept outside the git repo by default (large artifacts: weights, HF cache,
# docker build cache, TensorRT engines, benchmark logs). Override if you want
# these on a specific fast NVMe mount, e.g. ONEREC_DATA_DIR=/mnt/nvme/onerec.
export DATA_DIR="${ONEREC_DATA_DIR:-$HOME/onerec-data}"
export HF_HOME="${HF_HOME:-$DATA_DIR/hf-cache}"
export MODEL_DIR="${MODEL_DIR:-$DATA_DIR/models/OneRec-8B-pro}"
export TRTLLM_ENGINE_DIR="${TRTLLM_ENGINE_DIR:-$DATA_DIR/trtllm-engines/OneRec-8B-pro}"
export RESULTS_DIR="${RESULTS_DIR:-$REPO_ROOT/results}"
export LOG_DIR="${LOG_DIR:-$REPO_ROOT/logs}"
export VENV_DIR="${VENV_DIR:-$DATA_DIR/venv}"

mkdir -p "$DATA_DIR" "$HF_HOME" "$RESULTS_DIR" "$LOG_DIR"

# --- Docker images (pin versions so results are reproducible) --------------
# Snapshot taken 2026-07-24. These tags are known-good for Blackwell
# (B200/GB200, sm_100) at the time this toolkit was written. Fast-moving
# inference engines ship new releases every 1-2 weeks -- before a real
# benchmark run, check for newer tags and update here (see docs/RUNBOOK.md
# "Keeping engine versions current"):
#   vLLM:        https://hub.docker.com/r/vllm/vllm-openai/tags
#   SGLang:      https://hub.docker.com/r/lmsysorg/sglang/tags
#   TensorRT-LLM: https://catalog.ngc.nvidia.com/orgs/nvidia/teams/tensorrt-llm/containers/release/tags
export VLLM_IMAGE="${VLLM_IMAGE:-vllm/vllm-openai:v0.12.0}"
export SGLANG_IMAGE="${SGLANG_IMAGE:-lmsysorg/sglang:latest-cu130-runtime}"
export TRTLLM_IMAGE="${TRTLLM_IMAGE:-nvcr.io/nvidia/tensorrt-llm/release:1.3.0rc22}"

# --- Networking --------------------------------------------------------------
export VLLM_PORT="${VLLM_PORT:-8000}"
export SGLANG_PORT="${SGLANG_PORT:-8001}"
export TRTLLM_PORT="${TRTLLM_PORT:-8002}"
export SERVE_HOST="${SERVE_HOST:-0.0.0.0}"

# --- GPU topology -------------------------------------------------------------
export NUM_GPUS_TOTAL="${NUM_GPUS_TOTAL:-8}"

# --- Misc ---------------------------------------------------------------------
export CONTAINER_NAME_PREFIX="${CONTAINER_NAME_PREFIX:-onerec}"
export HF_TOKEN="${HF_TOKEN:-}"   # only needed if you point MODEL_ID at a gated repo
