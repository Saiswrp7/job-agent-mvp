"""Launch evals for search: filters, ranking, and a few whole chat turns.

Measures, does not fix. Writes JSON to evals/out/ for the sheet.

    python3 evals/launch.py filters      # ~42 sentences, the filter step
    python3 evals/launch.py rank         # 10 whole searches, ~18k tokens each
    python3 evals/launch.py chat         # 6 short conversations through the chat agent
    python3 evals/launch.py all

Safety, forced here and not optional:
- Runs in scratch profiles under profiles/eval-launch*, never a real person's.
- ALLOW_SUBMIT=0, JOB_AGENT_LIVE=0 (no live LinkedIn during a search).
- The chat probes stub `start_application`, `answer_application` and the
  background worker before the first turn, so nothing can start applying.
- `linkedin.remember` is stubbed, so eval sentences never become saved
  searches in the shared database.

Differences from evals/run.py, on purpose:
- The filter is scored as the search actually uses it: the model's filter,
  then the count read from the sentence (`search.run.wanted_count`). run.py
  scores the model alone.
- Every case also gets an "invented filter" check: any narrowing field that
  is set without the case allowing it is a failure.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
OUT = HERE / "out"

WARM = "profiles/eval-launch"          # Sai's resume, lives in Bengaluru
COLD = "profiles/eval-launch-cold"     # brand-new user, no resume
SAI_PDF = ROOT.parent / "Interview Prep" / "Sai Swaroop Growth.pdf"


def _prepare_env(home: str) -> None:
    # Before `paths` is imported by anything. See paths.py.
    os.environ["JOB_AGENT_HOME"] = home
    os.environ["ALLOW_SUBMIT"] = "0"
    os.environ["JOB_AGENT_LIVE"] = "0"
    os.environ.pop("JOB_AGENT_BROWSER", None)
    sys.path.insert(0, str(ROOT))


def _stub_side_effects() -> None:
    from engine import linkedin
    linkedin.remember = lambda *a, **k: None


def open_jobs() -> int:
    import sqlite3
    c = sqlite3.connect(f"file:{ROOT / 'jobs.db'}?mode=ro", uri=True)
    try:
        return c.execute(
            "SELECT COUNT(*) FROM jobs WHERE closed_at IS NULL").fetchone()[0]
    finally:
        c.close()


def _retry(fn, tries=4):
    for i in range(tries):
        try:
            return fn()
        except Exception as exc:                        # noqa: BLE001
            if "locked" in str(exc).lower() and i < tries - 1:
                time.sleep(30)
                continue
            raise


# --- filters ---------------------------------------------------------------

#: Fields that narrow the search. Setting one the person did not ask for is an
#: invented filter.
NARROWING = ["city", "remote", "country", "company_type", "industry", "stage",
             "seniority", "posted_within_days", "companies", "level", "owns_pnl"]
#: What counts as a "hard limit" failure for the launch bar.
HARD = {"city", "count", "role", "remote", "country"}

PLAIN = {"city": "the city", "count": "how many jobs", "role": "the role",
         "remote": "remote-only", "country": "the country",
         "posted_within_days": "how recent", "company_type": "B2B or B2C",
         "industry": "the industry", "stage": "company stage",
         "seniority": "seniority", "companies": "which companies",
         "level": "level", "owns_pnl": "owns a P&L",
         "title_keywords": "the job title words",
         "role_family": "the kind of work", "soft_criteria": "your preferences"}

INDIA_CITIES = {"bangalore", "mumbai", "delhi", "new delhi", "gurgaon", "noida",
                "hyderabad", "pune", "chennai", "kolkata", "delhi ncr", "ncr",
                "ahmedabad", "jaipur", "kochi"}


def _v(d, k):
    v = d.get(k)
    return None if v in ("", [], {}) else v


def _show(v):
    if v is None:
        return "nothing"
    if v is True:
        return "yes"
    if isinstance(v, list):
        return ", ".join(map(str, v))
    return str(v)


def understood(f: dict) -> str:
    """The filter as a short plain sentence for the sheet."""
    parts = []
    tk = _v(f, "title_keywords")
    rf = _v(f, "role_family")
    if tk or rf:
        parts.append("role: " + _show(tk or rf)
                     + (f" ({_show(rf)})" if tk and rf else ""))
    else:
        parts.append("role: any")
    parts.append(f"count: {f.get('count')}")
    parts.append(f"city: {_show(_v(f, 'city')) if _v(f, 'city') else 'any'}")
    for k in ("remote", "country", "posted_within_days", "company_type",
              "industry", "stage", "seniority", "level", "companies", "owns_pnl"):
        if _v(f, k) is not None:
            label = {"posted_within_days": "posted within (days)"}.get(k, PLAIN[k])
            parts.append(f"{label}: {_show(f[k])}")
    if _v(f, "soft_criteria"):
        parts.append(f'kept as a preference: "{f["soft_criteria"]}"')
    return "; ".join(parts)


def check_filter(c: dict, f: dict) -> list[tuple[str, str]]:
    """(kind, sentence) per problem. kind is the field, or 'invented'."""
    bad = []
    exp = c.get("expect") or {}
    for k, want in exp.items():
        have = _v(f, k)
        if want is None:
            if have is not None:
                bad.append(("invented", f"it set {PLAIN.get(k, k)} to "
                                        f"{_show(have)}, which you never said"))
        elif isinstance(want, str) and isinstance(have, str):
            if want.lower() != have.lower():
                bad.append((k, f"{PLAIN.get(k, k)} should be {want}, "
                               f"it was {_show(have)}"))
        elif have != want:
            bad.append((k, f"{PLAIN.get(k, k)} should be {_show(want)}, "
                           f"it was {_show(have)}"))
    for k, options in (c.get("expect_one_of") or {}).items():
        have = _v(f, k)
        if not any(str(have).lower() == str(o).lower() for o in options):
            bad.append((k, f"{PLAIN.get(k, k)} should be one of "
                           f"{_show(options)}, it was {_show(have)}"))
    for k, needle in (c.get("expect_contains") or {}).items():
        have = _v(f, k) or []
        if not any(needle.lower() in str(x).lower() for x in have):
            bad.append((k, f'{PLAIN.get(k, k)} should include "{needle}", '
                           f"it was {_show(_v(f, k))}"))
    for k, needle in (c.get("expect_like") or {}).items():
        if needle.lower() not in str(_v(f, k) or "").lower():
            bad.append((k, f'{PLAIN.get(k, k)} should mention "{needle}", '
                           f"it was {_show(_v(f, k))}"))
    for k in c.get("expect_nonempty") or []:
        if not _v(f, k):
            bad.append((k, f"it kept nothing for {PLAIN.get(k, k)}"))
    want_country = c.get("expect_country")
    if want_country and want_country.lower() not in json.dumps(f).lower():
        bad.append(("country", f'it ignored "{want_country}" completely'))
    hay = " ".join(str(x).lower() for x in
                   (_v(f, "title_keywords") or []) + (_v(f, "role_family") or []))
    for group in c.get("expect_role") or []:
        if not any(n.lower() in hay for n in group):
            bad.append(("role", f"the role should cover {' or '.join(group)}, "
                                f"it looked for {hay or 'any role'}"))

    # Invented filters: anything narrowing that the case did not ask about.
    declared = set(exp) | set(c.get("expect_one_of") or {}) \
        | set(c.get("expect_contains") or {}) | set(c.get("expect_like") or {}) \
        | set(c.get("allow") or [])
    if want_country:
        declared.add("country")
    for k in NARROWING:
        if k in declared or _v(f, k) is None:
            continue
        # "India" next to an Indian city narrows nothing the city did not.
        if k == "country" and str(f.get("country")).lower() == "india" and \
                str(_v(f, "city") or "").lower() in INDIA_CITIES:
            continue
        bad.append(("invented", f"it added {PLAIN.get(k, k)} = "
                                f"{_show(f[k])}, which you never asked for"))
    return bad


def run_filters(repeat: int = 3) -> dict:
    """Each sentence runs `repeat` times. The same sentence can come back as a
    different filter on a second try, and a case only passes if every try
    does: a bug that shows up one time in three still reaches one user in
    three."""
    _prepare_env(WARM)
    import tokens
    tokens.install()
    from search import filters as filters_mod
    from search.run import guard, wanted_count

    cases = [json.loads(l) for l in (HERE / "cases_filters.jsonl").read_text()
             .splitlines() if l.strip()]
    rows = []
    for c in cases:
        tries = []
        for _ in range(repeat):
            m = tokens.mark()
            t0 = time.monotonic()
            try:
                raw = _retry(lambda: filters_mod.extract(
                    c["say"], profile=c.get("profile", "")))
                eff = dict(raw)
                n = wanted_count(c["say"])
                if n is not None:
                    eff["count"] = n
                # The code guard search.run applies next: level and city only
                # when the sentence names them.
                guard(eff, c["say"])
                problems = check_filter(c, eff)
            except Exception as exc:                    # noqa: BLE001
                raw, eff = {}, {}
                problems = [("crash", f"it crashed: {type(exc).__name__}: {exc}")]
            tries.append({"raw": raw, "effective": eff, "problems": problems,
                          "understood": understood(eff) if eff else "",
                          "seconds": round(time.monotonic() - t0, 1),
                          "tokens": tokens.since(m)})
        passed = sum(1 for t in tries if not t["problems"])
        # Show the first failing try, so the sheet explains the failure.
        shown = next((t for t in tries if t["problems"]), tries[0])
        rows.append({**c, **shown, "passed": passed, "tries": len(tries),
                     "all_understood": [t["understood"] for t in tries],
                     "tokens": tries[0]["tokens"]})
        print(("ok  " if passed == len(tries) else "FAIL"), c["id"],
              f"{passed}/{len(tries)}", "|",
              "; ".join(p[1] for p in shown["problems"]))
    return {"ran_at": datetime.now().isoformat(timespec="seconds"),
            "open_jobs": open_jobs(), "repeat": repeat, "rows": rows}


# --- rank ------------------------------------------------------------------

NON_INDIA = {"san mateo", "san francisco", "london", "new york", "seoul",
             "redwood city", "singapore", "dubai", "austin", "berlin",
             "toronto", "sydney", "tokyo", "paris", "amsterdam", "seattle",
             "remote - us", "united states", "usa", "uk", "germany"}


def _is_remote(p: dict) -> bool:
    return bool(p.get("remote")) or str(p.get("work_mode") or "").lower() == "remote" \
        or "remote" in str(p.get("location") or "").lower()


def _age_days(p: dict) -> int | None:
    s = (p.get("posted_at") or "")[:10]
    try:
        return (date.today() - date.fromisoformat(s)).days
    except ValueError:
        return None


def check_picks(c: dict, picks: list[dict]) -> list[str]:
    rule = c.get("must_not_pick") or {}
    bad = []
    if rule.get("count_max") and len(picks) > rule["count_max"]:
        bad.append(f"it showed {len(picks)} jobs, you asked for "
                   f"{rule['count_max']}")
    for p in picks:
        city = (p.get("city") or p.get("location") or "").strip()
        low = (city + " " + str(p.get("location") or "")).lower()
        name = f"{p.get('title', '')[:50]} at {p.get('company')}"
        if rule.get("city_outside") and (
                any(n in low for n in NON_INDIA)
                or (p.get("country") and p["country"] not in ("India", "Global"))):
            bad.append(f"{name} is in {city or p.get('country')}, outside India")
        if rule.get("city_not") and not _is_remote(p) and not any(
                ok.lower() in low for ok in rule["city_not"]):
            bad.append(f"{name} is in {city or 'an unknown city'}")
        if rule.get("remote_only") and not _is_remote(p):
            bad.append(f"{name} is not remote ({city or 'no city'})")
        above = rule.get("years_min_above")
        if above and (p.get("years_min") or 0) >= above:
            bad.append(f"{name} wants {p['years_min']}+ years")
        if rule.get("role_not"):
            hay = f"{p.get('title', '')} {p.get('role_family') or ''}".lower()
            if not any(n in hay for n in rule["role_not"]):
                bad.append(f"{name} is not the role you asked for "
                           f"(kind of work: {p.get('role_family') or 'unknown'})")
        within = rule.get("posted_within")
        age = _age_days(p)
        if within and age is not None and age > within:
            bad.append(f"{name} was posted {age} days ago")
    return bad


def run_rank() -> dict:
    _prepare_env(WARM)
    import tokens
    tokens.install()
    _stub_side_effects()
    from engine import db
    from search.run import search

    cases = [json.loads(l) for l in (HERE / "cases_rank.jsonl").read_text()
             .splitlines() if l.strip()]
    n_open = open_jobs()
    conn = _retry(db.connect)
    rows = []
    try:
        for c in cases:
            m = tokens.mark()
            t0 = time.monotonic()
            try:
                r = _retry(lambda: search(c["say"], conn=conn))
                picks = r["picks"]
                bad = check_picks(c, picks)
                extra = {"matched": r["funnel"]["matched"],
                         "looked_for": r["funnel"]["looked_for"],
                         "notes": r.get("notes") or []}
            except Exception as exc:                    # noqa: BLE001
                picks, extra = [], {}
                bad = [f"it crashed: {type(exc).__name__}: {exc}"]
            rows.append({**c, "problems": bad, **extra,
                         "picks": [{"title": p.get("title"),
                                    "company": p.get("company"),
                                    "city": p.get("city") or p.get("location"),
                                    "remote": _is_remote(p),
                                    "role_family": p.get("role_family"),
                                    "years_min": p.get("years_min"),
                                    "posted_at": (p.get("posted_at") or "")[:10],
                                    "source": p.get("source")}
                                   for p in picks],
                         "seconds": round(time.monotonic() - t0, 1),
                         "tokens": tokens.since(m)})
            print(("ok  " if not bad else "FAIL"), c["id"], len(picks),
                  "|", "; ".join(bad))
    finally:
        conn.close()
    return {"ran_at": datetime.now().isoformat(timespec="seconds"),
            "open_jobs": n_open, "rows": rows}


# --- chat probes -----------------------------------------------------------
# Each probe is a fresh conversation. Checks are mechanical: a search filter
# from the log, or a phrase in the reply.

PROBES = [
    {"id": "chat-hi-attach", "profile": "cold", "turns": ["Hi"],
     "check": "no_phrase", "phrases": ["attach button", "button in the chat",
                                         "a button"],
     "bug": 2, "what": "Says 📎, not 'attach button' (Telegram has no button by that name)"},
    {"id": "chat-posted-today", "profile": "warm",
     "turns": ["Growth Roles which posted today"],
     "check": "no_city", "bug": 5,
     "what": "Searches every city (you did not name one)"},
    {"id": "chat-only-growth", "profile": "warm",
     "turns": ["Can you find only growth roles ?"],
     "check": "no_city", "bug": 5,
     "what": "Searches every city (you did not name one)"},
    {"id": "chat-founder-office", "profile": "warm",
     "turns": ["Find founder office roles, recency should be 7 to 10 days"],
     "check": "no_city", "bug": 5,
     "what": "Searches every city (you did not name one)"},
    {"id": "chat-see-resume", "profile": "warm",
     "turns": ["Build my resume", "Can I see the resume here ?"],
     "check": "no_path", "bug": 1,
     "what": "Sends or offers the file, does not paste a Mac file path"},
    {"id": "chat-blocked-not-applied", "profile": "warm", "blocked_app": True,
     "turns": ["Have I applied to CRED?"],
     "check": "no_phrase", "phrases": ["already applied", "you've applied",
                                        "you have applied", "application is in"],
     "bug": 4, "what": "Says the CRED form was filled but NOT submitted"},
]


def _stub_apply(chat_mod) -> None:
    from apply import worker

    def refuse(*a, **k):
        raise RuntimeError("eval: applying is disabled in launch evals")
    worker.start = refuse
    original = chat_mod.run_tool

    def run_tool(name, args, state, conn):
        if name in ("start_application", "answer_application"):
            return "ERROR: eval run, applying is switched off. Say so."
        return original(name, args, state, conn)
    chat_mod.run_tool = run_tool


def _seed_warm() -> None:
    """Sai's own resume and vault in the scratch profile, as the Telegram
    profile had them on 24 Sep (location Bengaluru)."""
    import shutil
    import paths
    import vault as v
    from resume import parse
    import career
    if paths.MASTER.exists():
        return
    paths.ensure()
    dest = paths.UPLOADS / SAI_PDF.name
    shutil.copy(SAI_PDF, dest)
    master = parse.parse(dest)
    for k, val in parse.vault_rows(master).items():
        v.put(k, val, source="resume")
    v.put("master_resume_path", str(dest), source="resume")
    career.seed_from_master(master)


def _blocked_row(conn) -> None:
    """A filled-not-submitted CRED application, like Sai's run on 24 Sep."""
    if conn.execute("SELECT 1 FROM applications WHERE company='CRED'").fetchone():
        return
    conn.execute(
        "INSERT INTO applications (source, source_id, company, title, apply_url,"
        " status) VALUES ('lever', 'b3fdba2a-802c-4e03-b445-5284c5e3c157',"
        " 'CRED', 'credit card bill payments growth',"
        " 'https://jobs.lever.co/cred/b3fdba2a-802c-4e03-b445-5284c5e3c157/apply',"
        " 'blocked')")
    conn.commit()


def run_chat(which: str) -> dict:
    _prepare_env(WARM if which == "warm" else COLD)
    import tokens
    tokens.install()
    _stub_side_effects()
    import chat
    import chatlog
    import paths
    from engine import db
    _stub_apply(chat)
    paths.ensure()
    if which == "warm":
        _seed_warm()
    rows = []
    for p in [p for p in PROBES if p["profile"] == which]:
        conn = _retry(db.connect)
        if p.get("blocked_app"):
            _blocked_row(conn)
        state = {"picks": [], "resumes": {}, "fixture": None, "channel": "telegram",
                 "session": chatlog.new_session()}
        history: list = []
        m = tokens.mark()
        t0 = time.monotonic()
        replies, funnels, err = [], [], None
        for turn in p["turns"]:
            try:
                text, history = chat.reply(turn, history, state, conn)
            except Exception as exc:                    # noqa: BLE001
                text, err = "", f"{type(exc).__name__}: {exc}"
            replies.append(text)
            row = conn.execute(
                "SELECT meta FROM messages WHERE session=? AND role='assistant' "
                "ORDER BY id DESC LIMIT 1", (state["session"],)).fetchone()
            meta = json.loads(row["meta"]) if row and row["meta"] else {}
            for s in meta.get("steps") or []:
                if s.get("funnel"):
                    funnels.append({"query": s["funnel"].get("query"),
                                    "filter": s["funnel"].get("filter")})
        last = replies[-1] if replies else ""
        problems = []
        if err:
            problems.append(f"it crashed: {err}")
        if p["check"] == "no_phrase":
            for ph in p["phrases"]:
                if ph.lower() in last.lower():
                    problems.append(f'the reply says "{ph}"')
        elif p["check"] == "no_city":
            if not funnels:
                problems.append("it never searched")
            for fn in funnels:
                city = (fn["filter"] or {}).get("city")
                if city:
                    problems.append(f'it searched "{fn["query"]}", adding city '
                                    f"{city} you never asked for")
        elif p["check"] == "no_path":
            if "/Users/" in last or ".pdf`" in last:
                problems.append("the reply pastes a file path on this Mac "
                                "instead of sending the PDF")
        rows.append({**p, "replies": replies, "funnels": funnels,
                     "problems": problems,
                     "seconds": round(time.monotonic() - t0, 1),
                     "tokens": tokens.since(m)})
        print(("ok  " if not problems else "FAIL"), p["id"], "|",
              "; ".join(problems))
        conn.close()
    return {"ran_at": datetime.now().isoformat(timespec="seconds"),
            "open_jobs": open_jobs(), "rows": rows}


def main(argv: list[str]) -> int:
    OUT.mkdir(exist_ok=True)
    which = argv[0] if argv else "all"
    if which == "all":
        for stage in ("filters", "rank", "chat"):
            subprocess.run([sys.executable, __file__, stage], cwd=ROOT)
        return 0
    if which == "chat":
        for prof in ("cold", "warm"):
            subprocess.run([sys.executable, __file__, "chat-" + prof], cwd=ROOT)
        # merge
        rows, meta = [], {}
        for prof in ("cold", "warm"):
            f = OUT / f"chat_{prof}.json"
            if f.exists():
                d = json.loads(f.read_text())
                rows += d["rows"]
                meta = d
        json.dump({**meta, "rows": rows}, open(OUT / "chat.json", "w"),
                  indent=1, ensure_ascii=False, default=str)
        return 0
    sys.path.insert(0, str(HERE))
    if which == "recheck-rank":
        # Re-score saved picks after a checker fix, without paying for the
        # model calls again.
        d = json.loads((OUT / "rank.json").read_text())
        cases = {json.loads(l)["id"]: json.loads(l) for l in
                 (HERE / "cases_rank.jsonl").read_text().splitlines() if l.strip()}
        for r in d["rows"]:
            r["must_not_pick"] = cases[r["id"]]["must_not_pick"]
            r["problems"] = check_picks(r, r["picks"])
            print(("ok  " if not r["problems"] else "FAIL"), r["id"], r["problems"])
        (OUT / "rank.json").write_text(json.dumps(d, indent=1, ensure_ascii=False))
        return 0
    if which == "filters":
        data, name = run_filters(), "filters.json"
    elif which == "rank":
        data, name = run_rank(), "rank.json"
    elif which in ("chat-cold", "chat-warm"):
        data, name = run_chat(which[5:]), f"chat_{which[5:]}.json"
    else:
        print(__doc__)
        return 2
    (OUT / name).write_text(json.dumps(data, indent=1, ensure_ascii=False,
                                       default=str))
    print(f"wrote {OUT / name}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
