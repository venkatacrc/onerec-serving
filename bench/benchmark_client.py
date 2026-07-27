#!/usr/bin/env python3
"""Async load generator for OpenAI-compatible LLM serving endpoints
(vLLM, SGLang, TensorRT-LLM's trtllm-serve all implement this surface).

Two modes:

  latency    Closed-loop, concurrency=1, sweeps a matrix of
             (input_len x output_len) pairs. Reports per-request TTFT,
             inter-token latency, and end-to-end latency percentiles.
             This is what a single interactive user would experience.

  throughput Closed-loop concurrency sweep at a fixed (input_len, output_len).
             At each concurrency level, N persistent workers keep the
             pipeline saturated; reports aggregate tokens/sec, requests/sec,
             and the latency percentiles *at that load level* -- i.e. the
             latency/throughput trade-off curve architects actually care
             about. Answers "what's the max sustainable throughput."

  open-loop  Open-loop Poisson-arrival load test: requests are submitted on
             their own independent schedule at a target rate (--qps-levels,
             or a full daily/seasonal --qps-trace-file), regardless of
             whether earlier requests have completed. Answers "at this
             measured/assumed production arrival rate, what's my latency,
             error rate, and is my backlog growing unboundedly" -- the
             complement to closed-loop's saturation-ceiling question. See
             docs/BENCHMARK_METHODOLOGY.md §2 and docs/CAPACITY_AND_COST.md.

Pass --prompt-file to use a pool of real (or feature-store-derived)
prompts instead of prompt_dataset.py's synthetic generator, in any mode --
see platform/feature_store/README.md and bench/build_prompts_from_feature_store.py.

Every run also samples GPU utilization/power/memory via `nvidia-smi dmon`
in the background so the report generator can compute tokens/sec/GPU and
tokens/sec/Watt.

Output: a single JSON file per invocation under results/<run_name>/.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

import aiohttp

sys.path.insert(0, str(Path(__file__).parent))
from prompt_dataset import build_prompt, count_tokens  # noqa: E402


def load_prompt_pool(path: str) -> list[str]:
    """Loads a JSONL file of {"prompt": "..."} lines, produced by
    platform/feature_store/../bench/build_prompts_from_feature_store.py
    from REAL (or feature-store-shaped) user-history records, as an
    alternative to the purely synthetic generator in prompt_dataset.py --
    see docs/BENCHMARK_METHODOLOGY.md §3 and §6."""
    prompts = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            prompts.append(json.loads(line)["prompt"])
    if not prompts:
        raise ValueError(f"prompt file {path} contained no prompts")
    return prompts


@dataclass
class RequestResult:
    input_tokens: int
    output_tokens: int
    ttft_s: float
    e2e_s: float
    itl_s: Optional[float]  # mean inter-token latency
    ok: bool
    error: Optional[str] = None


@dataclass
class GpuSample:
    t: float
    util_pct: float
    mem_used_mib: float
    power_w: float


class GpuSampler:
    """Background nvidia-smi sampler. Runs on the *host*, not inside the
    engine's container, so it captures whichever GPUs the caller points it
    at regardless of engine."""

    def __init__(self, gpu_ids: str, interval_s: float = 1.0):
        self.gpu_ids = gpu_ids
        self.interval_s = interval_s
        self._proc: Optional[subprocess.Popen] = None
        self._samples: list[GpuSample] = []
        self._task: Optional[asyncio.Task] = None
        self._stop = False

    async def start(self):
        self._stop = False
        self._task = asyncio.create_task(self._loop())

    async def _loop(self):
        ids = [i for i in self.gpu_ids.split(",") if i != ""]
        while not self._stop:
            try:
                out = subprocess.check_output(
                    [
                        "nvidia-smi",
                        f"--id={','.join(ids)}",
                        "--query-gpu=utilization.gpu,memory.used,power.draw",
                        "--format=csv,noheader,nounits",
                    ],
                    text=True,
                    timeout=5,
                )
                utils, mems, pows = [], [], []
                for line in out.strip().splitlines():
                    u, m, p = [float(x.strip()) for x in line.split(",")]
                    utils.append(u)
                    mems.append(m)
                    pows.append(p)
                if utils:
                    self._samples.append(
                        GpuSample(
                            t=time.time(),
                            util_pct=statistics.mean(utils),
                            mem_used_mib=sum(mems),
                            power_w=sum(pows),
                        )
                    )
            except Exception:
                pass
            await asyncio.sleep(self.interval_s)

    async def stop(self) -> dict:
        self._stop = True
        if self._task:
            await asyncio.wait_for(self._task, timeout=self.interval_s + 5)
        if not self._samples:
            return {"avg_util_pct": None, "avg_power_w": None, "peak_mem_mib": None, "n_samples": 0}
        return {
            "avg_util_pct": statistics.mean(s.util_pct for s in self._samples),
            "avg_power_w": statistics.mean(s.power_w for s in self._samples),
            "peak_mem_mib": max(s.mem_used_mib for s in self._samples),
            "n_samples": len(self._samples),
        }


async def send_one_request(
    session: aiohttp.ClientSession,
    base_url: str,
    served_model_name: str,
    prompt: str,
    max_tokens: int,
    timeout_s: float,
) -> RequestResult:
    url = f"{base_url}/v1/completions"
    payload = {
        "model": served_model_name,
        "prompt": prompt,
        "max_tokens": max_tokens,
        "temperature": 0.0,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    t_start = time.perf_counter()
    t_first_token = None
    token_times: list[float] = []
    output_tokens_from_usage = None
    try:
        async with session.post(url, json=payload, timeout=timeout_s) as resp:
            if resp.status != 200:
                body = await resp.text()
                return RequestResult(count_tokens(prompt, None), 0, 0, 0, None, False,
                                      error=f"HTTP {resp.status}: {body[:300]}")
            async for raw_line in resp.content:
                line = raw_line.decode("utf-8", errors="ignore").strip()
                if not line or not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                now = time.perf_counter()
                usage = chunk.get("usage")
                if usage and usage.get("completion_tokens"):
                    output_tokens_from_usage = usage["completion_tokens"]
                choices = chunk.get("choices") or []
                has_text = bool(choices) and choices[0].get("text")
                if has_text:
                    if t_first_token is None:
                        t_first_token = now
                    token_times.append(now)
        t_end = time.perf_counter()
    except Exception as exc:  # noqa: BLE001
        return RequestResult(count_tokens(prompt, None), 0, 0, 0, None, False, error=str(exc))

    if t_first_token is None:
        return RequestResult(count_tokens(prompt, None), 0, 0, t_end - t_start, None, False,
                              error="no tokens streamed back")

    out_tokens = output_tokens_from_usage or len(token_times)
    itl = None
    if len(token_times) > 1:
        deltas = [b - a for a, b in zip(token_times[:-1], token_times[1:])]
        itl = statistics.mean(deltas) if deltas else None

    return RequestResult(
        input_tokens=count_tokens(prompt, None),
        output_tokens=out_tokens,
        ttft_s=t_first_token - t_start,
        e2e_s=t_end - t_start,
        itl_s=itl,
        ok=True,
    )


def _percentiles(values: list[float], ps=(50, 90, 95, 99)) -> dict:
    if not values:
        return {f"p{p}": None for p in ps}
    values = sorted(values)
    out = {}
    for p in ps:
        k = (len(values) - 1) * (p / 100)
        f, c = int(k), min(int(k) + 1, len(values) - 1)
        out[f"p{p}"] = values[f] + (values[c] - values[f]) * (k - f)
    return out


async def run_latency_sweep(args, session: aiohttp.ClientSession, prompt_pool: Optional[list[str]] = None) -> dict:
    # With a real (feature-store-derived) prompt pool, input length is
    # whatever the real data gives us -- not a knob we control -- so we
    # collapse the input_lens sweep to a single "real" bucket instead of
    # pretending a fixed pool can hit arbitrary target lengths.
    input_lens = [int(x) for x in args.input_lens.split(",")] if prompt_pool is None else ["real"]
    output_lens = [int(x) for x in args.output_lens.split(",")]
    results = {}
    for in_len in input_lens:
        for out_len in output_lens:
            key = f"in{in_len}_out{out_len}"

            def get_prompt(seed: int) -> str:
                if prompt_pool is not None:
                    return prompt_pool[seed % len(prompt_pool)]
                return build_prompt(in_len, args.tokenizer_path, seed=seed)

            print(f"[latency] {key}: warmup...", flush=True)
            prompt = get_prompt(hash(key) % 1000)
            for _ in range(args.warmup_requests):
                await send_one_request(session, args.base_url, args.served_model_name, prompt, out_len, args.request_timeout_s)

            print(f"[latency] {key}: measuring {args.repetitions} requests...", flush=True)
            reqs = []
            for i in range(args.repetitions):
                p = get_prompt((hash(key) + i) % 100000)
                r = await send_one_request(session, args.base_url, args.served_model_name, p, out_len, args.request_timeout_s)
                reqs.append(r)

            ok = [r for r in reqs if r.ok]
            failed = len(reqs) - len(ok)
            ttft = _percentiles([r.ttft_s for r in ok])
            e2e = _percentiles([r.e2e_s for r in ok])
            itl_vals = [r.itl_s for r in ok if r.itl_s is not None]
            results[key] = {
                "input_len_target": in_len,
                "output_len_target": out_len,
                "n_requests": len(reqs),
                "n_failed": failed,
                "ttft_s": ttft,
                "e2e_s": e2e,
                "mean_itl_s": statistics.mean(itl_vals) if itl_vals else None,
                "raw": [asdict(r) for r in reqs],
            }
            print(f"[latency] {key}: TTFT p50={ttft['p50']:.3f}s p99={ttft['p99']:.3f}s  "
                  f"E2E p50={e2e['p50']:.3f}s  failed={failed}/{len(reqs)}", flush=True)
    return results


async def _throughput_worker(worker_id, args, session, in_len, out_len, stop_event, results: list, prompts_seed_offset,
                              prompt_pool: Optional[list[str]] = None):
    i = 0
    while not stop_event.is_set():
        seed = (prompts_seed_offset + worker_id * 100000 + i) % 10_000_000
        prompt = prompt_pool[seed % len(prompt_pool)] if prompt_pool is not None else build_prompt(in_len, args.tokenizer_path, seed=seed)
        r = await send_one_request(session, args.base_url, args.served_model_name, prompt, out_len, args.request_timeout_s)
        results.append(r)
        i += 1


async def run_throughput_sweep(args, session_factory, prompt_pool: Optional[list[str]] = None) -> dict:
    """`session_factory` is a zero-arg callable returning a fresh
    aiohttp.ClientSession as an async context manager. A NEW session (and
    therefore a fresh TCP connection pool) is used for every concurrency
    level, so that connection-pool state/backlog from one level can never
    bleed into the next -- this matters because closed-loop sweeps run all
    levels back-to-back in one process."""
    concurrency_levels = [int(x) for x in args.concurrency_levels.split(",")]
    in_len, out_len = args.fixed_input_len, args.fixed_output_len
    results = {}

    for c in concurrency_levels:
        async with session_factory() as session:
            print(f"[throughput] concurrency={c}: warmup ({args.warmup_seconds}s)...", flush=True)
            warmup_stop = asyncio.Event()
            warmup_results: list[RequestResult] = []
            warmup_workers = [
                asyncio.create_task(_throughput_worker(w, args, session, in_len, out_len, warmup_stop, warmup_results, 0, prompt_pool))
                for w in range(c)
            ]
            await asyncio.sleep(args.warmup_seconds)
            warmup_stop.set()
            await asyncio.gather(*warmup_workers, return_exceptions=True)

            print(f"[throughput] concurrency={c}: measuring ({args.measure_seconds}s)...", flush=True)
            gpu_sampler = GpuSampler(args.gpu_ids) if args.gpu_ids else None
            if gpu_sampler:
                await gpu_sampler.start()

            stop_event = asyncio.Event()
            measured: list[RequestResult] = []
            t0 = time.perf_counter()
            workers = [
                asyncio.create_task(_throughput_worker(w, args, session, in_len, out_len, stop_event, measured, 1, prompt_pool))
                for w in range(c)
            ]
            await asyncio.sleep(args.measure_seconds)
            stop_event.set()
            await asyncio.gather(*workers, return_exceptions=True)
            wall_s = time.perf_counter() - t0

        gpu_stats = await gpu_sampler.stop() if gpu_sampler else {}

        ok = [r for r in measured if r.ok]
        failed_reqs = [r for r in measured if not r.ok]
        failed = len(failed_reqs)
        total_out_tokens = sum(r.output_tokens for r in ok)
        total_in_tokens = sum(r.input_tokens for r in ok)
        ttft = _percentiles([r.ttft_s for r in ok])
        e2e = _percentiles([r.e2e_s for r in ok])

        # Surface *why* requests failed (timeout vs HTTP error vs connection
        # reset, etc.) instead of just a bare count -- critical for telling
        # "client-side bottleneck" apart from "server rejected the request"
        # apart from "genuine capacity ceiling."
        error_counts: dict[str, int] = {}
        for r in failed_reqs:
            key = (r.error or "unknown")[:120]
            error_counts[key] = error_counts.get(key, 0) + 1
        top_errors = sorted(error_counts.items(), key=lambda kv: -kv[1])[:5]

        n_gpus = max(1, len([g for g in (args.gpu_ids or "").split(",") if g != ""]))
        results[f"c{c}"] = {
            "concurrency": c,
            "input_len_target": in_len,
            "output_len_target": out_len,
            "wall_s": wall_s,
            "n_requests_completed": len(ok),
            "n_failed": failed,
            "top_errors": [{"error": e, "count": n} for e, n in top_errors],
            "output_tokens_per_s": total_out_tokens / wall_s if wall_s > 0 else None,
            "total_tokens_per_s": (total_out_tokens + total_in_tokens) / wall_s if wall_s > 0 else None,
            "requests_per_s": len(ok) / wall_s if wall_s > 0 else None,
            "output_tokens_per_s_per_gpu": (total_out_tokens / wall_s / n_gpus) if wall_s > 0 else None,
            "ttft_s": ttft,
            "e2e_s": e2e,
            "gpu": gpu_stats,
        }
        r = results[f"c{c}"]
        tok_s = r["output_tokens_per_s"]
        print(f"[throughput] concurrency={c}: {tok_s:.1f} tok/s  "
              f"{r['requests_per_s']:.2f} req/s  TTFT p50={ttft['p50']:.3f}s  "
              f"E2E p50={e2e['p50']:.3f}s  failed={failed}"
              + (f"  top_error={top_errors[0][0]!r}x{top_errors[0][1]}" if top_errors else ""),
              flush=True)
    return results


async def _open_loop_one_request(args, session, prompt_pool, in_len, out_len, seed, measured, state):
    state["inflight"] += 1
    state["max_inflight"] = max(state["max_inflight"], state["inflight"])
    prompt = prompt_pool[seed % len(prompt_pool)] if prompt_pool is not None else build_prompt(in_len, args.tokenizer_path, seed=seed)
    try:
        r = await send_one_request(session, args.base_url, args.served_model_name, prompt, out_len, args.request_timeout_s)
        measured.append(r)
    finally:
        state["inflight"] -= 1


def _qps_at(trace: list[tuple[float, float]], t: float) -> float:
    """Piecewise-linear interpolation of a (time_s, qps) trace, used to
    replay a measured production arrival-rate curve (e.g. a daily/seasonal
    traffic shape) rather than a single flat rate."""
    if t <= trace[0][0]:
        return trace[0][1]
    if t >= trace[-1][0]:
        return trace[-1][1]
    for (t0, q0), (t1, q1) in zip(trace, trace[1:]):
        if t0 <= t <= t1:
            if t1 == t0:
                return q0
            frac = (t - t0) / (t1 - t0)
            return q0 + frac * (q1 - q0)
    return trace[-1][1]


def load_qps_trace(path: str) -> list[tuple[float, float]]:
    """CSV with header `time_s,qps`, sorted ascending by time_s."""
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        next(f)  # header
        for line in f:
            line = line.strip()
            if not line:
                continue
            t, q = line.split(",")
            rows.append((float(t), float(q)))
    rows.sort(key=lambda r: r[0])
    if not rows:
        raise ValueError(f"qps trace file {path} had no data rows")
    return rows


async def _run_one_open_loop_window(args, session, prompt_pool, in_len, out_len, duration_s: float,
                                     qps_fn, gpu_ids: str, label: str) -> dict:
    """Runs a single open-loop (Poisson-arrival) load window: requests are
    submitted on their own schedule regardless of whether earlier requests
    have finished (the defining difference from the closed-loop throughput
    sweep in run_throughput_sweep, which always keeps exactly C requests
    in flight). This directly answers "at measured production arrival
    rate X, what's my latency / error rate / backlog growth" -- as opposed
    to closed-loop's "what's the max sustainable throughput" -- see
    docs/BENCHMARK_METHODOLOGY.md §2 and docs/CAPACITY_AND_COST.md.

    `qps_fn(elapsed_s) -> float` allows a time-varying rate (trace replay);
    for a flat-rate window the caller just passes `lambda _t: qps`.
    """
    rng = random.Random(hash(label) % (2**31))
    measured: list[RequestResult] = []
    state = {"inflight": 0, "max_inflight": 0}
    gpu_sampler = GpuSampler(gpu_ids) if gpu_ids else None
    if gpu_sampler:
        await gpu_sampler.start()

    t0 = time.perf_counter()
    tasks = []
    n_submitted = 0
    while True:
        elapsed = time.perf_counter() - t0
        if elapsed >= duration_s:
            break
        qps = max(qps_fn(elapsed), 1e-6)
        interarrival = rng.expovariate(qps)
        await asyncio.sleep(min(interarrival, duration_s - elapsed))
        if time.perf_counter() - t0 >= duration_s:
            break
        tasks.append(asyncio.create_task(
            _open_loop_one_request(args, session, prompt_pool, in_len, out_len, n_submitted, measured, state)
        ))
        n_submitted += 1

    backlog_at_window_end = state["inflight"]
    wall_s = time.perf_counter() - t0
    drain_timeout = min(args.request_timeout_s, 60.0)
    await asyncio.wait(tasks, timeout=drain_timeout)
    gpu_stats = await gpu_sampler.stop() if gpu_sampler else {}

    ok = [r for r in measured if r.ok]
    failed_reqs = [r for r in measured if not r.ok]
    n_gpus = max(1, len([g for g in (gpu_ids or "").split(",") if g != ""]))
    total_out_tokens = sum(r.output_tokens for r in ok)
    return {
        "label": label,
        "window_s": duration_s,
        "n_submitted": n_submitted,
        "n_completed_in_window": len(measured),
        "n_ok": len(ok),
        "n_failed": len(failed_reqs),
        "achieved_submit_rate_per_s": n_submitted / wall_s if wall_s > 0 else None,
        "achieved_completion_rate_per_s": len(ok) / wall_s if wall_s > 0 else None,
        "backlog_at_window_end": backlog_at_window_end,
        "max_concurrent_inflight": state["max_inflight"],
        "output_tokens_per_s": total_out_tokens / wall_s if wall_s > 0 else None,
        "output_tokens_per_s_per_gpu": (total_out_tokens / wall_s / n_gpus) if wall_s > 0 else None,
        "ttft_s": _percentiles([r.ttft_s for r in ok]),
        "e2e_s": _percentiles([r.e2e_s for r in ok]),
        "gpu": gpu_stats,
    }


async def run_open_loop_sweep(args, session_factory, prompt_pool: Optional[list[str]] = None) -> dict:
    """Fixed-QPS open-loop sweep: one window per level in --qps-levels."""
    in_len, out_len = args.fixed_input_len, args.fixed_output_len
    results = {}
    for qps in [float(x) for x in args.qps_levels.split(",")]:
        async with session_factory() as session:
            print(f"[open-loop] qps={qps}: warmup ({args.warmup_seconds}s)...", flush=True)
            await _run_one_open_loop_window(args, session, prompt_pool, in_len, out_len, args.warmup_seconds,
                                             lambda _t, qps=qps: qps, "", f"warmup_qps{qps}")
            print(f"[open-loop] qps={qps}: measuring ({args.measure_seconds}s)...", flush=True)
            r = await _run_one_open_loop_window(args, session, prompt_pool, in_len, out_len, args.measure_seconds,
                                                 lambda _t, qps=qps: qps, args.gpu_ids, f"qps{qps}")
        results[f"qps{qps}"] = {"target_qps": qps, **r}
        print(f"[open-loop] qps={qps}: submitted={r['n_submitted']} completed={r['n_ok']} "
              f"failed={r['n_failed']} backlog_at_end={r['backlog_at_window_end']} "
              f"TTFT p50={r['ttft_s']['p50']}", flush=True)
    return results


async def run_open_loop_trace(args, session_factory, prompt_pool: Optional[list[str]] = None) -> dict:
    """Replays a measured production QPS-vs-time trace (e.g. a daily
    traffic curve with a peak/spike) as one continuous open-loop window,
    instead of a sequence of flat-rate levels. See --qps-trace-file."""
    trace = load_qps_trace(args.qps_trace_file)
    duration_s = trace[-1][0] - trace[0][0]
    in_len, out_len = args.fixed_input_len, args.fixed_output_len

    def qps_fn(elapsed_s: float) -> float:
        return _qps_at(trace, trace[0][0] + elapsed_s)

    print(f"[open-loop-trace] replaying {args.qps_trace_file} over {duration_s:.0f}s "
          f"(peak qps={max(q for _, q in trace):.2f})...", flush=True)
    async with session_factory() as session:
        r = await _run_one_open_loop_window(args, session, prompt_pool, in_len, out_len, duration_s,
                                             qps_fn, args.gpu_ids, "trace_replay")
    print(f"[open-loop-trace] submitted={r['n_submitted']} completed={r['n_ok']} failed={r['n_failed']} "
          f"backlog_at_end={r['backlog_at_window_end']}", flush=True)
    return {"trace_replay": {"trace_file": args.qps_trace_file, "duration_s": duration_s, **r}}


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--mode", choices=["latency", "throughput", "open-loop"], required=True)
    p.add_argument("--base-url", required=True, help="e.g. http://localhost:8000")
    p.add_argument("--served-model-name", default="onerec-8b-pro")
    p.add_argument("--tokenizer-path", default=None, help="local HF model dir, for accurate token counts")
    p.add_argument("--run-name", required=True, help="e.g. vllm-tp1-bf16")
    p.add_argument("--engine", required=True, help="vllm | sglang | trtllm")
    p.add_argument("--output-dir", default="results")
    p.add_argument("--request-timeout-s", type=float, default=300)
    p.add_argument("--gpu-ids", default="", help="comma list of GPU ids used by this server, for power/util sampling")
    p.add_argument("--prompt-file", default=None,
                    help="JSONL of {\"prompt\": ...} lines (e.g. from build_prompts_from_feature_store.py) "
                         "to use instead of the synthetic generator, in any mode")

    # latency mode
    p.add_argument("--input-lens", default="128,512,2048")
    p.add_argument("--output-lens", default="128,256")
    p.add_argument("--repetitions", type=int, default=20)
    p.add_argument("--warmup-requests", type=int, default=3)

    # throughput mode
    p.add_argument("--concurrency-levels", default="1,2,4,8,16,32,64,128,256")
    p.add_argument("--fixed-input-len", type=int, default=512)
    p.add_argument("--fixed-output-len", type=int, default=256)
    p.add_argument("--warmup-seconds", type=float, default=10)
    p.add_argument("--measure-seconds", type=float, default=45)

    # open-loop mode
    p.add_argument("--qps-levels", default="1,2,5,10,20",
                    help="comma list of target arrival rates (requests/sec) for a flat-rate open-loop sweep")
    p.add_argument("--qps-trace-file", default=None,
                    help="CSV with header time_s,qps -- if set, replays this arrival-rate curve instead of --qps-levels "
                         "(e.g. to simulate a daily/seasonal traffic spike against measured production QPS)")

    return p.parse_args()


async def main_async(args):
    def session_factory():
        return aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=0))

    prompt_pool = load_prompt_pool(args.prompt_file) if args.prompt_file else None
    if prompt_pool:
        print(f"[prompts] using {len(prompt_pool)} real/feature-store-derived prompts from {args.prompt_file} "
              f"instead of the synthetic generator", flush=True)

    if args.mode == "latency":
        async with session_factory() as session:
            data = await run_latency_sweep(args, session, prompt_pool)
    elif args.mode == "throughput":
        data = await run_throughput_sweep(args, session_factory, prompt_pool)
    else:
        if args.qps_trace_file:
            data = await run_open_loop_trace(args, session_factory, prompt_pool)
        else:
            data = await run_open_loop_sweep(args, session_factory, prompt_pool)

    out_dir = Path(args.output_dir) / args.run_name
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"bench_{args.mode}.json"
    payload = {
        "run_name": args.run_name,
        "engine": args.engine,
        "mode": args.mode,
        "base_url": args.base_url,
        "gpu_ids": args.gpu_ids,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "args": vars(args),
        "results": data,
    }
    out_path.write_text(json.dumps(payload, indent=2))
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(main_async(args))
