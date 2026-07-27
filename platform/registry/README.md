# Model / Version Registry (reference)

Lightweight file-ledger registry (`ledger.json`, tracked in git) for every
serving configuration version, which one is active per traffic group, and
a rollback mechanism. See `docs/PRODUCTION_ARCHITECTURE.md` §3 "Model/
version registry" row for why this instead of MLflow, and
`docs/ROLLOUT_STRATEGY.md` for the full canary/rollback workflow this
plugs into.

```bash
# Register a new version after a benchmark run confirms it
python3 registry.py register --engine vllm --engine-image vllm/vllm-openai:v0.12.0 \
  --tp 1 --dtype bf16 --benchmark-ref results/report/REPORT.md \
  --k8s-deployment onerec-vllm --k8s-container vllm

python3 registry.py list
python3 registry.py show <version_id>

# Promote to canary, watch its metrics (docs/OBSERVABILITY.md), then to stable
python3 registry.py promote <version_id> --group canary
python3 registry.py promote <version_id> --group stable --apply   # --apply actually runs kubectl

# Something's wrong -- roll back immediately
python3 registry.py rollback --group stable --apply
python3 registry.py history --group stable
```

`ledger.json` starts empty (`{"versions": [], "active": {}, "history":
{}}`) — every command above mutates it in place. Commit it to git after
any promotion/rollback so the deployment history has an audit trail
independent of `kubectl` history/etcd.
