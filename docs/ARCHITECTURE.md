# Architecture & Deployment Context

## 1. Hardware

| Resource | Value |
|---|---|
| GPUs | 8x NVIDIA B200, 183 GB HBM3e each (1.43 TB total GPU memory) |
| Interconnect | Intra-node NVLink/NVSwitch (assumed — verify with `nvidia-smi topo -m`) |
| Driver / CUDA | 580.126.09 / CUDA 13.0 |
| CPU / RAM | Not the bottleneck for this workload; 2TB system RAM is far more than needed |
| Compute capability | sm_100 (Blackwell) — supports FP8 and FP4 tensor core paths at full rate, in addition to BF16/FP16 |

Single node, single NVLink domain. There is no multi-node/network-fabric
concern here (no InfiniBand/RoCE topology to design around) — everything in
this benchmark is intra-node tensor/data parallelism.

## 2. Model

`OpenOneRec/OneRec-8B-pro`:

- **Backbone:** Qwen3-8B (dense, *not* MoE — every token activates all 8.39B
  params, unlike the original 2025 OneRec MoE-based paper design, which used
  a custom generative recommender with ~13% activated params/token via MoE).
  The 2026 "OpenOneRec Foundation" release you're deploying re-based OneRec
  on a standard **dense** Qwen3 backbone specifically so it inherits the
  entire OSS LLM serving ecosystem (see `docs/SERVING_OPTIONS.md`) —
  operationally, that is the single most important fact about this model:
  **it needs no custom inference kernels or serving code.**
- **Modality bridge:** "Itemic Tokens" (hierarchical vector-quantized item
  embeddings) are folded into the token vocabulary during training, but at
  inference time the model is consumed exactly like any other causal LM:
  tokenize text (which may reference itemic tokens) in, sample tokens out.
- **Context length:** derived from Qwen3-8B defaults; `scripts/02_download_model.sh`
  prints the exact `max_position_embeddings` from `config.json` after
  download — use that to size `--max-model-len`/`--context-length` rather
  than assuming a number here.
- **Precision:** ships in BF16. FP8 is not pre-quantized in the checkpoint;
  this toolkit exercises vLLM's online FP8 quantization path as a
  Blackwell-specific optimization to measure (see the `vllm-tp1-fp8` matrix
  entry).
- **License:** Apache-2.0, model weights ungated on Hugging Face — no token
  required, no approval workflow, nothing that blocks automation.

Because inference-time behavior is indistinguishable from serving a normal
instruction-tuned Qwen3-8B, every technique, flag, and trade-off documented
for Qwen3-family serving directly applies to OneRec-8B-Pro. That is the
basis for treating this as a standard "deploy an 8B dense LLM on 8 GPUs"
problem rather than requiring a bespoke recommendation-serving stack.

## 3. What "8x B200 for one 8B model" actually means

An 8B BF16 model needs ~16.8GB just for weights, plus KV cache, activation
memory, and CUDA graph buffers — call it 20-30GB comfortably on a single
183GB B200. That means **you have roughly 6-9x more GPU memory than this
model needs on a single GPU.** The interesting engineering questions this
benchmark answers are not "does it fit" but:

1. **Replicate vs. shard?** Do you get more aggregate throughput from 8
   independent single-GPU replicas (data parallelism, e.g. behind a load
   balancer) than from one GPU running tensor-parallel across all 8
   (TP=8)? For a small dense model, TP overhead (all-reduce over NVLink on
   every layer) usually outweighs any benefit — the `vllm-tp1/2/4/8-bf16`
   matrix entries quantify this directly on this hardware.
2. **What's the actual ceiling on tokens/sec/GPU?** With this much spare HBM,
   a single B200 can hold an enormous KV cache and batch hundreds of
   concurrent sequences — throughput scaling with concurrency (not GPU
   count) is likely the dominant lever here.
3. **Does Blackwell's native FP8 change the calculus?** FP8 roughly doubles
   achievable GEMM throughput vs BF16 on Blackwell tensor cores; for a
   memory-rich, compute-bound-at-high-concurrency workload like this, that
   can materially shift the optimal replica/TP strategy.
4. **Engine choice matters more than parallelism strategy at this scale.**
   With this much headroom, differences in scheduler design, CUDA graph
   usage, attention kernel choice (FlashAttention-3/FlashInfer/TensorRT
   kernels), and continuous-batching implementation between vLLM/SGLang/
   TensorRT-LLM will show up clearly — that's the point of the 3-way engine
   comparison in `docs/SERVING_OPTIONS.md`.

## 4. Deployment topology options for production

Once the benchmark answers "replicate vs. shard" and "which engine" for this
specific model/hardware pairing, a production deployment is one of:

- **N independent single-GPU replicas** behind a load balancer/router
  (e.g. nginx, Envoy, or the engine's own multi-instance router). Simplest
  operationally; linear scaling; a GPU failure only removes 1/8 of capacity.
  Recommended default unless the throughput data shows TP>1 is meaningfully
  better *per GPU*, not just in aggregate.
- **A small number of TP-2 replicas** (4 replicas x TP=2) — a middle ground
  if TP=2 shows a real per-GPU throughput or latency win without TP=8's
  full all-reduce tax.
- **One TP=8 instance** — only justified if the data shows it winning on
  both tok/s/GPU *and* tail latency; otherwise it's strictly worse use of
  the hardware for a model this size (all eggs in one basket, one NCCL
  hang can take down 100% of capacity instead of 12.5%).

The benchmark matrix (`configs/matrix.yaml`) is deliberately built to let
the data — not intuition — decide which of these to recommend to the
architects.

## 5. Operational/security notes worth flagging to architects

- Containers run with `--gpus device=...` scoping GPUs per-instance;
  nothing runs with `--privileged`.
- Model weights and the HF cache are mounted read-only (`:ro`) into serving
  containers where practical.
- No inbound network exposure beyond the benchmarked ports on localhost in
  this toolkit; a production rollout needs a reverse proxy / auth layer in
  front of any of these engines' OpenAI-compatible endpoints (none of them
  ship with authentication by default).
- Engine container images are pinned to specific tags in `scripts/env.sh`
  for reproducibility — update deliberately, not via floating `:latest`.
