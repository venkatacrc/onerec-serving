#!/usr/bin/env bash
# One-shot entry point: preflight -> install deps -> download model -> run
# the full benchmark matrix -> generate the architect-facing report.
#
# Designed to be run once on a clean box with minimal supervision:
#   ./run_all.sh
#
# Safe to re-run: every step is idempotent, and run_matrix.py continues past
# a failed configuration instead of aborting the whole run. Expect this to
# take 2-4+ hours end to end on a fresh box (image pulls + model download +
# 8 benchmark configurations). Tail logs/ and results/ from another shell
# to check progress at any time.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
source scripts/env.sh
source scripts/common.sh

STEP_LOG="${LOG_DIR}/run_all.log"
mkdir -p "$LOG_DIR"
echo "$(timestamp) run_all.sh started" | tee -a "$STEP_LOG"

log_info "Step 1/5: preflight check"
./scripts/00_preflight_check.sh || die "Preflight failed. Fix issues above and re-run."

log_info "Step 2/5: installing base dependencies (docker, nvidia-container-toolkit, python venv)"
./scripts/01_install_base_deps.sh || die "Base dependency install failed."

log_info "Step 3/5: downloading model"
./scripts/02_download_model.sh || die "Model download failed."

log_info "Step 4/5: pulling engine container images"
for img in "$VLLM_IMAGE" "$SGLANG_IMAGE" "$TRTLLM_IMAGE"; do
  log_info "docker pull ${img}"
  retry 3 docker pull "$img" || log_warn "Failed to pre-pull ${img}; the serve script will retry the pull on first use."
done

log_info "Step 5/5: running full benchmark matrix (configs/matrix.yaml)"
# shellcheck disable=SC1091
source "${VENV_DIR}/bin/activate"
python3 bench/run_matrix.py --matrix configs/matrix.yaml --results-dir "$RESULTS_DIR" --log-dir "$LOG_DIR" --model-dir "$MODEL_DIR"
deactivate

echo "=================================================================="
log_ok "All done."
log_info "Report:   ${RESULTS_DIR}/report/REPORT.md"
log_info "Charts:   ${RESULTS_DIR}/report/*.png"
log_info "Raw data: ${RESULTS_DIR}/<run-name>/bench_*.json"
log_info "Run summary: ${RESULTS_DIR}/run_summary.json"
echo "=================================================================="
