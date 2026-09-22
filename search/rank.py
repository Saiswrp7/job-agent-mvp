"""20 rows -> 5 picks with reasons. One LLM call.

This is where everything SQL cannot express gets decided: is the company really
B2C, is this a growth job or a CRM job in disguise, are the skills actually
required or a nice-to-have at the bottom of the JD.

It is also the only expensive call in the system — the job descriptions are
essentially the entire token bill — so `DESC_CHARS` is the one number to turn
if cost becomes a problem. Shrink the shortlist before shrinking the text:
requirements usually sit at the bottom of a JD, which is exactly what
truncation cuts.
"""

from __future__ import annotations

import json

import llm

DESC_CHARS = 3500


def _render(rows: list[dict]) -> str:
    out = []
    for r in rows:
        desc = (r.get("description") or "")[:DESC_CHARS]
        years = ""
        if r.get("years_min"):
            years = f" · wants {r['years_min']}"
            if r.get("years_max"):
                years += f"-{r['years_max']}"
            years += " yrs"
        out.append(
            f"--- {r['source']}/{r['source_id']}\n"
            f"{r['title']} at {r['company']}\n"
            f"{r.get('city') or r.get('location') or '?'}"
            f"{' · remote' if r.get('remote') else ''}"
            f" · {r.get('company_type') or '?'} · {r.get('industry') or '?'}"
            f" · posted {(r.get('posted_at') or '?')[:10]}{years}\n\n"
            f"{desc}\n"
        )
    return "\n".join(out)


def pick(rows: list[dict], filters: dict, message: str,
         profile: str = "") -> tuple[list[dict], list[dict]]:
    """Returns (chosen, dropped).

    `profile` is what the person has said about themselves over time and
    `soft_criteria` is what they asked for just now. Between them they are the
    only preferences this step has: the prompt carries no taste of its own, so
    anything it rules out can be argued with by saying something different.

    `dropped` exists for the same reason. A list of five jobs looks equally
    confident whether the other fifteen were junk or whether one of them was
    the job they wanted — showing what was rejected, and why, is the only way
    they can tell the difference and push back.
    """
    if not rows:
        return [], []

    system = (llm.prompt("rank")
              .replace("{profile}", profile.strip() or "(nothing yet)")
              .replace("{soft_criteria}",
                       (filters.get("soft_criteria") or "").strip()
                       or "(nothing beyond the filters)"))
    user = (
        f"They asked: {message!r}\n"
        f"Return at most {filters['count']} jobs.\n\n"
        f"{_render(rows)}"
    )

    raw = llm.complete_json(system, user, model=llm.RANK_MODEL(), max_tokens=4096)

    # A bare list is the old shape. Accept it rather than fail: a provider that
    # ignores the new schema should cost the person their reject list, never
    # their search.
    if isinstance(raw, list):
        picks, dropped = raw, []
    elif isinstance(raw, dict):
        picks = raw.get("picks") or []
        dropped = raw.get("dropped") or []
    else:
        raise ValueError(f"rank: expected an object, got {type(raw).__name__}")

    by_id = {(r["source"], str(r["source_id"])): r for r in rows}

    def resolve(items: list, note: str) -> list[dict]:
        out = []
        for p in items:
            if not isinstance(p, dict):
                continue
            row = by_id.get((p.get("source"), str(p.get("source_id"))))
            if row is None:      # model named a job that wasn't in the list
                continue
            out.append({**row, note: p.get(note, "")})
        return out

    chosen = resolve(picks, "reason")[: filters["count"]]
    shown = {(r["source"], str(r["source_id"])) for r in chosen}
    # Never let a job appear as both picked and rejected.
    cut = [d for d in resolve(dropped, "why")
           if (d["source"], str(d["source_id"])) not in shown]
    return chosen, cut


def format_results(picks: list[dict], notes: list[str] | None = None) -> str:
    if not picks:
        return "Nothing matched. Try widening the city or the date range."
    lines = []
    for i, r in enumerate(picks, 1):
        posted = (r.get("posted_at") or "")[:10]
        where = r.get("city") or r.get("location") or ""
        lines.append(
            f"{i}. {r['title']} — {r['company']}, {where} · {posted}\n"
            f"   {r['reason']}\n"
            f"   {r.get('apply_url') or r.get('url')}"
        )
    body = "\n\n".join(lines)
    if notes:
        body += "\n\n(" + "; ".join(notes) + ")"
    return body


def to_reference_table(picks: list[dict]) -> str:
    """The compact form kept in chat context so "apply to 2" resolves.

    Titles and companies only — never the descriptions, which is the whole
    point of keeping it separate from the search result.
    """
    return json.dumps(
        [{"n": i, "source": r["source"], "source_id": r["source_id"],
          "title": r["title"], "company": r["company"]}
         for i, r in enumerate(picks, 1)]
    )
