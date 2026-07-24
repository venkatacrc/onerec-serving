#!/usr/bin/env bash
# Stops and removes every container this toolkit may have started.
# Usage: ./scripts/90_stop_serving.sh           (stop everything)
#        ./scripts/90_stop_serving.sh vllm       (stop only vLLM containers)
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

FILTER="${1:-}"
PATTERN="${CONTAINER_NAME_PREFIX}-${FILTER}"

NAMES=$(docker ps -a --format '{{.Names}}' | grep -E "^${PATTERN}" || true)

if [[ -z "$NAMES" ]]; then
  log_info "No matching containers found for pattern '${PATTERN}*'."
  exit 0
fi

echo "$NAMES" | while read -r name; do
  log_info "Stopping ${name}..."
  docker rm -f "$name" >/dev/null 2>&1 && log_ok "removed ${name}"
done

# Also reap any background `docker logs -f` tailers we spawned.
pkill -f "docker logs -f ${CONTAINER_NAME_PREFIX}-" 2>/dev/null || true

log_ok "Done."
