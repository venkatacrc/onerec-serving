#!/usr/bin/env bash
# Sends one real completion request to a running server and prints the
# response. Run this after any 1x_serve_*.sh script, before trusting
# benchmark numbers -- it catches broken chat templates / wrong dtype /
# garbage output early.
#
# Usage: ./scripts/20_smoke_test.sh --port 8000
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

PORT=8000
while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) PORT="$2"; shift 2;;
    *) die "unknown arg: $1";;
  esac
done

log_info "Sending smoke-test completion request to http://localhost:${PORT}/v1/completions"
RESP=$(curl -sS -X POST "http://localhost:${PORT}/v1/completions" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "onerec-8b-pro",
    "prompt": "User watched: [Action Movie A, Sci-Fi Series B, Documentary C]. Recommend 3 similar titles and briefly explain why:",
    "max_tokens": 128,
    "temperature": 0.7
  }')

echo "$RESP" | python3 -m json.tool 2>/dev/null || echo "$RESP"

if echo "$RESP" | python3 -c "import json,sys; d=json.load(sys.stdin); assert d['choices'][0]['text'].strip()" 2>/dev/null; then
  log_ok "Smoke test passed: server returned a non-empty completion."
  exit 0
else
  log_err "Smoke test FAILED: no valid completion in response (see above). Do not trust benchmark numbers from this server yet."
  exit 1
fi
