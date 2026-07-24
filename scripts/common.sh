#!/usr/bin/env bash
# Shared helper functions for all scripts. Source AFTER env.sh:
#   source "$(dirname "${BASH_SOURCE[0]}")/env.sh"
#   source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
set -uo pipefail

COLOR_RED='\033[0;31m'
COLOR_GREEN='\033[0;32m'
COLOR_YELLOW='\033[0;33m'
COLOR_BLUE='\033[0;34m'
COLOR_RESET='\033[0m'

log_info()  { echo -e "${COLOR_BLUE}[INFO]${COLOR_RESET}  $*"; }
log_ok()    { echo -e "${COLOR_GREEN}[ OK ]${COLOR_RESET}  $*"; }
log_warn()  { echo -e "${COLOR_YELLOW}[WARN]${COLOR_RESET}  $*"; }
log_err()   { echo -e "${COLOR_RED}[FAIL]${COLOR_RESET}  $*" >&2; }

die() { log_err "$*"; exit 1; }

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "required command '$1' not found on PATH"
}

# Retry a command up to N times with backoff. Useful for flaky network pulls.
retry() {
  local -r max_attempts="$1"; shift
  local attempt=1
  until "$@"; do
    if (( attempt >= max_attempts )); then
      log_err "command failed after ${max_attempts} attempts: $*"
      return 1
    fi
    log_warn "attempt ${attempt}/${max_attempts} failed, retrying in $((attempt * 5))s: $*"
    sleep $((attempt * 5))
    ((attempt++))
  done
}

# Poll an HTTP health endpoint until it responds or times out.
# usage: wait_for_http <url> <timeout_seconds> [label]
wait_for_http() {
  local url="$1" timeout="${2:-1800}" label="${3:-service}"
  local start elapsed
  start=$(date +%s)
  log_info "waiting for ${label} to become healthy at ${url} (timeout ${timeout}s)..."
  while true; do
    if curl -sf -o /dev/null --max-time 5 "$url"; then
      log_ok "${label} is healthy (${url})"
      return 0
    fi
    elapsed=$(( $(date +%s) - start ))
    if (( elapsed > timeout )); then
      log_err "${label} did not become healthy within ${timeout}s"
      return 1
    fi
    sleep 5
  done
}

container_running() {
  docker ps --format '{{.Names}}' | grep -qx "$1"
}

container_exists() {
  docker ps -a --format '{{.Names}}' | grep -qx "$1"
}

stop_container() {
  local name="$1"
  if container_exists "$name"; then
    log_info "stopping + removing existing container '${name}'"
    docker rm -f "$name" >/dev/null 2>&1 || true
  fi
}

# Convert "0,1,2,3" -> count 4 (used to size --tensor-parallel-size sanity checks)
gpu_count_from_list() {
  echo "$1" | tr ',' '\n' | sed '/^$/d' | wc -l | tr -d ' '
}

timestamp() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
run_id_now() { date +"%Y%m%d-%H%M%S"; }
