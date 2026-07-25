#!/usr/bin/env python3
"""Aggregates every results/<run_name>/bench_{latency,throughput}.json produced
by benchmark_client.py into a single architect-ready Markdown report with
comparison tables and charts.

Usage:
    python3 bench/generate_report.py --results-dir results --out-dir results/report
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd


def load_all(results_dir: Path) -> dict[str, dict]:
    runs = {}
    for run_dir in sorted(results_dir.iterdir()):
        if not run_dir.is_dir():
            continue
        run = {"run_name": run_dir.name}
        lat_f = run_dir / "bench_latency.json"
        thr_f = run_dir / "bench_throughput.json"
        if lat_f.exists():
            run["latency"] = json.loads(lat_f.read_text())
        if thr_f.exists():
            run["throughput"] = json.loads(thr_f.read_text())
        if "latency" in run or "throughput" in run:
            runs[run_dir.name] = run
    return runs


def build_latency_table(runs: dict) -> pd.DataFrame:
    rows = []
    for name, run in runs.items():
        lat = run.get("latency")
        if not lat:
            continue
        engine = lat.get("engine", "?")
        for key, r in lat["results"].items():
            rows.append({
                "run": name,
                "engine": engine,
                "input_len": r["input_len_target"],
                "output_len": r["output_len_target"],
                "ttft_p50_ms": r["ttft_s"]["p50"] * 1000 if r["ttft_s"]["p50"] else None,
                "ttft_p99_ms": r["ttft_s"]["p99"] * 1000 if r["ttft_s"]["p99"] else None,
                "e2e_p50_s": r["e2e_s"]["p50"],
                "e2e_p99_s": r["e2e_s"]["p99"],
                "mean_itl_ms": r["mean_itl_s"] * 1000 if r["mean_itl_s"] else None,
                "n_failed": r["n_failed"],
            })
    return pd.DataFrame(rows)


def collect_failure_notes(runs: dict) -> list[str]:
    notes = []
    for name, run in runs.items():
        thr = run.get("throughput")
        if not thr:
            continue
        for key, r in thr["results"].items():
            if r.get("n_failed", 0) > 0 and r.get("top_errors"):
                errs = ", ".join(f"`{e['error']}` x{e['count']}" for e in r["top_errors"])
                notes.append(f"- `{name}` @ concurrency={r['concurrency']}: {r['n_failed']} failed -- {errs}")
    return notes


def build_throughput_table(runs: dict) -> pd.DataFrame:
    rows = []
    for name, run in runs.items():
        thr = run.get("throughput")
        if not thr:
            continue
        engine = thr.get("engine", "?")
        for key, r in thr["results"].items():
            rows.append({
                "run": name,
                "engine": engine,
                "concurrency": r["concurrency"],
                "output_tok_s": r["output_tokens_per_s"],
                "output_tok_s_per_gpu": r["output_tokens_per_s_per_gpu"],
                "req_s": r["requests_per_s"],
                "ttft_p50_ms": r["ttft_s"]["p50"] * 1000 if r["ttft_s"]["p50"] else None,
                "e2e_p50_s": r["e2e_s"]["p50"],
                "avg_gpu_util_pct": (r.get("gpu") or {}).get("avg_util_pct"),
                "avg_power_w": (r.get("gpu") or {}).get("avg_power_w"),
                "tok_s_per_watt": (
                    r["output_tokens_per_s"] / r["gpu"]["avg_power_w"]
                    if r.get("output_tokens_per_s") and (r.get("gpu") or {}).get("avg_power_w")
                    else None
                ),
                "n_failed": r["n_failed"],
            })
    return pd.DataFrame(rows)


def plot_latency_throughput_curve(df_thr: pd.DataFrame, out_dir: Path):
    if df_thr.empty:
        return None
    fig, ax = plt.subplots(figsize=(8, 6))
    for run, g in df_thr.groupby("run"):
        g = g.sort_values("concurrency")
        ax.plot(g["output_tok_s"], g["e2e_p50_s"], marker="o", label=run)
    ax.set_xlabel("Aggregate output throughput (tokens/s)")
    ax.set_ylabel("Median end-to-end latency (s)")
    ax.set_title("Latency vs. Throughput (per serving configuration)")
    ax.legend(fontsize=8)
    ax.grid(True, alpha=0.3)
    path = out_dir / "latency_vs_throughput.png"
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return path


def plot_throughput_scaling(df_thr: pd.DataFrame, out_dir: Path):
    if df_thr.empty:
        return None
    fig, ax = plt.subplots(figsize=(8, 6))
    for run, g in df_thr.groupby("run"):
        g = g.sort_values("concurrency")
        ax.plot(g["concurrency"], g["output_tok_s"], marker="o", label=run)
    ax.set_xlabel("Concurrency (in-flight requests)")
    ax.set_ylabel("Aggregate output throughput (tokens/s)")
    ax.set_title("Throughput Scaling vs. Concurrency")
    ax.set_xscale("log", base=2)
    ax.legend(fontsize=8)
    ax.grid(True, alpha=0.3)
    path = out_dir / "throughput_scaling.png"
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return path


def plot_ttft_vs_concurrency(df_thr: pd.DataFrame, out_dir: Path):
    if df_thr.empty:
        return None
    fig, ax = plt.subplots(figsize=(8, 6))
    for run, g in df_thr.groupby("run"):
        g = g.sort_values("concurrency")
        ax.plot(g["concurrency"], g["ttft_p50_ms"], marker="o", label=run)
    ax.set_xlabel("Concurrency (in-flight requests)")
    ax.set_ylabel("Median TTFT (ms)")
    ax.set_title("Time-to-First-Token vs. Concurrency")
    ax.set_xscale("log", base=2)
    ax.legend(fontsize=8)
    ax.grid(True, alpha=0.3)
    path = out_dir / "ttft_vs_concurrency.png"
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return path


def best_by(df: pd.DataFrame, col: str, ascending: bool):
    if df.empty:
        return None
    return df.sort_values(col, ascending=ascending).iloc[0]


def render_markdown(runs, df_lat, df_thr, chart_paths, out_dir: Path, results_dir: Path) -> str:
    lines = []
    lines.append("# OneRec-8B-Pro Serving Benchmark Report\n")
    lines.append(f"_Generated automatically from `{results_dir}` -- do not hand-edit; re-run "
                  f"`bench/generate_report.py` after any new benchmark run._\n")

    lines.append("## Runs included\n")
    lines.append("| Run | Engine |")
    lines.append("|---|---|")
    for name, run in sorted(runs.items()):
        engine = (run.get("throughput") or run.get("latency") or {}).get("engine", "?")
        lines.append(f"| `{name}` | {engine} |")
    lines.append("")

    if not df_thr.empty:
        lines.append("## Throughput / concurrency sweep\n")
        best_thr = best_by(df_thr, "output_tok_s", ascending=False)
        best_lat_at_load = best_by(df_thr[df_thr["concurrency"] <= 8], "ttft_p50_ms", ascending=True) \
            if not df_thr[df_thr["concurrency"] <= 8].empty else None
        if best_thr is not None:
            lines.append(f"- **Highest peak throughput:** `{best_thr['run']}` "
                          f"({best_thr['output_tok_s']:.0f} output tok/s at concurrency={int(best_thr['concurrency'])})\n")
        if best_lat_at_load is not None:
            lines.append(f"- **Best low-concurrency TTFT:** `{best_lat_at_load['run']}` "
                          f"({best_lat_at_load['ttft_p50_ms']:.0f} ms p50 at concurrency={int(best_lat_at_load['concurrency'])})\n")
        lines.append("")
        lines.append(df_thr.round(2).to_markdown(index=False))
        lines.append("")
        for name, path in chart_paths.items():
            if path:
                rel = Path(path).relative_to(out_dir.parent) if out_dir.parent in Path(path).parents else path.name
                lines.append(f"![{name}]({path.name})\n")

    if not df_lat.empty:
        lines.append("## Single-user latency sweep (concurrency = 1)\n")
        lines.append(df_lat.round(3).to_markdown(index=False))
        lines.append("")

    failure_notes = collect_failure_notes(runs)
    if failure_notes:
        lines.append("## Data quality flags: request failures under load\n")
        lines.append(
            "Non-zero `n_failed` at a concurrency level means the numbers at "
            "**that level and above should not be trusted for capacity "
            "planning** until root-caused (see `docs/RUNBOOK.md` -> "
            "'Interpreting failed/anomalous throughput results'). Breakdown:\n"
        )
        lines.extend(failure_notes)
        lines.append("")

    lines.append("## How to read this report\n")
    lines.append(
        "- **TTFT (time-to-first-token)** approximates perceived responsiveness for an "
        "interactive/streaming UI.\n"
        "- **E2E latency** is full request completion time (prefill + decode of all output tokens).\n"
        "- **Throughput (tokens/s)** is aggregate *output* token throughput across all "
        "concurrent in-flight requests at that load level -- this is what determines how "
        "many GPUs/replicas you need to serve a given QPS target.\n"
        "- **tok/s/GPU** normalizes throughput by GPU count so tensor-parallel configs "
        "can be compared fairly against single-GPU replicas.\n"
        "- See `docs/BENCHMARK_METHODOLOGY.md` for full methodology and `docs/SERVING_OPTIONS.md` "
        "for qualitative engine trade-offs.\n"
    )
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results-dir", default="results")
    ap.add_argument("--out-dir", default="results/report")
    args = ap.parse_args()

    results_dir = Path(args.results_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    runs = load_all(results_dir)
    if not runs:
        print(f"No results found under {results_dir}. Run the benchmark matrix first.")
        return

    df_lat = build_latency_table(runs)
    df_thr = build_throughput_table(runs)

    charts = {
        "latency_vs_throughput": plot_latency_throughput_curve(df_thr, out_dir),
        "throughput_scaling": plot_throughput_scaling(df_thr, out_dir),
        "ttft_vs_concurrency": plot_ttft_vs_concurrency(df_thr, out_dir),
    }

    df_lat.to_csv(out_dir / "latency_table.csv", index=False)
    df_thr.to_csv(out_dir / "throughput_table.csv", index=False)

    md = render_markdown(runs, df_lat, df_thr, charts, out_dir, results_dir)
    report_path = out_dir / "REPORT.md"
    report_path.write_text(md)
    print(f"Wrote {report_path}")
    print(f"Wrote {out_dir / 'latency_table.csv'}")
    print(f"Wrote {out_dir / 'throughput_table.csv'}")
    for name, p in charts.items():
        if p:
            print(f"Wrote {p}")


if __name__ == "__main__":
    main()
