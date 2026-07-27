#!/usr/bin/env python3
"""Model/version registry: a lightweight, git-tracked JSON ledger of every
serving configuration that's been registered, which one is currently
active per traffic group ("stable"/"canary"), and a rollback mechanism.

Why a file ledger instead of MLflow/W&B Model Registry: see
docs/PRODUCTION_ARCHITECTURE.md §3 "Model/version registry" row. Graduate
to one of those once you have multiple models/teams sharing a registry;
for one model family with 3 engine backends, this gives the same audit
trail and rollback guarantee with zero extra infrastructure.

Every mutating command optionally drives a real rollout via `kubectl set
image` when --apply is passed (dry-run / prints-the-command-only by
default, so this is safe to explore against a real cluster).

Usage:
    python3 registry.py register --engine vllm --model-id OpenOneRec/OneRec-8B-pro \
        --model-revision main --engine-image vllm/vllm-openai:v0.12.0 \
        --tp 1 --dtype bf16 --benchmark-ref results/report/REPORT.md \
        --k8s-deployment onerec-vllm --k8s-container vllm

    python3 registry.py list
    python3 registry.py show <version_id>
    python3 registry.py promote <version_id> --group stable [--apply]
    python3 registry.py rollback --group stable [--to <version_id>] [--apply]
    python3 registry.py history --group stable
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

LEDGER_PATH = Path(__file__).parent / "ledger.json"


def _load_ledger() -> dict:
    if not LEDGER_PATH.exists():
        return {"versions": [], "active": {}, "history": {}}
    return json.loads(LEDGER_PATH.read_text())


def _save_ledger(ledger: dict):
    LEDGER_PATH.write_text(json.dumps(ledger, indent=2, sort_keys=False) + "\n")


def _config_hash(config: dict) -> str:
    blob = json.dumps(config, sort_keys=True).encode()
    return hashlib.sha256(blob).hexdigest()[:12]


def _git_commit() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True).strip()
    except Exception:
        return "unknown"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _find_version(ledger: dict, version_id: str) -> dict:
    for v in ledger["versions"]:
        if v["version_id"] == version_id:
            return v
    raise SystemExit(f"error: version '{version_id}' not found in ledger")


def cmd_register(args):
    ledger = _load_ledger()
    config = {
        "tp": args.tp,
        "dtype": args.dtype,
        "max_model_len": args.max_model_len,
        "extra_args": args.extra_args,
    }
    # Hash the engine image too (not just tp/dtype/etc.) so two registrations
    # in the same second that only bump the image tag don't collide -- the
    # timestamp alone is only 1s resolution and config_hash used to ignore
    # engine_image entirely.
    identity_hash = _config_hash({**config, "engine_image": args.engine_image})
    version_id = f"{args.engine}-{time.strftime('%Y%m%d-%H%M%S')}-{identity_hash}"
    existing_ids = {v["version_id"] for v in ledger["versions"]}
    if version_id in existing_ids:
        version_id = f"{version_id}-{sum(1 for i in existing_ids if i.startswith(version_id)) + 1}"
    entry = {
        "version_id": version_id,
        "model_id": args.model_id,
        "model_revision": args.model_revision,
        "engine": args.engine,
        "engine_image": args.engine_image,
        "config": config,
        "config_hash": _config_hash(config),
        "git_commit": _git_commit(),
        "benchmark_ref": args.benchmark_ref,
        "k8s_deployment": args.k8s_deployment,
        "k8s_container": args.k8s_container,
        "status": "registered",
        "registered_at": _now(),
        "notes": args.notes,
    }
    ledger["versions"].append(entry)
    _save_ledger(ledger)
    print(f"Registered {version_id}")
    print(json.dumps(entry, indent=2))


def cmd_list(args):
    ledger = _load_ledger()
    rows = ledger["versions"]
    if args.engine:
        rows = [v for v in rows if v["engine"] == args.engine]
    active_by_group = ledger.get("active", {})
    active_ids = set(active_by_group.values())
    print(f"{'version_id':<32} {'engine':<8} {'status':<12} {'active_group':<12} {'registered_at'}")
    for v in rows:
        active_group = next((g for g, vid in active_by_group.items() if vid == v["version_id"]), "-")
        print(f"{v['version_id']:<32} {v['engine']:<8} {v['status']:<12} {active_group:<12} {v['registered_at']}")
    if not rows:
        print("(no versions registered yet)")


def cmd_show(args):
    ledger = _load_ledger()
    v = _find_version(ledger, args.version_id)
    print(json.dumps(v, indent=2))


def _apply_k8s(entry: dict, apply: bool):
    deployment, container = entry.get("k8s_deployment"), entry.get("k8s_container")
    if not deployment or not container:
        print("  (no k8s_deployment/k8s_container recorded for this version -- skipping kubectl patch)")
        return
    cmd = ["kubectl", "set", "image", f"deployment/{deployment}", f"{container}={entry['engine_image']}"]
    print(f"  kubectl command: {' '.join(cmd)}")
    if apply:
        subprocess.run(cmd, check=True)
    else:
        print("  (dry-run: pass --apply to actually run this against your cluster)")


def cmd_promote(args):
    ledger = _load_ledger()
    entry = _find_version(ledger, args.version_id)

    previous_active_id = ledger.get("active", {}).get(args.group)
    if previous_active_id:
        prev = _find_version(ledger, previous_active_id)
        if prev["status"] == "active":
            prev["status"] = "retired"

    entry["status"] = "canary" if args.group == "canary" else "active"
    entry["promoted_at"] = _now()
    ledger.setdefault("active", {})[args.group] = entry["version_id"]
    ledger.setdefault("history", {}).setdefault(args.group, []).append({
        "version_id": entry["version_id"], "promoted_at": entry["promoted_at"],
    })
    _save_ledger(ledger)

    print(f"Promoted {entry['version_id']} to group='{args.group}' (previous: {previous_active_id or 'none'})")
    _apply_k8s(entry, args.apply)


def cmd_rollback(args):
    ledger = _load_ledger()
    history = ledger.get("history", {}).get(args.group, [])

    if args.to:
        target_id = args.to
    else:
        if len(history) < 2:
            raise SystemExit(f"error: no earlier version to roll back to for group '{args.group}'")
        target_id = history[-2]["version_id"]

    target = _find_version(ledger, target_id)
    current_id = ledger.get("active", {}).get(args.group)
    if current_id:
        current = _find_version(ledger, current_id)
        if current["status"] in ("active", "canary"):
            current["status"] = "rolled_back"

    target["status"] = "canary" if args.group == "canary" else "active"
    ledger.setdefault("active", {})[args.group] = target["version_id"]
    ledger.setdefault("history", {}).setdefault(args.group, []).append({
        "version_id": target["version_id"], "promoted_at": _now(), "rollback": True,
    })
    _save_ledger(ledger)

    print(f"Rolled back group='{args.group}' from {current_id} to {target['version_id']}")
    _apply_k8s(target, args.apply)


def cmd_history(args):
    ledger = _load_ledger()
    for h in ledger.get("history", {}).get(args.group, []):
        marker = " [ROLLBACK]" if h.get("rollback") else ""
        print(f"{h['promoted_at']}  {h['version_id']}{marker}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("register", help="register a new serving configuration version")
    r.add_argument("--engine", required=True, choices=["vllm", "sglang", "trtllm"])
    r.add_argument("--model-id", default="OpenOneRec/OneRec-8B-pro")
    r.add_argument("--model-revision", default="main")
    r.add_argument("--engine-image", required=True)
    r.add_argument("--tp", type=int, default=1)
    r.add_argument("--dtype", default="bf16")
    r.add_argument("--max-model-len", type=int, default=8192)
    r.add_argument("--extra-args", default="")
    r.add_argument("--benchmark-ref", default="", help="path to the report this version's numbers came from")
    r.add_argument("--k8s-deployment", default="", help="k8s Deployment name to patch on promote (optional)")
    r.add_argument("--k8s-container", default="", help="container name within that Deployment (optional)")
    r.add_argument("--notes", default="")
    r.set_defaults(func=cmd_register)

    l = sub.add_parser("list", help="list all registered versions")
    l.add_argument("--engine", default=None)
    l.set_defaults(func=cmd_list)

    s = sub.add_parser("show", help="show full detail for one version")
    s.add_argument("version_id")
    s.set_defaults(func=cmd_show)

    pr = sub.add_parser("promote", help="make a version active for a traffic group")
    pr.add_argument("version_id")
    pr.add_argument("--group", default="stable", choices=["stable", "canary"])
    pr.add_argument("--apply", action="store_true", help="actually run kubectl (default: dry-run/print only)")
    pr.set_defaults(func=cmd_promote)

    rb = sub.add_parser("rollback", help="roll a traffic group back to its previous (or a specific) version")
    rb.add_argument("--group", default="stable", choices=["stable", "canary"])
    rb.add_argument("--to", default=None, help="specific version_id to roll back to (default: previous)")
    rb.add_argument("--apply", action="store_true")
    rb.set_defaults(func=cmd_rollback)

    h = sub.add_parser("history", help="show promotion history for a traffic group")
    h.add_argument("--group", default="stable", choices=["stable", "canary"])
    h.set_defaults(func=cmd_history)

    return p


def main():
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    sys.exit(main() or 0)
