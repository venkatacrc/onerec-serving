# Observability

Covers dashboards, alerting, tracing, and structured/PII-safe logging for
the `platform/` layer. See `docs/PRODUCTION_ARCHITECTURE.md` §3 for why
each tool was chosen.

## 1. Metrics

Three scrape targets, all discovered via the classic `prometheus.io/scrape`
pod annotations (see `platform/observability/kube-prometheus-values.yaml`):

| Source | What it exposes | Consumed by |
|---|---|---|
| Engine pods (vLLM/SGLang native `/metrics`) | `vllm:generation_tokens_total`, `vllm:time_to_first_token_seconds`, `vllm:e2e_request_latency_seconds`, running/waiting request gauges | Grafana dashboard, capacity sanity-checks |
| `platform/router/metrics.py` (`/metrics` on the router) | request rate by status/group, end-to-end + queue-wait latency histograms, per-backend queue depth/outstanding/health/breaker-state, canary weight | Grafana dashboard, `platform/k8s/30-keda-scaledobjects.yaml` (queue-depth trigger), `prometheus-rules.yaml` alerts |
| `dcgm-exporter` DaemonSet (`platform/observability/dcgm-exporter.yaml`) | per-GPU utilization, power, memory, ECC errors, thermal/clock throttle reasons | Grafana dashboard, KEDA (GPU-util trigger), `prometheus-rules.yaml` alerts |

Import `platform/observability/grafana-dashboard-onerec.json` into Grafana
(Dashboards -> Import) for tok/s, TTFT/E2E percentiles, queue depth, and
GPU util/power per replica in one view — exactly the signal set requested
for capacity/health at a glance.

## 2. Alerting

`platform/observability/prometheus-rules.yaml` (a `PrometheusRule` picked
up automatically by the Prometheus Operator) covers:

- **Latency SLO breach** — router p99 latency > 5s for 5m, per traffic group.
- **Error-rate spikes** — router error rate > 5% for 5m; a separate,
  higher-severity alert fires the moment *any* degraded-fallback response
  is served (real capacity exhaustion, not just elevated latency — see
  `platform/router/fallback.py`).
- **Circuit breaker / backend health** — a backend's breaker opening, or
  failing health checks, pages before it necessarily shows up as a big
  aggregate error-rate blip.
- **GPU ECC errors** — any double-bit (uncorrectable) ECC error is
  `critical`; these can silently corrupt output, not just crash.
- **GPU thermal/clock throttling** — sustained throttling silently
  degrades tok/s below every number in `results/report/REPORT.md` without
  necessarily causing errors, which is exactly why it needs its own alert
  rather than relying on the latency/error alerts to catch it.
- **OOM-killed / preempted pods** — `kube_pod_container_status_last_terminated_reason` /
  `kube_pod_status_reason`, scoped to the `onerec` namespace.

**Alertmanager ships with zero receivers configured by default** — see
the comment in `platform/observability/kube-prometheus-values.yaml`. Wire
in a real Slack/PagerDuty/email receiver before treating any of this as
real on-call coverage; until then, alerts fire and are visible in the
Alertmanager UI but page no one.

## 3. Distributed tracing

`platform/router/tracing.py` creates a root span (`router.request`) per
request and a child span (`backend.call`) for the outbound call to
whichever engine replica was selected, exported via OTLP
(`platform.observability/otel-collector.yaml`) to Jaeger
(`platform/observability/jaeger.yaml`). Trace context (W3C `traceparent`
header) is injected into the outbound request to the engine and returned
in the router's structured log line (`trace_id` field) so a slow/failed
request can be correlated between Jaeger and the raw JSON logs.

**Known gap, stated plainly:** vLLM/SGLang/TensorRT-LLM do not currently
propagate the inbound trace context into their own internal spans (e.g. a
separate "prefill" / "decode" span per request inside the engine). The
trace today shows client -> router -> "time spent waiting for the engine's
HTTP response" as one span, not a further breakdown of what happened
inside the engine during that time — for that level of detail, correlate
the `trace_id`/`request_id` in the router's log line with the engine's own
timestamped logs for the same time window. Revisit this once (if) an
engine adds native OTel span propagation.

## 4. Structured logging + PII handling

`platform/router/logging_util.py` — every request produces one JSON log
line (`request_complete`) with: `request_id`, `trace_id`, `group`,
`backend`, `routing_strategy`, `status`, `http_status`, `latency_ms`,
`queue_wait_ms`, `degraded`, `prompt_len_chars`, and on failure `error`.

PII-specific defaults, deliberately conservative (a recommendation
product's prompts embed a user's watched/purchased/clicked history, which
is behavioral PII):

- **`user_id` is never logged in plaintext** — only a truncated one-way
  SHA-256 hash (`user_id_hash`), enough to correlate a user's requests
  across log lines for debugging without recovering the identifier from
  logs alone.
- **Prompt text is never logged by default.** `LOG_PROMPT_PREVIEW=true`
  (operator opt-in, e.g. for a scoped debugging session) logs only the
  first `PROMPT_PREVIEW_CHARS` (default 80) characters. Treat flipping
  this on in a shared/long-lived environment as a deliberate product/
  privacy decision, not a debugging convenience — see the docstring in
  `logging_util.py` for the exact reasoning.
- Log **access control**, not the hash, is the real control — the hash
  only prevents a log line by itself from trivially reversing to a raw
  user ID; anyone with both log access and the ability to correlate
  hashes back to users through another system already has that access
  through the other system.

## 5. Verifying the observability stack

```bash
# metrics
kubectl -n onerec-observability port-forward svc/kube-prometheus-stack-prometheus 9090:9090
# http://localhost:9090 -- try: onerec_router_backend_queue_depth

# dashboards
kubectl -n onerec-observability port-forward svc/kube-prometheus-stack-grafana 3000:80
# http://localhost:3000 -- import platform/observability/grafana-dashboard-onerec.json

# traces
kubectl -n onerec-observability port-forward svc/jaeger-query 16686:16686
# http://localhost:16686 -- service "onerec-router"

# alerts
kubectl -n onerec-observability port-forward svc/kube-prometheus-stack-alertmanager 9093:9093
```
