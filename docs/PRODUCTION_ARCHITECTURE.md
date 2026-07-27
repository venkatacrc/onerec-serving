# Production Architecture

This document is the entry point for the **`platform/`** layer added on top of
the benchmarking toolkit (`scripts/`, `bench/`, `configs/`). Where the
benchmarking toolkit answers "how fast is OneRec-8B-Pro on this hardware,
per engine/parallelism config", this layer answers "how do you actually run
this as a resilient, observable, autoscaling service" — on the same single
8x B200/GB200 node, since that's the hardware available to test against.

Read this doc first; it links out to the deeper docs (`OBSERVABILITY.md`,
`ROLLOUT_STRATEGY.md`, `CAPACITY_AND_COST.md`) and to the code under
`platform/`.

## 1. Honesty about scope: what "production-grade on one node" means

Every feature below is implemented for real and is runnable on the box you
already have. But two categories of caveat apply throughout, stated once
here instead of repeated on every bullet:

1. **Single-node ceiling.** Load balancing, autoscaling, canary rollout,
   etc. all operate over **8 GPUs on one machine**. Kubernetes, KEDA, and
   the router work identically whether the cluster has 1 node or 200 —
   nothing here is a toy that needs to be rebuilt for a real fleet — but
   this deployment cannot demonstrate node failure, multi-rack placement,
   or cross-region failover, because there's only one node. Section 8
   explains exactly what changes going from 1 node to N.
2. **Integrations that don't exist yet are built as reference
   implementations behind clean interfaces, not stubs left unimplemented.**
   There is no real feature store, model registry service, or trace
   backend already running for this product. Rather than hand-waving these
   as "TODO: integrate with your feature store," this repo ships a small,
   real, working implementation of each (`platform/feature_store/`,
   `platform/registry/`) that follows the same interface a production
   system would — swapping the mock Redis-backed feature store for Feast/
   Tecton, or the file-ledger registry for MLflow, is a contained
   single-file change (see each component's doc section for exactly what
   to swap).

## 2. Component map

```
                                   ┌─────────────────────────────┐
                                   │   Observability stack        │
                                   │   Prometheus / Grafana /     │
                                   │   Alertmanager / Jaeger /    │
                                   │   DCGM-exporter              │
                                   └───────────▲──────────────────┘
                                               │ scrape / traces
  ┌────────────┐   retry+breaker   ┌───────────┴───────────┐  admission   ┌──────────────────────┐
  │ Calling    │ ────────────────► │  OneRec Router         │ ───────────►│ Engine replica pool   │
  │ service /  │ ◄──────────────── │  (platform/router)      │ ◄───────────│ vLLM / SGLang / TRT-LLM│
  │ client_sdk │   fallback resp.  │  admission control,     │  fallback    │ (k8s Deployments,     │
  └────────────┘                   │  prefix/least-outstand. │  cache hit   │  1 GPU/replica by     │
        │                          │  routing, canary split, │              │  default -- see §4)  │
        │ user_id                  │  Prometheus + OTel      │              └──────────┬────────────┘
        ▼                          └───────────┬─────────────┘                          │ HPA/KEDA scales on
  ┌────────────────┐                           │ user history lookup                     │ queue depth + GPU util
  │ Feature store   │◄──────────────────────────┘                                         ▼
  │ (platform/      │                                                              ┌─────────────┐
  │  feature_store) │                                                              │  k3s + NVIDIA │
  └────────────────┘                                                              │  device plugin│
                                                                                    └─────────────┘
```

## 3. Concrete technology decisions and why

| Layer | Choice | Why this and not the alternatives |
|---|---|---|
| Orchestrator | **k3s** (single-node, lightweight Kubernetes) + NVIDIA device plugin | You explicitly asked for k8s-native liveness/readiness, rolling/canary deploys, and HPA — those are Kubernetes concepts, so Kubernetes is the right target rather than bolting equivalent logic onto Docker Compose/Swarm. k3s is a single ~70MB binary that gives a fully conformant, single-binary k8s control plane + kubelet with no external etcd/HA complexity to run on one box; a full kubeadm cluster or a managed control plane would be pure overhead here. Moving to a real multi-node cluster later is a `kubectl apply` against the same manifests (see §8), not a rewrite. |
| GPU scheduling | **NVIDIA device plugin for Kubernetes** (`nvidia.com/gpu` resource) | Standard, maintained-by-NVIDIA way to expose GPUs as a schedulable k8s resource; every engine Deployment below just requests `nvidia.com/gpu: N` in its pod spec, identical to how a real GPU cluster would run it. |
| Router / load balancer | **Custom lightweight async Python (FastAPI + httpx) service**, not Envoy/nginx/Istio | Prefix-aware routing (route a request to whichever replica most likely has its prompt prefix cached in SGLang's RadixAttention/vLLM's prefix cache), admission control with backpressure, and canary weight-splitting are business logic specific to LLM serving that off-the-shelf L7 proxies don't support out of the box without custom Lua/WASM filters (more moving parts, harder to reason about, harder to unit test than ~600 lines of readable Python). The router is still a normal stateless HTTP service from k8s's point of view — it gets its own Deployment/Service/HPA like anything else. |
| Autoscaling | **KEDA** (Kubernetes Event-Driven Autoscaler), Prometheus scaler | Native k8s HPA only scales cleanly on CPU/memory out of the box; scaling on GPU utilization or router queue depth requires either the custom-metrics-apiserver adapter (more infra) or KEDA (a single CRD-based add-on that queries Prometheus directly). KEDA is the de facto standard for "scale on a PromQL query" today. |
| Observability metrics | **kube-prometheus-stack** (Prometheus + Grafana + Alertmanager) + **DCGM-exporter** | vLLM and SGLang both natively expose Prometheus metrics at `/metrics` (request rate, TTFT/E2E histograms, running/waiting request counts) — Prometheus is the natural scrape target with zero engine-side changes. DCGM-exporter is NVIDIA's own supported exporter for GPU utilization/power/ECC/throttle metrics, i.e. exactly the per-replica GPU signal KEDA and Grafana both need. |
| Tracing | **OpenTelemetry Collector -> Jaeger (all-in-one)** | OTel is the vendor-neutral instrumentation standard; Jaeger all-in-one is the simplest single-node trace backend to stand up (one Deployment, in-memory storage is fine for a dev/test node). The router is instrumented end-to-end (client -> router -> backend call); engine-internal spans are documented as a known gap (see `docs/OBSERVABILITY.md` §4) since vLLM/SGLang don't yet propagate W3C trace context internally. |
| Canary / blue-green | **Router-level weighted traffic split** between a `stable` and `canary` Kubernetes Service, plus native k8s `RollingUpdate` strategy for routine upgrades | Argo Rollouts/Flagger/Istio give you the same capability with a service mesh's worth of extra components to operate. Since the router already sits in the request path and already does routing decisions, canary weighting is ~40 lines of code there, with the exact same operational effect (X% of traffic to the new version, promote or roll back based on its metrics) and zero extra infrastructure. `docs/ROLLOUT_STRATEGY.md` documents Argo Rollouts as the natural upgrade if you later adopt a service mesh fleet-wide for other reasons. |
| Feature store | **Reference FastAPI + Redis mock service**, behind a `FeatureStoreClient` interface | No real feature store exists for this product yet. Building the real integration is impossible without knowing which system you'll standardize on (Feast, Tecton, an internal system) — so this ships a working reference implementation of the *interface* (`get_user_history`, `get_item_metadata`) that the router and benchmark client call today, with exactly one class (`RedisFeatureStore`) to swap for a real client. See `platform/feature_store/adapter.py`. |
| Model/version registry | **File-ledger registry** (`platform/registry/registry.py` + JSON ledger), not MLflow | MLflow's tracking server + backing DB is the right call once you have many models/teams sharing a registry; for one model family with 3 engine backends, a git-tracked JSON ledger with `register`/`promote`/`rollback` subcommands gives the same audit trail and rollback mechanism with zero extra infrastructure to run/secure/back up. `docs/ROLLOUT_STRATEGY.md` §5 documents exactly when to graduate to MLflow/W&B Model Registry. |

## 4. Replicate vs. shard — answered from the data you already have

You flagged this as blocking capacity purchase decisions. The concurrency
>= 16 rows in `results/report/REPORT.md` are flagged unreliable (client
connection-pool exhaustion causing `Server disconnected` errors — see
`docs/RUNBOOK.md` "Troubleshooting: docker `--gpus` multi-device bug" for
the *server-start* half of that debugging, and the "Interpreting
failed/anomalous throughput results" section for the client-side half).
**But the concurrency 1/2/4/8 rows have zero failures across every config**
and already show a consistent, large effect:

| concurrency | vllm-tp1 tok/s/GPU | vllm-tp2 | vllm-tp4 | vllm-tp8 |
|---:|---:|---:|---:|---:|
| 1 | 137.7 | 77.0 | 41.9 | 22.3 |
| 2 | 269.6 | 151.0 | 83.4 | 42.6 |
| 4 | 371.4 | 187.1 | 94.6 | 47.8 |
| 8 | 408.4 | 197.1 | 98.3 | 49.1 |

Tensor-parallel sharding **monotonically reduces tokens/sec per GPU** at
every concurrency level, by roughly the TP degree itself (TP=8 gets ~1/6th
the per-GPU throughput of TP=1, not the 8x-lower-latency-for-free result
TP is meant to buy you at *low* concurrency for a much larger model). This
is exactly what `docs/ARCHITECTURE.md` §3 predicted going in: OneRec-8B-Pro
is small enough (8.39B dense params, ~17GB in BF16) that a single B200's
183GB is never memory-constrained, so TP only adds inter-GPU all-reduce
tax with no capacity upside.

**Recommendation, confirmed by the reliable portion of the data: replicate,
don't shard.** Run N independent TP=1 replicas behind the router rather
than fewer TP>1 replicas. This is also what `platform/k8s/10-vllm-deployment.yaml`
implements by default (`nvidia.com/gpu: 1` per pod, `replicas: 8`).

This conclusion doesn't need the failed concurrency>=16 rows to hold — the
effect is already an order of magnitude at concurrency 1-8 — but re-running
the full matrix after the two root-cause fixes already applied
(`10/11/12_serve_*.sh` quoting fix, SGLang `distro` fix) will let you
confirm it holds at production-realistic concurrency too, and is a
prerequisite for the capacity numbers in `docs/CAPACITY_AND_COST.md` to be
final rather than provisional.

## 5. Traffic & scaling

- **Load balancer / router:** `platform/router/` — round-robin,
  least-outstanding-requests, and prefix-hash-aware strategies (selectable
  per deployment via `ROUTING_STRATEGY` env var). Prefix-aware routing
  hashes a configurable prefix of the prompt (default: first 256 chars) to
  consistently map similar prompts to the same replica, which increases
  KV-cache/RadixAttention cache hit rates — most valuable with SGLang,
  supported identically for vLLM's own prefix caching.
- **Admission control & backpressure:** `platform/router/admission.py` —
  a global in-flight-request semaphore plus a bounded FIFO queue per
  backend; once the queue is full, the router immediately returns `HTTP
  429` with `Retry-After` rather than accepting unbounded work and letting
  latency degrade for everyone (see `docs/BENCHMARK_METHODOLOGY.md`-style
  reasoning: an unbounded queue just moves the failure from "explicit 429"
  to "everyone times out together").
- **Horizontal autoscaling:** `platform/k8s/30-keda-scaledobjects.yaml` —
  KEDA `ScaledObject`s per engine Deployment, scaling on two Prometheus
  queries: router-reported queue depth per backend group, and DCGM GPU
  utilization. Whichever signal is hotter drives scale-out (KEDA supports
  multiple triggers per `ScaledObject`; it scales on the max of all
  triggers' recommendations).

## 6. Reliability & resilience

- **Health checks:** every engine Deployment has k8s `livenessProbe`,
  `readinessProbe`, and `startupProbe` wired to each engine's own
  `/health` endpoint (`platform/k8s/1{0,2,3}-*.yaml`), with generous
  `startupProbe` timeouts (up to 30 min for TensorRT-LLM engine
  compilation) so k8s doesn't kill a pod that's still loading.
- **Graceful degradation:** `platform/router/fallback.py` — when every
  backend in a group is either unhealthy or its admission-control queue is
  full, the router serves a **heuristic top-popular-items fallback**
  response (cached, refreshed periodically from recent successful
  responses) instead of a hard failure, clearly marked
  `"degraded": true` in the response body/headers so callers/dashboards
  can distinguish it from a real model response.
- **Circuit breakers + retry/backoff:** `platform/client_sdk/onerec_client.py`
  — a reference client any calling service should use instead of hitting
  the router with a bare HTTP client: closed/open/half-open breaker state
  machine per backend group, exponential backoff with jitter on retries,
  and automatic fallback to a local heuristic when the breaker is open.
- **Rolling / canary / blue-green:** see §3 and `docs/ROLLOUT_STRATEGY.md`.

## 7. Observability, feature-store integration, registry, cost/capacity

Each has its own doc (kept out of this file to keep this one skimmable):

- `docs/OBSERVABILITY.md` — dashboards, alert rules, tracing, structured
  logging + PII handling.
- `docs/ROLLOUT_STRATEGY.md` — canary/blue-green mechanics, model/version
  registry, rollback runbook.
- `docs/CAPACITY_AND_COST.md` — tok/s/Watt and tok/s/$ modeling, reserved
  vs on-demand strategy, open-loop load testing methodology.
- `platform/feature_store/` — mock feature-store service + adapter
  interface + how the benchmark client now optionally builds prompts from
  real-shaped user-history records instead of only synthetic ones.

## 8. Going from 1 node to a real fleet

Everything here is written so this is a config change, not a rewrite:

1. **k3s -> real cluster:** point `kubectl`/`helm` at your real cluster
   context; every manifest under `platform/k8s/` is plain Kubernetes YAML
   with no k3s-specific fields. Multi-node adds pod anti-affinity across
   nodes (already specified, currently a no-op with 1 node) and lets PDBs
   (`minAvailable`) actually protect you during node drains.
   `platform/bootstrap_k8s.sh` becomes unnecessary — your platform team's
   existing cluster bootstrap replaces it.
2. **Router:** already stateless and horizontally scalable (its own HPA);
   put a real L4/L7 load balancer (cloud LB, MetalLB, or an Ingress
   controller) in front of the router's Service instead of `NodePort`.
3. **DCGM/KEDA/Prometheus:** identical at fleet scale; Prometheus
   federation or Thanos/Mimir replaces single-instance Prometheus once
   you outgrow one node's retention/query load.
4. **Capacity planning:** `bench/capacity_planner.py` already models
   $/GPU-hour x replica count x utilization target — feed it your real
   fleet's reserved/on-demand pricing instead of the placeholder rates in
   `configs/cost_assumptions.yaml`.
