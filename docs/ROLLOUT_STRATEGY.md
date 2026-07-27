# Rollout Strategy: Canary / Blue-Green + Model/Version Registry

See `docs/PRODUCTION_ARCHITECTURE.md` §3 for why this is router-level
weighted traffic splitting + native k8s rolling updates, instead of Argo
Rollouts/Flagger/Istio.

## 1. Two independent upgrade mechanisms, used together

1. **Router-level canary weighting** (`platform/router/`) — a genuinely
   separate deployment (`onerec-vllm-canary` StatefulSet,
   `platform/k8s/11-vllm-canary-statefulset.yaml`) receives
   `CANARY_WEIGHT_PCT`% of traffic, adjustable live without a redeploy.
   Use this for anything you want to validate against real traffic before
   it's trusted at 100%: a new engine image tag, a new `--dtype`/
   quantization setting, a new model revision.
2. **Native k8s `RollingUpdate`** on the `stable` StatefulSet — once a
   canary is promoted (see §3), rolling it out to the rest of the stable
   pool is k8s's own pod-by-pod replacement, respecting the
   `PodDisruptionBudget` (`platform/k8s/10-vllm-statefulset.yaml`) so you
   never lose more than 2 of 7 stable replicas at once.

## 2. Standard canary workflow, end to end

```bash
# 1. Register the new version (after it's passed the benchmark toolkit's
#    smoke test at minimum -- scripts/20_smoke_test.sh):
python3 platform/registry/registry.py register --engine vllm \
  --engine-image vllm/vllm-openai:v0.12.1 --tp 1 --dtype bf16 \
  --benchmark-ref results/report/REPORT.md \
  --k8s-deployment onerec-vllm-canary --k8s-container vllm \
  --notes "v0.12.1 patch release, re-benchmarked on B200"

# 2. Point the canary StatefulSet at it and promote in the registry:
python3 platform/registry/registry.py promote <version_id> --group canary --apply
#   (--apply runs: kubectl set image statefulset/onerec-vllm-canary vllm=vllm/vllm-openai:v0.12.1)

# 3. Ramp traffic to the canary gradually, watching docs/OBSERVABILITY.md's
#    dashboard + alerts at each step (start low, e.g. 5%, and only increase
#    once you've observed a full traffic cycle with no regression):
curl -X POST http://onerec-router:9000/admin/canary_weight -d '{"canary_weight_pct": 5}'
#   ... watch latency/error-rate/tok-s for the "canary" group specifically ...
curl -X POST http://onerec-router:9000/admin/canary_weight -d '{"canary_weight_pct": 25}'
#   ... watch again ...
curl -X POST http://onerec-router:9000/admin/canary_weight -d '{"canary_weight_pct": 100}'

# 4. Once confident, promote to stable and roll it out to the rest of the pool:
python3 platform/registry/registry.py promote <version_id> --group stable --apply
kubectl -n onerec rollout status statefulset/onerec-vllm

# 5. Ramp the router's canary weight back to 0 and scale the canary
#    StatefulSet back to 1 idle replica, ready for the next version.
curl -X POST http://onerec-router:9000/admin/canary_weight -d '{"canary_weight_pct": 0}'
```

## 3. Rollback

Two independent rollback paths depending on how far the rollout got:

- **Still canarying (haven't touched stable yet):** just set
  `canary_weight_pct` back to 0 via the same `/admin/canary_weight` call —
  instant, no pod changes needed.
- **Already promoted to stable:**
  ```bash
  python3 platform/registry/registry.py rollback --group stable --apply
  kubectl -n onerec rollout status statefulset/onerec-vllm
  ```
  This re-points the stable StatefulSet's image at whatever was active
  immediately before the bad promotion (or pass `--to <version_id>` for a
  specific earlier version), and the ledger (`platform/registry/ledger.json`)
  records the rollback with a timestamp — commit that file to git right
  after so the rollback has an audit trail independent of `kubectl`/etcd
  history.

`kubectl -n onerec rollout undo statefulset/onerec-vllm` is the raw k8s
equivalent if you need to roll back purely at the orchestrator level
without touching the registry ledger (e.g. registry ledger is
unavailable) — prefer the registry path when available since it keeps
the ledger's audit trail consistent with what's actually running.

## 4. Blue-green (whole-pool cutover instead of gradual ramp)

For a change too risky to expose to even a small % of real traffic
gradually (e.g. a new model revision with a materially different output
distribution), skip the ramp: stand up a second full-size stable-equivalent
pool (a third StatefulSet, or temporarily resize the canary StatefulSet to
match the stable pool's replica count), validate it fully out-of-band
(shadow traffic, or the benchmark toolkit's latency/throughput sweeps
pointed at it directly), then flip `canary_weight_pct` straight from 0 to
100 in one step instead of a gradual ramp, and immediately flip back to 0
on any regression. The router's admission control means this cutover
doesn't cause a request spike/drop on either pool — it's purely a routing
decision.

## 5. When to graduate off this and onto Argo Rollouts / a service mesh

This router-level approach is deliberately the simplest thing that gives
you real canary/blue-green behavior with zero extra infrastructure. Move
to Argo Rollouts + a service mesh (Istio/Linkerd) once **any** of these
become true:

- You need automated canary analysis (auto-promote/auto-rollback based on
  metric thresholds) instead of a human watching the dashboard at each
  ramp step.
- Multiple independent services beyond OneRec need the same canary
  mechanism, and you want it enforced consistently at the mesh level
  rather than re-implemented per service.
- You adopt a service mesh fleet-wide for other reasons (mTLS, more
  sophisticated traffic policy) — at that point canary/blue-green comes
  for free from the mesh and duplicating it in the router is pure
  redundancy.
