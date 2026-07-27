"""Backend pool: per-replica admission control, outbound circuit breaker,
and background health checking.

One `Backend` = one engine replica (one vLLM/SGLang/TensorRT-LLM
OpenAI-compatible server). A `BackendPool` groups backends by traffic group
("stable"/"canary") and exposes them to the routing strategies in
`routing.py`.
"""
from __future__ import annotations

import asyncio
import enum
import time
from dataclasses import dataclass, field

import httpx

from config import RouterSettings


class BreakerState(str, enum.Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Simple failure-count breaker, one instance per backend.

    CLOSED -> OPEN after `failure_threshold` consecutive failures.
    OPEN -> HALF_OPEN after `open_seconds` have elapsed.
    HALF_OPEN -> CLOSED after `half_open_probes` consecutive successes;
    HALF_OPEN -> OPEN immediately on any failure.
    """

    def __init__(self, failure_threshold: int, open_seconds: float, half_open_probes: int):
        self.failure_threshold = failure_threshold
        self.open_seconds = open_seconds
        self.half_open_probes = half_open_probes
        self._state = BreakerState.CLOSED
        self._consecutive_failures = 0
        self._consecutive_successes = 0
        self._opened_at: float = 0.0

    @property
    def state(self) -> BreakerState:
        if self._state == BreakerState.OPEN and (time.monotonic() - self._opened_at) >= self.open_seconds:
            self._state = BreakerState.HALF_OPEN
            self._consecutive_successes = 0
        return self._state

    def allow_request(self) -> bool:
        return self.state in (BreakerState.CLOSED, BreakerState.HALF_OPEN)

    def record_success(self):
        self._consecutive_failures = 0
        if self._state == BreakerState.HALF_OPEN:
            self._consecutive_successes += 1
            if self._consecutive_successes >= self.half_open_probes:
                self._state = BreakerState.CLOSED

    def record_failure(self):
        self._consecutive_successes = 0
        if self._state == BreakerState.HALF_OPEN:
            self._state = BreakerState.OPEN
            self._opened_at = time.monotonic()
            return
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.failure_threshold:
            self._state = BreakerState.OPEN
            self._opened_at = time.monotonic()


@dataclass
class Backend:
    url: str
    group: str
    settings: RouterSettings

    outstanding: int = 0
    queued: int = 0
    healthy: bool = True
    total_requests: int = 0
    total_failures: int = 0
    last_health_check_ok: bool = True
    last_health_check_at: float = 0.0

    breaker: CircuitBreaker = field(init=False)
    _sem: asyncio.Semaphore = field(init=False)

    def __post_init__(self):
        self.breaker = CircuitBreaker(
            self.settings.breaker_failure_threshold,
            self.settings.breaker_open_seconds,
            self.settings.breaker_half_open_probes,
        )
        self._sem = asyncio.Semaphore(self.settings.max_inflight_per_backend)

    @property
    def id(self) -> str:
        return self.url

    @property
    def available(self) -> bool:
        """Can this backend accept a *new* request right now (used by
        routing strategies to pick the least-loaded candidate, and by
        admission control to decide whether to queue vs. reject)."""
        return self.healthy and self.breaker.allow_request()

    @property
    def at_capacity(self) -> bool:
        return self.outstanding >= self.settings.max_inflight_per_backend

    def load_score(self) -> float:
        """Lower is better. Used by least_outstanding / prefix_hash
        tie-breaking. Unhealthy/open-breaker backends sort last."""
        if not self.available:
            return float("inf")
        return self.outstanding + 0.1 * self.queued

    async def acquire_slot(self, timeout_s: float) -> bool:
        """Blocks (counted as 'queued') until a concurrency slot frees up,
        up to `timeout_s`. Returns False on timeout (caller should treat
        this as admission-control rejection, i.e. HTTP 429)."""
        self.queued += 1
        try:
            await asyncio.wait_for(self._sem.acquire(), timeout=timeout_s)
            return True
        except asyncio.TimeoutError:
            return False
        finally:
            self.queued -= 1

    def release_slot(self):
        self._sem.release()


class BackendPool:
    def __init__(self, settings: RouterSettings):
        self.settings = settings
        self.backends: list[Backend] = [Backend(url=b.url, group=b.group, settings=settings) for b in settings.backends]
        self._rr_cursor: dict[str, int] = {}
        self._health_task: asyncio.Task | None = None
        self._client: httpx.AsyncClient | None = None

    def group(self, name: str) -> list[Backend]:
        return [b for b in self.backends if b.group == name]

    def group_names(self) -> list[str]:
        seen = []
        for b in self.backends:
            if b.group not in seen:
                seen.append(b.group)
        return seen

    def next_round_robin_index(self, group: str, n: int) -> int:
        idx = self._rr_cursor.get(group, 0)
        self._rr_cursor[group] = (idx + 1) % max(n, 1)
        return idx

    async def start_health_checks(self):
        self._client = httpx.AsyncClient(timeout=self.settings.health_check_timeout_s)
        self._health_task = asyncio.create_task(self._health_loop())

    async def stop_health_checks(self):
        if self._health_task:
            self._health_task.cancel()
        if self._client:
            await self._client.aclose()

    async def _health_loop(self):
        while True:
            await asyncio.gather(*(self._check_one(b) for b in self.backends), return_exceptions=True)
            await asyncio.sleep(self.settings.health_check_interval_s)

    async def _check_one(self, b: Backend):
        try:
            resp = await self._client.get(f"{b.url}{self.settings.health_check_path}")
            b.healthy = resp.status_code == 200
        except Exception:
            b.healthy = False
        b.last_health_check_ok = b.healthy
        b.last_health_check_at = time.time()

    def snapshot(self) -> list[dict]:
        return [
            {
                "url": b.url,
                "group": b.group,
                "healthy": b.healthy,
                "breaker_state": b.breaker.state.value,
                "outstanding": b.outstanding,
                "queued": b.queued,
                "total_requests": b.total_requests,
                "total_failures": b.total_failures,
                "load_score": b.load_score(),
            }
            for b in self.backends
        ]
