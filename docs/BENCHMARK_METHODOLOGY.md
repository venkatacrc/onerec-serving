# Benchmark Methodology

This document defines exactly what `bench/benchmark_client.py` measures and
how, so the numbers in `results/report/REPORT.md` can be defended in front
of architects (and reproduced by anyone else).

## 1. Metrics, precisely defined

| Metric | Definition | Why it matters |
|---|---|---|
| **TTFT** (time-to-first-token) | Wall-clock time from request submission to the first streamed token of the response. | The dominant contributor to perceived latency for any streaming/chat UI. Driven by queueing delay + prefill (prompt-processing) time. |
| **ITL** (inter-token latency) | Mean wall-clock gap between consecutive streamed tokens after the first. | Determines how "smooth" streamed output feels; driven by decode-step time and how many concurrent sequences are competing for GPU time. |
| **E2E latency** | Wall-clock time from request submission to the final token / stream close. | What a non-streaming client (or an SLA) actually experiences. |
| **Output throughput (tok/s)** | Sum of completion tokens across all requests that finished in a measurement window, divided by wall-clock window duration. | The number that determines how many GPUs/replicas are needed to hit a target QPS x avg-output-length. |
| **tok/s/GPU** | Output throughput divided by GPU count used by that configuration. | The only fair way to compare a TP=8 instance against 8x TP=1 replicas, or against a different engine using a different GPU count. |
| **tok/s/Watt** | Output throughput divided by average sampled GPU power draw across the window. | Power/cost efficiency signal — relevant at fleet scale and for TCO conversations with architects/finance. |

All latency numbers are reported as **p50/p90/p95/p99** — never a bare
average, because tail latency is what breaks SLAs and averages hide it.

## 2. Two distinct test modes, on purpose

### Latency mode (`--mode latency`, concurrency = 1)

Simulates a single interactive user with no contention. Sweeps a matrix of
`(input_len, output_len)` pairs (default: input lengths 128/512/2048 tokens
x output lengths 128/256 tokens) because prefill cost scales with input
length and decode cost scales with output length — collapsing this to one
number would hide which phase dominates for your actual traffic shape.

For each `(input_len, output_len)` cell:

1. A small number of **warmup requests** (default 3) are sent and
   discarded, to avoid measuring one-time JIT/CUDA-graph-capture costs.
2. **N repetitions** (default 20) are sent sequentially (never more than
   one in flight), each with a freshly generated prompt (different random
   seed) so results aren't an artifact of KV-cache/prefix-cache reuse on a
   single fixed prompt.
3. Percentiles are computed over those N repetitions.

### Throughput mode (`--mode throughput`, concurrency sweep)

Simulates production load at increasing levels of concurrency (default:
1, 2, 4, 8, 16, 32, 64, 128, 256 concurrent in-flight requests), at one
fixed `(input_len, output_len)` — default 512 in / 256 out, a reasonable
proxy for "moderate user history context, moderate recommendation-list
output."

This is a **closed-loop** load test: at concurrency level *C*, exactly *C*
persistent workers each immediately issue a new request as soon as their
previous one completes, for a fixed measurement window (default 45s, after
a 10s warmup window that is discarded). This is the standard methodology
used by vLLM's/SGLang's own `benchmark_serving`-style tools for
saturation testing, and is what determines the **maximum sustainable
throughput** at each concurrency level — as opposed to an *open-loop*
Poisson-arrival test (not implemented here), which would instead answer "at
real-world arrival rate X, what's my latency?" Both are valid; closed-loop
is the right tool for answering "what's the ceiling," which is the
question this exercise is optimizing for. If you need open-loop
arrival-rate modeling for capacity planning against a specific measured
production QPS curve, that's a natural extension — see
`docs/RUNBOOK.md` → "Extending this toolkit."

At each concurrency level, in addition to throughput, TTFT/E2E percentiles
are captured **at that load level** — this is what produces the
latency-vs-throughput curve (`throughput_scaling.png`,
`latency_vs_throughput.png`) that is the single most useful chart for
capacity planning: it shows the concurrency level at which latency starts
to degrade unacceptably, i.e. where you actually need to add a replica.

## 3. Prompt construction

Prompts are not random tokens or Lorem Ipsum. `bench/prompt_dataset.py`
builds prompts out of a pool of short recommendation-domain fragments
(watched/clicked/purchased items with metadata — see the module for the
full list) shuffled and concatenated to hit an exact target token count
(measured with the model's *own* tokenizer, downloaded alongside the
weights), wrapped in a system preamble instructing the model to produce
recommendations. This keeps prefill cost realistic for this model's actual
use case instead of being distorted by an unrepresentative token
distribution (e.g. highly repetitive or unnaturally compressible text).

Every request uses a different random seed for prompt content, so results
reflect steady-state serving behavior rather than one lucky/unlucky cached
prompt.

## 4. Token counting

- **Input tokens:** counted with the model's real tokenizer
  (`AutoTokenizer.from_pretrained` on the local model directory).
- **Output tokens:** taken from the server's own reported `usage.completion_tokens`
  when available (all three engines support OpenAI's `stream_options:
  {include_usage: true}`); falls back to counting streamed SSE chunks
  if a server doesn't populate `usage` on a given response.

## 5. GPU utilization / power sampling

While a throughput measurement window is running, `bench/benchmark_client.py`
spawns a background `nvidia-smi` poll (1s interval) scoped to exactly the
GPU IDs that configuration is using, recording utilization %, memory used,
and power draw. These are averaged (utilization/power) or maxed (memory)
over the window and reported alongside throughput — this is what makes
tok/s/Watt and "is this GPU actually saturated" claims verifiable rather
than assumed.

## 6. What this methodology deliberately does NOT measure

- **Output quality / correctness.** This is a *performance* benchmark. Spot
  check outputs via `scripts/20_smoke_test.sh` and manual inspection before
  trusting a configuration (especially FP8) for production — a fast wrong
  answer is not a win. Consider `RecIF-Bench` (linked from the model's
  HF page) if you need a quality regression suite for quantized variants.
- **Cross-node / multi-node scaling.** Everything here is single-node,
  intra-NVLink-domain. Not applicable at 8 GPUs on one node.
- **Cold-start / autoscaling latency.** This measures a warm, already-running
  server. Time-to-first-healthy-container (relevant for autoscaling) is
  visible in `logs/*.serve.log` timestamps if you need it, but isn't
  reported as a formal metric.
- **Real production traffic shape.** The synthetic prompt pool approximates
  the domain but is not a replay of real user traffic. Before finalizing a
  capacity plan, validate the input/output length assumptions
  (`configs/matrix.yaml` → `defaults.latency`/`defaults.throughput`) against
  actual logged request distributions if available.

## 7. Reproducibility

- Engine container image tags are pinned in `scripts/env.sh` (not `:latest`).
- Every raw per-request measurement is preserved in
  `results/<run>/bench_latency.json` (`.results.<key>.raw`), not just
  aggregated percentiles — re-slice/re-analyze without re-running.
- `results/run_summary.json` records pass/fail per configuration so partial
  runs (e.g. one engine's smoke test failing) are visible, not silently
  dropped from the report.
