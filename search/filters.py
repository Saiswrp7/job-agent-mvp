"""Message -> filter JSON. One LLM call, low effort, no loop."""

from __future__ import annotations

import llm
from search.query import DEFAULTS, normalize

#: Fields SQL can act on. Anything else the model invents is dropped rather
#: than passed through, so a hallucinated key can never reach the query.
ALLOWED = set(DEFAULTS)


def extract(message: str, *, profile: str = "") -> dict:
    system = llm.prompt("filters")
    if profile:
        system += f"\n\n## About this user (context only, do not filter on it)\n{profile}"

    raw = llm.complete_json(system, message, effort="low", max_tokens=1024)
    if not isinstance(raw, dict):
        raise ValueError(f"filters: expected an object, got {type(raw).__name__}")

    clean = {k: v for k, v in raw.items() if k in ALLOWED}
    for k in ("title_keywords", "must_mention", "companies"):
        v = clean.get(k)
        if isinstance(v, str):
            clean[k] = [v]
        elif v is not None and not isinstance(v, list):
            clean[k] = None
    return normalize(clean)
