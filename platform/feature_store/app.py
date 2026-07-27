"""Mock/reference feature-store service: a minimal real-time user-history
API a recommendation product would call before building a OneRec prompt.

Backend selectable via FEATURE_STORE_BACKEND=memory (default, per-process,
zero dependencies) or =redis (shared across replicas, requires `redis`
package + REDIS_URL). See adapter.py for the interface both implement and
exactly what to change to point this at a real feature store instead.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from dataclasses import asdict

from fastapi import FastAPI
from pydantic import BaseModel

from adapter import FeatureStoreClient, InMemoryFeatureStore, InteractionEvent, RedisFeatureStore
from prompt_builder import build_prompt_from_history

_store: FeatureStoreClient


def _build_store() -> FeatureStoreClient:
    backend = os.environ.get("FEATURE_STORE_BACKEND", "memory")
    if backend == "redis":
        return RedisFeatureStore(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
    return InMemoryFeatureStore()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _store
    _store = _build_store()
    yield


app = FastAPI(title="OneRec Feature Store (reference)", lifespan=lifespan)


class RecordEventRequest(BaseModel):
    item_id: str
    item_title: str
    category: str
    action: str
    timestamp: float
    extra: dict = {}


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/user/{user_id}/history")
async def get_history(user_id: str, limit: int = 20):
    history = await _store.get_user_history(user_id, limit=limit)
    return {"user_id": user_id, "events": [asdict(e) for e in history.events]}


@app.get("/user/{user_id}/prompt")
async def get_prompt(user_id: str, limit: int = 20):
    """Convenience endpoint: fetch history AND build the ready-to-send
    OneRec prompt in one call -- what the router or a benchmark script
    would use to get a real-shaped prompt for this user."""
    history = await _store.get_user_history(user_id, limit=limit)
    return {"user_id": user_id, "prompt": build_prompt_from_history(history)}


@app.get("/item/{item_id}/metadata")
async def get_item_metadata(item_id: str):
    return await _store.get_item_metadata(item_id)


@app.post("/user/{user_id}/history")
async def record_event(user_id: str, req: RecordEventRequest):
    await _store.record_event(user_id, InteractionEvent(**req.model_dump()))
    return {"status": "recorded"}


@app.get("/users")
async def list_users(limit: int = 50):
    ids = await _store.list_user_ids()
    return {"user_ids": ids[:limit], "total": len(ids)}
