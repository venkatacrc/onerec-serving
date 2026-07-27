# OneRec Router

Stateless HTTP reverse proxy in front of the engine replica pool (vLLM /
SGLang / TensorRT-LLM). See `docs/PRODUCTION_ARCHITECTURE.md` §3/§5/§6 for
why this exists and why it's a custom service instead of Envoy/nginx.

## Run locally (no k8s/GPU needed)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export ROUTER_STABLE_BACKENDS="http://localhost:8000"   # your vLLM/SGLang/trtllm server
uvicorn app:app --host 0.0.0.0 --port 9000
```

Then point `bench/benchmark_client.py --base-url http://localhost:9000` at
it instead of the engine directly, to benchmark *through* the router
(recommended for any capacity-planning number that should reflect
production reality, since it includes admission control/queueing).

## Self-test (no engine required at all)

```bash
python3 test_router_local.py
```

Spins up two fake in-process upstream servers and exercises routing,
admission control/backpressure, the circuit breaker, graceful degradation,
and canary weighting end to end. Runs in a few seconds, zero external
dependencies beyond `requirements.txt`.

## Configuration

All tunables are environment variables (see `config.py` for the full list
and defaults) plus an optional backend-topology YAML file
(`config.example.yaml`) pointed at by `ROUTER_BACKENDS_FILE`. Highlights:

| Env var | Purpose |
|---|---|
| `ROUTER_BACKENDS_FILE` | path to a `groups: {stable: [...], canary: [...]}` YAML file (production) |
| `ROUTER_STABLE_BACKENDS` / `ROUTER_CANARY_BACKENDS` | comma-separated URL lists (quick local testing, skips the YAML file) |
| `ROUTING_STRATEGY` | `least_outstanding` (default) \| `round_robin` \| `prefix_hash` |
| `MAX_INFLIGHT_PER_BACKEND`, `MAX_QUEUE_DEPTH_PER_BACKEND`, `QUEUE_WAIT_TIMEOUT_S` | admission control / backpressure |
| `BREAKER_FAILURE_THRESHOLD`, `BREAKER_OPEN_SECONDS` | outbound circuit breaker per backend |
| `CANARY_WEIGHT_PCT` | % of traffic to the canary group (also settable live via `POST /admin/canary_weight`) |
| `FALLBACK_ENABLED`, `FALLBACK_CACHE_SIZE` | graceful degradation |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | tracing export target; unset = tracing disabled |
| `LOG_PROMPT_PREVIEW`, `PROMPT_PREVIEW_CHARS` | PII-sensitive: off by default, see `logging_util.py` docstring |

## Endpoints

- `POST /v1/completions`, `POST /v1/chat/completions` — proxied to the
  selected backend (streaming supported).
- `GET /healthz` — liveness (process up, does not depend on backends).
- `GET /readyz` — readiness (at least one available backend).
- `GET /status` — human/debug JSON view of the whole backend pool.
- `GET /metrics` — Prometheus exposition format.
- `POST /admin/canary_weight` — `{"canary_weight_pct": 10}` to ramp a canary live.
