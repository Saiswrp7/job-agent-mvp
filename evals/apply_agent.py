"""Agent eval: does the REAL apply agent fill real live forms, all the way?

`evals/apply_forms.py` fills forms with fixed answers and no model. That
missed a real failure: on 2026-09-30 a Swiggy (SmartRecruiters) form went to
page 2, the reader dropped page 2's salary / notice / relocation boxes because
their labels were only "*", and the agent asked the person to approve a
half-filled form. Nothing in a fixed-answer eval could see that, because the
dropped boxes never reached anything that counted them.

This eval runs the agent as it runs for a person: the real model
(`apply.agent.start`), a real Browserbase browser (`CloudBrowser`), on real
live job forms, with a MADE-UP applicant (example.com email, fake phone,
synthetic resume). It stops where the agent would ask the person to approve
sending, and checks the page at that moment:

  reached_submit  the agent called submit with every field it read filled, and
                  was refused only because submitting is off for this run
  dropped == 0    every question visible on the final page was read (counted
                  independently of the reader, by a deep shadow-DOM walk)
  required filled no required field the agent knew of is empty
  named           no box the reader could not name is left empty (since
                  2026-10-01 such a box reaches the agent as "field 12" or
                  its HTML name instead of being dropped)

A form passes only with all four.

**It never submits.** Every agent process runs with ALLOW_SUBMIT=0 and
SUBMIT_USERS= (empty); real environment values win over .env. The script
refuses to start otherwise, and checks `confirm.not_allowed()` refuses too.
With submit off, `submit` raises SubmitRefused inside the browser guard: that
refusal is the "reached the end, would ask for approval" signal measured here.
It uses a scratch profile only (profiles/eval-apply-agent), never a real one.

    ALLOW_SUBMIT=0 SUBMIT_USERS= python3 evals/apply_agent.py                 # 9 sites x 5
    ALLOW_SUBMIT=0 SUBMIT_USERS= python3 evals/apply_agent.py --sites lever --per-site 1

Writes evals/out/apply_agent/<site>_<n>.{json,png,log} and
evals/out/apply_agent_report.md.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRATCH = ROOT / "profiles" / "eval-apply-agent"
OUT = ROOT / "evals" / "out"
RUNS = OUT / "apply_agent"
RESUME = ROOT / "evals" / "fixtures" / "resumes" / "synthetic_senior.pdf"
SITES = ["greenhouse", "lever", "smartrecruiters", "keka", "ashby", "workable",
         "recruitee", "teamtailor", "breezy"]
MAX_RESUMES = 2
#: One worker process: start + up to two resumes, each a cloud session capped
#: at 15 minutes. Past this the process is killed and the form marked failed.
WORKER_TIMEOUT = 45 * 60

#: Every agent process gets exactly this, whatever the shell or .env says.
SAFE_ENV = {"ALLOW_SUBMIT": "0", "SUBMIT_USERS": "", "JOB_AGENT_HOME": str(SCRATCH),
            "JOB_AGENT_BROWSER": "cloud", "PYTHONUNBUFFERED": "1"}


def _refuse(why: str) -> None:
    sys.exit(f"apply_agent eval refused to start: {why}")


# --- safety, before any project import -----------------------------------
# Paths are resolved when `paths` is imported, so the profile must be set
# first. Only when run as a script: tests import the pure helpers below.
if __name__ == "__main__":
    if os.environ.get("ALLOW_SUBMIT") != "0":
        _refuse("run it with ALLOW_SUBMIT=0 in the environment "
                "(ALLOW_SUBMIT=0 SUBMIT_USERS= python3 evals/apply_agent.py)")
    os.environ.update(SAFE_ENV)
    sys.path.insert(0, str(ROOT))


def _check_safe() -> None:
    """After the project is imported (llm has read .env by then): submit is
    off, and this process is in the scratch profile and nowhere else."""
    import paths                                    # noqa: PLC0415
    from apply import confirm                       # noqa: PLC0415
    import llm                                      # noqa: F401,PLC0415  (loads .env)
    if os.environ.get("ALLOW_SUBMIT") != "0":
        _refuse("ALLOW_SUBMIT is not 0 after loading .env")
    if os.environ.get("SUBMIT_USERS", "") != "":
        _refuse("SUBMIT_USERS is not empty")
    if confirm.not_allowed() is None:
        _refuse("confirm.not_allowed() says this profile may submit")
    if paths.HOME != SCRATCH.resolve() or paths.IS_DEFAULT or paths.HOME.name.startswith("tg-"):
        _refuse(f"profile is {paths.HOME}, not the scratch profile {SCRATCH}")


# --- the made-up applicant (matches evals/apply_forms.py and the resume) ---

#: Vault rows for the scratch profile. Keys are vault.FIELDS keys.
VAULT = {
    "full_name": "Kavita Iyer",
    "first_name": "Kavita",
    "last_name": "Iyer",
    "email": "kavita.iyer@example.com",
    "phone": "9000000003",
    "location": "Bengaluru, India",
    "linkedin": "https://www.linkedin.com/in/example-kavita",
    "portfolio": "https://example.com",
    "current_company": "Example Payments",
    "current_title": "Director of Product",
    "years_experience": "11",
    "notice_period": "30 days",
    "current_ctc": "2500000",
    "current_fixed_ctc": "2200000",
    "current_variable_ctc": "300000",
    "expected_ctc": "3000000",
    "work_authorization": "Yes, authorized to work in India",
    "sponsorship": "No",
    "relocate": "Yes",
    "gender": "Prefer not to say",
    "ethnicity": "Prefer not to say",
    "veteran_status": "Prefer not to say",
    "disability": "Prefer not to say",
    "referral": "LinkedIn",
}

#: What "the person" replies when the agent parks on a question.
ANSWER = (
    "Here are my details, use whichever the form asks for: "
    "full name Kavita Iyer; email kavita.iyer@example.com; phone 9000000003; "
    "location Bengaluru, India; current company Example Payments; current title "
    "Director of Product; total experience 11 years; notice period 30 days; "
    "current CTC 2500000 INR a year (fixed 2200000, variable 300000); expected CTC "
    "3000000 INR a year; authorized to work in India: yes; need visa sponsorship: no; "
    "willing to relocate: yes; heard about the job on LinkedIn; highest education "
    "B.Tech from Example Institute of Technology, graduated 2014; date of birth "
    "01/01/1990; gender, ethnicity, veteran and disability: prefer not to say; "
    "previously worked at this company: no; earliest start date 2026-11-02. "
    "Skip optional questions not covered here. For a required question not covered "
    "here, answer Yes to a yes/no question, pick the first sensible option in a "
    "dropdown, and write one short plain sentence for free text."
)


def resumable(question: str | None) -> bool:
    """A park worth answering: not a sign-in wall or a captcha, and not the
    approval question (which never comes while submit is off)."""
    q = question or ""
    return not re.search(r"sign[ -]?in|log ?in|password|captcha|READY TO SUBMIT", q, re.I)


# --- counting what the page shows, independently of the reader -------------

def count_js() -> str:
    """Every visible control a person would answer, from a deep shadow-DOM
    walk. Uses cloud._DEEP for `deepAll`, `shown`, `notAsked` (honeypots,
    cookie banners, search boxes), and reports each control's `data-ja-ref`
    so it can be matched against what the reader kept."""
    from apply.cloud import _DEEP, CUSTOM_CAPTCHAS  # noqa: PLC0415
    # A captcha box the cloud browser solves (Keka) is not the agent's to read.
    cap = json.dumps(", ".join(v[1] for v in CUSTOM_CAPTCHAS.values()))
    return r"""() => {""" + _DEEP + "\n  const CAPTCHA = " + cap + ";\n" + r"""
  const txt = n => (n ? (n.innerText || n.textContent || '') : '').replace(/\s+/g, ' ').trim();
  const out = [];
  for (const el of deepAll('input,select,textarea,[role=combobox]')) {
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || (tag === 'input' ? 'text' : tag)).toLowerCase();
    if (['hidden', 'submit', 'button', 'image', 'reset', 'search'].includes(type) || el.disabled) continue;
    // A combobox wrapper around its own input is one control, not two.
    if (!['input', 'select', 'textarea'].includes(tag) && el.querySelector('input,select,textarea')) continue;
    if (type !== 'file' && !shown(el)) continue;
    // react-select's validation twin: not something a person sees.
    if (type !== 'file' && el.getAttribute('aria-hidden') === 'true') continue;
    if (notAsked(el)) continue;
    // A read-only box nobody can type in is a label in disguise (Keka's
    // "Years", "Months", "INR"); a calendar box is the exception. The reader
    // skips these on purpose since 2026-10-01.
    if (tag === 'input' && type !== 'file' && (el.readOnly || el.hasAttribute('readonly'))
        && !/datepicker|date-picker/i.test(String(el.className))
        && !closestDeep(el, '[class*=datepicker i],[class*=date-picker i],[class*=input-calendar i]')
        && !/date|dob|birth/i.test(`${el.id} ${el.getAttribute('name') || ''}`)) continue;
    // A styled dropdown (select2, Keka's own) is the list behind it: the
    // reader keeps that list's ref, so count the question under it.
    let refEl = el;
    if (el.getAttribute('role') === 'combobox' && !['input', 'select', 'textarea'].includes(tag)) {
      let n = el, found = null;
      for (let k = 0; k < 4 && n && !found; k++) {
        n = n.parentElement;
        if (!n) break;
        const prev = n.previousElementSibling;
        found = (prev && prev.tagName === 'SELECT') ? prev : n.querySelector('select');
      }
      if (found) refEl = found;
    }
    const host = hostOf(el);
    let value = '';
    if (type === 'radio' || type === 'checkbox') value = el.checked ? 'on' : '';
    else if (type === 'file') value = el.files && el.files.length ? el.files[0].name : '';
    else if (tag === 'select') value = el.value ? txt(el.selectedOptions[0]) : '';
    else value = (el.value || '').trim();
    const lab = (el.labels && el.labels.length) ? txt(el.labels[0]) : '';
    out.push({
      ref: refEl.getAttribute('data-ja-ref') || '', tag, type,
      name: el.getAttribute('name') || el.id || '',
      group: (type === 'radio' || type === 'checkbox') ? (el.getAttribute('name') || '') : '',
      required: !!(el.required || el.getAttribute('aria-required') === 'true'
                   || (host && host.hasAttribute('required') && host.getAttribute('required') !== 'false')),
      hint: (el.getAttribute('aria-label') || lab || el.getAttribute('placeholder')
             || (host && (host.getAttribute('label') || host.tagName.toLowerCase())) || '').slice(0, 80),
      value: value.slice(0, 60),
      captcha: !!(CAPTCHA && el.matches(CAPTCHA))});
  }
  return out;
}"""


_HANDOFF = re.compile(r"\bapply (with|using)\b", re.I)


def questions(controls: list[dict]) -> list[dict]:
    """Controls folded into questions: a radio group, or a set of checkboxes
    sharing a name, is one question. A lone control is its own."""
    out, by_group = [], {}
    for c in controls:
        key = c.get("group") or ""
        if key and key in by_group:
            q = by_group[key]
            q["refs"].append(c.get("ref") or "")
            q["required"] = q["required"] or bool(c.get("required"))
            q["value"] = q["value"] or c.get("value") or ""
            continue
        q = {"refs": [c.get("ref") or ""], "type": c.get("type"), "name": c.get("name") or "",
             "hint": c.get("hint") or "", "required": bool(c.get("required")),
             "value": c.get("value") or "",
             # carried through so `classify` can tell a solved captcha box
             # from a dropped question (it read q["captcha"], never set).
             "captcha": bool(c.get("captcha"))}
        out.append(q)
        if key:
            by_group[key] = q
    return out


def kept_refs(where: dict) -> set[str]:
    """The refs the reader kept (CloudBrowser._where after read_form)."""
    refs = set()
    for spot in (where or {}).values():
        if spot.get("ref"):
            refs.add(str(spot["ref"]))
        for r in (spot.get("options") or {}).values():
            refs.add(str(r))
    return refs


def dropped(qs: list[dict], kept: set[str]) -> list[dict]:
    """Questions on the page the reader did not hand the agent. "Apply with
    LinkedIn" boxes are left out on purpose by the reader, so not counted."""
    return [q for q in qs
            if not any(r and r in kept for r in q["refs"])
            and not _HANDOFF.search(f"{q['hint']} {q['name']}")]


_PAGE_ID = re.compile(r"field \d+|[\w\-\[\].:]*[\d_\[][\w\-\[\].:]*")


def unlabeled(fields: list[dict]) -> list[str]:
    """Fields the reader kept but could not name: since 2026-10-01 a box with
    no readable label reaches the agent as "field 12" or its HTML name
    ("cards[a][field0]") instead of being dropped. Kept is better than
    dropped, but the agent still cannot know what such a box asks."""
    out = []
    for f in fields:
        if f.get("type") == "file":
            continue
        label = re.sub(r" \(\d+\)$", "", (f.get("label") or f.get("name") or "").strip())
        if not label or _PAGE_ID.fullmatch(label):
            out.append(f["name"])
    return out


# --- reading the run's log ------------------------------------------------

def read_log(rows: list[dict], refusal: str) -> dict:
    """What a run did, from its agent_logs rows (id, step, kind, payload),
    oldest first. `refusal` is confirm.not_allowed()'s text."""
    steps = pages_ok = next_calls = 0
    reached = sent = False
    replays, submit_results, last_call = [], [], None
    for r in rows:
        kind, payload = r["kind"], r["payload"] or ""
        if kind == "prompt":
            steps += 1
        elif kind == "browser":
            try:
                d = json.loads(payload)
            except ValueError:
                d = {}
            if d.get("replay"):
                replays.append(d["replay"])
        elif kind == "tool_call":
            try:
                last_call = json.loads(payload).get("name")
            except ValueError:
                last_call = None
        elif kind == "tool_result":
            if last_call == "next_page":
                next_calls += 1
                if payload.startswith("on the next page"):
                    pages_ok += 1
                if payload.startswith(("submitted", "unconfirmed")):
                    sent = True
            elif last_call == "submit":
                submit_results.append(payload[:300])
                if payload.startswith("REFUSED:") and refusal and refusal in payload:
                    reached = True
                if payload.startswith(("submitted", "unconfirmed")):
                    sent = True
            last_call = None
    return {"steps": steps, "pages": 1 + pages_ok, "next_page_calls": next_calls,
            "reached_submit": reached, "sent": sent, "replays": replays,
            "submit_results": submit_results}


def classify(status: str | None, log: dict, final: dict | None) -> tuple[str, bool, list[str]]:
    """(class, passed, reasons). `final` is the last capture of the page."""
    final = final or {}
    if log.get("sent"):
        return "SENT", False, ["SAFETY: the log says something was sent"]
    if log.get("reached_submit"):
        klass = "reached_submit"
    elif status == "waiting":
        klass = "parked"
    elif status in ("no_form", "sign_in"):
        klass = status
    else:
        klass = "failed"
    why = []
    if klass != "reached_submit":
        why.append({"parked": "parked on a question",
                    "no_form": "no form found", "sign_in": "sign-in wall"}.get(
                        klass, f"did not reach submit (status {status})"))
    # Solved by the cloud browser: counts only if it was left empty.
    drop = [q for q in (final.get("dropped") or []) if not (q.get("captcha") and q.get("value"))]
    if drop:
        why.append(f"{len(drop)} visible question(s) not read: "
                   + ", ".join(repr(q.get("hint") or q.get("name") or q.get("type")) for q in drop[:6]))
    if final.get("required_empty"):
        why.append(f"required empty: {final['required_empty'][:6]}")
    if final.get("unlabeled_empty"):
        why.append(f"{len(final['unlabeled_empty'])} unnamed box(es) left empty: {final['unlabeled_empty'][:6]}")
    if final.get("error"):
        why.append(f"capture failed: {final['error']}")
    passed = klass == "reached_submit" and not drop and not final.get("required_empty") \
        and not final.get("unlabeled_empty") and not final.get("error")
    return klass, passed, why


# --- one form, in its own process ------------------------------------------

def _capture(b, shot: Path) -> dict:
    """The page as it is just before the session closes."""
    out: dict = {"at": time.time()}
    try:
        fields = b.read_form()
        values = b.current_values()
        st = b.page_state() or {}
        try:
            b.page.screenshot(path=str(shot), full_page=True)
            out["screenshot"] = str(shot.relative_to(ROOT))
        except Exception as exc:                    # noqa: BLE001
            out["screenshot_error"] = str(exc)[:200]
        controls = b.page.evaluate(count_js())
        qs = questions(controls)
        drop = dropped(qs, kept_refs(getattr(b, "_where", {})))
        out.update(
            url=st.get("url"), title=st.get("title"), buttons=(st.get("buttons") or [])[:20],
            captcha=st.get("captcha"), invalid=st.get("invalid"),
            read=len(fields), visible=len(qs), dropped=drop,
            fields=fields, values=values,
            required_empty=[f["name"] for f in fields if f.get("required") and not values.get(f["name"])],
            optional_empty=[f["name"] for f in fields if not f.get("required") and not values.get(f["name"])],
            unlabeled=unlabeled(fields),
            unlabeled_empty=[n for n in unlabeled(fields) if not values.get(n)],
            text=(st.get("text") or "")[:1500])
    except Exception as exc:                        # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
    return out


def _wrap_close(b, captures: list, sessions: list, shot: Path) -> None:
    """Record the final page before agent._close shuts the session."""
    orig = b.close
    opened = time.time()

    def close() -> None:
        try:
            captures.append(_capture(b, shot))
        finally:
            sessions.append(round(time.time() - opened, 1))
            orig()
    b.close = close


def work_one(job: dict, key: str) -> dict:
    """Run the agent on one form; must be in the scratch profile already."""
    _check_safe()
    from apply import agent, confirm                # noqa: PLC0415
    from engine import db                           # noqa: PLC0415
    refusal = confirm.not_allowed() or ""
    conn = db.connect()
    t0 = time.time()
    r = {"key": key, "site": job["source"], "company": job["company"], "title": job["title"],
         "url": job.get("apply_url") or job.get("url"), "resumes": 0, "questions": []}
    captures, sessions = [], []
    status, result, floor = None, {}, 0
    try:
        app_id = agent.create(job, str(RESUME), conn)
        r["app_id"] = app_id
        floor = conn.execute("SELECT COALESCE(MAX(id), 0) FROM agent_logs").fetchone()[0]
        b = agent.browser_for(job, app_id)
        _wrap_close(b, captures, sessions, RUNS / f"{key}.png")
        print(f"[{key}] started, replay {getattr(b, 'replay_url', None)}", flush=True)
        result = agent.start(app_id, job, b, str(RESUME), conn)
        status = result.get("status")
        print(f"[{key}] start -> {status}", flush=True)
        while status == "waiting" and r["resumes"] < MAX_RESUMES and resumable(result.get("question")):
            r["questions"].append(result.get("question"))
            b = agent.browser_for(job, app_id, resume=True)
            _wrap_close(b, captures, sessions, RUNS / f"{key}.png")
            r["resumes"] += 1
            result = agent.resume_run(app_id, ANSWER, b, conn)
            status = result.get("status")
            print(f"[{key}] resume {r['resumes']} -> {status}", flush=True)
        if status == "waiting":
            r["questions"].append(result.get("question"))
    except Exception as exc:                        # noqa: BLE001
        # A crash mid-run (a model error, a dead session) is a failure, even
        # if an earlier leg had parked.
        r["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        status = "failed"
    log = {}
    try:
        if r.get("app_id"):
            rows = [dict(x) for x in conn.execute(
                "SELECT id, step, kind, payload FROM agent_logs WHERE app_id = ? AND id > ? ORDER BY id",
                (r["app_id"], floor))]
            # Pages are counted in the last run only: a resumed run starts again on page 1.
            starts = [i for i, x in enumerate(rows)
                      if x["kind"] == "browser" and '"replay"' in (x["payload"] or "")]
            log = read_log(rows, refusal)
            last = read_log(rows[starts[-1]:] if starts else rows, refusal)
            r.update(steps=log["steps"], pages=last["pages"], next_page_calls=log["next_page_calls"],
                     replays=log["replays"], submit_results=log["submit_results"],
                     reached_submit=log["reached_submit"], sent=log["sent"])
    finally:
        conn.close()
    final = captures[-1] if captures else {}
    klass, passed, why = classify(status, log, final)
    if r.get("error"):
        why.insert(0, r["error"])
    r.update(status=status, klass=klass, passed=passed, reasons=why,
             message=(result.get("message") or result.get("question") or result.get("detail") or "")[:600],
             read=final.get("read"), visible=final.get("visible"),
             dropped=final.get("dropped") or [], required_empty=final.get("required_empty") or [],
             optional_empty=final.get("optional_empty") or [],
             unlabeled=final.get("unlabeled") or [],
             final_url=final.get("url"), screenshot=final.get("screenshot"),
             captures=captures, session_seconds=sessions,
             browser_minutes=sum(max(1, math.ceil(s / 60)) for s in sessions),
             seconds=round(time.time() - t0, 1))
    return r


# --- the sweep ---------------------------------------------------------------

def sample(conn, sites: list[str], per_site: int, seed: int) -> list[dict]:
    """Live form jobs, one per company (as evals/apply_forms.sample; copied,
    not imported: that module pops ALLOW_SUBMIT at import)."""
    from engine import db                           # noqa: PLC0415
    rnd = random.Random(seed)
    picked = []
    for site in sites:
        rows = [dict(r) for r in conn.execute(
            f"SELECT source, source_id, company, title, apply_url, url FROM jobs "
            f"WHERE {db.VISIBLE} AND apply_kind='form' AND source=? ORDER BY source_id", (site,))]
        rnd.shuffle(rows)
        seen = set()
        for r in rows:
            if r["company"] in seen:
                continue
            seen.add(r["company"])
            picked.append(r)
            if len(seen) >= per_site:
                break
    return picked


def live_counts(conn, sites: list[str]) -> dict[str, int]:
    from engine import db                           # noqa: PLC0415
    return {s: conn.execute(f"SELECT COUNT(*) FROM jobs WHERE {db.VISIBLE} "
                            f"AND apply_kind='form' AND source=?", (s,)).fetchone()[0]
            for s in sites}


def seed_vault(conn) -> None:
    import vault                                    # noqa: PLC0415
    conn.execute("PRAGMA main.journal_mode=WAL")    # four workers write logs at once
    for k, v in VAULT.items():
        assert k in vault.FIELDS, k
        vault.put(k, v, source="user", conn=conn)


def _launch(job: dict, key: str) -> dict:
    jf = RUNS / f"{key}.job.json"
    jf.write_text(json.dumps(job))
    env = {**os.environ, **SAFE_ENV}
    t0 = time.time()
    with open(RUNS / f"{key}.log", "w") as log:
        try:
            p = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--one", str(jf), "--key", key],
                               env=env, stdout=log, stderr=subprocess.STDOUT, timeout=WORKER_TIMEOUT,
                               cwd=str(ROOT))
            code = p.returncode
        except subprocess.TimeoutExpired:
            code = "timeout"
    out = RUNS / f"{key}.json"
    if code == 0 and out.exists():
        return json.loads(out.read_text())
    return {"key": key, "site": job["source"], "company": job["company"], "title": job["title"],
            "url": job.get("apply_url") or job.get("url"), "klass": "failed", "passed": False,
            "status": "crashed", "reasons": [f"worker {'timed out' if code == 'timeout' else f'exited {code}'}; "
                                             f"see {(RUNS / (key + '.log')).relative_to(ROOT)}"],
            "dropped": [], "required_empty": [], "replays": [], "seconds": round(time.time() - t0, 1)}


def _reason_kind(r: dict) -> str:
    if r.get("passed"):
        return ""
    part = (r.get("reasons") or ["unknown"])[0]
    return re.split(r":| \(status", re.sub(r"^\d+ ", "N ", part))[0].strip()


def report(results: list[dict], live: dict[str, int], stamp: str) -> str:
    ok = sum(1 for r in results if r.get("passed"))
    lines = [f"# Apply agent eval, {stamp}", "",
             f"**{ok}/{len(results)} forms pass**: the real agent reached the submit step "
             f"with every visible question read and every required field filled.", "",
             "Real model, real Browserbase browser, real live forms, made-up applicant. "
             "Submit was off (ALLOW_SUBMIT=0, SUBMIT_USERS empty): nothing was sent.", "",
             "| Site | Live form jobs | Run | Pass | Top failure |", "|---|---|---|---|---|"]
    by: dict[str, list[dict]] = {}
    for r in results:
        by.setdefault(r["site"], []).append(r)
    for site, rs in by.items():
        kinds = Counter(_reason_kind(r) for r in rs if not r.get("passed"))
        top = ", ".join(f"{k} ({n})" for k, n in kinds.most_common(2)) or "none"
        lines.append(f"| {site} | {live.get(site, '?')} | {len(rs)} | "
                     f"{sum(1 for r in rs if r.get('passed'))} | {top} |")
    lines += ["", "## Every form", ""]
    for r in results:
        verdict = "PASS" if r.get("passed") else "FAIL"
        why = "; ".join(r.get("reasons") or []) or "ok"
        replay = ", ".join(r.get("replays") or []) or "no replay"
        lines.append(f"- **{verdict}** {r['key']} {r['company']} ({(r.get('title') or '')[:50]}): "
                     f"{r.get('klass')}, pages {r.get('pages', '?')}, read {r.get('read')} / "
                     f"visible {r.get('visible')}, dropped {len(r.get('dropped') or [])}, "
                     f"unnamed {len(r.get('unlabeled') or [])}, "
                     f"steps {r.get('steps', '?')}, {r.get('seconds')} s. {why}. Replay: {replay}")
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sites", default=",".join(SITES))
    ap.add_argument("--per-site", type=int, default=5)
    ap.add_argument("--parallel", type=int, default=4)
    ap.add_argument("--seed", type=int, default=30)
    ap.add_argument("--one", help=argparse.SUPPRESS)       # worker: a job file
    ap.add_argument("--key", help=argparse.SUPPRESS)
    a = ap.parse_args()
    RUNS.mkdir(parents=True, exist_ok=True)
    _check_safe()

    if a.one:
        r = work_one(json.loads(Path(a.one).read_text()), a.key)
        (RUNS / f"{a.key}.json").write_text(json.dumps(r, indent=1, default=str))
        print(f"[{a.key}] {'PASS' if r['passed'] else 'FAIL'} {r['klass']}: {'; '.join(r['reasons'])}")
        return

    from engine import db                           # noqa: PLC0415
    sites = [s.strip() for s in a.sites.split(",") if s.strip()]
    conn = db.connect()
    seed_vault(conn)
    jobs = sample(conn, sites, a.per_site, a.seed)
    live = live_counts(conn, sites)
    conn.close()
    n = Counter()
    keyed = []
    for j in jobs:
        n[j["source"]] += 1
        keyed.append((f"{j['source']}_{n[j['source']]}", j))
    # Interleave sites so parallel sessions are on different boards.
    keyed.sort(key=lambda kj: (int(kj[0].rsplit("_", 1)[1]), sites.index(kj[1]["source"])))
    print(f"{len(keyed)} forms, {a.parallel} at once, profile {SCRATCH.relative_to(ROOT)}, submit OFF")
    t0 = time.time()

    def go(kj):
        key, job = kj
        print(f"  start {key:20} {job['company'][:30]}", flush=True)
        r = _launch(job, key)
        print(f"  done  {key:20} {'PASS' if r.get('passed') else 'FAIL'} {r.get('klass')}: "
              f"{'; '.join(r.get('reasons') or [])[:140]}", flush=True)
        return r

    with ThreadPoolExecutor(max_workers=max(1, a.parallel)) as ex:
        results = list(ex.map(go, keyed))
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    md = report(results, live, stamp)
    (OUT / "apply_agent_report.md").write_text(md)
    mins = sum(r.get("browser_minutes") or 0 for r in results)
    print("\n" + md.split("## Every form")[0])
    print(f"wall {round(time.time() - t0)} s, about {mins} Browserbase minute(s) billed")


if __name__ == "__main__":
    main()
