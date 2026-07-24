"""Synthetic but domain-flavored prompt generation for benchmarking OneRec-8B-Pro.

OneRec is a *generative recommendation* model: its real inputs look like
user interaction histories (watched/purchased/clicked items + metadata)
rather than generic chit-chat. We build prompts out of a small pool of
recommendation-domain sentence fragments (repeated/shuffled) and trim them
to an exact token length using the model's own tokenizer, so that latency
and throughput numbers reflect realistic prefill costs instead of being
skewed by an unrepresentative prompt distribution.

Falls back to a crude chars-per-token heuristic if the tokenizer can't be
loaded (e.g. transformers not installed), which is fine for smoke-testing
but should not be used for numbers you plan to present to architects.
"""
from __future__ import annotations

import itertools
import random
from functools import lru_cache
from typing import Optional

_ITEM_FRAGMENTS = [
    "watched 'Midnight Signal' (Sci-Fi Thriller, 2024) and rated it 4.5/5",
    "clicked on 'Coastal Kitchen' (Cooking Show, S3E12) but dropped off after 40 seconds",
    "purchased 'TrailRunner Pro Hiking Boots' (Outdoor Gear, $129.99)",
    "added 'Quantum Finance Basics' (Ad, Fintech) to favorites",
    "skipped 'Late Night Comedy Hour' (Talk Show) after 3 seconds",
    "shared 'Urban Skyline Timelapse' (Short Video, Travel) with 2 friends",
    "liked 'Homegrown Garden Tips' (Short Video, Lifestyle)",
    "commented on 'Street Food Diaries: Bangkok' (Short Video, Food)",
    "browsed 'Wireless Noise-Cancelling Headphones' (Product, Electronics) for 90 seconds",
    "returned 'Compact Air Fryer 4L' (Product, Home) after one use",
    "followed the creator behind 'Minimalist Desk Setups' (Short Video, Tech)",
    "re-watched 'Grandmaster Chess Endgames' (Educational, Ep. 7) twice",
    "left a 2-star review on 'Budget Wireless Earbuds X200' (Product, Electronics)",
    "saved 'Weekend Trail Map: Sierra Foothills' (Ad, Outdoor) for later",
    "muted recommendations similar to 'True Crime Weekly' (Podcast, Crime)",
]

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


@lru_cache(maxsize=1)
def _load_tokenizer(tokenizer_path: Optional[str]):
    if not tokenizer_path:
        return None
    try:
        from transformers import AutoTokenizer  # local import: optional dep

        return AutoTokenizer.from_pretrained(tokenizer_path, trust_remote_code=True)
    except Exception as exc:  # noqa: BLE001 - best-effort fallback is intentional
        print(f"[prompt_dataset] WARNING: could not load tokenizer from "
              f"'{tokenizer_path}' ({exc}); falling back to chars/4 heuristic.")
        return None


def count_tokens(text: str, tokenizer_path: Optional[str]) -> int:
    tok = _load_tokenizer(tokenizer_path)
    if tok is not None:
        return len(tok.encode(text))
    return max(1, len(text) // 4)


def build_prompt(target_input_tokens: int, tokenizer_path: Optional[str], seed: int = 0) -> str:
    """Builds a prompt whose tokenized length is as close as possible to
    `target_input_tokens` (never exceeding it), using shuffled recommendation
    domain fragments padded with the standard system preamble/instruction."""
    rng = random.Random(seed)
    fragments = _ITEM_FRAGMENTS[:]
    rng.shuffle(fragments)
    cycle = itertools.cycle(fragments)

    tok = _load_tokenizer(tokenizer_path)
    body_lines: list[str] = []
    budget = target_input_tokens - count_tokens(_SYSTEM_PREAMBLE + _TRAILING_INSTRUCTION, tokenizer_path)
    budget = max(budget, 8)

    used = 0
    idx = 1
    while used < budget:
        frag = next(cycle)
        line = f"  {idx}. {frag}"
        line_tokens = count_tokens(line + "\n", tokenizer_path)
        if used + line_tokens > budget and body_lines:
            break
        body_lines.append(line)
        used += line_tokens
        idx += 1
        if idx > 500:  # safety valve for pathological tokenizers
            break

    prompt = _SYSTEM_PREAMBLE + "\n".join(body_lines) + _TRAILING_INSTRUCTION

    # Trim precisely if we overshot (rare, only with very small targets).
    if tok is not None:
        ids = tok.encode(prompt)
        if len(ids) > target_input_tokens:
            ids = ids[:target_input_tokens]
            prompt = tok.decode(ids)
    return prompt


def build_prompt_pool(target_input_tokens: int, tokenizer_path: Optional[str], n: int) -> list[str]:
    return [build_prompt(target_input_tokens, tokenizer_path, seed=i) for i in range(n)]
