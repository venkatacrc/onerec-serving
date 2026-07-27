#!/usr/bin/env python3
"""Cost & capacity planner: turns the benchmark report's measured
tok/s/GPU numbers + configs/cost_assumptions.yaml into a GPU/replica count,
$/month, tok/s/$, and a reserved-vs-on-demand split for a target QPS.

Every dollar figure here is only as good as configs/cost_assumptions.yaml
-- see docs/CAPACITY_AND_COST.md §1 for exactly which placeholders need a
real number before this should inform an actual purchase decision. What
IS real: the tok/s/GPU ceiling, which comes directly from
results/report/throughput_table.csv (i.e. from actually running the
model on this hardware), restricted to concurrency levels with zero
request failures (see docs/RUNBOOK.md "Interpreting failed/anomalous
throughput results").

Usage:
    python3 bench/capacity_planner.py --target-qps 50 --avg-output-tokens 256
    python3 bench/capacity_planner.py --run vllm-tp1-bf16 --target-qps 200 --avg-output-tokens 128 --out results/report/CAPACITY_PLAN.md
"""
from __future__ import annotations

import argparse
import math
import re
from pathlib import Path

import pandas as pd
import yaml


def load_throughput_table(report_dir: Path) -> pd.DataFrame:
    path = report_dir / "throughput_table.csv"
    if not path.exists():
        raise SystemExit(f"error: {path} not found -- run bench/generate_report.py first")
    return pd.read_csv(path)


def infer_tp_from_run_name(run_name: str) -> int:
    m = re.search(r"tp(\d+)", run_name)
    return int(m.group(1)) if m else 1


def pick_best_config(df: pd.DataFrame, requested_run: str | None) -> tuple[str, pd.Series]:
    """Picks the (run, row) with the highest RELIABLE (n_failed == 0)
    output_tok_s_per_gpu -- i.e. lets the data answer "replicate vs
    shard" and "which engine" automatically, the same conclusion
    docs/PRODUCTION_ARCHITECTURE.md §4 draws by hand from this report."""
    reliable = df[df["n_failed"] == 0]
    if reliable.empty:
        raise SystemExit("error: no concurrency level in the report has zero failures; re-run the benchmark "
                          "(see docs/RUNBOOK.md) before trusting any capacity numbers.")
    if requested_run:
        reliable = reliable[reliable["run"] == requested_run]
        if reliable.empty:
            raise SystemExit(f"error: run '{requested_run}' has no reliable (n_failed==0) rows in the report")
    best_idx = reliable["output_tok_s_per_gpu"].idxmax()
    best_row = reliable.loc[best_idx]
    return best_row["run"], best_row


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--report-dir", default="results/report")
    p.add_argument("--cost-assumptions", default="configs/cost_assumptions.yaml")
    p.add_argument("--run", default=None, help="restrict to this run (default: auto-pick best reliable tok/s/GPU across all runs)")
    p.add_argument("--target-qps", type=float, required=True, help="target AVERAGE requests/sec to provision for")
    p.add_argument("--avg-output-tokens", type=float, required=True, help="avg completion length for your real traffic (not the synthetic default) -- see docs/BENCHMARK_METHODOLOGY.md §3")
    p.add_argument("--out", default=None, help="optional path to also write this report as Markdown")
    args = p.parse_args()

    report_dir = Path(args.report_dir)
    df = load_throughput_table(report_dir)
    costs = yaml.safe_load(Path(args.cost_assumptions).read_text())

    run_name, row = pick_best_config(df, args.run)
    tp = infer_tp_from_run_name(run_name)
    ceiling_tok_s_per_gpu = float(row["output_tok_s_per_gpu"])
    measured_power_w_per_gpu = float(row["avg_power_w"]) / max(tp, 1)

    util_target = costs["utilization_target_pct"] / 100.0
    effective_tok_s_per_gpu = ceiling_tok_s_per_gpu * util_target

    required_tok_s = args.target_qps * args.avg_output_tokens
    required_gpus_raw = required_tok_s / effective_tok_s_per_gpu
    required_replicas = math.ceil(required_gpus_raw / tp)
    provisioned_gpus = required_replicas * tp
    provisioned_capacity_tok_s = provisioned_gpus * effective_tok_s_per_gpu

    on_demand_hourly = provisioned_gpus * costs["on_demand_usd_per_gpu_hour"]
    reserved_1yr_hourly = provisioned_gpus * costs["reserved_1yr_usd_per_gpu_hour"]
    reserved_3yr_hourly = provisioned_gpus * costs["reserved_3yr_usd_per_gpu_hour"]

    hours_per_month = 24 * 30
    cost_per_million_tokens_on_demand = (on_demand_hourly / (provisioned_capacity_tok_s * 3600)) * 1e6
    cost_per_million_tokens_reserved = (reserved_1yr_hourly / (provisioned_capacity_tok_s * 3600)) * 1e6

    power_cost_hourly = provisioned_gpus * (measured_power_w_per_gpu / 1000.0) * costs["power_usd_per_kwh"]

    # --- Reserved vs on-demand blended strategy for a peaky traffic shape.
    peak_ratio = costs["peak_to_average_qps_ratio"]
    peak_gpus_needed = math.ceil((provisioned_gpus * peak_ratio))
    reserved_coverage = costs["reserved_coverage_of_average_pct"] / 100.0
    reserved_gpus = math.ceil(provisioned_gpus * reserved_coverage)
    burst_on_demand_gpus_at_peak = max(0, peak_gpus_needed - reserved_gpus)

    always_on_reserved_hourly = reserved_gpus * costs["reserved_1yr_usd_per_gpu_hour"]
    peak_burst_hourly_extra = burst_on_demand_gpus_at_peak * costs["on_demand_usd_per_gpu_hour"]

    all_on_demand_monthly = on_demand_hourly * hours_per_month
    # Illustrative: assume peak conditions hold 6h/day (25% of hours) -- see docs/CAPACITY_AND_COST.md §3 for how to replace this with your real diurnal curve.
    peak_hours_fraction = 0.25
    blended_monthly = (always_on_reserved_hourly * hours_per_month
                        + peak_burst_hourly_extra * hours_per_month * peak_hours_fraction)

    lines = []
    lines.append("# Capacity & Cost Plan\n")
    lines.append(f"_Generated from `{run_name}` (engine config with the highest RELIABLE tok/s/GPU in the report) "
                  f"-- see docs/CAPACITY_AND_COST.md for methodology and which inputs are still placeholders._\n")
    lines.append("## Inputs\n")
    lines.append(f"- Chosen config: **{run_name}** (TP={tp}), measured ceiling **{ceiling_tok_s_per_gpu:.1f} tok/s/GPU** "
                  f"at the best reliable (zero-failure) concurrency level in the report")
    lines.append(f"- Utilization target: **{costs['utilization_target_pct']}%** of ceiling "
                  f"(effective **{effective_tok_s_per_gpu:.1f} tok/s/GPU** planned) -- leaves burst headroom instead of planning at the ragged edge")
    lines.append(f"- Target average load: **{args.target_qps} req/s** x **{args.avg_output_tokens} avg output tokens** "
                  f"= **{required_tok_s:.0f} tok/s** required aggregate throughput\n")
    lines.append("## Sizing\n")
    lines.append(f"- Required GPUs (raw): {required_gpus_raw:.2f}")
    lines.append(f"- Provisioned: **{required_replicas} replicas x TP={tp} = {provisioned_gpus} GPUs** "
                  f"(supports {provisioned_capacity_tok_s:.0f} tok/s at target utilization)\n")
    lines.append("## Cost (USD, from configs/cost_assumptions.yaml -- REPLACE placeholders before using for real budgeting)\n")
    lines.append(f"- On-demand: ${on_demand_hourly:,.2f}/hr -> **${all_on_demand_monthly:,.0f}/month**")
    lines.append(f"- 1yr reserved (100% reserved): ${reserved_1yr_hourly:,.2f}/hr -> **${reserved_1yr_hourly * hours_per_month:,.0f}/month**")
    lines.append(f"- 3yr reserved (100% reserved): ${reserved_3yr_hourly:,.2f}/hr -> **${reserved_3yr_hourly * hours_per_month:,.0f}/month**")
    lines.append(f"- Power only (on-prem cross-check, {measured_power_w_per_gpu:.0f}W/GPU measured): ${power_cost_hourly:,.2f}/hr\n")
    lines.append(f"- Cost per million output tokens: on-demand **${cost_per_million_tokens_on_demand:.2f}**, "
                  f"1yr-reserved **${cost_per_million_tokens_reserved:.2f}**\n")
    lines.append("## Reserved vs. on-demand blended strategy (peaky traffic)\n")
    lines.append(f"- Assumed peak/average ratio: **{peak_ratio}x** (placeholder -- replace with your measured diurnal curve)")
    lines.append(f"- Peak requires ~{peak_gpus_needed} GPUs; reserve **{reserved_gpus} GPUs** "
                  f"({costs['reserved_coverage_of_average_pct']}% of average load) always-on, "
                  f"burst **{burst_on_demand_gpus_at_peak} GPUs** on-demand during peak windows")
    lines.append(f"- Illustrative blended monthly (assuming peak conditions {peak_hours_fraction*100:.0f}% of hours/day): "
                  f"**${blended_monthly:,.0f}/month** vs. **${all_on_demand_monthly:,.0f}/month** if 100% on-demand "
                  f"sized to peak year-round\n")

    report_text = "\n".join(lines)
    print(report_text)

    if args.out:
        Path(args.out).write_text(report_text + "\n")
        print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
