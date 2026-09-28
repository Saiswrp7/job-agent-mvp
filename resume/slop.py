"""Words that make a resume line read as AI-written. No model.

RESUME_PLAN §9. The 2026-09-28 evals found 9 of 70 rewritten lines carrying
them: em dashes, "data-driven", "very", "orchestrating". The tailor prompt
asks for plain words; this is the part that holds when the prompt does not.

Only what the model *added* counts. A word already in their own bullet is
theirs, and taking it out would be rewriting them.
"""

from __future__ import annotations

import re

#: Same list the launch evals score against (evals/resume_run.py).
BANNED = ["spearheaded", "leveraged", "leverage", "orchestrated", "orchestrating",
          "utilize", "utilized", "synergy", "results-driven", "passionate",
          "dynamic", "delve", "robust", "tapestry", "testament", "go-getter",
          "team player", "detail-oriented", "self-starter", "proven track record",
          "think outside the box", "seamless", "cutting-edge", "data-driven"]
FILLER = ["responsible for", "helped", "various", "multiple", "very", "extremely"]
PRONOUN = re.compile(r"\b(I|me|my|we)\b")


def found(text: str) -> set[str]:
    low = (text or "").lower()
    hits = {w for w in BANNED + FILLER if re.search(rf"\b{re.escape(w)}\b", low)}
    hits |= {f'"{m}"' for m in PRONOUN.findall(text or "")}
    return hits


def added(original: str, rewritten: str) -> list[str]:
    """Slop in the rewrite that the original did not have."""
    return sorted(found(rewritten) - found(original))


def tidy(text: str) -> str:
    """Em dashes out. Sai's rule for anything a person reads, and the most
    common tell in the evals (4 of the 9 lines)."""
    return re.sub(r"\s*—\s*", ", ", text or "").replace(" ,", ",")
