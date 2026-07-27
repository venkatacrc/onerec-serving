"""Builds a OneRec-8B-Pro prompt from a REAL (or feature-store-shaped)
`UserHistory` record, as the production-equivalent of
`bench/prompt_dataset.py`'s synthetic generator.

Same preamble/instruction framing as the benchmark's synthetic prompts (so
latency/throughput numbers measured on synthetic vs. real-shaped prompts
stay comparable), but the interaction-history body comes from an actual
`FeatureStoreClient.get_user_history()` call instead of a fixed fragment
pool -- this is the piece `docs/BENCHMARK_METHODOLOGY.md` §6 flagged as
missing ("Real production traffic shape").
"""
from __future__ import annotations

from adapter import UserHistory

_SYSTEM_PREAMBLE = (
    "You are a generative recommendation assistant. Given a user's recent "
    "interaction history below, predict the next 5 items the user is most "
    "likely to engage with, and briefly justify each recommendation using "
    "the observed behavioral signals.\n\nUser interaction history:\n"
)

_TRAILING_INSTRUCTION = (
    "\n\nBased on the above, list the top 5 recommended items with a one-line "
    "justification for each."
)


def _format_event(idx: int, e) -> str:
    rating = f", rated {e.extra.get('rating')}/5" if e.extra.get("rating") else ""
    return f"  {idx}. {e.action} '{e.item_title}' ({e.category}){rating}"


def build_prompt_from_history(history: UserHistory, max_events: int = 30) -> str:
    lines = [_format_event(i + 1, e) for i, e in enumerate(history.events[:max_events])]
    if not lines:
        lines = ["  (no recent interaction history for this user)"]
    return _SYSTEM_PREAMBLE + "\n".join(lines) + _TRAILING_INSTRUCTION
