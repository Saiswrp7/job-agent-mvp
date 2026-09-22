"""search() — the whole thing: message in, 5 jobs out.

Two LLM calls and one SQL query. No network call to any job board, because the
fetch engine already downloaded everything.
"""

from __future__ import annotations

from search import filters as filters_mod
from search import query, rank


def describe(f: dict) -> list[str]:
    """The filter, in the words someone would use to say it out loud.

    The translation from a sentence to a filter is the first place a search
    can go wrong, and it is invisible unless it is shown. A search that
    quietly added "3-5 years" looks identical to one that did not.
    """
    out = []
    if f.get("title_keywords"):
        out.append("title has " + " or ".join(f'"{k}"' for k in f["title_keywords"]))
    out.append(f"city is {f['city']}" if f.get("city") else "anywhere")
    for key, label in (("company_type", ""), ("industry", ""), ("stage", ""),
                       ("seniority", "")):
        if f.get(key):
            out.append(f"{label}{f[key]}")
    if f.get("companies"):
        out.append("at " + ", ".join(f["companies"]))
    out.append(f"posted in the last {f['posted_within_days']} days"
               if f.get("posted_within_days") else "no date limit")
    if f.get("must_mention"):
        out.append("prefers " + ", ".join(f["must_mention"]))
    return out


def search(message: str, *, profile: str = "", conn=None) -> dict:
    f = filters_mod.extract(message, profile=profile)
    rows, f = query.search(f, conn)

    # Sparse results relax in a fixed, logged order — never by asking a second
    # question, never by silently padding to hit the number.
    notes: list[str] = []
    while len(rows) < f["count"]:
        step = query.relax(f)
        if step is None:
            break
        f, note = step
        notes.append(note)
        rows, f = query.search(f, conn)

    picks, dropped = rank.pick(rows, f, message, profile=profile)

    open_jobs = 0
    if conn is not None:
        row = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE closed_at IS NULL").fetchone()
        open_jobs = row[0] if row else 0

    return {
        "picks": picks,
        "dropped": dropped,
        "filters": f,
        "notes": notes,
        "candidates": len(rows),
        # The funnel, for the live panel and the log. `matched == shown` means
        # the ranking step had no choice to make, which is a different answer
        # from "these were the best five" and should not look the same.
        # `looked_for` is prose for a person; `filter` is the machine-readable
        # thing that produced it. Both are stored: without the second, a real
        # conversation can never become an eval case, and every test sentence
        # has to be invented by hand instead of harvested from use.
        "funnel": {"open": open_jobs, "matched": len(rows), "shown": len(picks),
                   "looked_for": describe(f), "relaxed": notes,
                   "filter": {k: v for k, v in f.items() if v not in (None, "", [])}},
        "text": rank.format_results(picks, notes),
        "reference_table": rank.to_reference_table(picks),
    }
