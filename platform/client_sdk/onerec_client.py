#!/usr/bin/env python3
"""Reference client SDK for calling services talking to the OneRec router.

Demonstrates the resilience patterns any real calling service (e.g. a
recommendation-surface backend, a batch scoring job) should use instead of
hitting the router with a bare HTTP client:

  - exponential backoff + full jitter retries on transient failures
    (connection errors, timeouts, 5xx, 429 honoring `Retry-After`)
  - a client-side circuit breaker per router endpoint, so a struggling
    router/downstream doesn't get hammered by every caller's retries at
    once (this is a DIFFERENT breaker than the router's own outbound
    breaker to its backends -- this one protects the router from clients,
    the router's protects engines from the router)
  - a local heuristic fallback if the breaker is open or retries exhaust,
    so a calling service degrades gracefully instead of propagating a hard
    failure to its own callers

This mirrors platform/router/backend_pool.py's CircuitBreaker on purpose
(same state machine) but is intentionally a separate, dependency-free
implementation: a client SDK ships to many other services/repos and should
not import router internals.
"""
from __future__ import annotations

import asyncio
import enum
import random
import time
from dataclasses import dataclass
from typing import Optional

import httpx

_LOCAL_FALLBACK_TEXT = (
    "[client-sdk local fallback] Router unreachable/open-circuit; "
    "serving a static heuristic recommendation instead of failing the caller."
)


class BreakerState(str, enum.Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    def __init__(self, failure_threshold: int = 5, open_seconds: float = 15.0, half_open_probes: int = 1):
        self.failure_threshold = failure_threshold
        self.open_seconds = open_seconds
        self.half_open_probes = half_open_probes
        self._state = BreakerState.CLOSED
        self._consecutive_failures = 0
        self._consecutive_successes = 0
        self._opened_at = 0.0

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
class RetryPolicy:
    max_attempts: int = 4
    base_delay_s: float = 0.2
    max_delay_s: float = 5.0

    def delay_for_attempt(self, attempt: int, retry_after_s: Optional[float] = None) -> float:
        """Full-jitter exponential backoff (attempt is 0-indexed), honoring
        a server-provided Retry-After if present (never retry sooner than
        the server asked)."""
        exp_delay = min(self.max_delay_s, self.base_delay_s * (2 ** attempt))
        jittered = random.uniform(0, exp_delay)
        if retry_after_s is not None:
            return max(jittered, retry_after_s)
        return jittered


class CircuitOpenError(RuntimeError):
    pass


class OneRecClient:
    """Usage:

        client = OneRecClient(base_url="http://onerec-router:9000")
        result = await client.complete("some prompt", max_tokens=128)
        if result.degraded:
            ... # log/metric that this response was a fallback, not a real generation
    """

    def __init__(
        self,
        base_url: str,
        served_model_name: str = "onerec-8b-pro",
        timeout_s: float = 30.0,
        retry_policy: Optional[RetryPolicy] = None,
        breaker: Optional[CircuitBreaker] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.served_model_name = served_model_name
        self.retry_policy = retry_policy or RetryPolicy()
        self.breaker = breaker or CircuitBreaker()
        self._client = httpx.AsyncClient(timeout=timeout_s)

    async def aclose(self):
        await self._client.aclose()

    async def complete(self, prompt: str, max_tokens: int = 128, user_id: Optional[str] = None) -> "CompletionResult":
        if not self.breaker.allow_request():
            return CompletionResult(text=_LOCAL_FALLBACK_TEXT, degraded=True, degraded_reason="circuit_open")

        headers = {"X-User-Id": user_id} if user_id else {}
        payload = {"model": self.served_model_name, "prompt": prompt, "max_tokens": max_tokens, "stream": False}

        last_error: Optional[str] = None
        for attempt in range(self.retry_policy.max_attempts):
            try:
                resp = await self._client.post(f"{self.base_url}/v1/completions", json=payload, headers=headers)
            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                self.breaker.record_failure()
                await asyncio.sleep(self.retry_policy.delay_for_attempt(attempt))
                continue

            if resp.status_code == 200:
                self.breaker.record_success()
                body = resp.json()
                degraded = bool(body.get("degraded"))
                return CompletionResult(text=_extract_text(body), degraded=degraded,
                                         degraded_reason=body.get("degraded_source") if degraded else None)

            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", 1))
                last_error = "HTTP 429 (admission control)"
                await asyncio.sleep(self.retry_policy.delay_for_attempt(attempt, retry_after_s=retry_after))
                continue  # 429 is expected backpressure, not a breaker failure

            if resp.status_code >= 500:
                last_error = f"HTTP {resp.status_code}"
                self.breaker.record_failure()
                await asyncio.sleep(self.retry_policy.delay_for_attempt(attempt))
                continue

            # 4xx other than 429: caller error, don't retry, don't trip breaker.
            return CompletionResult(text="", degraded=True, degraded_reason=f"HTTP {resp.status_code}", error=resp.text[:300])

        return CompletionResult(text=_LOCAL_FALLBACK_TEXT, degraded=True,
                                 degraded_reason="retries_exhausted", error=last_error)


@dataclass
class CompletionResult:
    text: str
    degraded: bool
    degraded_reason: Optional[str] = None
    error: Optional[str] = None


def _extract_text(body: dict) -> str:
    choices = body.get("choices") or []
    if choices:
        return choices[0].get("text", "")
    return ""
