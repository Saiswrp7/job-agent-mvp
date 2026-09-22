"""Replay a fixed set of sentences through the real code and score the result.

**Why this exists.** The two bugs found on 2026-09-22 — "in India" never
reaching the filter, and "find me 3" being answered with five — were both
invisible because the final answer read perfectly well. Neither was a crash.
Neither failed a test. You could only see them by looking at the *middle* of
the pipeline, which is exactly what this replays.

**What it scores, and what it deliberately does not.** Only steps with a
checkable answer. "Did your sentence become the right filter" has one. "Were
those the best five jobs" does not, so it is not scored — instead the rank
cases check the one thing that *is* objectively wrong: a job that violates
something you actually said, like a San Mateo role when you asked for India.

Two sources of cases:

- `evals/cases_*.jsonl` — hand-written, the ones that must keep passing
- `--from-log` — sentences you really typed, harvested out of the chat log.
  This is why the raw filter is stored alongside the prose description: a
  real conversation becomes a test case without anyone inventing one.

Costs money: every case is a live model call. The filter cases are cheap
(small prompt); the rank cases are not (~18k tokens each), so there are three
of them, not thirty.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

#: Cities that are not in India, for the one check SQL cannot express. Kept
#: short and explicit: a wrong entry here fails a real case loudly, which is
#: better than a clever rule that quietly passes everything.
NON_INDIA = {"san mateo", "san francisco", "london", "new york", "seoul",
             "redwood city", "singapore", "dubai", "austin", "berlin"}


def load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()
            if line.strip()]


def _get(d: dict, key: str):
    v = d.get(key)
    return None if v in ("", [], {}) else v


#: Field names are what the code calls things. These are what a person calls
#: them. A report that says `posted_within_days` is a report you have to be a
#: programmer to read, which defeats the point of running it.
PLAIN_FIELD = {
    "count":             "how many jobs to show",
    "city":              "the city",
    "title_keywords":    "the words to look for in the job title",
    "posted_within_days": "how recent a job has to be",
    "remote":            "remote-only",
    "company_type":      "the kind of company",
    "companies":         "specific companies",
    "industry":          "the industry",
    "stage":             "the company stage",
    "seniority":         "the seniority",
    "must_mention":      "skills to prefer",
    "soft_criteria":     "your preferences, in your own words",
}


def fname(key: str) -> str:
    return PLAIN_FIELD.get(key, key)


def fval(v) -> str:
    if v is None:
        return "nothing"
    if v is True:
        return "yes"
    if v is False:
        return "no"
    if isinstance(v, list):
        return ", ".join(str(x) for x in v) or "nothing"
    return str(v)


# --- the filter step -------------------------------------------------------

def check_filter(case: dict, got: dict) -> list[str]:
    """Returns what went wrong, in sentences. Empty list means it passed."""
    bad = []

    for key, want in (case.get("expect") or {}).items():
        have = _get(got, key)
        if want is None:
            if have is not None:
                bad.append(f"it set {fname(key)} to {fval(have)}, "
                           f"and you never mentioned that")
        elif have != want:
            bad.append(f"{fname(key)} should have been {fval(want)}, "
                       f"it was {fval(have)}")

    for key, needle in (case.get("expect_contains") or {}).items():
        have = _get(got, key) or []
        if not any(needle.lower() in str(v).lower() for v in have):
            bad.append(f'{fname(key)} should have included "{needle}", '
                       f"it was {fval(have)}")

    for key in case.get("expect_nonempty") or []:
        if not _get(got, key):
            bad.append(f"it kept nothing for {fname(key)}")

    # There is no country column, so "in India" has nowhere to land. The case
    # asserts the requirement, not the current design — a failing row here is
    # the gap made visible, not a broken test.
    want_country = case.get("expect_country")
    if want_country:
        if want_country.lower() not in json.dumps(got).lower():
            bad.append(f'it ignored the word "{want_country}" completely')
    return bad


def run_filters(cases: list[dict]) -> list[dict]:
    from search import filters as filters_mod

    out = []
    for c in cases:
        try:
            got = filters_mod.extract(c["say"])
            fails = check_filter(c, got)
        except Exception as exc:                        # noqa: BLE001
            got, fails = {}, [f"it crashed: {type(exc).__name__}: {exc}"]
        out.append({**c, "got": got, "fails": fails})
    return out


# --- the ranking step ------------------------------------------------------

def check_picks(case: dict, picks: list[dict]) -> list[str]:
    rule = case.get("must_not_pick") or {}
    bad = []
    for p in picks:
        city = (p.get("city") or p.get("location") or "").strip()
        low = city.lower()
        name = f"{p.get('title', '')[:40]} at {p.get('company')}"

        if rule.get("city_outside") and any(n in low for n in NON_INDIA):
            bad.append(f"it showed you {name}, which is in {city}")
        if rule.get("city_not") and city and not any(
                ok.lower() in low for ok in rule["city_not"]):
            bad.append(f"it showed you {name}, which is in {city}")
        above = rule.get("years_min_above")
        if above and (p.get("years_min") or 0) >= above:
            bad.append(f"it showed you {name}, which wants "
                       f"{p['years_min']}+ years")
    return bad


def run_rank(cases: list[dict]) -> list[dict]:
    from engine import db
    from search.run import search

    conn = db.connect()
    try:
        out = []
        for c in cases:
            try:
                r = search(c["say"], conn=conn)
                picks = r["picks"]
                fails = check_picks(c, picks)
                extra = {"shown": len(picks), "matched": r["funnel"]["matched"],
                         "dropped": len(r.get("dropped") or [])}
            except Exception as exc:                    # noqa: BLE001
                picks, extra = [], {}
                fails = [f"it crashed: {type(exc).__name__}: {exc}"]
            out.append({**c, "picks": picks, "fails": fails, **extra})
        return out
    finally:
        conn.close()


# --- harvesting real turns -------------------------------------------------

def from_log(limit: int = 40) -> list[dict]:
    """Turn real searches into cases.

    No expectations attached — nobody labelled them. They are replayed to see
    whether the same sentence still produces the same filter, which catches a
    prompt change that quietly moves an answer you were happy with.
    """
    import chatlog
    from engine import db

    conn = db.connect()
    try:
        cases, seen = [], set()
        for row in chatlog.turns(conn, limit=limit * 4):
            for s in ((row.get("meta") or {}).get("steps") or []):
                fn = s.get("funnel") or {}
                said, was = fn.get("query"), fn.get("filter")
                if not said or not was or said in seen:
                    continue
                seen.add(said)
                cases.append({"id": f"log-{len(cases) + 1}", "say": said,
                              "was": was})
        return cases[-limit:]
    finally:
        conn.close()


def run_replay(cases: list[dict]) -> list[dict]:
    from search import filters as filters_mod

    out = []
    for c in cases:
        try:
            got = filters_mod.extract(c["say"])
        except Exception as exc:                        # noqa: BLE001
            out.append({**c, "got": {}, "changed": [f"crashed: {exc}"]})
            continue
        was, changed = c["was"], []
        for key in sorted(set(was) | set(got)):
            if key == "soft_criteria":
                continue          # free text, different wording is not a change
            a, b = _get(was, key), _get(got, key)
            if a != b:
                changed.append(f"{fname(key)} was {fval(a)} last time, "
                               f"and is {fval(b)} now")
        out.append({**c, "got": got, "changed": changed})
    return out


# --- reporting -------------------------------------------------------------
# Written for the person whose job search this is, not for whoever wrote the
# code. Every failure answers three questions in this order: what went wrong,
# what it costs you, and where it lives. The third is last on purpose — a file
# path is the least useful of the three unless you are about to open it.

GREEN, RED, DIM, BOLD, OFF = ("\033[32m", "\033[31m", "\033[2m",
                             "\033[1m", "\033[0m")

#: Used when a case does not spell out its own consequence.
DEFAULT_MEANS = ("The search runs with different settings than you asked for, "
                 "so you get a different list of jobs and nothing tells you.")

REPLAY_MEANS = ("The same question now searches differently than it did "
                "before. A list you were happy with can change without you "
                "changing anything.")


def _c(s, code):
    return f"{code}{s}{OFF}" if sys.stdout.isatty() else s


def _wrap(text, indent="       ", width=64):
    import textwrap
    return textwrap.fill(text, width, initial_indent=indent,
                         subsequent_indent=indent)


def summary(groups: list[dict]) -> None:
    total = sum(len(g["rows"]) for g in groups)
    fails = [(g, r) for g in groups for r in g["rows"] if r.get(g["key"])]

    print()
    print(_c("=" * 68, DIM))
    print(_c("  WHAT WORKS, AND WHAT DOESN'T", BOLD))
    print(_c("=" * 68, DIM))
    print()
    print(f"  {total - len(fails)} of {total} checks passed.")
    print()

    if not fails:
        print(_c("  Nothing is broken. Every sentence produced the search "
                 "it should.", GREEN))
        print()
        return

    # One entry per distinct consequence, so two cases that break the same
    # way read as one problem rather than two.
    seen, problems = set(), []
    for g, r in fails:
        means = r.get("means") or g.get("means") or DEFAULT_MEANS
        if means in seen:
            problems[[p[0] for p in problems].index(means)][1].append(r["say"])
            continue
        seen.add(means)
        problems.append([means, [r["say"]]])

    print(_c(f"  {len(problems)} problem{'' if len(problems) == 1 else 's'} "
             f"found:", RED))
    print()
    for i, (means, says) in enumerate(problems, 1):
        body = _wrap(means, "     ", 62)
        print(f"  {i}.{body[4:]}")
        for s in says[:3]:
            print(_c(f'       when you said "{s}"', DIM))
        if len(says) > 3:
            print(_c(f"       and {len(says) - 3} more", DIM))
        print()
    print(_c("  None of these crash anything. All of them change which jobs "
             "you see.", DIM))
    print()


def detail(g: dict) -> None:
    key = g["key"]
    print(_c("-" * 68, DIM))
    print(f"  {_c(g['title'], BOLD)}")
    print(_c(f"  {g['file']}", DIM))
    print(_c(f"  {g['does']}", DIM))
    print()
    passed = 0
    for r in g["rows"]:
        problems = r.get(key) or []
        if not problems:
            passed += 1
            print(f"  {_c('ok', GREEN)}   {r['say']}")
            continue
        print(f"  {_c('NO', RED)}   {_c(r['say'], BOLD)}")
        print(_c("         what went wrong", DIM))
        for p in problems:
            print(_wrap(p, "           ", 62))
        print(_c("         what that means for you", DIM))
        print(_wrap(r.get("means") or g.get("means") or DEFAULT_MEANS,
                    "           ", 62))
        print()
    print()
    print(f"  {passed} of {len(g['rows'])} passed")
    print()


def main(which: str = "all", limit: int = 40) -> int:
    groups = []

    if which in ("all", "filters"):
        groups.append({
            "title": "Understanding what you asked for",
            "file": "search/filters.py - asks the AI",
            "does": "reads your sentence and decides what to search for",
            "key": "fails",
            "rows": run_filters(load(HERE / "cases_filters.jsonl")),
        })

    if which in ("all", "rank"):
        groups.append({
            "title": "Choosing which jobs to show you",
            "file": "search/rank.py - asks the AI",
            "does": "reads every shortlisted job and keeps the ones that fit",
            "key": "fails",
            "rows": run_rank(load(HERE / "cases_rank.jsonl")),
        })

    if which in ("all", "replay"):
        cases = from_log(limit)
        if cases:
            groups.append({
                "title": "Checking nothing moved since last time",
                "file": "search/filters.py - asks the AI",
                "does": "asks the same questions you already asked, and sees "
                        "whether the answer changed",
                "key": "changed",
                "means": REPLAY_MEANS,
                "rows": run_replay(cases),
            })

    if not groups:
        print("Nothing to check yet. Have a conversation first, then run "
              "this again.")
        return 0

    summary(groups)
    print(_c("  THE DETAIL", BOLD))
    print()
    for g in groups:
        detail(g)

    print(_c("  The middle step, narrowing 670 jobs down to a shortlist, is "
             "not", DIM))
    print(_c("  checked here: it never asks the AI, so ordinary tests already "
             "cover", DIM))
    print(_c("  it. Only the steps that ask the AI can surprise you.", DIM))
    print()

    return sum(1 for g in groups for r in g["rows"] if r.get(g["key"]))
