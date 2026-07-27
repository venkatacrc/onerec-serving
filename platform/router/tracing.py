"""OpenTelemetry wiring: root span per request ("router.request"), child
span for the outbound call to an engine backend ("backend.call"), exported
via OTLP to the collector (platform/observability/otel-collector.yaml) ->
Jaeger.

Tracing is entirely optional and no-ops cleanly if
`OTEL_EXPORTER_OTLP_ENDPOINT` is unset (e.g. local dev), so importing this
module never requires an OTel collector to be reachable.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Optional

_tracer = None


def init_tracing(service_name: str, otlp_endpoint: str) -> bool:
    global _tracer
    if not otlp_endpoint:
        return False
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import SERVICE_NAME, Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = TracerProvider(resource=Resource.create({SERVICE_NAME: service_name}))
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint, insecure=True)))
        trace.set_tracer_provider(provider)
        _tracer = trace.get_tracer(service_name)
        return True
    except Exception:
        # Tracing is a nice-to-have; never let a missing/misconfigured
        # collector take the router down.
        _tracer = None
        return False


@contextmanager
def span(name: str, attributes: Optional[dict] = None):
    if _tracer is None:
        yield None
        return
    with _tracer.start_as_current_span(name) as s:
        if attributes:
            for k, v in attributes.items():
                s.set_attribute(k, v)
        yield s


def current_trace_id() -> Optional[str]:
    if _tracer is None:
        return None
    try:
        from opentelemetry import trace

        ctx = trace.get_current_span().get_span_context()
        if ctx and ctx.trace_id:
            return format(ctx.trace_id, "032x")
    except Exception:
        pass
    return None


def inject_trace_headers(headers: dict) -> dict:
    if _tracer is None:
        return headers
    try:
        from opentelemetry.propagate import inject

        inject(headers)
    except Exception:
        pass
    return headers
