# Operator Runbook

Everything below assumes you're on the target box (`b200-72` or equivalent),
in this repo's root directory, as a user with `sudo` access.

## 0. Fastest path

```bash
./run_all.sh
```

Does everything (preflight, install, download, pull images, run full
matrix, generate report). Read on for the step-by-step version, which is
recommended **the first time** so you can catch anything engine/version
related early instead of 3 hours into an unattended run.

## 1. Step-by-step (recommended first time)

### 1.1 Preflight (no changes made, ~10s)

```bash
./scripts/00_preflight_check.sh
```

Confirms GPU count/driver, disk space at `$ONEREC_DATA_DIR` (default
`~/onerec-data`), and base tools. Fix anything reported as `[FAIL]` before
continuing; `[WARN]` items are informational (usually resolved by the next
step).

### 1.2 Install base dependencies (~5-10 min)

```bash
./scripts/01_install_base_deps.sh
```

Installs Docker Engine, NVIDIA Container Toolkit, and a Python venv at
`~/onerec-data/venv` used only for the orchestration/benchmark scripts
(never for running the model itself — that's always inside a container).

**If this is the very first time your user was added to the `docker`
group**, start a new shell before continuing:

```bash
exec su -l "$USER"     # or just log out and back in
cd /path/to/onerec-serving
```

### 1.3 Download the model (~1-5 min depending on bandwidth, ~16GB)

```bash
./scripts/02_download_model.sh
```

Downloads `OpenOneRec/OneRec-8B-pro` into `~/onerec-data/models/OneRec-8B-pro`
and prints its architecture facts (context length, dtype, layer count) —
sanity check that `max_position_embeddings` looks like a normal Qwen3-8B
value (tens of thousands) and that `torch_dtype` is `bfloat16`.

### 1.4 Pull engine images (~10-30 min depending on bandwidth, ~50GB combined)

```bash
source scripts/env.sh
docker pull "$VLLM_IMAGE"
docker pull "$SGLANG_IMAGE"
docker pull "$TRTLLM_IMAGE"
```

(`run_all.sh` does this for you; listed here so you can pre-warm the cache
or debug a specific pull failure in isolation.)

### 1.5 Smoke-test one engine before running the full matrix (~2-5 min)

This is the single most valuable step to run manually before walking away
from an unattended multi-hour job:

```bash
source scripts/env.sh
./scripts/10_serve_vllm.sh --run-name smoke --tp 1 --gpus 0 --dtype bfloat16 --max-model-len 8192
./scripts/20_smoke_test.sh --port 8000
./scripts/90_stop_serving.sh vllm
```

If this works, the pipeline works end to end and the full matrix should
run unattended without surprises. If it doesn't, fix it here — it'll fail
the same way for every vLLM matrix entry.

### 1.6 Run the full benchmark matrix (~2-4+ hours)

```bash
source scripts/env.sh
source "$VENV_DIR/bin/activate"
python3 bench/run_matrix.py --matrix configs/matrix.yaml
deactivate
```

Runs every entry in `configs/matrix.yaml` sequentially: start server, smoke
test, latency sweep, throughput sweep, stop server, next. Continues past a
failed entry (logged in `results/run_summary.json`) instead of aborting the
whole matrix — you can walk away and check back later.

**To run just a subset first** (e.g. to sanity-check timing before
committing to the full 8-entry matrix):

```bash
python3 bench/run_matrix.py --matrix configs/matrix.yaml --only vllm-tp1-bf16,sglang-tp1-bf16
```

### 1.7 Generate/regenerate the report

`run_matrix.py` does this automatically at the end. To regenerate later
(e.g. after manually adding a run, or re-running one failed entry):

```bash
source "$VENV_DIR/bin/activate"
python3 bench/generate_report.py --results-dir results --out-dir results/report
deactivate
```

Open `results/report/REPORT.md`.

## 2. Monitoring an in-progress run

From another shell on the same box:

```bash
tail -f logs/run_all.log                      # top-level progress
tail -f logs/<run-name>.serve.log             # a specific server's startup/runtime log
tail -f logs/<run-name>.bench_throughput.log  # live benchmark progress + numbers
watch -n2 nvidia-smi                          # confirm GPUs are actually busy
docker ps                                     # which container is currently serving
```

`bench/benchmark_client.py` prints per-concurrency-level results to stdout
as it goes (captured into the `.bench_throughput.log` file), so you can see
tok/s numbers well before the full matrix finishes.

## 3. Cleaning up

```bash
./scripts/90_stop_serving.sh          # stop + remove every onerec-* container
docker system df                       # see how much disk containers/images are using
docker image prune -a                  # reclaim space from old image layers (careful: re-pull cost)
```

Model weights and HF cache live outside the repo at `$ONEREC_DATA_DIR`
(default `~/onerec-data`) — delete that directory to fully reclaim disk
once you're done, or leave it if you plan to re-run with a different matrix.

## 4. Troubleshooting

### GPU passthrough test fails in `00_preflight_check.sh` / `01_install_base_deps.sh`
Almost always one of: (a) NVIDIA Container Toolkit not yet installed — let
`01_install_base_deps.sh` finish; (b) Docker daemon needs a restart after
toolkit install (`sudo systemctl restart docker`); (c) driver/CUDA13
mismatch with an old toolkit version — `sudo apt-get update && sudo
apt-get install --only-upgrade nvidia-container-toolkit`.

### `docker: permission denied` even after `usermod -aG docker`
Group membership changes require a new login session. Run `exec su -l
"$USER"` or fully log out/in, then re-check with `groups`.

### A serve script times out waiting for `/health`
Check `logs/<container-name>.log` (or `logs/<run-name>.serve.log` if
launched via `run_matrix.py`) for the actual engine error — common causes:
- **Port already in use:** another leftover container is bound to that
  port. `docker ps -a | grep onerec` then `./scripts/90_stop_serving.sh`.
- **OOM during weight loading:** shouldn't happen at 8B on a 183GB GPU, but
  if you've lowered `--gpu-mem-util`/`--mem-fraction-static` too far, raise it.
- **`--tp`/`--gpus` mismatch:** the serve scripts hard-fail if the GPU list
  length doesn't match `--tp`; check `configs/matrix.yaml` entries.

### TensorRT-LLM troubleshooting
`trtllm-serve` has the newest/least stable CLI surface of the three engines
and its flags change between releases. If `12_serve_trtllm.sh` fails:

1. Check `logs/*trtllm*.log` for the actual rejected-flag error.
2. `docker exec -it <container-name> trtllm-serve --help` (if the container
   is still up) to see the exact flag names for your pulled image version.
3. If the high-level `trtllm-serve /model` PyTorch-backend path isn't
   available in your pulled tag, fall back to the explicit build path:
   ```bash
   docker run --rm -it --gpus all -v "$MODEL_DIR":/model:ro -v "$TRTLLM_ENGINE_DIR":/engine "$TRTLLM_IMAGE" bash
   # inside the container:
   trtllm-build --checkpoint_dir /model --output_dir /engine --gemm_plugin bfloat16
   trtllm-serve /engine --host 0.0.0.0 --port 8000
   ```
   then adjust `scripts/12_serve_trtllm.sh` to point at the prebuilt
   `/engine_cache` directory instead of `/model` once you've confirmed the
   right build flags for your image version, and re-run that one matrix
   entry with `--only trtllm-tp1-bf16`.
4. NVIDIA's own quick-start guide for the exact pulled tag is the source of
   truth: https://nvidia.github.io/TensorRT-LLM/quick-start-guide.html

### `docker pull nvcr.io/...` fails with `unauthorized` / `denied`
NGC catalog images are usually pullable anonymously, but if your network/org
requires it: create a free account at https://ngc.nvidia.com, generate an
API key, then:
```bash
docker login nvcr.io -u '$oauthtoken' -p <YOUR_NGC_API_KEY>
```

### Hugging Face download fails / rate limited
`OpenOneRec/OneRec-8B-pro` is public and ungated, so this is usually a
transient network issue — `02_download_model.sh` already retries 3x. If it
keeps failing, set `HF_TOKEN` (even for a public model, an authenticated
request gets a higher rate limit):
```bash
export HF_TOKEN=hf_xxxxxxxx
./scripts/02_download_model.sh
```

### Report generation fails with "No results found"
`bench/run_matrix.py` writes results even for partially-failed runs, but if
every entry failed at the server-start stage, there's nothing to
aggregate — check `results/run_summary.json` and the corresponding
`logs/*.serve.log` files first.

## 5. Keeping engine versions current

vLLM, SGLang, and TensorRT-LLM all ship new releases every 1-3 weeks with
real Blackwell performance improvements. Before a benchmark run that will
inform a real capacity-planning decision:

1. Check current tags:
   - vLLM: https://hub.docker.com/r/vllm/vllm-openai/tags
   - SGLang: https://hub.docker.com/r/lmsysorg/sglang/tags
   - TensorRT-LLM: https://catalog.ngc.nvidia.com/orgs/nvidia/teams/tensorrt-llm/containers/release/tags
2. Update `VLLM_IMAGE` / `SGLANG_IMAGE` / `TRTLLM_IMAGE` in `scripts/env.sh`.
3. Re-run (old results aren't overwritten unless you reuse the same
   `--run-name`s — consider archiving `results/` to `results-2026-07-24/`
   before a re-run so you can diff engine-version-over-time later).

## 6. Extending this toolkit

- **Add a config to the matrix:** add an entry to `configs/matrix.yaml`
  (e.g. `sglang-tp2-bf16`, `vllm-tp1-fp4` once FP4 checkpoints/paths mature).
  No code changes needed.
- **Add a fourth engine (e.g. TGI):** copy `scripts/11_serve_sglang.sh` as a
  template, add `"tgi": SCRIPTS_DIR / "13_serve_tgi.sh"` to
  `SERVE_SCRIPT` in `bench/run_matrix.py`, add matrix entries with
  `engine: tgi`.
- **Open-loop (Poisson arrival) load testing:** `bench/benchmark_client.py`'s
  `run_throughput_sweep` is closed-loop by design (see
  `docs/BENCHMARK_METHODOLOGY.md` §2); add a `--mode open-loop` that issues
  requests on a Poisson-process schedule at a target QPS instead of "always
  keep C in flight," if you need to validate against a specific measured
  production arrival rate rather than find the saturation ceiling.
- **Automated output-quality regression** (especially for the FP8 config):
  wire in `RecIF-Bench` (linked from the model's Hugging Face page) or a
  smaller golden-set eval, and gate the report on a quality delta, not just
  speed.
