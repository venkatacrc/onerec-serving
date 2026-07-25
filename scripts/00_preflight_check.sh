#!/usr/bin/env bash
# Verifies the box is what we expect before touching anything.
# Safe to re-run any time; makes no changes.
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

FAILS=0
check() { # check <description> <command...>
  local desc="$1"; shift
  if "$@" >/dev/null 2>&1; then
    log_ok "$desc"
  else
    log_err "$desc"
    FAILS=$((FAILS + 1))
  fi
}

echo "=================================================================="
echo " OneRec-8B-Pro serving benchmark toolkit -- preflight check"
echo "=================================================================="

log_info "Kernel: $(uname -r)"
log_info "OS: $(. /etc/os-release 2>/dev/null; echo "${PRETTY_NAME:-unknown}")"

# --- GPUs --------------------------------------------------------------------
if command -v nvidia-smi >/dev/null 2>&1; then
  N_GPU=$(nvidia-smi --query-gpu=name --format=csv,noheader | wc -l | tr -d ' ')
  log_info "GPUs detected: ${N_GPU}"
  nvidia-smi --query-gpu=index,name,memory.total,driver_version,pstate --format=csv
  if [[ "$N_GPU" -lt "$NUM_GPUS_TOTAL" ]]; then
    log_warn "Expected ${NUM_GPUS_TOTAL} GPUs but found ${N_GPU}. Update NUM_GPUS_TOTAL in scripts/env.sh or configs/matrix.yaml if this is intentional."
  else
    log_ok "GPU count matches expectation (${NUM_GPUS_TOTAL})"
  fi
  DRIVER_VER=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1)
  log_info "Driver version: ${DRIVER_VER} (need >= 550.x for Blackwell/B200; ideally 570+/CUDA12.8+)"
else
  log_err "nvidia-smi not found. Cannot proceed without NVIDIA driver installed."
  FAILS=$((FAILS + 1))
fi

# --- CPU / RAM / Disk ---------------------------------------------------------
log_info "CPU cores: $(nproc)"
log_info "RAM: $(free -h | awk '/^Mem:/ {print $2 " total, " $7 " available"}')"

mkdir -p "$DATA_DIR"
DISK_AVAIL_GB=$(df -BG --output=avail "$DATA_DIR" | tail -1 | tr -dc '0-9')
log_info "Disk available at ${DATA_DIR}: ${DISK_AVAIL_GB}GB"
if [[ "${DISK_AVAIL_GB:-0}" -lt 300 ]]; then
  log_warn "Less than 300GB free at ${DATA_DIR}. Model weights (~16GB), 3 container images (~60GB combined), TensorRT-LLM engine caches, and benchmark logs can add up. Consider pointing ONEREC_DATA_DIR at a larger/faster disk."
else
  log_ok "Sufficient disk space at ${DATA_DIR}"
fi

# --- Base tools ---------------------------------------------------------------
for c in curl wget git jq python3; do
  check "command available: $c" command -v "$c"
done

# --- Docker + NVIDIA container toolkit ---------------------------------------
if command -v docker >/dev/null 2>&1; then
  log_ok "docker installed: $(docker --version)"
  if docker info >/dev/null 2>&1; then
    log_ok "docker daemon reachable (current user can run docker, or script is run with sudo)"
  else
    log_warn "docker daemon not reachable by current user. scripts/01_install_base_deps.sh will add you to the 'docker' group (requires re-login) or you can prefix commands with sudo."
  fi
  if docker run --rm --gpus all nvidia/cuda:13.0.0-base-ubuntu22.04 nvidia-smi >/dev/null 2>&1; then
    log_ok "GPU passthrough into containers works (docker run --gpus all)"
  else
    log_warn "Could not verify GPU passthrough into containers yet (nvidia-container-toolkit likely not installed). scripts/01_install_base_deps.sh will fix this."
  fi

  # Multi-GPU device selection uses --gpus "device=0,1,..." (see
  # scripts/10_serve_vllm.sh for why the embedded quotes matter). Verify
  # this specific form works -- a known Docker CLI parsing bug rejects it
  # with "cannot set both Count and DeviceIDs on device request" on some
  # Docker/toolkit version combinations, which otherwise only surfaces 30
  # minutes into a matrix run when a TP>1 config's health check times out.
  if [[ "${N_GPU:-0}" -ge 2 ]]; then
    MGPU_LOG=$(mktemp)
    if docker run --rm --gpus "\"device=0,1\"" nvidia/cuda:13.0.0-base-ubuntu22.04 nvidia-smi -L >"$MGPU_LOG" 2>&1; then
      log_ok "Multi-GPU device selection works (docker run --gpus \"device=0,1\")"
    else
      log_err "Multi-GPU device selection FAILED -- any TP>1 configuration in configs/matrix.yaml will hang for 30 min and then fail. Error:"
      tail -n 5 "$MGPU_LOG" | sed 's/^/    /'
      log_err "This is the exact bug documented in docs/RUNBOOK.md -> 'Troubleshooting: docker --gpus multi-device bug'. If you still hit it after that fix, try upgrading nvidia-container-toolkit / Docker Engine."
      FAILS=$((FAILS + 1))
    fi
    rm -f "$MGPU_LOG"
  fi
else
  log_warn "docker not installed yet -- scripts/01_install_base_deps.sh will install it."
fi

echo "=================================================================="
if [[ "$FAILS" -eq 0 ]]; then
  log_ok "Preflight check passed. Next: ./scripts/01_install_base_deps.sh"
else
  log_err "Preflight check found ${FAILS} hard failure(s). Resolve before continuing."
fi
echo "=================================================================="
exit "$FAILS"
