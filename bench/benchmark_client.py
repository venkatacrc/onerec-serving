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
             about.

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


async def run_latency_sweep(args, session: aiohttp.ClientSession) -> dict:
    input_lens = [int(x) for x in args.input_lens.split(",")]
    output_lens = [int(x) for x in args.output_lens.split(",")]
    results = {}
    for in_len in input_lens:
        for out_len in output_lens:
            key = f"in{in_len}_out{out_len}"
            print(f"[latency] {key}: warmup...", flush=True)
            prompt = build_prompt(in_len, args.tokenizer_path, seed=hash(key) % 1000)
            for _ in range(args.warmup_requests):
                await send_one_request(session, args.base_url, args.served_model_name, prompt, out_len, args.request_timeout_s)

            print(f"[latency] {key}: measuring {args.repetitions} requests...", flush=True)
            reqs = []
            for i in range(args.repetitions):
                p = build_prompt(in_len, args.tokenizer_path, seed=(hash(key) + i) % 100000)
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


async def _throughput_worker(worker_id, args, session, in_len, out_len, stop_event, results: list, prompts_seed_offset):
    i = 0
    while not stop_event.is_set():
        prompt = build_prompt(in_len, args.tokenizer_path, seed=(prompts_seed_offset + worker_id * 100000 + i) % 10_000_000)
        r = await send_one_request(session, args.base_url, args.served_model_name, prompt, out_len, args.request_timeout_s)
        results.append(r)
        i += 1


async def run_throughput_sweep(args, session: aiohttp.ClientSession) -> dict:
    concurrency_levels = [int(x) for x in args.concurrency_levels.split(",")]
    in_len, out_len = args.fixed_input_len, args.fixed_output_len
    results = {}

    for c in concurrency_levels:
        print(f"[throughput] concurrency={c}: warmup ({args.warmup_seconds}s)...", flush=True)
        warmup_stop = asyncio.Event()
        warmup_results: list[RequestResult] = []
        warmup_workers = [
            asyncio.create_task(_throughput_worker(w, args, session, in_len, out_len, warmup_stop, warmup_results, 0))
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
            asyncio.create_task(_throughput_worker(w, args, session, in_len, out_len, stop_event, measured, 1))
            for w in range(c)
        ]
        await asyncio.sleep(args.measure_seconds)
        stop_event.set()
        await asyncio.gather(*workers, return_exceptions=True)
        wall_s = time.perf_counter() - t0

        gpu_stats = await gpu_sampler.stop() if gpu_sampler else {}

        ok = [r for r in measured if r.ok]
        failed = len(measured) - len(ok)
        total_out_tokens = sum(r.output_tokens for r in ok)
        total_in_tokens = sum(r.input_tokens for r in ok)
        ttft = _percentiles([r.ttft_s for r in ok])
        e2e = _percentiles([r.e2e_s for r in ok])

        n_gpus = max(1, len([g for g in (args.gpu_ids or "").split(",") if g != ""]))
        results[f"c{c}"] = {
            "concurrency": c,
            "input_len_target": in_len,
            "output_len_target": out_len,
            "wall_s": wall_s,
            "n_requests_completed": len(ok),
            "n_failed": failed,
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
              f"E2E p50={e2e['p50']:.3f}s  failed={failed}", flush=True)
    return results


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--mode", choices=["latency", "throughput"], required=True)
    p.add_argument("--base-url", required=True, help="e.g. http://localhost:8000")
    p.add_argument("--served-model-name", default="onerec-8b-pro")
    p.add_argument("--tokenizer-path", default=None, help="local HF model dir, for accurate token counts")
    p.add_argument("--run-name", required=True, help="e.g. vllm-tp1-bf16")
    p.add_argument("--engine", required=True, help="vllm | sglang | trtllm")
    p.add_argument("--output-dir", default="results")
    p.add_argument("--request-timeout-s", type=float, default=300)
    p.add_argument("--gpu-ids", default="", help="comma list of GPU ids used by this server, for power/util sampling")

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

    return p.parse_args()


async def main_async(args):
    connector = aiohttp.TCPConnector(limit=0)
    async with aiohttp.ClientSession(connector=connector) as session:
        if args.mode == "latency":
            data = await run_latency_sweep(args, session)
        else:
            data = await run_throughput_sweep(args, session)

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
