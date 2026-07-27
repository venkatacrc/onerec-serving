#!/usr/bin/env python3
"""Example: how a calling service (e.g. a recommendation-surface backend)
should use OneRecClient instead of a bare HTTP client.

Run against a real router:
    python3 example_usage.py --base-url http://localhost:9000
"""
from __future__ import annotations

import argparse
import asyncio

from onerec_client import OneRecClient


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--base-url", default="http://localhost:9000")
    p.add_argument("--user-id", default="user-12345")
    args = p.parse_args()

    client = OneRecClient(base_url=args.base_url)
    try:
        result = await client.complete(
            "User recently watched 3 sci-fi thrillers and purchased hiking "
            "gear. Recommend 5 items.",
            max_tokens=64,
            user_id=args.user_id,
        )
        if result.degraded:
            print(f"[DEGRADED response, reason={result.degraded_reason}] {result.text}")
        else:
            print(f"[OK] {result.text}")
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
