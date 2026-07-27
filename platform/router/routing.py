"""Routing strategies: given a traffic group's list of backends (already
filtered to available ones by the caller) and the request context, pick one
backend.

All strategies are pure functions over a `list[Backend]` so they're trivial
to unit test without any network I/O (see test_router_local.py).
"""
from __future__ import annotations

import hashlib
from typing import Optional

from backend_pool import Backend, BackendPool


def prefix_hash_key(prompt: str, n_chars: int) -> str:
    prefix = prompt[:n_chars]
    return hashlib.sha256(prefix.encode("utf-8", errors="ignore")).hexdigest()


def pick_round_robin(pool: BackendPool, candidates: list[Backend], group: str) -> Optional[Backend]:
    if not candidates:
        return None
    idx = pool.next_round_robin_index(group, len(candidates))
    return candidates[idx % len(candidates)]


def pick_least_outstanding(candidates: list[Backend]) -> Optional[Backend]:
    if not candidates:
        return None
    return min(candidates, key=lambda b: b.load_score())


def pick_prefix_hash(candidates: list[Backend], prompt: str, n_chars: int) -> Optional[Backend]:
    """Consistent-hash-ish: hash the prompt prefix, map into the candidate
    list. This is deliberately simple (mod-hash, not full consistent
    hashing with virtual nodes) -- at the replica counts this router deals
    with (single/low double digits), mod-hash's rebalance-on-resize cost is
    negligible, and it's far easier to reason about than a ring.

    Falls back to least-outstanding among candidates whose hash bucket
    isn't available, so a request never gets stuck routing to a
    healthy-but-hashed-away backend when others are idle -- prefix affinity
    is a cache-hit-rate *optimization*, not a correctness requirement, so it
    should never make admission control worse.
    """
    if not candidates:
        return None
    key = prefix_hash_key(prompt, n_chars)
    bucket = int(key, 16) % len(candidates)
    primary = candidates[bucket]
    if primary.available and not primary.at_capacity:
        return primary
    return pick_least_outstanding(candidates)


def select_backend(
    pool: BackendPool,
    group: str,
    strategy: str,
    prompt: str,
    prefix_hash_chars: int,
) -> Optional[Backend]:
    candidates = [b for b in pool.group(group) if b.available]
    if not candidates:
        return None
    if strategy == "round_robin":
        return pick_round_robin(pool, candidates, group)
    if strategy == "prefix_hash":
        return pick_prefix_hash(candidates, prompt, prefix_hash_chars)
    return pick_least_outstanding(candidates)  # default / "least_outstanding"


def select_group(pool: BackendPool, canary_weight_pct: float, rng_value: float) -> str:
    """rng_value: caller-supplied float in [0, 100) so this is a pure,
    testable function instead of calling random() inline."""
    has_canary = bool(pool.group("canary"))
    if has_canary and rng_value < canary_weight_pct:
        return "canary"
    return "stable"
