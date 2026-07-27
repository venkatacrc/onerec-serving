"""Feature-store client interface + two implementations.

No real feature store (Feast/Tecton/an internal system) is wired up for
this product yet, so this module defines the interface the router and
benchmark tooling call, plus:

  - `InMemoryFeatureStore` — zero-dependency, used by default and by tests.
  - `RedisFeatureStore` — a real network-backed implementation (same
    interface), so `platform/feature_store/app.py` demonstrates an actual
    service boundary rather than an in-process stub.

**Swapping in a real feature store later is exactly this: implement
`FeatureStoreClient` against Feast/Tecton's SDK and change one line in
`app.py`'s `_build_store()`.** Nothing else in the router or benchmark
tooling needs to change, since they only depend on this interface.
"""
from __future__ import annotations

import json
import random
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Optional


@dataclass
class InteractionEvent:
    item_id: str
    item_title: str
    category: str
    action: str  # watched | clicked | purchased | liked | skipped | ...
    timestamp: float
    extra: dict = field(default_factory=dict)


@dataclass
class UserHistory:
    user_id: str
    events: list[InteractionEvent]


class FeatureStoreClient(ABC):
    @abstractmethod
    async def get_user_history(self, user_id: str, limit: int = 20) -> UserHistory: ...

    @abstractmethod
    async def get_item_metadata(self, item_id: str) -> dict: ...

    @abstractmethod
    async def record_event(self, user_id: str, event: InteractionEvent) -> None: ...

    @abstractmethod
    async def list_user_ids(self) -> list[str]: ...


# --- Synthetic seed data, used to make both backends demoable out of the box.
_CATEGORIES = ["Sci-Fi Thriller", "Cooking Show", "Outdoor Gear", "Fintech Ad", "Talk Show",
               "Travel Video", "Lifestyle", "Food", "Electronics", "Home", "Tech", "Educational", "Crime Podcast"]
_ACTIONS = ["watched", "clicked", "purchased", "liked", "skipped", "shared", "reviewed", "saved"]
_TITLES = ["Midnight Signal", "Coastal Kitchen", "TrailRunner Pro Boots", "Quantum Finance Basics",
           "Late Night Comedy Hour", "Urban Skyline Timelapse", "Homegrown Garden Tips",
           "Street Food Diaries", "Noise-Cancelling Headphones", "Compact Air Fryer 4L",
           "Minimalist Desk Setups", "Grandmaster Chess Endgames", "True Crime Weekly"]


def _synthetic_history(user_id: str, n_events: int, seed: Optional[int] = None) -> UserHistory:
    rng = random.Random(seed if seed is not None else hash(user_id))
    now = time.time()
    events = []
    for i in range(n_events):
        events.append(InteractionEvent(
            item_id=f"item-{rng.randint(1000, 9999)}",
            item_title=rng.choice(_TITLES),
            category=rng.choice(_CATEGORIES),
            action=rng.choice(_ACTIONS),
            timestamp=now - rng.randint(0, 30 * 86400),
            extra={"rating": round(rng.uniform(1, 5), 1)} if rng.random() < 0.3 else {},
        ))
    events.sort(key=lambda e: e.timestamp, reverse=True)
    return UserHistory(user_id=user_id, events=events)


class InMemoryFeatureStore(FeatureStoreClient):
    def __init__(self, n_seed_users: int = 200, events_per_user: int = 15):
        self._users = [f"user-{i:05d}" for i in range(n_seed_users)]
        self._histories: dict[str, UserHistory] = {
            u: _synthetic_history(u, events_per_user, seed=i) for i, u in enumerate(self._users)
        }
        self._item_metadata: dict[str, dict] = {}

    async def get_user_history(self, user_id: str, limit: int = 20) -> UserHistory:
        if user_id not in self._histories:
            self._histories[user_id] = _synthetic_history(user_id, limit)
        h = self._histories[user_id]
        return UserHistory(user_id=user_id, events=h.events[:limit])

    async def get_item_metadata(self, item_id: str) -> dict:
        return self._item_metadata.get(item_id, {"item_id": item_id, "known": False})

    async def record_event(self, user_id: str, event: InteractionEvent) -> None:
        h = self._histories.setdefault(user_id, UserHistory(user_id=user_id, events=[]))
        h.events.insert(0, event)

    async def list_user_ids(self) -> list[str]:
        return list(self._histories.keys())


class RedisFeatureStore(FeatureStoreClient):
    """Same interface, backed by Redis so `platform/feature_store/app.py`
    can run as a real horizontally-scalable service with shared state
    across replicas (unlike InMemoryFeatureStore, which is per-process).
    Requires `redis>=5` (`redis.asyncio`), imported lazily so this module
    has no hard dependency on it for users who only need the in-memory
    backend."""

    def __init__(self, redis_url: str, n_seed_users: int = 200, events_per_user: int = 15):
        import redis.asyncio as redis  # local import: optional dependency

        self._redis = redis.from_url(redis_url, decode_responses=True)
        self._n_seed_users = n_seed_users
        self._events_per_user = events_per_user
        self._seeded = False

    async def _ensure_seeded(self):
        if self._seeded:
            return
        if await self._redis.exists("onerec:seeded"):
            self._seeded = True
            return
        for i in range(self._n_seed_users):
            user_id = f"user-{i:05d}"
            history = _synthetic_history(user_id, self._events_per_user, seed=i)
            await self._redis.set(f"onerec:history:{user_id}", json.dumps([asdict(e) for e in history.events]))
            await self._redis.sadd("onerec:users", user_id)
        await self._redis.set("onerec:seeded", "1")
        self._seeded = True

    async def get_user_history(self, user_id: str, limit: int = 20) -> UserHistory:
        await self._ensure_seeded()
        raw = await self._redis.get(f"onerec:history:{user_id}")
        if raw is None:
            history = _synthetic_history(user_id, limit)
            await self._redis.set(f"onerec:history:{user_id}", json.dumps([asdict(e) for e in history.events]))
            await self._redis.sadd("onerec:users", user_id)
            return history
        events = [InteractionEvent(**e) for e in json.loads(raw)[:limit]]
        return UserHistory(user_id=user_id, events=events)

    async def get_item_metadata(self, item_id: str) -> dict:
        raw = await self._redis.get(f"onerec:item:{item_id}")
        return json.loads(raw) if raw else {"item_id": item_id, "known": False}

    async def record_event(self, user_id: str, event: InteractionEvent) -> None:
        await self._ensure_seeded()
        raw = await self._redis.get(f"onerec:history:{user_id}")
        events = json.loads(raw) if raw else []
        events.insert(0, asdict(event))
        await self._redis.set(f"onerec:history:{user_id}", json.dumps(events))

    async def list_user_ids(self) -> list[str]:
        await self._ensure_seeded()
        return list(await self._redis.smembers("onerec:users"))
