#!/usr/bin/env python3
"""End-to-end orchestrator: for every entry in configs/matrix.yaml, start the
server, smoke-test it, run the latency + throughput benchmark suites, tear
it down, and move on -- continuing past failures so one broken config
doesn't block the rest of the matrix. Finishes by invoking generate_report.py.

Usage (run from repo root, with the orchestration venv activated):
    python3 bench/run_matrix.py --matrix configs/matrix.yaml
    python3 bench/run_matrix.py --matrix configs/matrix.yaml --only vllm-tp1-bf16,sglang-tp1-bf16
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"

SERVE_SCRIPT = {
    "vllm": SCRIPTS_DIR / "10_serve_vllm.sh",
    "sglang": SCRIPTS_DIR / "11_serve_sglang.sh",
    "trtllm": SCRIPTS_DIR / "12_serve_trtllm.sh",
}
DEFAULT_PORT = {"vllm": 8000, "sglang": 8001, "trtllm": 8002}


def sh(cmd: list[str], log_path: Path | None = None, timeout: int | None = None) -> subprocess.CompletedProcess:
    print(f"$ {' '.join(cmd)}")
    if log_path:
        with open(log_path, "a") as fh:
            fh.write(f"\n$ {' '.join(cmd)}\n")
            proc = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT, timeout=timeout, text=True)
            return proc
    return subprocess.run(cmd, timeout=timeout)


def load_matrix(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def merged_config(run: dict, defaults: dict) -> dict:
    cfg = {**run}
    cfg["max_model_len"] = run.get("max_model_len", defaults.get("max_model_len", 8192))
    cfg["latency"] = {**defaults.get("latency", {}), **run.get("latency", {})}
    cfg["throughput"] = {**defaults.get("throughput", {}), **run.get("throughput", {})}
    return cfg


def start_server(cfg: dict, log_dir: Path) -> tuple[bool, int]:
    engine = cfg["engine"]
    script = SERVE_SCRIPT[engine]
    port = DEFAULT_PORT[engine]
    cmd = [
        str(script),
        "--run-name", cfg["name"],
        "--tp", str(cfg["tp"]),
        "--gpus", cfg["gpus"],
        "--dtype", cfg["dtype"],
        "--max-model-len", str(cfg["max_model_len"]),
        "--port", str(port),
    ]
    log_path = log_dir / f"{cfg['name']}.serve.log"
    proc = sh(cmd, log_path=log_path, timeout=3600)
    return proc.returncode == 0, port


def smoke_test(port: int, log_dir: Path, name: str) -> bool:
    log_path = log_dir / f"{name}.smoke.log"
    proc = sh([str(SCRIPTS_DIR / "20_smoke_test.sh"), "--port", str(port)], log_path=log_path, timeout=120)
    return proc.returncode == 0


def stop_server(engine: str, name: str):
    sh([str(SCRIPTS_DIR / "90_stop_serving.sh"), f"{engine}-{name}"], timeout=60)


def run_bench(mode: str, cfg: dict, port: int, results_dir: Path, model_dir: str, log_dir: Path) -> bool:
    latc, thrc = cfg["latency"], cfg["throughput"]
    base = [
        sys.executable, str(Path(__file__).parent / "benchmark_client.py"),
        "--mode", mode,
        "--base-url", f"http://localhost:{port}",
        "--run-name", cfg["name"],
        "--engine", cfg["engine"],
        "--output-dir", str(results_dir),
        "--tokenizer-path", model_dir,
        "--gpu-ids", cfg["gpus"],
    ]
    if mode == "latency":
        base += [
            "--input-lens", latc["input_lens"],
            "--output-lens", latc["output_lens"],
            "--repetitions", str(latc["repetitions"]),
            "--warmup-requests", str(latc["warmup_requests"]),
        ]
        timeout = 3600
    else:
        base += [
            "--concurrency-levels", thrc["concurrency_levels"],
            "--fixed-input-len", str(thrc["fixed_input_len"]),
            "--fixed-output-len", str(thrc["fixed_output_len"]),
            "--warmup-seconds", str(thrc["warmup_seconds"]),
            "--measure-seconds", str(thrc["measure_seconds"]),
        ]
        timeout = 7200

    log_path = log_dir / f"{cfg['name']}.bench_{mode}.log"
    proc = sh(base, log_path=log_path, timeout=timeout)
    return proc.returncode == 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--matrix", default=str(REPO_ROOT / "configs" / "matrix.yaml"))
    ap.add_argument("--results-dir", default=str(REPO_ROOT / "results"))
    ap.add_argument("--log-dir", default=str(REPO_ROOT / "logs"))
    ap.add_argument("--model-dir", default=None, help="defaults to $MODEL_DIR from scripts/env.sh")
    ap.add_argument("--only", default=None, help="comma list of run names to run (default: all)")
    ap.add_argument("--skip-latency", action="store_true")
    ap.add_argument("--skip-throughput", action="store_true")
    args = ap.parse_args()

    model_dir = args.model_dir or os.environ.get("MODEL_DIR")
    if not model_dir:
        print("ERROR: --model-dir not given and $MODEL_DIR not set. Source scripts/env.sh first.")
        sys.exit(1)

    matrix = load_matrix(Path(args.matrix))
    defaults = matrix.get("defaults", {})
    runs = matrix["runs"]
    if args.only:
        wanted = set(args.only.split(","))
        runs = [r for r in runs if r["name"] in wanted]

    results_dir = Path(args.results_dir)
    log_dir = Path(args.log_dir)
    results_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    summary = []
    for run in runs:
        cfg = merged_config(run, defaults)
        name = cfg["name"]
        print("\n" + "=" * 70)
        print(f" Run: {name}  (engine={cfg['engine']}, tp={cfg['tp']}, dtype={cfg['dtype']})")
        print("=" * 70)
        t0 = time.time()
        entry = {"name": name, "engine": cfg["engine"], "tp": cfg["tp"], "dtype": cfg["dtype"]}
        try:
            ok, port = start_server(cfg, log_dir)
            if not ok:
                entry["status"] = "server_failed_to_start"
                print(f"!! {name}: server failed to start, skipping. See {log_dir}/{name}.serve.log")
                continue

            if not smoke_test(port, log_dir, name):
                entry["status"] = "smoke_test_failed"
                print(f"!! {name}: smoke test failed, skipping benchmarks. See {log_dir}/{name}.smoke.log")
                continue

            lat_ok = args.skip_latency or run_bench("latency", cfg, port, results_dir, model_dir, log_dir)
            thr_ok = args.skip_throughput or run_bench("throughput", cfg, port, results_dir, model_dir, log_dir)

            entry["status"] = "ok" if (lat_ok and thr_ok) else "partial_failure"
            entry["latency_ok"] = lat_ok
            entry["throughput_ok"] = thr_ok
        except Exception as exc:  # noqa: BLE001
            entry["status"] = f"exception: {exc}"
            print(f"!! {name}: exception: {exc}")
        finally:
            stop_server(cfg["engine"], name)
            entry["duration_s"] = round(time.time() - t0, 1)
            summary.append(entry)
            print(f"-- {name}: done in {entry['duration_s']:.0f}s, status={entry.get('status')}")

    summary_path = results_dir / "run_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2))
    print("\n" + "=" * 70)
    print(f"Matrix complete. Summary written to {summary_path}")
    for e in summary:
        print(f"  {e['name']:24s} {e.get('status')}")
    print("=" * 70)

    print("\nGenerating report...")
    sh([sys.executable, str(Path(__file__).parent / "generate_report.py"),
        "--results-dir", str(results_dir), "--out-dir", str(results_dir / "report")])


if __name__ == "__main__":
    main()
