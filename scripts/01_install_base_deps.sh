#!/usr/bin/env bash
# Clean-slate host setup: OS packages, Docker Engine, NVIDIA Container
# Toolkit, and a local Python venv used only for orchestration (the
# benchmark client + report generator -- NOT for running the model itself,
# which happens inside the per-engine containers).
#
# Idempotent: safe to re-run. Requires sudo for apt/docker install steps.
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

SUDO=""
if [[ "$(id -u)" -ne 0 ]]; then
  need_cmd sudo
  SUDO="sudo"
fi

log_info "Updating apt and installing base utilities..."
$SUDO apt-get update -y
$SUDO apt-get install -y \
  build-essential ca-certificates curl wget gnupg lsb-release \
  git git-lfs jq tmux htop unzip software-properties-common \
  python3 python3-venv python3-pip

# --- Docker Engine ------------------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
  log_info "Installing Docker Engine from the official Docker apt repo..."
  $SUDO install -m 0755 -d /etc/apt/keyrings
  if [[ ! -f /etc/apt/keyrings/docker.gpg ]]; then
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | $SUDO gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  fi
  $SUDO chmod a+r /etc/apt/keyrings/docker.gpg
  ARCH=$(dpkg --print-architecture)
  CODENAME=$(. /etc/os-release && echo "$VERSION_CODENAME")
  echo "deb [arch=${ARCH} signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu ${CODENAME} stable" \
    | $SUDO tee /etc/apt/sources.list.d/docker.list >/dev/null
  $SUDO apt-get update -y
  $SUDO apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  log_ok "Docker Engine installed: $(docker --version)"
else
  log_ok "Docker already installed: $(docker --version)"
fi

# Let the current (non-root) user run docker without sudo. Requires a new
# shell/session to take effect -- we detect and warn rather than silently
# relying on it.
if [[ "$SUDO" == "sudo" ]] && ! id -nG "$USER" | grep -qw docker; then
  log_info "Adding ${USER} to the 'docker' group (takes effect on next login/shell)."
  $SUDO groupadd -f docker
  $SUDO usermod -aG docker "$USER"
  log_warn "You must start a new shell session (or 'newgrp docker') for group changes to apply. Until then, scripts will use 'sudo docker' automatically if needed."
fi

# --- NVIDIA Container Toolkit --------------------------------------------------
if ! dpkg -l | grep -q nvidia-container-toolkit; then
  log_info "Installing NVIDIA Container Toolkit..."
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
    | $SUDO gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
  curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
    | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
    | $SUDO tee /etc/apt/sources.list.d/nvidia-container-toolkit.list >/dev/null
  $SUDO apt-get update -y
  $SUDO apt-get install -y nvidia-container-toolkit
  $SUDO nvidia-ctk runtime configure --runtime=docker
  $SUDO systemctl restart docker
  log_ok "NVIDIA Container Toolkit installed and Docker runtime configured."
else
  log_ok "NVIDIA Container Toolkit already installed."
fi

# Verify GPU passthrough
DOCKER_CMD="docker"
if ! docker info >/dev/null 2>&1; then DOCKER_CMD="sudo docker"; fi
log_info "Verifying GPU passthrough into containers (this pulls a small test image)..."
if retry 3 $DOCKER_CMD run --rm --gpus all nvidia/cuda:13.0.0-base-ubuntu22.04 nvidia-smi >/tmp/onerec_gpu_check.log 2>&1; then
  log_ok "GPU passthrough verified. Sample output:"
  tail -n 15 /tmp/onerec_gpu_check.log
else
  die "GPU passthrough test failed. See /tmp/onerec_gpu_check.log. Common cause: need a new shell session after being added to the 'docker' group, or driver/toolkit mismatch."
fi

# --- Local orchestration Python venv ------------------------------------------
# This venv is ONLY for the benchmark client + report generator scripts
# (aiohttp, pandas, matplotlib, huggingface_hub). It never touches CUDA.
log_info "Creating orchestration venv at ${VENV_DIR}..."
python3 -m venv "$VENV_DIR"
# shellcheck disable=SC1091
source "${VENV_DIR}/bin/activate"
pip install --upgrade pip -q
pip install -q -r "${REPO_ROOT}/bench/requirements.txt"
deactivate
log_ok "Orchestration venv ready at ${VENV_DIR}"

echo "=================================================================="
log_ok "Base dependency install complete."
log_info "If this was the first time your user was added to the 'docker' group, run: exec su -l \$USER   (or log out/in) before continuing."
log_info "Next: ./scripts/02_download_model.sh"
echo "=================================================================="
