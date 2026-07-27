#!/usr/bin/env python3
"""Pulls N users' real-time interaction histories from a running
platform/feature_store service and writes a JSONL prompt pool that
bench/benchmark_client.py can consume via --prompt-file, as the
real-shaped alternative to prompt_dataset.py's purely synthetic prompts.

Usage:
    python3 bench/build_prompts_from_feature_store.py \
        --feature-store-url http://localhost:9100 \
        --n-users 100 --out prompts_real.jsonl

    python3 bench/benchmark_client.py --mode throughput \
        --base-url http://localhost:8000 --run-name vllm-tp1-real-prompts \
        --engine vllm --prompt-file prompts_real.jsonl ...
"""
from __future__ import annotations

import argparse
import asyncio
import json

import httpx


async def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--feature-store-url", default="http://localhost:9100")
    p.add_argument("--n-users", type=int, default=100)
    p.add_argument("--history-limit", type=int, default=20)
    p.add_argument("--out", default="prompts_real.jsonl")
    args = p.parse_args()

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{args.feature_store_url}/users", params={"limit": args.n_users})
        resp.raise_for_status()
        user_ids = resp.json()["user_ids"]
        print(f"Fetched {len(user_ids)} user ids from {args.feature_store_url}")

        prompts = []
        for uid in user_ids:
            resp = await client.get(f"{args.feature_store_url}/user/{uid}/prompt", params={"limit": args.history_limit})
            resp.raise_for_status()
            prompts.append(resp.json()["prompt"])

    with open(args.out, "w", encoding="utf-8") as f:
        for prompt in prompts:
            f.write(json.dumps({"prompt": prompt}) + "\n")

    print(f"Wrote {len(prompts)} real-shaped prompts to {args.out}")


if __name__ == "__main__":
    asyncio.run(main())
