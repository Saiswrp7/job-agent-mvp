"""The five checks that would have caught what 31 conversations found by hand.

`FINDINGS.md` ends by listing them and saying none needs a human judge. This is
that list, executable. It reads `runs/*.json` and nothing else — no model, no
provider, no network — so it runs while every API key on the machine is dead.

The check that already existed, `promise_check` in `drive.py`, compares the
words in the request against the words in the titles. It passed 26 of 27 and
caught none of the eight bugs, because every one of them lived in the
**trajectory**: what was typed, what reached the tool, what came back, what was
shown. Each check below reads two of those and compares them.

    python3 scenario-runs/assertions.py                  # newest run
    python3 scenario-runs/assertions.py runs/run-*.json  # specific runs

Exit code is the number of violations, so it can gate a run in CI.

**Two eras.** Before the 2026-09-23 fixes, tools addressed a job by position
(`n`); after, by name (`ref`). Checks that care read whichever the log holds,
so a historical run is still judged by how it actually worked. Without that,
every pre-fix run would look clean for the wrong reason and every post-fix run
would look broken for the wrong reason.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from search.run import added_without_asking, wanted_count   # noqa: E402

#: A numbered item as the person reads it: "1. **Title** — Company".
#: Indented continuation lines are not items.
_SHOWN = re.compile(r"^\s{0,3}(\d{1,2})[.)]\s+(.+)$", re.M)

#: Them pointing at one of those numbers in their next message.
_ASKED_FOR = re.compile(
    r"\b(?:apply(?:\s+to)?|tailor[^.\n]{0,30}?for|do|start|use|go with|"
    r"tell me about|more about|details? on)\s+(?:number\s+)?#?(\d{1,2})\b", re.I)

#: Only the **completed** claim counts: "all five are submitted". The bare verb
#: does not — "once you give me those, I'll submit all five" is a promise about
#: the future and flagging it would fire the check on correct behaviour, which
#: is how a guard stops being read. Negations and future passives come out
#: first, because this system says both constantly and honestly.
_NOT_SUBMITTED = re.compile(
    r"\b(?:not|never|n't|unable to|could ?n't|did ?n't|without)\b[^.\n]{0,40}"
    r"\bsubmit(?:ted)?\b", re.I)
_FUTURE_SUBMITTED = re.compile(
    r"\b(?:will be|to be|can be|once|after|when|before|ready to be|"
    r"going to be|cannot be|can't be)\b[^.\n]{0,25}\bsubmitted\b", re.I)
_SUBMITTED = re.compile(r"\bsubmitted\b", re.I)

#: The agent admitting it searched more than once.
_MULTI = re.compile(
    r"\b(?:tried|searched|ran|looked)\b[^.\n]{0,60}"
    r"\b(?:twice|again|a few|several|multiple|two|three|2|3)\b"
    r"|\b(?:a few|several|multiple|two|three) (?:different )?(?:searches|ways|angles)",
    re.I)


def _flat(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _tokens(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if len(w) > 2}


def _best_match(line: str, picks: list[dict]) -> tuple[dict | None, float]:
    """Which pick is the one this displayed line is describing?

    Comparing company alone is not enough and it hid the worst real case:
    shown "Product Manager — Lending Product", resolved to "Product Management
    — Paytm Insurance". Same company, different job, six-to-eight years for a
    candidate with four. Matching the whole line against every pick finds the
    one it is actually describing, and then the only question left is whether
    the tool was handed that one.
    """
    said = _tokens(line)
    best, score = None, 0.0
    for p in picks:
        want = _tokens(f"{p.get('title','')} {p.get('company','')}")
        if not want:
            continue
        s = len(said & want) / len(want)
        if s > score:
            best, score = p, s
    return best, score


def _steps(turn: dict, tool: str | None = None) -> list[dict]:
    steps = turn.get("steps") or []
    return [s for s in steps if tool is None or s.get("tool") == tool]


def _filters(turn: dict) -> list[dict]:
    """Every filter that actually reached SQL this turn."""
    return [(s.get("funnel") or {}).get("filter") or {}
            for s in _steps(turn, "search_jobs")]


# --- the five --------------------------------------------------------------

def a1_numbers_resolve(turn: dict, prev: dict | None) -> list[str]:
    """The number they typed must reach the job they were shown as that number.

    Bug 1, the one that sends a real application to the wrong company. The
    agent renumbers results when it writes them out; the tools indexed search
    order; nothing reconciled the two, and "apply to 2" tailored a resume for a
    different company in roughly half of all conversations.

    This compares the two things that must agree and never did: the list in the
    reply they *read*, and the job the tool was handed on their *next* message.
    """
    if prev is None:
        return []
    m = _ASKED_FOR.search(turn.get("user") or "")
    if not m:
        return []
    n = m.group(1)
    shown = dict(_SHOWN.findall(prev.get("assistant") or ""))
    line = shown.get(n)
    if line is None:
        return []

    picks = prev.get("picks_after") or []
    meant, score = _best_match(line, picks)
    if meant is None or score < 0.5:
        return []                # cannot tell what they were looking at; say nothing

    want = _flat(f"{meant.get('company','')} {meant.get('title','')}")
    out = []
    for s in _steps(turn):
        arg = s.get("input") or {}
        if "ref" in arg:                       # after the fix: addressed by name
            got = _flat(str(arg["ref"]))
            if got and got not in want:
                out.append(
                    f"they said {n} — shown {line.strip()[:50]!r} — "
                    f"{s['tool']} got ref {arg['ref']!r}")
        elif "n" in arg:                       # before the fix: addressed by position
            by_i = {p["i"]: p for p in picks}
            p = by_i.get(int(arg["n"]))
            if p is None:
                out.append(f"they said {n} — {s['tool']} got n={arg['n']}, "
                           f"which resolves to nothing")
            elif p is not meant:
                out.append(
                    f"they said {n} — shown {line.strip()[:50]!r} — "
                    f"{s['tool']} got {p.get('title','?')[:35]} — {p.get('company')}")
    return out


def a2_no_false_submitted(turn: dict, prev: dict | None) -> list[str]:
    """Never say submitted unless a tool said SUBMITTED.

    Bug 2. `run_tool` wrapped the apply agent's honest "I'm unable to submit"
    in the word `done`, and the chat model told someone five applications were
    in. `_OUTCOME` in chat.py spells the outcome out now; this checks that the
    reply repeats it faithfully.
    """
    reply = turn.get("assistant") or ""
    honest = _FUTURE_SUBMITTED.sub("", _NOT_SUBMITTED.sub("", reply))
    if not _SUBMITTED.search(honest):
        return []
    confirmed = any("SUBMITTED —" in (s.get("result") or "")
                    and "NOT SUBMITTED" not in (s.get("result") or "")
                    for s in _steps(turn))
    if confirmed:
        return []
    return ["claims something was submitted; no tool returned SUBMITTED"]


def a3_count_reaches_filter(turn: dict, prev: dict | None) -> list[str]:
    """The number they asked for is the number that reaches SQL.

    Bug 4, and the direct test of its fix: seven sentences with an explicit
    count produced `count = 5` seven times, because the agent paraphrases
    before calling the tool and the number is what falls out of the paraphrase.
    `wanted_count` reads it from their raw words instead.
    """
    n = wanted_count(turn.get("user") or "")
    if n is None:
        return []
    return [f"asked for {n}, filter said count={f.get('count')}"
            for f in _filters(turn) if f.get("count") != n]


def a4_one_search_or_say_so(turn: dict, prev: dict | None) -> list[str]:
    """Search once, or tell them you did not.

    Bug 5. Nine of 49 turns ran `search_jobs` more than once, re-phrasing after
    thin results without saying so — one message triggered six, each carrying
    an ~18k-token ranking call, and the person saw a single answer.
    """
    n = len(_steps(turn, "search_jobs"))
    if n <= 1:
        return []
    if _MULTI.search(turn.get("assistant") or ""):
        return []
    return [f"ran {n} searches and the reply does not mention more than one"]


def a5_no_undisclosed_filter(turn: dict, prev: dict | None) -> list[str]:
    """A filter they did not ask for has to be named in the reply.

    Bug 6. "i need a job" reached the query as Bangalore, from the vault, four
    times in one run — once alongside the word *remote* in the same sentence.
    Applying it is defensible; not saying so is not, because five Bangalore
    jobs look exactly like five jobs.
    """
    said = turn.get("user") or ""
    reply = (turn.get("assistant") or "").lower()
    seen, out = set(), []
    for f in _filters(turn):
        for added in added_without_asking(f, said):
            if added in seen:
                continue
            seen.add(added)
            if added.split()[-1].lower() not in reply:
                out.append(f"applied {added!r}, never said so in the reply")
    return out


CHECKS = [
    ("numbers-resolve", a1_numbers_resolve),
    ("no-false-submitted", a2_no_false_submitted),
    ("count-reaches-filter", a3_count_reaches_filter),
    ("one-search-or-say-so", a4_one_search_or_say_so),
    ("no-undisclosed-filter", a5_no_undisclosed_filter),
]


def check_run(path: Path) -> list[dict]:
    scenarios = json.loads(path.read_text())
    out = []
    for s in scenarios:
        prev = None
        for turn in s["turns"]:
            if turn.get("error"):
                continue          # a turn that never ran cannot be judged
            for name, fn in CHECKS:
                for detail in fn(turn, prev):
                    out.append({"scenario": s["id"], "name": s["name"],
                                "turn": turn["n"], "check": name,
                                "user": turn.get("user", ""), "detail": detail})
            prev = turn
    return out


def main() -> int:
    args = [Path(a) for a in sys.argv[1:]]
    if not args:
        runs = sorted((HERE / "runs").glob("run-*.json"))
        if not runs:
            print("no runs to check")
            return 0
        args = [runs[-1]]

    total = 0
    for path in args:
        violations = check_run(path)
        total += len(violations)
        by_check: dict[str, int] = {}
        for v in violations:
            by_check[v["check"]] = by_check.get(v["check"], 0) + 1

        print(f"\n{path.name} — {len(violations)} violation(s)")
        if not violations:
            print("  all five checks pass on every turn that ran")
            continue
        for name, _ in CHECKS:
            print(f"  {name:<22} {by_check.get(name, 0)}")
        print()
        for v in violations:
            print(f"  [{v['check']}] {v['scenario']}.{v['turn']} "
                  f"{v['user'][:50]!r}\n      {v['detail']}")
    return total


if __name__ == "__main__":
    sys.exit(min(main(), 125))
