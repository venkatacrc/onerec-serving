# OneRec Client SDK (reference)

A small, dependency-light (`httpx` only) reference client any calling
service should copy/vendor instead of hitting the router with a bare HTTP
client. See `docs/PRODUCTION_ARCHITECTURE.md` §6.

- `onerec_client.py` — `OneRecClient`: retry with full-jitter exponential
  backoff (honors `Retry-After` on 429), a client-side circuit breaker, and
  a local heuristic fallback when the breaker is open or retries exhaust.
- `example_usage.py` — minimal example against a real router.
- `test_client_local.py` — smoke test against fake in-process routers (no
  real router/GPU needed): `python3 test_client_local.py`.

This breaker is deliberately a separate implementation from the router's
own outbound breaker (`platform/router/backend_pool.py`): this one protects
the router from a thundering herd of retrying callers; the router's
protects engine replicas from the router itself. Two independent layers of
the same pattern, each scoped to what it can see.
