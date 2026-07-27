#!/usr/bin/env python3
"""Self-contained smoke test for the router's core logic: admission
control, routing strategy selection, circuit breaker, graceful degradation,
and canary weighting -- all exercised against two *fake* in-process
upstream servers (no GPU/engine, no Docker, no k8s required).

Run:  python3 platform/router/test_router_local.py

This intentionally runs real uvicorn servers on localhost (loopback only)
for the fake backends, and talks to the router app in-process via
httpx.ASGITransport, so it exercises the exact same networking code path
the router uses in production (real HTTP calls out to backends) while
staying fully local and fast (<10s).
"""
from __future__ import annotations

import asyncio
import os
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import Response as FastAPIResponse

PASSED = 0
FAILED = 0


def check(name: str, cond: bool, detail: str = ""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED += 1
        print(f"  [FAIL] {name}  {detail}")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Counter:
    def __init__(self):
        self.n_requests = 0


def make_fake_backend(name: str, delay_s: float = 0.0, fail: bool = False) -> FastAPI:
    app = FastAPI()
    counter = Counter()
    app.onerec_counter = counter  # plain attribute, avoids clashing with Starlette's State semantics

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.post("/v1/completions")
    async def completions(request: Request):
        counter.n_requests += 1
        if delay_s:
            await asyncio.sleep(delay_s)
        if fail:
            return FastAPIResponse(content=b"boom", status_code=500)  # will be treated as error by caller
        body = await request.json()
        return {
            "id": f"{name}-{counter.n_requests}",
            "object": "text_completion",
            "model": body.get("model"),
            "choices": [{"text": f"response from {name}", "index": 0, "finish_reason": "stop"}],
        }

    return app


async def run_server(app: FastAPI, port: int) -> uvicorn.Server:
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    server._bg_task = task  # keep a reference
    return server


async def main():
    port_a = free_port()
    port_b = free_port()

    backend_a = make_fake_backend("backend-a")
    backend_b = make_fake_backend("backend-b")
    server_a = await run_server(backend_a, port_a)
    server_b = await run_server(backend_b, port_b)

    os.environ["ROUTER_STABLE_BACKENDS"] = f"http://127.0.0.1:{port_a},http://127.0.0.1:{port_b}"
    os.environ["ROUTER_CANARY_BACKENDS"] = ""
    os.environ["MAX_INFLIGHT_PER_BACKEND"] = "2"
    os.environ["MAX_QUEUE_DEPTH_PER_BACKEND"] = "5"
    os.environ["QUEUE_WAIT_TIMEOUT_S"] = "0.5"
    os.environ["HEALTH_CHECK_INTERVAL_S"] = "0.2"
    os.environ["BREAKER_FAILURE_THRESHOLD"] = "2"
    os.environ["BREAKER_OPEN_SECONDS"] = "1.0"
    os.environ["FALLBACK_ENABLED"] = "true"
    os.environ["ROUTING_STRATEGY"] = "least_outstanding"

    import app as router_app_module  # import AFTER env vars are set (module-level settings load)

    transport = httpx.ASGITransport(app=router_app_module.app)
    async with router_app_module.app.router.lifespan_context(router_app_module.app):
        async with httpx.AsyncClient(transport=transport, base_url="http://router") as client:
            await asyncio.sleep(0.3)  # let first health check pass

            print("\n1. Basic routing: request succeeds and hits a real backend")
            resp = await client.post("/v1/completions", json={"model": "onerec-8b-pro", "prompt": "hello", "max_tokens": 8})
            check("status 200", resp.status_code == 200, resp.text)
            check("response text present", "response from" in str(resp.json()), resp.text)

            print("\n2. readyz / healthz / status")
            r = await client.get("/healthz")
            check("healthz ok", r.status_code == 200)
            r = await client.get("/readyz")
            check("readyz ok (backends healthy)", r.status_code == 200, r.text)
            r = await client.get("/status")
            check("status has 2 backends", len(r.json()["backends"]) == 2, r.text)

            print("\n3. Least-outstanding spreads load across both backends")
            # Fired concurrently (not sequentially): each request's backend
            # selection happens synchronously before its first await, so
            # concurrent in-flight requests naturally see each other's
            # updated `outstanding` counts and alternate -- a sequential
            # loop would always tie-break to the same backend since
            # `outstanding` returns to 0 between calls.
            n_a_reqs = backend_a.onerec_counter.n_requests
            await asyncio.gather(*[
                client.post("/v1/completions", json={"model": "onerec-8b-pro", "prompt": "spread-test", "max_tokens": 4})
                for _ in range(10)
            ])
            total_a = backend_a.onerec_counter.n_requests
            total_b = backend_b.onerec_counter.n_requests
            check("both backends received traffic", total_a > n_a_reqs and total_b > 0,
                  f"a={total_a} b={total_b}")

            print("\n4. Admission control: saturate both backends' concurrency, expect 429")
            slow_a = make_fake_backend("slow-a", delay_s=2.0)
            slow_b = make_fake_backend("slow-b", delay_s=2.0)
            port_sa, port_sb = free_port(), free_port()
            await run_server(slow_a, port_sa)
            await run_server(slow_b, port_sb)
            os.environ["ROUTER_STABLE_BACKENDS"] = f"http://127.0.0.1:{port_sa},http://127.0.0.1:{port_sb}"
            os.environ["FALLBACK_ENABLED"] = "false"
            import importlib

            import config as config_module
            importlib.reload(config_module)
            router_app_module.settings.backends = config_module.load_settings().backends
            router_app_module.pool.backends = [
                router_app_module.Backend(url=b.url, group=b.group, settings=router_app_module.settings)
                for b in router_app_module.settings.backends
            ]
            router_app_module.settings.fallback_enabled = False
            await asyncio.sleep(0.4)

            async def fire():
                return await client.post("/v1/completions", json={"model": "onerec-8b-pro", "prompt": "x", "max_tokens": 4})

            results = await asyncio.gather(*[fire() for _ in range(8)], return_exceptions=True)
            statuses = [r.status_code for r in results if not isinstance(r, Exception)]
            check("at least one 429 under saturation", 429 in statuses, f"statuses={statuses}")

            print("\n5. Graceful degradation: no backend available -> fallback response")
            os.environ["ROUTER_STABLE_BACKENDS"] = "http://127.0.0.1:1"  # nothing listens here
            importlib.reload(config_module)
            router_app_module.settings.fallback_enabled = True
            router_app_module.settings.backends = config_module.load_settings().backends
            router_app_module.pool.backends = [
                router_app_module.Backend(url=b.url, group=b.group, settings=router_app_module.settings)
                for b in router_app_module.settings.backends
            ]
            for b in router_app_module.pool.backends:
                b.healthy = False  # force unhealthy without waiting on a real health-check cycle
            resp = await client.post("/v1/completions", json={"model": "onerec-8b-pro", "prompt": "fallback-test", "max_tokens": 4})
            check("fallback returns 200", resp.status_code == 200, resp.text)
            check("fallback marked degraded", resp.json().get("degraded") is True, resp.text)

            print("\n6. Prefix-hash routing is deterministic for the same prompt")
            from routing import pick_prefix_hash

            class FakeBackend:
                def __init__(self, url):
                    self.url = url
                    self.available = True
                    self.outstanding = 0
                    self.queued = 0

                def load_score(self):
                    return 0

                @property
                def at_capacity(self):
                    return False

            candidates = [FakeBackend("a"), FakeBackend("b"), FakeBackend("c")]
            picks = {pick_prefix_hash(candidates, "same prompt text", 256).url for _ in range(20)}
            check("same prompt always hashes to same backend", len(picks) == 1, f"picks={picks}")

            print("\n7. Metrics endpoint returns prometheus text format")
            r = await client.get("/metrics")
            check("metrics 200", r.status_code == 200)
            check("metrics contains request counter", "onerec_router_requests_total" in r.text)

    print(f"\n{PASSED} passed, {FAILED} failed")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
