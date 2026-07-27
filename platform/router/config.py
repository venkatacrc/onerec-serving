"""Router configuration: backend topology + all tunables, loaded from a YAML
file (topology, rarely changes at runtime) plus environment variables
(operational knobs, changed per-deployment/canary-rollout).

See config.example.yaml for the backend-topology file format.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml

RoutingStrategy = Literal["round_robin", "least_outstanding", "prefix_hash"]


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _env_bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class BackendSpec:
    url: str
    group: str = "stable"


@dataclass
class RouterSettings:
    backends: list[BackendSpec] = field(default_factory=list)

    routing_strategy: RoutingStrategy = "least_outstanding"
    prefix_hash_chars: int = 256

    # Admission control / backpressure (per backend).
    max_inflight_per_backend: int = 32
    max_queue_depth_per_backend: int = 64
    queue_wait_timeout_s: float = 30.0

    # Outbound circuit breaker (router -> engine backend).
    breaker_failure_threshold: int = 5
    breaker_open_seconds: float = 30.0
    breaker_half_open_probes: int = 1

    # Health checking.
    health_check_path: str = "/health"
    health_check_interval_s: float = 5.0
    health_check_timeout_s: float = 3.0

    # Canary / blue-green.
    canary_weight_pct: float = 0.0  # % of NEW requests routed to the "canary" group

    # Graceful degradation.
    fallback_enabled: bool = True
    fallback_cache_size: int = 256

    # Forwarding.
    upstream_timeout_s: float = 300.0
    served_model_name: str = "onerec-8b-pro"

    # PII / structured logging.
    log_prompt_preview: bool = False
    prompt_preview_chars: int = 80
    user_id_header: str = "X-User-Id"

    # Observability.
    otel_exporter_otlp_endpoint: str = ""  # empty = tracing disabled
    service_name: str = "onerec-router"

    # HTTP server.
    host: str = "0.0.0.0"
    port: int = 9000


def _load_backends_yaml(path: str) -> list[BackendSpec]:
    data = yaml.safe_load(Path(path).read_text()) or {}
    specs: list[BackendSpec] = []
    for group_name, urls in (data.get("groups") or {}).items():
        for url in urls or []:
            specs.append(BackendSpec(url=url, group=group_name))
    return specs


def load_settings() -> RouterSettings:
    s = RouterSettings()

    backends_file = os.environ.get("ROUTER_BACKENDS_FILE")
    if backends_file and Path(backends_file).exists():
        s.backends = _load_backends_yaml(backends_file)
    else:
        # Fall back to a flat env var for quick local testing:
        #   ROUTER_STABLE_BACKENDS="http://localhost:8000,http://localhost:8010"
        #   ROUTER_CANARY_BACKENDS="http://localhost:8020"
        for group, env_name in (("stable", "ROUTER_STABLE_BACKENDS"), ("canary", "ROUTER_CANARY_BACKENDS")):
            raw = os.environ.get(env_name, "")
            for url in [u.strip() for u in raw.split(",") if u.strip()]:
                s.backends.append(BackendSpec(url=url, group=group))

    s.routing_strategy = os.environ.get("ROUTING_STRATEGY", s.routing_strategy)  # type: ignore[assignment]
    s.prefix_hash_chars = _env_int("PREFIX_HASH_CHARS", s.prefix_hash_chars)

    s.max_inflight_per_backend = _env_int("MAX_INFLIGHT_PER_BACKEND", s.max_inflight_per_backend)
    s.max_queue_depth_per_backend = _env_int("MAX_QUEUE_DEPTH_PER_BACKEND", s.max_queue_depth_per_backend)
    s.queue_wait_timeout_s = _env_float("QUEUE_WAIT_TIMEOUT_S", s.queue_wait_timeout_s)

    s.breaker_failure_threshold = _env_int("BREAKER_FAILURE_THRESHOLD", s.breaker_failure_threshold)
    s.breaker_open_seconds = _env_float("BREAKER_OPEN_SECONDS", s.breaker_open_seconds)
    s.breaker_half_open_probes = _env_int("BREAKER_HALF_OPEN_PROBES", s.breaker_half_open_probes)

    s.health_check_path = os.environ.get("HEALTH_CHECK_PATH", s.health_check_path)
    s.health_check_interval_s = _env_float("HEALTH_CHECK_INTERVAL_S", s.health_check_interval_s)
    s.health_check_timeout_s = _env_float("HEALTH_CHECK_TIMEOUT_S", s.health_check_timeout_s)

    s.canary_weight_pct = _env_float("CANARY_WEIGHT_PCT", s.canary_weight_pct)

    s.fallback_enabled = _env_bool("FALLBACK_ENABLED", s.fallback_enabled)
    s.fallback_cache_size = _env_int("FALLBACK_CACHE_SIZE", s.fallback_cache_size)

    s.upstream_timeout_s = _env_float("UPSTREAM_TIMEOUT_S", s.upstream_timeout_s)
    s.served_model_name = os.environ.get("SERVED_MODEL_NAME", s.served_model_name)

    s.log_prompt_preview = _env_bool("LOG_PROMPT_PREVIEW", s.log_prompt_preview)
    s.prompt_preview_chars = _env_int("PROMPT_PREVIEW_CHARS", s.prompt_preview_chars)
    s.user_id_header = os.environ.get("USER_ID_HEADER", s.user_id_header)

    s.otel_exporter_otlp_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", s.otel_exporter_otlp_endpoint)
    s.service_name = os.environ.get("OTEL_SERVICE_NAME", s.service_name)

    s.host = os.environ.get("ROUTER_HOST", s.host)
    s.port = _env_int("ROUTER_PORT", s.port)

    return s
