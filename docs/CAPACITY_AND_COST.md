# Capacity & Cost Planning

`bench/capacity_planner.py` turns the benchmark's measured tok/s/GPU
numbers into a GPU count, $/month estimate, and a reserved-vs-on-demand
strategy for a target request rate. This doc covers the methodology, what
in the current output is real vs. a placeholder, and how open-loop load
testing fits in.

## 1. What's real today vs. what's still a placeholder

| Input | Status | Source |
|---|---|---|
| tok/s/GPU ceiling per engine/TP config | **Real, measured** | `results/report/throughput_table.csv`, restricted to concurrency levels with `n_failed == 0` |
| Measured GPU power draw | **Real, measured** | Same report, `avg_power_w` column (sampled via `nvidia-smi` during the run) |
| GPU $/hour (on-demand / reserved) | **Placeholder** | `configs/cost_assumptions.yaml` — illustrative list-price-class numbers, not a quote. Replace with your actual negotiated rate before this informs a purchase. |
| Target QPS, avg output tokens | **Your input, per invocation** | `--target-qps` / `--avg-output-tokens` CLI args — should come from real production traffic logs, not guessed |
| Peak-to-average ratio | **Placeholder** | `configs/cost_assumptions.yaml` `peak_to_average_qps_ratio` — replace with your measured daily/seasonal curve (see §3) |

Running `bench/capacity_planner.py` today already gives a defensible GPU
count and engine/TP recommendation (see §2) because that half of the
input is real hardware data. It does **not** give you a defensible dollar
figure until you fill in the two placeholder categories above with real
pricing and real traffic data — the tool prints this caveat every run.

## 2. Replicate vs. shard, and engine choice — picked automatically

`capacity_planner.py` doesn't take the engine/TP config as a leap of faith
— it scans every row in `results/report/throughput_table.csv`, throws out
any row with `n_failed > 0` (see `docs/RUNBOOK.md` "Interpreting
failed/anomalous throughput results" — those rows are contaminated by a
client-side connection-pool issue, not genuine engine behavior), and picks
whichever remaining (engine, TP) combination has the highest tok/s/GPU.
As of the current report, that's `trtllm-tp1-bf16` (388.7 tok/s/GPU) by a
narrow margin over `vllm-tp1-bf16` (371.4 tok/s/GPU) — both TP=1
("replicate"), both far ahead of any TP>1 config, which is the same
conclusion `docs/PRODUCTION_ARCHITECTURE.md` §4 draws by hand. Use
`--run <name>` to force a specific config instead (e.g. to compare two
candidates side by side).

**Re-running the full 8-config matrix after the two root-cause fixes**
(`docs/RUNBOOK.md` — docker `--gpus` quoting, SGLang `distro` module) will
widen the reliable (zero-failure) dataset this picks from, which is worth
doing before finalizing a fleet-scale purchase even though the conclusion
is unlikely to flip given how large the current TP1-vs-TP8 gap already is.

## 3. Getting the peak-to-average ratio and output-length distribution right

The single biggest lever on the dollar output of this tool is
`peak_to_average_qps_ratio` and `--avg-output-tokens` — both are currently
placeholders. To replace them with real numbers:

1. Pull your actual request logs (or a proxy signal, e.g. app-server
   request counts to the current recommendation service) bucketed by hour
   over at least one full week (to capture weekday/weekend shape) and
   ideally a known seasonal peak (e.g. a holiday/promotional event).
2. `peak_to_average_qps_ratio = max(hourly_qps) / mean(hourly_qps)`.
3. Export that same log's response/output-length field (or a reasonable
   proxy, e.g. current recommendation-list size x average tokens per item
   description) as `--avg-output-tokens`.
4. Turn the full hourly curve into `time_s,qps` rows and feed it to
   `bench/benchmark_client.py --mode open-loop --qps-trace-file` (see §4)
   to validate latency/error-rate at the *actual* shape, not just the
   peak/average summary stat this cost tool uses.

## 4. Open-loop load testing — validating capacity against real arrival patterns

`bench/benchmark_client.py --mode throughput` (closed-loop) answers "what's
the max sustainable throughput" — the right tool for the tok/s/GPU ceiling
this cost model is built on. It does **not** tell you what latency your
users actually see at your real, bursty arrival pattern, because closed-loop
always keeps exactly C requests in flight by construction.

`--mode open-loop` fixes this: requests are submitted on an independent
Poisson-process schedule at a target rate, regardless of whether earlier
requests finished — the same failure mode a real production system faces
(sudden bursts create backlog that closed-loop tests can't reveal).

```bash
# Flat-rate sweep: "at each of these steady QPS levels, what's my latency/backlog?"
python3 bench/benchmark_client.py --mode open-loop --base-url http://localhost:9000 \
  --run-name vllm-tp1-openloop --engine vllm --qps-levels 5,10,20,40,80 \
  --fixed-input-len 512 --fixed-output-len 256 --measure-seconds 60

# Full daily-curve replay: "at my ACTUAL measured production traffic shape, what happens?"
python3 bench/benchmark_client.py --mode open-loop --base-url http://localhost:9000 \
  --run-name vllm-tp1-dailytrace --engine vllm --qps-trace-file my_production_qps_trace.csv \
  --fixed-input-len 512 --fixed-output-len 256
```

(Point `--base-url` at the router, not the engine directly, so the result
also reflects admission control/backpressure — see
`docs/PRODUCTION_ARCHITECTURE.md` §5.)

`my_production_qps_trace.csv` is a simple `time_s,qps` CSV (see §3 step 4);
`bench/benchmark_client.py` linearly interpolates between rows, so a
handful of hourly buckets is enough to capture a daily shape.

Key fields to check in the output JSON (`results/<run>/bench_open-loop.json`):

- **`backlog_at_window_end`** — requests still in flight when the window
  ended. Non-zero and growing across QPS levels (or across the trace's
  peak period) means that arrival rate exceeds sustainable capacity —
  exactly the "am I provisioned for peak" question this whole document
  exists to answer, now validated against a realistic arrival process
  instead of only a closed-loop ceiling number.
- **`ttft_s`/`e2e_s` percentiles** — do they blow up during/after the
  trace's peak, and if so, do they recover once the peak passes (transient
  queueing, fine) or stay elevated (under-provisioned, not fine)?

## 5. Running the planner

```bash
python3 bench/capacity_planner.py --target-qps 50 --avg-output-tokens 256
python3 bench/capacity_planner.py --run vllm-tp1-bf16 --target-qps 200 --avg-output-tokens 128 \
  --out results/report/CAPACITY_PLAN.md
```

Edit `configs/cost_assumptions.yaml` first with your real pricing/traffic
inputs (every field has an inline comment on where the number should come
from).
