# Serving Options: Comparison & Rationale

This toolkit benchmarks three engines head-to-head, because together they
cover the realistic production shortlist for a dense 8B LLM on NVIDIA GPUs
in 2026. This document explains what each one is, why it's included (or
not), and the qualitative trade-offs that raw benchmark numbers won't show.

## 1. The shortlist

| Engine | What it is | Why it's in scope |
|---|---|---|
| **vLLM** | The de facto open-source standard for high-throughput LLM serving. PagedAttention KV cache, continuous batching, huge model/quantization/hardware coverage, largest community. | Default choice for most teams; the baseline everything else is measured against. |
| **SGLang** | Originated at LMSYS/UC Berkeley; RadixAttention (prefix-cache-first design), a very fast scheduler, and a track record of leading throughput benchmarks on several model families, especially at high concurrency. | Closest credible throughput/latency competitor to vLLM with a maturing OpenAI-compatible server. |
| **TensorRT-LLM** | NVIDIA's own inference library; compiles models into optimized TensorRT engines (or runs via its newer PyTorch-native backend) with hand-tuned Blackwell kernels. | The "vendor-optimized" option — usually the ceiling for raw per-GPU throughput on NVIDIA hardware, at the cost of operational simplicity. |

### Also considered, deliberately out of scope for this round

| Option | Why not benchmarked here (yet) |
|---|---|
| **Hugging Face TGI** | Solid and simple, but on recent throughput/feature benchmarks it consistently trails vLLM/SGLang for this model class; add it to `configs/matrix.yaml` if architects want a fourth data point — the toolkit's structure makes that a ~30 min addition (`scripts/13_serve_tgi.sh` following the same pattern as the other three). |
| **NVIDIA Triton Inference Server + TensorRT-LLM backend** | Adds a model-orchestration/multi-framework layer (ensembles, non-LLM models, gRPC) on top of the same TensorRT-LLM engines already benchmarked directly. Worth adopting later if you need multi-model routing or non-LLM models in the same serving fleet; not a distinct performance data point over `trtllm-serve` for this single-model benchmark. |
| **NVIDIA NIM** | A packaged/managed version of TensorRT-LLM (and sometimes vLLM) with enterprise support, prebuilt containers per model, and license/entitlement requirements. Same underlying engines as above — evaluate for the *support contract*, not new performance data. |
| **Ray Serve** | An orchestration layer (autoscaling, multi-model composition, deployment graphs) that typically wraps vLLM underneath, not a competing inference engine. Relevant once you move from "one model, one node" to a multi-tenant fleet. |
| **llama.cpp / GGUF quantized serving** | Optimized for CPU/consumer-GPU/edge deployment, not for saturating 8x data-center B200s. The GGUF quants of this model that exist on Hugging Face (`mradermacher/OneRec-8B-pro-GGUF`) are aimed at that use case, not this one. |

## 2. Feature/maturity matrix (as of 2026-07)

| Capability | vLLM | SGLang | TensorRT-LLM |
|---|---|---|---|
| OpenAI-compatible server | Yes (mature) | Yes (mature) | Yes (`trtllm-serve`, newer) |
| Continuous batching | Yes | Yes | Yes |
| Prefix / KV cache reuse | Yes (`--enable-prefix-caching`) | Yes, RadixAttention is a first-class design pillar | Yes (KV cache reuse block manager) |
| Chunked prefill | Yes | Yes | Yes |
| Speculative decoding | Yes | Yes | Yes (often best-in-class here) |
| FP8 on Blackwell | Yes (online + pre-quantized) | Yes | Yes, typically the most mature/fastest path |
| FP4 on Blackwell | Emerging | Emerging | Most mature (NVIDIA co-designs kernels with hardware) |
| Setup complexity | Low (pip/docker, HF checkpoint directly) | Low (pip/docker, HF checkpoint directly) | Higher (engine build step or PyTorch-backend JIT warmup; more knobs) |
| Time-to-first-token after container start | Fast | Fast | Slower on first launch (compilation/CUDA graph capture) |
| Community size / issue turnaround | Largest | Large, fast-moving | Large but NVIDIA-gated release cadence |
| Multi-node tensor/pipeline parallel | Yes | Yes | Yes (most battle-tested at very large scale, e.g. MoE frontier models) |
| Best-documented for | Broad model coverage, fastest to get running | High-concurrency throughput, prefix-cache-heavy workloads (e.g. shared system prompts) | Squeezing maximum tok/s/GPU out of NVIDIA hardware specifically |

This table is qualitative and will drift as all three projects ship weekly —
treat the **measured** results in `results/report/REPORT.md` as ground
truth for this specific model/hardware/date, and this table as context for
*why* the numbers came out the way they did.

## 3. How to read the benchmark results through this lens

- If **TensorRT-LLM wins throughput but vLLM/SGLang are within ~10-15%**,
  the operational simplicity of vLLM/SGLang (faster iteration, simpler
  upgrades, no engine-build step) usually outweighs a marginal throughput
  edge — recommend vLLM/SGLang unless you're squeezed on GPU budget at
  fleet scale, where a 10-15% throughput gain compounds significantly.
- If **SGLang's RadixAttention shows a disproportionate win specifically at
  high concurrency**, that's a signal your production traffic (repeated
  system prompts / shared user-history prefixes, which is very plausible
  for a recommendation model re-scoring the same catalog against many
  users) has exploitable prefix-cache structure — worth a deeper look
  regardless of which engine you ship.
- If **FP8 (`vllm-tp1-fp8`) delivers a large throughput jump with
  negligible output-quality regression** (spot-check outputs — this
  toolkit does not run an automated quality eval), that is probably the
  single highest-leverage recommendation for the architects: it's "free"
  throughput from hardware you already bought.
- If **TP>1 configurations don't beat TP=1 on tok/s/GPU**, that validates
  the "8 independent replicas" deployment topology from
  `docs/ARCHITECTURE.md` — simpler ops, better fault isolation, same or
  better aggregate throughput.

## 4. Keeping this comparison current

All three engines ship new releases every 1-3 weeks, frequently with
double-digit throughput improvements on new hardware like Blackwell as
kernel support matures. Treat any serving-engine benchmark (this one
included) as a snapshot, not a permanent verdict — see
`docs/RUNBOOK.md` → "Keeping engine versions current" for how to refresh
the image tags in `scripts/env.sh` and re-run.
