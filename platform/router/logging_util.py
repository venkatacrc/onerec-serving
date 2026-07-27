"""Structured JSON request/response logging with PII handling.

Design for a recommendation product: user identifiers and prompt bodies
(which embed a user's interaction history -- watched/purchased/clicked
items, i.e. behavioral PII) must never land in plaintext logs by default.

- `user_id` is one-way hashed (SHA-256, truncated) -- enough to correlate
  a user's requests across log lines for debugging/rate-limit investigation
  without recovering the identifier, and the hash is *not* the same as any
  hash used elsewhere (no salt reuse across services) since this module
  owns its own fixed salt purely to prevent trivial rainbow-table reversal
  by anyone with only log access, not as a security boundary on its own --
  treat log access control as the real control.
- prompt text is never logged by default; `log_prompt_preview=True`
  (operator opt-in, e.g. for a debugging session) logs only the first
  `prompt_preview_chars` characters, which is still a product/PII decision
  operators should make deliberately per environment, not a default.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sys
import time

_LOG_SALT = b"onerec-router-log-salt-v1"


def hash_user_id(user_id: str | None) -> str | None:
    if not user_id:
        return None
    return hashlib.sha256(_LOG_SALT + user_id.encode("utf-8", errors="ignore")).hexdigest()[:16]


def redact_prompt(prompt: str, preview_enabled: bool, preview_chars: int) -> str | None:
    if not preview_enabled:
        return None
    return prompt[:preview_chars] + ("...[redacted-tail]" if len(prompt) > preview_chars else "")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "fields", None)
        if extra:
            payload.update(extra)
        return json.dumps(payload, default=str)


def get_logger(name: str = "onerec.router") -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


def log_request(
    logger: logging.Logger,
    *,
    request_id: str,
    trace_id: str | None,
    user_id: str | None,
    group: str,
    backend: str | None,
    strategy: str,
    status: str,
    http_status: int,
    latency_ms: float,
    queue_wait_ms: float,
    degraded: bool,
    prompt_len: int,
    prompt_preview: str | None,
    error: str | None = None,
):
    fields = {
        "request_id": request_id,
        "trace_id": trace_id,
        "user_id_hash": hash_user_id(user_id),
        "group": group,
        "backend": backend,
        "routing_strategy": strategy,
        "status": status,
        "http_status": http_status,
        "latency_ms": round(latency_ms, 2),
        "queue_wait_ms": round(queue_wait_ms, 2),
        "degraded": degraded,
        "prompt_len_chars": prompt_len,
    }
    if prompt_preview is not None:
        fields["prompt_preview"] = prompt_preview
    if error:
        fields["error"] = error
    logger.info("request_complete", extra={"fields": fields})
