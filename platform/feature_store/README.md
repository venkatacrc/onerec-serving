# OneRec Feature Store (reference implementation)

Mock/reference real-time user-history service, standing in for a real
feature store (Feast/Tecton/an internal system) that doesn't exist yet for
this product. See `docs/PRODUCTION_ARCHITECTURE.md` §3 "Feature store" row
for why this is a real working service rather than a stub, and exactly
what to change to point it at a real system.

## Run locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
FEATURE_STORE_BACKEND=memory uvicorn app:app --port 9100
```

Seeded with 200 synthetic users with realistic interaction histories on
first request. Try it:

```bash
curl localhost:9100/users?limit=3
curl localhost:9100/user/user-00001/history
curl localhost:9100/user/user-00001/prompt   # ready-to-send OneRec prompt
```

Set `FEATURE_STORE_BACKEND=redis` + `REDIS_URL=redis://...` to run the
Redis-backed variant instead (shared state across replicas — needed once
this runs as more than one pod).

## Self-test

```bash
python3 test_feature_store_local.py
```

## Feeding real-shaped prompts into the benchmark toolkit

`bench/build_prompts_from_feature_store.py` pulls N users' histories from
a running feature-store instance and writes a JSONL prompt pool that
`bench/benchmark_client.py --prompt-file` consumes instead of (or alongside)
the purely synthetic generator in `bench/prompt_dataset.py` — see
`docs/BENCHMARK_METHODOLOGY.md` §3 and `docs/RUNBOOK.md`.

## Swapping in a real feature store

Implement `FeatureStoreClient` (in `adapter.py`) against your real
system's SDK (Feast's `FeatureStore.get_online_features`, Tecton's
`FeatureService`, etc.), then change one line in `app.py`'s
`_build_store()`. The router and benchmark tooling only depend on this
interface, so nothing else changes.
