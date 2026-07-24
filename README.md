# OneRec-8B-Pro Serving Benchmark Toolkit

Self-contained toolkit to deploy **[OpenOneRec/OneRec-8B-pro](https://huggingface.co/OpenOneRec/OneRec-8B-pro)**
(an 8B-parameter generative-recommendation LLM, Qwen3 architecture, Apache-2.0)
on a single node of 8x NVIDIA B200/GB200 GPUs, benchmark latency and throughput
across multiple serving frameworks, and produce an architect-ready comparison
report — end to end, with minimal manual intervention.

Built for a clean-slate box like this one:

```
8x NVIDIA B200, 183GB HBM3e each (1.43TB total)
Driver 580.126.09, CUDA 13.0
2TB system RAM
```

## What this does

1. **Installs everything needed** (Docker, NVIDIA Container Toolkit, a Python
   orchestration venv) on a bare Ubuntu box with only NVIDIA drivers present.
2. **Downloads OneRec-8B-Pro** from Hugging Face and introspects its config.
3. **Serves the model** with three production-grade inference engines —
   vLLM, SGLang, and TensorRT-LLM — each in its own container, across a matrix
   of tensor-parallel sizes and precisions.
4. **Benchmarks** every configuration for both single-user latency (TTFT,
   inter-token latency, end-to-end) and saturated throughput (tokens/sec,
   requests/sec, latency-vs-throughput curves), while sampling GPU
   utilization/power for efficiency metrics.
5. **Generates a Markdown report** with comparison tables and charts, ready
   to paste into a slide deck or doc for architects.

## Quickstart (on the GPU box)

```bash
git clone <this-repo-url> onerec-serving   # or scp this directory over
cd onerec-serving
./run_all.sh
```

That single command runs preflight checks, installs dependencies, downloads
the model, pulls the three engine images, and executes the full benchmark
matrix defined in `configs/matrix.yaml` — expect **2-4+ hours** end to end
depending on network/disk speed (see `docs/RUNBOOK.md` for a time breakdown
and how to run a faster subset first).

When it finishes:

- `results/report/REPORT.md` — the comparison report (tables + charts)
- `results/report/*.png` — latency-vs-throughput, throughput-scaling, TTFT charts
- `results/<run-name>/bench_*.json` — raw per-request data for deeper analysis
- `results/run_summary.json` — pass/fail status of every matrix entry
- `logs/` — full stdout/stderr of every step, for debugging

If you'd rather run things step by step (recommended the first time, so you
can sanity-check each stage), see `docs/RUNBOOK.md`.

## Repository layout

```
docs/
  ARCHITECTURE.md            Hardware/model context + deployment topology options
  SERVING_OPTIONS.md         vLLM vs SGLang vs TensorRT-LLM (+ others) comparison
  BENCHMARK_METHODOLOGY.md   Exact definitions/statistics behind every number
  RUNBOOK.md                 Step-by-step operator runbook + troubleshooting
scripts/
  env.sh, common.sh          Shared config + helper functions
  00_preflight_check.sh      Verify GPUs/driver/disk/tools, no changes made
  01_install_base_deps.sh    Docker, NVIDIA Container Toolkit, orchestration venv
  02_download_model.sh       Pull OneRec-8B-Pro from Hugging Face
  10/11/12_serve_*.sh        Launch vLLM / SGLang / TensorRT-LLM as OpenAI-API servers
  20_smoke_test.sh           One real request against a running server
  90_stop_serving.sh         Tear down containers
configs/
  matrix.yaml                The benchmark run matrix (edit to add/remove configs)
bench/
  prompt_dataset.py          Domain-flavored synthetic prompt generator
  benchmark_client.py        Async load generator (latency + throughput modes)
  run_matrix.py              Orchestrates serve -> smoke test -> benchmark -> teardown
  generate_report.py         Aggregates results/ into results/report/REPORT.md
run_all.sh                   One-shot end-to-end entry point
results/, logs/              Generated output (gitignored)
```

## Model summary

| | |
|---|---|
| Model | `OpenOneRec/OneRec-8B-pro` |
| Backbone | Qwen3-8B (standard `AutoModelForCausalLM`, no custom serving code required) |
| Params | 8.39B, BF16 checkpoint |
| License | Apache 2.0, ungated |
| Purpose | Generative recommendation foundation model — treats items as a distinct "Itemic Token" modality on top of a general-purpose LLM backbone |

Because it's architecturally a plain Qwen3-8B causal LM, it is directly
compatible with every mainstream high-performance LLM serving stack — this
toolkit exploits that to run a true apples-to-apples engine comparison.
See `docs/ARCHITECTURE.md` for details.

## Requirements assumed

- Ubuntu with NVIDIA driver already installed (confirmed: driver 580.126.09, CUDA 13.0, 8x B200)
- Outbound internet access (Docker Hub, NGC, Hugging Face Hub)
- A user with `sudo` (for Docker/toolkit installation) — everything else runs unprivileged
