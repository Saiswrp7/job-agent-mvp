"""The patch sanity gate. No model — that is the point.

"Rephrase, never invent" is a prompt instruction, and prompt instructions get
talked around. This is the mechanical version: a rewritten bullet that
introduces a number, a company, or a product name the original did not contain
is reverted to its original text.

Fabricated experience reaching a recruiter is permanent damage, and it is
cheap to make impossible.
"""

from __future__ import annotations

import re

NUM = re.compile(r"\d[\d,\.]*\s*(?:%|x|k|m|cr|lakh|lpa|bn)?", re.I)
PROPER = re.compile(r"\b[A-Z][a-zA-Z0-9]*(?:\.[a-z]+)?\b")

#: Sentence-initial and common words that capitalize without being entities.
STOPWORDS = {
    "I", "A", "An", "The", "And", "Or", "But", "For", "With", "By", "At", "In",
    "On", "To", "From", "Of", "As", "Led", "Built", "Drove", "Owned", "Grew",
    "Ran", "Managed", "Launched", "Scaled", "Reduced", "Increased", "Improved",
    "Designed", "Created", "Delivered", "Partnered", "Worked", "Developed",
    "Shipped", "Defined", "Established", "Coordinated", "Analyzed", "Drove",
}


def _numbers(s: str) -> set[str]:
    return {m.group(0).replace(" ", "").lower().rstrip(".") for m in NUM.finditer(s)}


def _propers(s: str) -> set[str]:
    return {w for w in PROPER.findall(s) if w not in STOPWORDS and len(w) > 2}


def check_bullet(original: str, rewritten: str) -> list[str]:
    problems = []
    new_nums = _numbers(rewritten) - _numbers(original)
    if new_nums:
        problems.append(f"invented number(s): {sorted(new_nums)}")
    new_names = _propers(rewritten) - _propers(original)
    if new_names:
        problems.append(f"invented name(s): {sorted(new_names)}")
    return problems


def apply_patch(master: dict, patch: dict) -> tuple[dict, list[str]]:
    """Returns (tailored_master, reverted_notes).

    A failing bullet is reverted, not rejected — one bad rewrite should cost
    that bullet's improvement, not the whole resume.
    """
    out = {**master, "experience": []}
    notes: list[str] = []

    if patch.get("summary"):
        out["summary"] = patch["summary"]

    by_index = {int(p["index"]): p for p in patch.get("experience", [])
                if "index" in p}

    for i, job in enumerate(master.get("experience", [])):
        p = by_index.get(i)
        bullets = list(job.get("bullets", []))

        if p:
            keep = p.get("keep")
            rewrites = {int(k): v for k, v in (p.get("rewrites") or {}).items()}
            order = keep if isinstance(keep, list) and keep else range(len(bullets))

            chosen = []
            for idx in order:
                if not (0 <= idx < len(bullets)):
                    continue
                original = bullets[idx]
                text = rewrites.get(idx, original)
                if text != original:
                    problems = check_bullet(original, text)
                    if problems:
                        notes.append(
                            f"{job.get('company','?')} bullet {idx}: reverted "
                            f"({'; '.join(problems)})"
                        )
                        text = original
                chosen.append(text)
            bullets = chosen or bullets

        out["experience"].append({**job, "bullets": bullets})

    # Locked fields must be byte-identical. Nothing in the patch can reach
    # them, but assert it rather than trust it.
    for i, job in enumerate(master.get("experience", [])):
        for field in ("company", "title", "dates"):
            if out["experience"][i].get(field) != job.get(field):
                raise AssertionError(f"locked field changed: {field}")

    return out, notes
