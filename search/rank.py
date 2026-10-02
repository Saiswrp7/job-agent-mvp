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
import re

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

    raw = _ask(system, user)
    if _says_nothing(raw):
        # 20 APM jobs matched and the ranker picked none and rejected none:
        # "Nothing for APM roles today" (Telegram eval, 2026-10-01). The same
        # search picked 5 on a replay. An answer with no picks and no rejects
        # is a failed call, so ask once more.
        raw = _ask(system, user)

    # A bare list is the old shape. Accept it rather than fail: a provider that
    # ignores the new schema should cost the person their reject list, never
    # their search.
    if isinstance(raw, list):
        picks, dropped = raw, []
    elif isinstance(raw, dict):
        picks = raw.get("picks") or []
        dropped = raw.get("dropped") or []
    elif raw is None:
        picks, dropped = [], []
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
    if not chosen and not cut:
        # Still nothing, with jobs that matched every filter: show them in
        # the table's order rather than tell the person there are none.
        chosen = [{**r, "reason": ""} for r in rows[: filters["count"]]]
    # One job once: the replay listed Clickpost's APM role twice.
    seen, unique = set(), []
    for r in chosen:
        key = (r["source"], str(r["source_id"]))
        if key not in seen:
            seen.add(key)
            unique.append(r)
    return unique, cut


def _ask(system: str, user: str):
    try:
        return llm.complete_json(system, user, model=llm.RANK_MODEL(), max_tokens=4096)
    except ValueError:
        return None                                # unreadable: treated as empty


def _says_nothing(raw) -> bool:
    if raw is None or raw == [] or raw == {}:
        return True
    return isinstance(raw, dict) and not raw.get("picks") and not raw.get("dropped")


def ref(job: dict) -> str:
    """A short name for one job, used everywhere a job is addressed.

    **Why a name and not a position.** Tools used to take `n`, and every n from
    1 to 5 resolves to something — so a wrong one is not an error, it is a
    different job, applied to silently. The agent renumbers the list when it
    writes the reply, so the n the person typed and the n the tool used had
    already drifted apart in half the conversations we recorded.

    A name can be wrong, and a wrong name raises. That is the entire point.
    Derived from company and title, so it is also readable in a log.
    """
    slug = re.sub(r"[^a-z0-9]+", "-",
                  f"{job.get('company','')} {job.get('title','')}".lower())
    return slug.strip("-")[:48] or str(job.get("source_id", "job"))


def format_results(picks: list[dict], notes: list[str] | None = None,
                   numbered: bool = True) -> str:
    """`numbered=False` is what the chat agent gets.

    A numbered tool result is a second numbered list in the context, in search
    order, competing with the one the agent writes to the person in its own
    order. Asked to "tailor my resume for 1", the model took 1 from this list
    rather than from its own reply and tailored for a different job. Only the
    reply the person actually reads should carry numbers.
    """
    if not picks:
        # The notes are the useful part of an empty result — what was tried,
        # what was kept, what exists nearby. Dropping them left the agent with
        # nothing to offer but "try widening".
        if notes:
            return "Nothing matched. (" + "; ".join(notes) + ")"
        return "Nothing matched. Try widening the city or the date range."
    lines = []
    for i, r in enumerate(picks, 1):
        posted = (r.get("posted_at") or "")[:10]
        where = r.get("city") or r.get("location") or ""
        head = f"{i}. " if numbered else ""
        lines.append(
            f"{head}[{ref(r)}] {r['title']} — {r['company']}, {where} · {posted}\n"
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
        [{"ref": ref(r), "source": r["source"], "source_id": r["source_id"],
          "title": r["title"], "company": r["company"]}
         for r in picks]
    )
