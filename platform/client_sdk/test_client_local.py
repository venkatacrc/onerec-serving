#!/usr/bin/env python3
"""Local smoke test for OneRecClient's retry/backoff + circuit breaker
logic, against fake in-process HTTP servers (no real router/GPU needed).

Run:  python3 platform/client_sdk/test_client_local.py
"""
from __future__ import annotations

import asyncio
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from onerec_client import CircuitBreaker, OneRecClient, RetryPolicy

PASSED, FAILED = 0, 0


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


async def run_server(app: FastAPI, port: int):
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    return server, task


def make_flaky_router(fail_n_times: int, then_429: bool = False):
    app = FastAPI()
    state = {"calls": 0}

    @app.post("/v1/completions")
    async def completions(request: Request):
        state["calls"] += 1
        if state["calls"] <= fail_n_times:
            if then_429:
                return JSONResponse(status_code=429, headers={"Retry-After": "0.05"}, content={"error": "saturated"})
            return Response(content=b"boom", status_code=500)
        return {"choices": [{"text": "real response"}], "degraded": False}

    return app, state


def make_always_500_router():
    app = FastAPI()

    @app.post("/v1/completions")
    async def completions(request: Request):
        return Response(content=b"boom", status_code=500)

    return app


async def main():
    print("1. Retries succeed after transient 500s")
    app, state = make_flaky_router(fail_n_times=2)
    port = free_port()
    server, task = await run_server(app, port)
    client = OneRecClient(base_url=f"http://127.0.0.1:{port}", retry_policy=RetryPolicy(max_attempts=5, base_delay_s=0.01, max_delay_s=0.05))
    result = await client.complete("hi", max_tokens=4)
    check("eventually succeeds", not result.degraded and result.text == "real response", str(result))
    await client.aclose()
    server.should_exit = True
    await task

    print("\n2. 429 with Retry-After is retried without tripping the breaker")
    app, state = make_flaky_router(fail_n_times=2, then_429=True)
    port = free_port()
    server, task = await run_server(app, port)
    client = OneRecClient(base_url=f"http://127.0.0.1:{port}", retry_policy=RetryPolicy(max_attempts=5, base_delay_s=0.01, max_delay_s=0.1))
    t0 = time.monotonic()
    result = await client.complete("hi", max_tokens=4)
    check("eventually succeeds after 429s", not result.degraded, str(result))
    check("breaker still closed after only 429s", client.breaker.state.value == "closed", client.breaker.state.value)
    await client.aclose()
    server.should_exit = True
    await task

    print("\n3. Breaker opens after repeated hard failures, then serves local fallback")
    app = make_always_500_router()
    port = free_port()
    server, task = await run_server(app, port)
    client = OneRecClient(
        base_url=f"http://127.0.0.1:{port}",
        retry_policy=RetryPolicy(max_attempts=1, base_delay_s=0.001, max_delay_s=0.005),
        breaker=CircuitBreaker(failure_threshold=3, open_seconds=1.0),
    )
    for _ in range(3):
        r = await client.complete("hi", max_tokens=4)
        check("degraded during failures", r.degraded, str(r))
    check("breaker now open", client.breaker.state.value == "open", client.breaker.state.value)

    t0 = time.monotonic()
    result = await client.complete("hi", max_tokens=4)
    elapsed = time.monotonic() - t0
    check("open-circuit short-circuits instantly (no network call)", elapsed < 0.05, f"elapsed={elapsed:.3f}s")
    check("open-circuit response is degraded/local fallback", result.degraded and result.degraded_reason == "circuit_open", str(result))

    await client.aclose()
    server.should_exit = True
    await task

    print(f"\n{PASSED} passed, {FAILED} failed")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
