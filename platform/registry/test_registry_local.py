#!/usr/bin/env python3
"""Local smoke test for the registry CLI's register/list/promote/rollback
logic, against a temporary ledger file (never touches the real
ledger.json). Run: python3 platform/registry/test_registry_local.py
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import registry  # noqa: E402

PASSED, FAILED = 0, 0


def check(name: str, cond: bool, detail: str = ""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED += 1
        print(f"  [FAIL] {name}  {detail}")


def ns(**kwargs) -> argparse.Namespace:
    return argparse.Namespace(**kwargs)


def main():
    with tempfile.TemporaryDirectory() as tmp:
        registry.LEDGER_PATH = Path(tmp) / "ledger.json"

        print("1. Register two versions")
        registry.cmd_register(ns(
            engine="vllm", model_id="OpenOneRec/OneRec-8B-pro", model_revision="main",
            engine_image="vllm/vllm-openai:v0.12.0", tp=1, dtype="bf16", max_model_len=8192,
            extra_args="", benchmark_ref="results/report/REPORT.md",
            k8s_deployment="onerec-vllm", k8s_container="vllm", notes="v1",
        ))
        registry.cmd_register(ns(
            engine="vllm", model_id="OpenOneRec/OneRec-8B-pro", model_revision="main",
            engine_image="vllm/vllm-openai:v0.12.1", tp=1, dtype="bf16", max_model_len=8192,
            extra_args="", benchmark_ref="", k8s_deployment="onerec-vllm", k8s_container="vllm", notes="v2",
        ))
        ledger = registry._load_ledger()
        check("two versions registered", len(ledger["versions"]) == 2, str(ledger))
        v1, v2 = ledger["versions"][0]["version_id"], ledger["versions"][1]["version_id"]

        print("\n2. Promote v1 to stable")
        registry.cmd_promote(ns(version_id=v1, group="stable", apply=False))
        ledger = registry._load_ledger()
        check("v1 active on stable", ledger["active"]["stable"] == v1, str(ledger["active"]))
        check("v1 status active", registry._find_version(ledger, v1)["status"] == "active")

        print("\n3. Promote v2 to stable (v1 should become retired)")
        registry.cmd_promote(ns(version_id=v2, group="stable", apply=False))
        ledger = registry._load_ledger()
        check("v2 now active on stable", ledger["active"]["stable"] == v2)
        check("v1 retired", registry._find_version(ledger, v1)["status"] == "retired")

        print("\n4. Rollback stable (should go back to v1)")
        registry.cmd_rollback(ns(group="stable", to=None, apply=False))
        ledger = registry._load_ledger()
        check("rolled back to v1", ledger["active"]["stable"] == v1, str(ledger["active"]))
        check("v2 marked rolled_back", registry._find_version(ledger, v2)["status"] == "rolled_back")

        print("\n5. Promote v2 to canary independently of stable")
        registry.cmd_promote(ns(version_id=v2, group="canary", apply=False))
        ledger = registry._load_ledger()
        check("stable and canary track independently", ledger["active"]["stable"] == v1 and ledger["active"]["canary"] == v2,
              str(ledger["active"]))

        print("\n6. Rollback with no history raises a clean error")
        try:
            registry.cmd_rollback(ns(group="canary", to=None, apply=False))
            check("rollback with <2 history entries raises SystemExit", False, "did not raise")
        except SystemExit:
            check("rollback with <2 history entries raises SystemExit", True)

    print(f"\n{PASSED} passed, {FAILED} failed")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
