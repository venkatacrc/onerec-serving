"""Prometheus metrics for the router. Scraped by kube-prometheus-stack
(see platform/observability/) at GET /metrics.

Metric names deliberately mirror what a Grafana dashboard needs directly
(see platform/observability/grafana-dashboard-onerec.json):
tok/s-equivalent request throughput, latency percentiles (via histogram),
queue depth, and canary/stable split -- so KEDA and Grafana query the same
metrics the router already emits, with no separate exporter needed.
"""
from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

REQUESTS_TOTAL = Counter(
    "onerec_router_requests_total",
    "Total requests handled by the router.",
    ["group", "status"],  # status: ok | error | rejected_admission | degraded_fallback
)

REQUEST_LATENCY_SECONDS = Histogram(
    "onerec_router_request_latency_seconds",
    "End-to-end request latency as observed by the router (includes queue wait).",
    ["group"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 45, 90, 180, 300),
)

QUEUE_WAIT_SECONDS = Histogram(
    "onerec_router_queue_wait_seconds",
    "Time spent waiting for an admission-control slot before forwarding.",
    ["group"],
    buckets=(0, 0.01, 0.05, 0.1, 0.5, 1, 2, 5, 10, 30),
)

BACKEND_QUEUE_DEPTH = Gauge(
    "onerec_router_backend_queue_depth",
    "Current number of requests queued (waiting for an admission-control slot) per backend.",
    ["backend", "group"],
)

BACKEND_OUTSTANDING = Gauge(
    "onerec_router_backend_outstanding",
    "Current number of in-flight requests forwarded to a backend.",
    ["backend", "group"],
)

BACKEND_HEALTHY = Gauge(
    "onerec_router_backend_healthy",
    "1 if the backend's last health check succeeded, else 0.",
    ["backend", "group"],
)

BACKEND_BREAKER_STATE = Gauge(
    "onerec_router_backend_breaker_state",
    "Circuit breaker state per backend: 0=closed, 1=half_open, 2=open.",
    ["backend", "group"],
)

CANARY_TRAFFIC_RATIO = Gauge(
    "onerec_router_canary_weight_pct",
    "Currently configured canary traffic weight percentage.",
)

_BREAKER_STATE_VALUE = {"closed": 0, "half_open": 1, "open": 2}


def refresh_backend_gauges(pool_snapshot: list[dict]):
    for b in pool_snapshot:
        labels = {"backend": b["url"], "group": b["group"]}
        BACKEND_QUEUE_DEPTH.labels(**labels).set(b["queued"])
        BACKEND_OUTSTANDING.labels(**labels).set(b["outstanding"])
        BACKEND_HEALTHY.labels(**labels).set(1 if b["healthy"] else 0)
        BACKEND_BREAKER_STATE.labels(**labels).set(_BREAKER_STATE_VALUE.get(b["breaker_state"], 0))
