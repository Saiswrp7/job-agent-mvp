"""search() — the whole thing: message in, 5 jobs out.

Two LLM calls and one SQL query. No network call to any job board, because the
fetch engine already downloaded everything.
"""

from __future__ import annotations

import re

from engine import db
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
    if f.get("role_family"):
        out.append("kind of work: " + " or ".join(f["role_family"]))
    if f.get("level"):
        out.append("level: " + " or ".join(f["level"]))
    if f.get("country"):
        out.append(f"in {f['country']}")
    if f.get("owns_pnl"):
        out.append("owns a P&L or revenue line first")
    if f.get("companies"):
        out.append("at " + ", ".join(f["companies"]))
    out.append(f"posted in the last {f['posted_within_days']} days"
               if f.get("posted_within_days") else "no date limit")
    if f.get("must_mention"):
        out.append("prefers " + ", ".join(f["must_mention"]))
    return out


#: "find me 3 …", "show me 5 …", "get 4 …". Only a count that is clearly about
#: the results — a bare number anywhere in the sentence would catch "3 years".
_COUNT = re.compile(
    r"\b(?:find|show|get|give|send)\s+(?:me\s+)?(?:some\s+)?(\d{1,2})\b", re.I)


def wanted_count(said: str) -> int | None:
    """How many they asked for, read straight out of their sentence.

    Not left to the model, because it is not a judgment call and the model kept
    losing it: seven sentences with an explicit count produced a count of 5
    every time. The agent paraphrases before calling the tool, and the number
    is what falls out of the paraphrase — "find me 3 growth roles in India"
    reaches the search as "growth roles in India".

    Same division the rest of the system already uses: SQL narrows on things
    that are columns, the model judges what is not one. A count is a column.
    """
    m = _COUNT.search(said or "")
    if not m:
        return None
    n = int(m.group(1))
    return n if 1 <= n <= 20 else None


#: Spellings of the same place, so "Bengaluru" counts as saying "Bangalore".
#: Levels too: "fresher" is asking for junior, and was told it had not
#: (2026-09-28), which is part of why a fresher search showed nothing.
_SAME = [{"bangalore", "bengaluru", "blr"}, {"gurgaon", "gurugram", "ggn"},
         {"delhi ncr", "delhi", "new delhi", "ncr"}, {"mumbai", "bombay"},
         {"junior", "fresher", "freshers", "entry level", "entry-level",
          "graduate", "passout", "new grad", "jr"},
         {"senior", "sr"}, {"intern", "internship", "internships"}]

#: Any word that asks for a level. With none of them in their sentence, the
#: level stays open: "growth manager jobs" got `senior` added one time in
#: three, because "manager" sounds senior to a model. It is not a level.
_LEVEL_SAID = re.compile(
    r"\b(senior|sr|junior|jr|lead|leads|head|principal|director|vp|chief|"
    r"fresher|freshers|entry[- ]level|graduate|grads?|passout|pass[- ]out|"
    r"interns?|internships?|mid[- ]level|leadership|executive|"
    r"\d{1,2}\s*\+?\s*(?:years?|yrs?))\b", re.I)

#: Cities read straight from their sentence, for when the model leaves the
#: city empty. "growth jobs in Delhi NCR" came back with no city one time in
#: three, and those searches returned all of India.
_CITY_SAID = [
    (re.compile(r"\b(?:delhi[\s-]*ncr|ncr)\b", re.I), "Delhi NCR"),
    (re.compile(r"\b(?:bangalore|bengaluru|blr|banglore)\b", re.I), "Bangalore"),
    (re.compile(r"\b(?:mumbai|bombay)\b", re.I), "Mumbai"),
    (re.compile(r"\b(?:gurgaon|gurugram)\b", re.I), "Gurgaon"),
    (re.compile(r"\bnoida\b", re.I), "Noida"),
    (re.compile(r"\b(?:new\s+)?delhi\b", re.I), "Delhi"),
    (re.compile(r"\bhyderabad\b", re.I), "Hyderabad"),
    (re.compile(r"\bpune\b", re.I), "Pune"),
    (re.compile(r"\bchennai\b", re.I), "Chennai"),
    (re.compile(r"\b(?:kolkata|calcutta)\b", re.I), "Kolkata"),
]

#: "near me", "in my city": asking for where they live without naming it.
_NEAR_ME = re.compile(r"\b(near me|nearby|my city|where i live|locally|local)\b",
                      re.I)


def city_said(said: str) -> str | None:
    """The city in their own sentence, if exactly one is named."""
    found = [name for rx, name in _CITY_SAID if rx.search(said or "")]
    # "Delhi NCR" also matches "delhi"; the region is what they asked for.
    if "Delhi NCR" in found:
        found = [c for c in found if c != "Delhi"]
    return found[0] if len(found) == 1 else None


def guard(f: dict, said: str) -> list[str]:
    """Hold the filter to what they said, in code. Returns notes.

    The prompt already says "fill only what the user said". The model still
    added `senior` to "growth manager jobs", dropped "Delhi NCR", and the chat
    agent searched again with Bengaluru added after an empty result. A prompt
    line did not hold, so these are checked against their own words here.
    """
    notes = []
    if (f.get("seniority") or f.get("level")) and not _LEVEL_SAID.search(said or ""):
        f["seniority"], f["level"] = None, None
    if f.get("city") and not mentioned(f["city"], said) \
            and not _NEAR_ME.search(said or ""):
        notes.append(f"left out city {f['city']}: they did not name a city — "
                     f"ask them if they want one")
        f["city"] = None
    if not f.get("city"):
        named = city_said(said)
        if named:
            f["city"] = named
    return notes


def mentioned(value, said: str) -> bool:
    """Did they say this — allowing other spellings and typos?

    Exact substring was the old test, and it told someone who typed
    "banagalroe" that they had not asked for Bangalore.
    """
    import difflib
    v, low = str(value).lower().strip(), (said or "").lower()
    if not v:
        return True
    names = next((g for g in _SAME if v in g), {v})
    if any(n in low for n in names):
        return True
    words = re.findall(r"[a-z]{4,}", low)
    return any(difflib.SequenceMatcher(None, w, n).ratio() >= 0.75
               for w in words for n in names if len(n) >= 4)


def added_without_asking(f: dict, said: str) -> list[str]:
    """Filters that are narrowing the search but are not in their sentence.

    The city is the one that bites. Say "i need a job" and the agent fills in
    the city from the vault — reasonable, it is where they live, and it was
    done four times in one recorded run. Once the sentence said *remote* and it
    added Bangalore anyway.

    Adding it is fine. Not saying so is not: a list of five Bangalore jobs
    looks exactly like a list of five jobs.
    """
    low = (said or "").lower()
    out = []
    for key in ("city", "industry", "seniority", "company_type", "country"):
        v = f.get(key)
        if v and not mentioned(v, said):
            out.append(f"{key.replace('_', ' ')} {v}")
    if f.get("remote") and not mentioned("remote", said):
        out.append("remote only")
    return out


_YEARS = re.compile(r"(\d{1,2})\+?\s*(?:years|yrs)", re.I)


def experience_years(conn=None) -> int | None:
    """How many years they have, for ordering — never for filtering.

    The vault wins when they have said it; otherwise the resume summary
    ("Growth Product Manager with 4+ years …"). None when neither says, which
    turns the fit ordering off rather than guessing.
    """
    if conn is not None:
        row = conn.execute(
            "SELECT value FROM vault WHERE key = 'years_experience'").fetchone()
        if row and str(row[0]).strip().isdigit():
            return int(str(row[0]).strip())
    try:
        import json
        import paths
        summary = json.loads(paths.MASTER.read_text()).get("summary") or ""
    except (OSError, ValueError):
        return None
    m = _YEARS.search(summary)
    return int(m.group(1)) if m else None


def live_target(f: dict) -> tuple[str, str, bool] | None:
    """(keywords, location, remote) for LinkedIn, or None when the ask cannot
    be one — no role named, or outside India, which is all we fetch for now."""
    if not f.get("title_keywords"):
        return None
    if f.get("companies"):
        # They named the company — they are after a job we already have. A
        # live search for its title word ("lead") across India finds nothing
        # they asked for; it only spends a LinkedIn request.
        return None
    country = (f.get("country") or "India").strip().lower()
    if country != "india":
        return None
    keywords = f["title_keywords"][0].strip()
    if f.get("city"):
        # "Bangalore or remote": the city is the narrower ask to fill live.
        # LinkedIn's "New Delhi" already covers all of NCR.
        city = "New Delhi" if f["city"] == "Delhi NCR" else f["city"]
        return keywords, f"{city}, India", False
    if f.get("remote"):
        return keywords, "India", True
    return keywords, "India", False


def go_live(conn, target: tuple[str, str, bool]) -> tuple[list, str]:
    """Search LinkedIn now and label what came back. Returns (added, note)."""
    from engine import labels, linkedin
    keywords, location, remote = target
    try:
        added = linkedin.live(conn, keywords, location, remote)
    except linkedin.Blocked:
        return [], "tried a live LinkedIn search, but it is not answering right now"
    if added:
        try:
            labels.run(conn, only=added, verbose=False)
        except Exception:                             # noqa: BLE001
            pass        # unlabelled rows still match; the daily run labels them
        return added, (f"searched LinkedIn live just now and added "
                       f"{len(added)} new job{'s' if len(added) != 1 else ''}")
    return [], "searched LinkedIn live just now — nothing new beyond what was here"


def search(message: str, *, profile: str = "", conn=None,
           said_verbatim: str | None = None, fit_years: int | None = None,
           live: bool = False, said_recently: str | None = None,
           exclude: list[str] | None = None) -> dict:
    """`live` allows one LinkedIn search while they wait, only when the table
    comes up short. Off by default so nothing reaches the network unasked —
    tests, evals and replays stay offline. `JOB_AGENT_LIVE=0` turns it off for
    callers that pass True, which is how the scenario harness keeps a recorded
    run repeatable."""
    import os
    # A live LinkedIn search adds LinkedIn rows, and LinkedIn is out of search
    # (it cannot be checked for still being open): nothing it adds would be
    # shown. It comes back on if LinkedIn ever leaves db.AGGREGATORS.
    live = (live and os.environ.get("JOB_AGENT_LIVE", "1") != "0"
            and "linkedin" not in db.AGGREGATORS)
    # What they actually typed, when the caller has it. The chat agent
    # paraphrases `message` before this is reached, so anything checked against
    # their own words has to be checked against this instead.
    said_text = said_verbatim if said_verbatim is not None else message

    f = filters_mod.extract(message, profile=profile)

    n = wanted_count(said_text)
    if n is not None:
        f["count"] = n

    # Their recent words, not only this message: "yes" to "Bangalore only?"
    # names Bangalore through the question it answered (see chat.reply).
    guard_notes = guard(f, said_recently or said_text)

    # Captured before anything widens, because after the first relax step you
    # can no longer tell a field the user asked for from one the cascade left
    # behind. This is the only moment the distinction exists.
    said = query.stated(f)

    if exclude:
        f["exclude"] = list(exclude)    # shown before: see chat search_jobs
    rows, f = query.search(f, conn, fit_years)

    # Sparse results relax in a fixed, logged order — never by asking a second
    # question, never by silently padding to hit the number, and **never by
    # discarding something the person actually said**. When only their own
    # constraints are left, the search stops short and says so.
    notes: list[str] = list(guard_notes)
    while len(rows) < f["count"]:
        step = query.relax(f, protect=said)
        if step is None:
            break
        f, note = step
        notes.append(note)
        rows, f = query.search(f, conn, fit_years)

    # Every ask becomes a saved search, found or not, so the daily fetch covers
    # it from tomorrow. The first person to ask for UX in Kochi waits; the next
    # one does not.
    target = live_target(f)
    if conn is not None and target:
        from engine import linkedin
        linkedin.remember(conn, *target)

    # Short before ranking: look live now, then query again.
    went_live = False
    if live and conn is not None and target and len(rows) < f["count"]:
        went_live = True
        _, note = go_live(conn, target)
        notes.append(note)
        rows, f = query.search(f, conn, fit_years)

    # Stopping short is a result, not an error, but it has to be visible or it
    # reads as "that is all there is" — which is how four Noida jobs passed for
    # a Bangalore search. The note names what was kept and what could give, so
    # the agent can offer the trade instead of taking it unasked.
    if len(rows) < f["count"] and said:
        # Only fields actually in their sentence get described as theirs. The
        # agent paraphrases a city in from the vault, and `stated()` cannot
        # tell that from something they typed — so this note once told someone
        # who wrote "i need a job" that 0 jobs matched "what you asked for —
        # I kept city Bangalore". They had not asked for Bangalore.
        kept = ", ".join(
            f"{k.replace('_', ' ')} {v}" for k, v in sorted(f.items())
            if k in said and v not in (None, "", [])
            and mentioned(v, said_recently or said_text)
        )
        if kept:
            notes.append(
                f"only {len(rows)} match what you asked for — "
                f"I kept {kept} rather than widening it without asking"
            )
        else:
            notes.append(f"only {len(rows)} match")

    # Anything narrowing the search that they did not say. Goes in `notes`,
    # which is rendered into the result the agent reads, so it has to account
    # for it rather than quietly hand over a filtered list.
    # Their last few messages, not just this one: "yes" to "Bangalore only?"
    # is asking for Bangalore, and was once told it had not.
    quiet = added_without_asking(f, said_recently or said_text)
    if quiet:
        notes.append("you didn't ask for " + " or ".join(quiet)
                     + " — I applied it anyway, say if you want it wider")

    picks, dropped = rank.pick(rows, f, message, profile=profile)

    # Enough rows, but the ranker kept fewer than asked: the table had jobs,
    # not good ones. Look live once, and rank only what is new — the rest were
    # just judged, and judging them twice doubles the bill for nothing.
    if (live and not went_live and conn is not None and target
            and len(picks) < f["count"]):
        added, note = go_live(conn, target)
        notes.append(note)
        if added:
            new_ids = {(s_, str(i)) for s_, i in added}
            fresh = [r for r in query.search({**f, "count": 20}, conn, fit_years)[0]
                     if (r["source"], str(r["source_id"])) in new_ids]
            more, more_dropped = rank.pick(
                fresh, {**f, "count": f["count"] - len(picks)}, message,
                profile=profile)
            picks += more
            dropped += more_dropped

    # Still short with a city named: count what exists nearby and remotely, and
    # hand that over as a choice. Never as results.
    if len(picks) < f["count"] and conn is not None and f.get("city") \
            and not f.get("remote"):
        remote_n, cities = query.elsewhere(f, conn)
        options = []
        if remote_n:
            options.append(f"{remote_n} remote")
        options += [f"{n} in {c}" for c, n in cities]
        if options:
            notes.append(
                "other options they did not ask for — offer these as a choice, "
                "do not show them as matches: possible roles " + ", ".join(options))

    open_jobs = 0
    if conn is not None:
        row = conn.execute(
            f"SELECT COUNT(*) FROM jobs WHERE {db.visible()}").fetchone()
        open_jobs = row[0] if row else 0

    # Nothing new, but there were matches they have already seen: that is a
    # different answer from "nothing out there", and they get to choose.
    # Only when the seen list is what emptied it: no rows left after it, and
    # rows there without it. Rows left but none picked is "nothing good", not
    # "all seen": an invited user had seen 1 job and was told "all 20 are already in your
    # results" (2026-10-01).
    all_seen = 0
    if not picks and not rows and exclude and conn is not None:
        again, _ = query.search({**f, "exclude": []}, conn, fit_years)
        seen = set(exclude)
        all_seen = sum(1 for r in again if f"{r.get('source')}:{r.get('source_id')}" in seen)

    return {
        "picks": picks,
        "all_seen": all_seen,
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
        # Unnumbered, for the chat agent. See format_results.
        "text_agent": rank.format_results(picks, notes, numbered=False),
        "reference_table": rank.to_reference_table(picks),
    }
