#!/usr/bin/env python3
"""Local smoke test for the feature-store reference service (in-memory
backend, no Redis required). Run: python3 test_feature_store_local.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import httpx

PASSED, FAILED = 0, 0


def check(name: str, cond: bool, detail: str = ""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED += 1
        print(f"  [FAIL] {name}  {detail}")


async def main():
    import os

    os.environ["FEATURE_STORE_BACKEND"] = "memory"
    import app as fs_app

    transport = httpx.ASGITransport(app=fs_app.app)
    async with fs_app.app.router.lifespan_context(fs_app.app):
        async with httpx.AsyncClient(transport=transport, base_url="http://fs") as client:
            print("1. Health check")
            r = await client.get("/health")
            check("health ok", r.status_code == 200)

            print("\n2. Seeded users list")
            r = await client.get("/users?limit=5")
            check("has seeded users", r.json()["total"] >= 5, r.text)

            print("\n3. Fetch a user's history")
            user_id = r.json()["user_ids"][0]
            r = await client.get(f"/user/{user_id}/history?limit=10")
            events = r.json()["events"]
            check("history has events", len(events) > 0 and len(events) <= 10, r.text)
            check("events have required fields", all("item_title" in e and "action" in e for e in events), r.text)

            print("\n4. Build a ready-to-send prompt for a user")
            r = await client.get(f"/user/{user_id}/prompt")
            prompt = r.json()["prompt"]
            check("prompt mentions recommendation instruction", "recommend" in prompt.lower(), prompt[:200])
            check("prompt includes an interaction verb", any(v in prompt for v in ["watched", "clicked", "purchased", "liked", "skipped", "shared", "reviewed", "saved"]), prompt[:200])

            print("\n5. Record a new event, confirm it shows up")
            new_user = "user-99999-test"
            await client.post(f"/user/{new_user}/history", json={
                "item_id": "item-0001", "item_title": "Test Item", "category": "Test",
                "action": "watched", "timestamp": 1234567890.0, "extra": {},
            })
            r = await client.get(f"/user/{new_user}/history?limit=5")
            check("recorded event retrievable", any(e["item_title"] == "Test Item" for e in r.json()["events"]), r.text)

            print("\n6. Unknown item metadata returns known=False")
            r = await client.get("/item/does-not-exist/metadata")
            check("unknown item flagged", r.json()["known"] is False, r.text)

    print(f"\n{PASSED} passed, {FAILED} failed")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
