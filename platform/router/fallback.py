"""Graceful degradation: when a traffic group has no available/healthy
backend (all breakers open, or every backend's admission-control queue is
full), serve a fallback response instead of a hard failure.

Two tiers, cheapest/most-available first:
  1. Recent-response cache keyed on prompt-prefix hash -- if we've served a
     similar prompt recently, replaying it is cheap and often reasonable
     for a recommendation product (same user context -> similar candidate
     set) for the short window until capacity recovers.
  2. Static heuristic ("popular items") fallback -- always available, used
     when the cache has no relevant entry.

Every fallback response is marked `"degraded": true` so callers and
dashboards never mistake it for a real model response.
"""
from __future__ import annotations

import time
from collections import OrderedDict

_HEURISTIC_TOP_ITEMS = [
    "Midnight Signal (Sci-Fi Thriller)",
    "Coastal Kitchen S3E12",
    "TrailRunner Pro Hiking Boots",
    "Quantum Finance Basics",
    "Urban Skyline Timelapse",
]

_HEURISTIC_TEXT = (
    "[degraded-mode recommendation] Capacity is temporarily exhausted, "
    "serving cached top-popular items instead of a personalized "
    "generation: " + "; ".join(_HEURISTIC_TOP_ITEMS)
)


class FallbackCache:
    def __init__(self, max_size: int = 256):
        self.max_size = max_size
        self._store: OrderedDict[str, tuple[str, float]] = OrderedDict()

    def put(self, key: str, text: str):
        if key in self._store:
            self._store.move_to_end(key)
        self._store[key] = (text, time.time())
        while len(self._store) > self.max_size:
            self._store.popitem(last=False)

    def get(self, key: str) -> str | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        self._store.move_to_end(key)
        return entry[0]

    def __len__(self) -> int:
        return len(self._store)


def build_fallback_payload(cache: FallbackCache, cache_key: str, served_model_name: str) -> dict:
    cached_text = cache.get(cache_key)
    text = cached_text if cached_text is not None else _HEURISTIC_TEXT
    source = "cache" if cached_text is not None else "heuristic"
    return {
        "id": "fallback-degraded",
        "object": "text_completion",
        "model": served_model_name,
        "degraded": True,
        "degraded_source": source,
        "choices": [{"text": text, "index": 0, "finish_reason": "fallback"}],
    }
