"""OneRec Router: the single entry point in front of the engine replica
pool (vLLM/SGLang/TensorRT-LLM), implementing:

  - admission control + backpressure (HTTP 429 with Retry-After)
  - least-outstanding / round-robin / prefix-hash-aware routing
  - canary/blue-green traffic weighting between "stable" and "canary" groups
  - outbound circuit breaker per backend
  - graceful degradation (cached/heuristic fallback) when a group has no
    available backend
  - Prometheus metrics, OpenTelemetry tracing, structured PII-safe logging

Run locally:  uvicorn app:app --port 9000
See platform/router/README.md for env vars / config file format, and
test_router_local.py for a self-contained smoke test using fake upstream
servers (no GPU/engine required).
"""
from __future__ import annotations

import random
import time
import uuid
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from backend_pool import Backend, BackendPool
from config import load_settings
from fallback import FallbackCache, build_fallback_payload
from logging_util import get_logger, log_request, redact_prompt
from metrics import (
    BACKEND_QUEUE_DEPTH,
    CANARY_TRAFFIC_RATIO,
    QUEUE_WAIT_SECONDS,
    REQUEST_LATENCY_SECONDS,
    REQUESTS_TOTAL,
    refresh_backend_gauges,
)
from routing import prefix_hash_key, select_backend, select_group
from tracing import current_trace_id, init_tracing, inject_trace_headers, span

settings = load_settings()
pool = BackendPool(settings)
fallback_cache = FallbackCache(max_size=settings.fallback_cache_size)
logger = get_logger()
_http_client: httpx.AsyncClient | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _http_client
    _http_client = httpx.AsyncClient(timeout=settings.upstream_timeout_s)
    init_tracing(settings.service_name, settings.otel_exporter_otlp_endpoint)
    await pool.start_health_checks()
    yield
    await pool.stop_health_checks()
    await _http_client.aclose()


app = FastAPI(title="OneRec Router", lifespan=lifespan)


@app.get("/healthz")
async def healthz():
    """Kubernetes liveness probe target: process is up and serving HTTP.
    Deliberately does NOT depend on backend health -- a backend outage
    should trigger degraded-mode responses, not a router restart loop."""
    return {"status": "ok"}


@app.get("/readyz")
async def readyz():
    """Kubernetes readiness probe target: at least one backend, in at
    least one group, is available. If not, k8s should stop sending this
    router pod traffic (useful during router rollout itself)."""
    any_available = any(b.available for b in pool.backends)
    if any_available or not pool.backends:
        return {"status": "ready", "backends": len(pool.backends)}
    return JSONResponse(status_code=503, content={"status": "not_ready", "backends": len(pool.backends)})


@app.get("/status")
async def status():
    return {
        "routing_strategy": settings.routing_strategy,
        "canary_weight_pct": settings.canary_weight_pct,
        "fallback_cache_size": len(fallback_cache),
        "backends": pool.snapshot(),
    }


@app.get("/metrics")
async def metrics():
    refresh_backend_gauges(pool.snapshot())
    CANARY_TRAFFIC_RATIO.set(settings.canary_weight_pct)
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/admin/canary_weight")
async def set_canary_weight(request: Request):
    """Runtime control for a canary rollout: ramp traffic to the canary
    group up/down without a redeploy. See docs/ROLLOUT_STRATEGY.md."""
    body = await request.json()
    pct = float(body.get("canary_weight_pct", settings.canary_weight_pct))
    settings.canary_weight_pct = max(0.0, min(100.0, pct))
    logger.info("canary_weight_updated", extra={"fields": {"canary_weight_pct": settings.canary_weight_pct}})
    return {"canary_weight_pct": settings.canary_weight_pct}


async def _forward_streaming(backend: Backend, path: str, payload: dict, headers: dict):
    url = f"{backend.url}{path}"
    async with _http_client.stream("POST", url, json=payload, headers=headers) as resp:
        if resp.status_code != 200:
            body = await resp.aread()
            raise httpx.HTTPStatusError(f"HTTP {resp.status_code}: {body[:300]!r}", request=resp.request, response=resp)
        async for chunk in resp.aiter_bytes():
            yield chunk


@app.post("/v1/completions")
async def completions(request: Request):
    return await _handle_completion_like(request, "/v1/completions")


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    return await _handle_completion_like(request, "/v1/chat/completions")


async def _handle_completion_like(request: Request, upstream_path: str):
    t_request_start = time.perf_counter()
    request_id = str(uuid.uuid4())
    payload = await request.json()
    prompt_text = payload.get("prompt") or str(payload.get("messages", ""))
    user_id = request.headers.get(settings.user_id_header)
    is_streaming = bool(payload.get("stream"))

    group = select_group(pool, settings.canary_weight_pct, random.uniform(0, 100))

    with span("router.request", {"onerec.group": group, "onerec.request_id": request_id}):
        backend = select_backend(pool, group, settings.routing_strategy, prompt_text, settings.prefix_hash_chars)

        if backend is None:
            return await _serve_fallback(request_id, group, prompt_text, user_id, t_request_start, "no_available_backend")

        t_queue_start = time.perf_counter()
        acquired = await backend.acquire_slot(settings.queue_wait_timeout_s)
        queue_wait_ms = (time.perf_counter() - t_queue_start) * 1000
        QUEUE_WAIT_SECONDS.labels(group=group).observe(queue_wait_ms / 1000)

        if not acquired:
            REQUESTS_TOTAL.labels(group=group, status="rejected_admission").inc()
            log_request(
                logger, request_id=request_id, trace_id=current_trace_id(), user_id=user_id, group=group,
                backend=backend.id, strategy=settings.routing_strategy, status="rejected_admission",
                http_status=429, latency_ms=(time.perf_counter() - t_request_start) * 1000,
                queue_wait_ms=queue_wait_ms, degraded=False, prompt_len=len(prompt_text),
                prompt_preview=redact_prompt(prompt_text, settings.log_prompt_preview, settings.prompt_preview_chars),
                error="admission_control_queue_timeout",
            )
            return JSONResponse(
                status_code=429,
                content={"error": "backend saturated, try again shortly", "degraded": False},
                headers={"Retry-After": "2"},
            )

        backend.outstanding += 1
        backend.total_requests += 1
        headers = inject_trace_headers({"content-type": "application/json"})
        if user_id:
            headers[settings.user_id_header] = user_id

        try:
            with span("backend.call", {"onerec.backend": backend.url}):
                if is_streaming:
                    return await _stream_and_record(
                        backend, upstream_path, payload, headers, group, request_id, user_id,
                        prompt_text, t_request_start, queue_wait_ms,
                    )
                resp = await _http_client.post(f"{backend.url}{upstream_path}", json=payload, headers=headers)
                if resp.status_code != 200:
                    raise httpx.HTTPStatusError(f"HTTP {resp.status_code}", request=resp.request, response=resp)
                backend.breaker.record_success()
                text = _extract_text(resp.json())
                if text:
                    fallback_cache.put(prefix_hash_key(prompt_text, settings.prefix_hash_chars), text)
                REQUESTS_TOTAL.labels(group=group, status="ok").inc()
                latency_ms = (time.perf_counter() - t_request_start) * 1000
                REQUEST_LATENCY_SECONDS.labels(group=group).observe(latency_ms / 1000)
                log_request(
                    logger, request_id=request_id, trace_id=current_trace_id(), user_id=user_id, group=group,
                    backend=backend.id, strategy=settings.routing_strategy, status="ok", http_status=200,
                    latency_ms=latency_ms, queue_wait_ms=queue_wait_ms, degraded=False, prompt_len=len(prompt_text),
                    prompt_preview=redact_prompt(prompt_text, settings.log_prompt_preview, settings.prompt_preview_chars),
                )
                return JSONResponse(content=resp.json())
        except Exception as exc:  # noqa: BLE001
            backend.total_failures += 1
            backend.breaker.record_failure()
            REQUESTS_TOTAL.labels(group=group, status="error").inc()
            log_request(
                logger, request_id=request_id, trace_id=current_trace_id(), user_id=user_id, group=group,
                backend=backend.id, strategy=settings.routing_strategy, status="error",
                http_status=502, latency_ms=(time.perf_counter() - t_request_start) * 1000,
                queue_wait_ms=queue_wait_ms, degraded=False, prompt_len=len(prompt_text),
                prompt_preview=redact_prompt(prompt_text, settings.log_prompt_preview, settings.prompt_preview_chars),
                error=str(exc)[:300],
            )
            return await _serve_fallback(request_id, group, prompt_text, user_id, t_request_start, f"backend_error: {exc}")
        finally:
            backend.outstanding -= 1
            backend.release_slot()


async def _stream_and_record(backend, upstream_path, payload, headers, group, request_id, user_id,
                              prompt_text, t_request_start, queue_wait_ms):
    collected: list[bytes] = []

    async def gen():
        try:
            async for chunk in _forward_streaming(backend, upstream_path, payload, headers):
                collected.append(chunk)
                yield chunk
            backend.breaker.record_success()
            REQUESTS_TOTAL.labels(group=group, status="ok").inc()
        except Exception as exc:  # noqa: BLE001
            backend.total_failures += 1
            backend.breaker.record_failure()
            REQUESTS_TOTAL.labels(group=group, status="error").inc()
            log_request(
                logger, request_id=request_id, trace_id=current_trace_id(), user_id=user_id, group=group,
                backend=backend.id, strategy=settings.routing_strategy, status="error", http_status=502,
                latency_ms=(time.perf_counter() - t_request_start) * 1000, queue_wait_ms=queue_wait_ms,
                degraded=False, prompt_len=len(prompt_text), prompt_preview=None, error=str(exc)[:300],
            )
            raise
        finally:
            backend.outstanding -= 1
            backend.release_slot()
            latency_ms = (time.perf_counter() - t_request_start) * 1000
            REQUEST_LATENCY_SECONDS.labels(group=group).observe(latency_ms / 1000)
            log_request(
                logger, request_id=request_id, trace_id=current_trace_id(), user_id=user_id, group=group,
                backend=backend.id, strategy=settings.routing_strategy, status="ok", http_status=200,
                latency_ms=latency_ms, queue_wait_ms=queue_wait_ms, degraded=False, prompt_len=len(prompt_text),
                prompt_preview=redact_prompt(prompt_text, settings.log_prompt_preview, settings.prompt_preview_chars),
            )

    return StreamingResponse(gen(), media_type="text/event-stream")


def _extract_text(body: dict) -> str:
    choices = body.get("choices") or []
    if choices and isinstance(choices, list):
        c0 = choices[0]
        return c0.get("text") or (c0.get("message") or {}).get("content") or ""
    return ""


async def _serve_fallback(request_id, group, prompt_text, user_id, t_request_start, reason: str):
    if not settings.fallback_enabled:
        REQUESTS_TOTAL.labels(group=group, status="error").inc()
        return JSONResponse(status_code=503, content={"error": reason, "degraded": False})

    cache_key = prefix_hash_key(prompt_text, settings.prefix_hash_chars)
    payload = build_fallback_payload(fallback_cache, cache_key, settings.served_model_name)
    REQUESTS_TOTAL.labels(group=group, status="degraded_fallback").inc()
    latency_ms = (time.perf_counter() - t_request_start) * 1000
    log_request(
        logger, request_id=request_id, trace_id=current_trace_id(), user_id=user_id, group=group,
        backend=None, strategy=settings.routing_strategy, status="degraded_fallback", http_status=200,
        latency_ms=latency_ms, queue_wait_ms=0.0, degraded=True, prompt_len=len(prompt_text),
        prompt_preview=None, error=reason,
    )
    return JSONResponse(content=payload, headers={"X-OneRec-Degraded": "true"})
