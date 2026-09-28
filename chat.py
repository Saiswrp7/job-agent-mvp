"""The chat agent — the only thing the user talks to.

It routes and it speaks. It never does work itself: search, tailoring and
applying are functions behind five tools.

Its loop is shallow (one to three steps) because routing is all it does. The
deep loop lives in the apply agent, which has a context full of DOM and could
not write a decent sentence if it tried. That is the whole reason these are two
agents and not one.

Context assembly is in `context()`, and what it leaves out matters as much as
what it includes:

- the vault goes in as a **one-pager**, not every row
- the last results go in as a **reference table** — ids, titles, companies —
  never the job descriptions, which is what makes "apply to 2" cheap
- open applications are read **fresh from the database every turn**, so status
  can never be stale in the way a remembered fact can
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path

import chatlog
import llm
import usage
import paths
import vault
from apply import agent as apply_agent
from engine import db
from memory import reconcile

HERE = Path(__file__).resolve().parent
#: Per-profile. A new profile that inherited this would open with the previous
#: user's results already numbered, so "apply to 2" would mean something.
LAST = paths.LAST_SEARCH
KEEP_TURNS = 20
COMPACT_AT = 30_000        # characters, not tokens — a rough, cheap proxy

TOOLS = [
    {
        "name": "search_jobs",
        "description": ("Find jobs. Pass the person's request in plain English, "
                        "roughly as they said it."),
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"], "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "get_job",
        "description": ("Full detail on one job, by the [name] shown beside it "
                        "in the results."),
        "input_schema": {
            "type": "object",
            "properties": {"ref": {"type": "string"}},
            "required": ["ref"], "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "tailor_resume",
        "description": (
            "Build or rebuild a tailored resume for one job, by the [name] "
            "shown beside it. Returns the bullets as text. Call it again with "
            "`instruction` to revise — that is how a resume gets edited, and "
            "the only way: saying a bullet is changed does not change the PDF."),
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string"},
                "instruction": {
                    "type": "string",
                    "description": "what they asked for, in their words — "
                                   "\"stronger on revenue\", \"cut the last "
                                   "bullet\". Omit on a first build."},
                "layout": {
                    "type": "string",
                    "description": "a look they asked for, by name or in their "
                                   "words — \"Harvard\", \"something modern\", "
                                   "\"the IIM one\". Omit to use their saved "
                                   "one. What they pick becomes their default."},
            },
            "required": ["ref"], "additionalProperties": False,
        },
    },
    {
        "name": "build_resume",
        "description": (
            "Build their resume with no job attached, from everything on "
            "record — the resume they uploaded plus every accomplishment they "
            "have told you since. Use it when they ask for their resume rather "
            "than a resume *for* something. Returns the text and a PDF path."),
        "input_schema": {
            "type": "object",
            "properties": {
                "instruction": {
                    "type": "string",
                    "description": "what they asked for, in their words. "
                                   "Omit for a plain build."},
                "layout": {
                    "type": "string",
                    "description": "a look they asked for, by name or in their "
                                   "words — \"Harvard\", \"something modern\", "
                                   "\"the IIM one\". Omit to use their saved "
                                   "one. What they pick becomes their default."},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "resume_layouts",
        "description": (
            "What their resume can look like. Returns every layout, one line "
            "each, and the two that suit their role and years. With `preview`, "
            "renders page 1 of THEIR resume in those layouts as images and "
            "returns the paths, so they choose by looking, not by name. "
            "Choosing is done by passing `layout` to build_resume or "
            "tailor_resume."),
        "input_schema": {
            "type": "object",
            "properties": {
                "preview": {
                    "type": "array", "items": {"type": "string"},
                    "description": "layouts to show as images, by name or "
                                   "description. Omit for the list only."},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "send_resume",
        "description": (
            "Send them a resume that already exists: `file` is the one they "
            "uploaded, `latest` is the newest one built here. Use it when they "
            "ask to see, get, or resend their resume. Builds nothing."),
        "input_schema": {
            "type": "object",
            "properties": {
                "which": {"type": "string", "enum": ["file", "latest"],
                          "description": "`file` = their upload; `latest` = "
                                         "the newest built PDF."},
            },
            "required": ["which"],
            "additionalProperties": False,
        },
    },
    {
        "name": "remember_experience",
        "description": (
            "Put something they did on the record: an accomplishment, a "
            "metric, a skill. Use it when they tell you about their work — "
            "\"I grew activation 18%\", \"I led a team of six\" — and when "
            "they answer a question about where an unplaced item belongs. "
            "What is on the record is what a resume is allowed to say, so "
            "nothing they did not state goes in here."),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string",
                         "enum": ["accomplishment", "skill", "metric",
                                  "role", "education"]},
                "text": {"type": "string",
                         "description": "the claim, in their words"},
                "company": {"type": "string",
                            "description": "which role it belongs to, if they said"},
            },
            "required": ["kind", "text"], "additionalProperties": False,
        },
    },
    {
        "name": "set_resume_preference",
        "description": (
            "Change the default offered when you ask which resume to apply "
            "with — only when they state a standing preference (\"use my "
            "updated one from now on\"). Rarely needed: whatever they pick on an "
            "apply becomes the default by itself. It never answers the "
            "question for an application; you still ask on every apply."),
        "input_schema": {
            "type": "object",
            "properties": {
                "choice": {"type": "string",
                           "enum": ["file", "updated", "tailored"]},
            },
            "required": ["choice"], "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "start_application",
        "description": (
            "Begin applying to one result. Only after the person explicitly "
            "said to apply. Which resume goes out is asked on **every** apply "
            "— once for a batch — and passed as `resume`: `file` (the one "
            "they uploaded, as-is), `updated` (their general resume with what "
            "they have told you since, fitted to the page) or `tailored` "
            "(rebuilt for this job). It is checked against what they actually said, so a choice "
            "they did not make is refused and hands you the question to ask."),
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string",
                        "description": "the [name] shown beside the job"},
                "resume": {"type": "string",
                           "enum": ["file", "updated", "tailored"],
                           "description": "the one they chose for this apply"},
            },
            "required": ["ref"], "additionalProperties": False,
        },
    },
    {
        "name": "answer_application",
        "description": (
            "Give a parked application the values it asked for, so it can "
            "carry on. Use this whenever the person supplies details that an "
            "application is waiting on — never just acknowledge them, or the "
            "answers are lost. `app_id` comes from the open applications list."),
        "input_schema": {
            "type": "object",
            "properties": {
                "app_id": {"type": "integer"},
                "answer": {"type": "string",
                           "description": "their reply, verbatim"},
            },
            "required": ["app_id", "answer"], "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "application_status",
        "description": "Where every application got to.",
        "input_schema": {"type": "object", "properties": {},
                         "additionalProperties": False},
    },
]


#: Which files each tool actually runs, for the log.
#:
#: It lives here, next to `run_tool`, so a tool added without an entry is
#: obvious at the place you'd add it. It is written into each log row rather
#: than looked up when the log is printed: a turn from last week should still
#: say what ran last week, even after the code moves.
#:
#: `resume/parse.py` is deliberately absent — no tool reaches it. A resume can
#: only be ingested from the terminal or the upload button, which is a real gap
#: and not an omission here.
#: Each entry is [path, what that file does] — the second half is written for
#: someone who has not read the file, because a path alone only tells you
#: where something happened, never what.
TOOL_FILES = {
    "search_jobs": [
        ["search/run.py", "runs the three steps below, in order"],
        ["search/filters.py", "turns your sentence into a filter · asks the AI"],
        ["search/query.py", "narrows 670 jobs down to 20 · just SQL, no AI"],
        ["search/rank.py", "reads all 20 and picks the best · asks the AI"],
    ],
    "get_job": [
        ["artifacts/last_search.json", "the jobs from your last search, on disk"],
    ],
    "send_resume": [
        ["artifacts/resumes/", "the PDFs built for you, newest one goes"],
    ],
    "tailor_resume": [
        ["resume/tailor.py", "rewrites your bullets for this job · asks the AI"],
        ["resume/verify.py", "reverts any bullet that gained a fact · no AI"],
        ["resume/render.py", "builds the PDF"],
    ],
    "resume_layouts": [
        ["resume/layouts/__init__.py", "the looks available, and which suit you · no AI"],
        ["resume/generate.py", "renders page 1 of your resume in each · no AI"],
    ],
    "build_resume": [
        ["career.py", "everything you've told us you did · just SQL, no AI"],
        ["resume/generate.py", "your resume plus what you've added since · no AI"],
        ["resume/render.py", "builds the PDF"],
    ],
    "remember_experience": [
        ["career.py", "one typed row, with the message you said it in"],
    ],
    "set_resume_preference": [
        ["vault.py", "saves the resume offered first next time you apply"],
    ],
    "start_application": [
        ["resume/tailor.py", "builds a tailored resume first, if that is the one you chose"],
        ["resume/generate.py", "builds your updated resume first, if that is the one you chose"],
        ["apply/agent.py", "reads the whole form, works out what is missing"],
        ["apply/harness.py", "the loop · up to 40 steps, saved as it goes"],
        ["apply/browser.py", "drives the real page · refuses to submit"],
    ],
    "answer_application": [
        ["apply/agent.py", "reloads the parked run out of the database"],
        ["apply/harness.py", "carries on from exactly where it stopped"],
        ["apply/browser.py", "drives the real page · refuses to submit"],
    ],
    "application_status": [
        ["chat.py", "one read of the applications table"],
    ],
}


# --- state -----------------------------------------------------------------

def load_last() -> list[dict]:
    return json.loads(LAST.read_text()) if LAST.exists() else []


def save_last(picks: list[dict]) -> None:
    LAST.parent.mkdir(parents=True, exist_ok=True)
    LAST.write_text(json.dumps(picks, indent=1, default=str))


def reference_table(picks: list[dict]) -> str:
    """Names, titles, companies. Never descriptions — that is the point.

    **Deliberately unnumbered.** Numbering this table was tried and it caused
    the bug it was meant to fix: the person says "apply to 1" meaning the first
    job in the reply, and the model resolved it against *this* list instead,
    which is in search order. The two disagree whenever the reply is reordered,
    which is most of the time.

    With no numbers here there is only one numbered list in the context — the
    one the model wrote to them — so "1" can only mean what they read.
    """
    from search.rank import ref as _ref
    if not picks:
        return "(no search yet)"
    return "\n".join(
        f"[{_ref(p)}] {p['title']} — {p['company']}" for p in picks
    )


def open_applications(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        "SELECT id, company, title, status, question FROM applications "
        "WHERE status IN ('waiting', 'running', 'queued') ORDER BY id DESC LIMIT 10"
    ).fetchall()
    if not rows:
        return "(none)"
    out = []
    for r in rows:
        line = f"[{r['id']}] {r['title']} — {r['company']}: {r['status']}"
        if r["status"] == "waiting" and r["question"]:
            line += f" — waiting on: {r['question']}"
        out.append(line)
    return "\n".join(out)


def resume_status(conn: sqlite3.Connection) -> str:
    """Whether a resume is on file, in words the agent can act on.

    The vault one-pager is a fixed short list of keys and does not include
    `master_resume_path`, so without this the agent has no way to know a
    resume exists and tells the person to send one they already sent.
    """
    path = vault.get("master_resume_path", conn)
    from resume.parse import MASTER
    if not MASTER.exists():
        return ("No resume on file. Ask them to send it as a PDF with the 📎 "
                "paperclip.")
    try:
        master = json.loads(MASTER.read_text())
    except Exception:                                  # noqa: BLE001
        return f"A resume is on file at {path}."
    roles = ", ".join(
        f"{e.get('title')} at {e.get('company')}" for e in master.get("experience", [])
    )
    # Their last answer to "which resume goes out". It is a default to offer,
    # not a decision: the question is asked on every apply, and without the
    # last answer here the agent would ask it cold every time instead of
    # offering the one-word "same as last time".
    last = SHOWN_AS.get(RESUME_MODES.get(vault.get("resume_mode", conn) or ""))
    pref = ("Which resume to apply with: **ask on every apply** (once for a "
            "batch): \"" + resume_question(None, _has_updates(conn)) + "\" "
            + (f"Offer `{last}` as the default: it is what they picked last "
               f"time." if last else "No default yet: they have never picked."))
    from resume import layouts
    look = (f"Layout: {layouts.get(vault.get('resume_layout', conn))['label']}. "
            f"{len(layouts.LAYOUTS)} are available; resume_layouts lists and "
            f"previews them.")
    return (f"**A resume is on file** — parsed and ready, no need to ask for "
            f"it again.\nFile: {path}\nRoles: {roles or '(none parsed)'}\n"
            f"Summary: {master.get('summary', '')[:400]}\n{pref}\n{look}")


def context(conn: sqlite3.Connection) -> str:
    """The stable prefix. Cached; everything volatile goes after it."""
    profile = reconcile.read_profile(conn)
    return "\n\n".join([
        llm.prompt("chat"),
        "## Known about them\n" + vault.one_pager(conn),
        "## Resume\n" + resume_status(conn),
        "## Profile\n" + (profile[-2000:] if profile else "(nothing learned yet)"),
    ])


def turn_state(conn: sqlite3.Connection, picks: list[dict],
               news: list[dict] | None = None) -> str:
    """Rebuilt every turn. Never cached, never trusted from memory."""
    # The model has no clock. Without this it called a 24 Sep job "posted
    # today" on 28 Sep.
    from datetime import datetime
    today = datetime.now().strftime("%A %d %B %Y")
    out = (f"## Today\n{today}\n\n"
           f"## Last results — unordered. The only numbering that counts is "
           f"the one in your own reply to them.\n{reference_table(picks)}\n\n"
           f"## Open applications\n{open_applications(conn)}")
    if news:
        out += ("\n\n## Already told them, above your reply — do not repeat it\n"
                + news_lines(news))
    return out


#: How a finished background run reads to the person. Plain, and never "done":
#: `blocked` is a full form that was not sent.
_NEWS = {
    "submitted": "submitted",
    "blocked": "form filled, not submitted",
    "waiting": "paused, it needs an answer from you",
    "failed": "stopped before finishing",
}


def news_lines(news: list[dict]) -> str:
    out = []
    for r in news:
        detail = (r.get("outcome") or "").strip()
        first = re.split(r"(?<=[.!?])\s", detail)[0] if detail else ""
        out.append(f"Update — {r['title']} at {r['company']}: "
                   f"{_NEWS.get(r['status'], r['status'])}."
                   + (f" {first}" if first else ""))
    return "\n".join(out)


def compact(history: list, summarize=None) -> list:
    """Drop old turns, keep the tail, and leave the thread behind as notes.

    Facts already left the conversation as rows (career, preferences, vault),
    so what the dropped turns still held was thread: which jobs were compared,
    what they said they'd think about. It used to go with them. `summarize` is
    called only when something is actually dropped, and returns the running
    notes (`memory/thread.py`), which go where the turns were.
    """
    if len(json.dumps(history, default=str)) < COMPACT_AT:
        return history
    kept = history[-KEEP_TURNS:]
    # Never start on a tool_result — it would reference a tool_use that is gone.
    while kept and isinstance(kept[0].get("content"), list) and any(
            isinstance(b, dict) and b.get("type") == "tool_result"
            for b in kept[0]["content"]):
        kept = kept[1:]
    notes = ""
    if summarize is not None:
        try:
            notes = summarize() or ""
        except Exception:                              # noqa: BLE001
            notes = ""
    content = ("[earlier conversation trimmed. Facts and preferences are in "
               "the profile and vault above.")
    content += (f" What happened before, in notes:\n{notes}]" if notes else "]")
    return [{"role": "user", "content": content}, *kept]


# --- which resume goes out -------------------------------------------------
#
# Three resumes, and the person picks one on every apply: your file (the one
# they uploaded), updated (that plus what they have told us since, fitted to
# the page) and tailored. The record behind them, every verified fact, is never
# sent whole: nobody applies with everything they have ever done.
#
# Stored values outlive the names. `existing` has always meant their file.
# `master` was the earlier name for the updated one and reads as `updated` now,
# so a default saved before the rename still means what the person picked.

RESUME_MODES = {"file": "existing", "initial": "existing", "existing": "existing",
                "updated": "updated", "master": "updated", "tailored": "tailored"}
SHOWN_AS = {"existing": "your file", "updated": "updated", "tailored": "tailored"}

#: How each one sounds in a message. Deliberately narrow: a word that only
#: *might* mean a resume ("full", "my resume") is not an answer, it is a reason
#: to ask. Asking once more costs a message; guessing sends a document.
_SAID = [
    ("existing", re.compile(r"\b(initial|original|existing|as[- ]is|uploaded|"
                            r"(my|your|the|own) (own )?file|"
                            r"the one i (sent|uploaded|attached))\b", re.I)),
    ("updated", re.compile(r"\b(updated|general|master)\b", re.I)),
    ("tailored", re.compile(r"\btailor(ed|ing)?\b", re.I)),
]
#: At most one word between, and spaces only: punctuation ends the reach, so
#: "not tailored, the one I uploaded" negates tailored and nothing after it.
#: "no" is left out on purpose: "no, updated" is an answer, not a negation.
_NEGATED = re.compile(r"\b(not|don'?t|never|without|instead of)\s+(\w+\s+)?$", re.I)
_SAME = re.compile(r"\b(same( one)?|as before|last time|the usual|default)\b", re.I)
_YES = re.compile(r"^\W*(yes|yep|yeah|ya|ok(ay)?|sure|go( ahead)?|do it|fine|"
                  r"sounds good|that one|perfect)\b", re.I)


def _asks_which_resume(text: str) -> bool:
    """Whether a reply put the question to them — two of the three names in
    one message. Read off the reply itself because the model often asks in
    prose without touching a tool, and an answer is only an answer next to
    the question it answers."""
    low = text.lower()
    names = (("file", "initial"), ("updated", "master"), ("tailored",))
    return sum(any(w in low for w in ws) for ws in names) >= 2


def said_resume(state: dict, default: str | None) -> set[str]:
    """Which resume the person chose **in this message**, as stored values.

    The per-apply question exists so the person picks, and "the person picked"
    is the one thing the model cannot be trusted to report: told "just apply to
    everything, don't ask me anything", it once saved `tailored` itself and
    sent five applications in the same breath. So the choice is read from what
    they typed, by code, and `start_application` sends only what this returns.

    Three ways a message counts as an answer:
    - it names one — "apply to 2 with my updated resume", or "tailored" in reply
    - it says "same" / "as before" and there is a last answer to be the same as
    - it is a bare yes, straight after we asked with a default on offer
    A bare "yes" with no question behind it is not an answer to anything.
    """
    msg = state.get("user_message") or ""
    named = set()
    for mode, rx in _SAID:
        for m in rx.finditer(msg):
            # "not tailored, the one I sent" names both; only one was chosen.
            if not _NEGATED.search(msg[:m.start()]):
                named.add(mode)
    if named:
        return named
    if not default:
        return set()
    asked = state.get("resume_asked_turn") == state.get("turn", 0) - 1
    if _SAME.search(msg) and (asked or re.search(r"\b(resume|cv)\b", msg, re.I)):
        return {default}
    if asked and _YES.search(msg):
        return {default}
    return set()


def _has_updates(conn: sqlite3.Connection) -> bool:
    """Whether the record adds anything to their file. Best effort: a record
    that cannot be read offers all three rather than hiding one."""
    try:
        from resume import generate
        return generate.additions(conn=conn) > 0
    except Exception:                                  # noqa: BLE001
        return True


def _saved_layout(conn: sqlite3.Connection) -> str:
    from resume import layouts
    return layouts.get(vault.get("resume_layout", conn))["key"]


def _role_years(conn: sqlite3.Connection) -> tuple[str, int | None]:
    """Their latest title and years, for suggesting a layout. Years come from
    the same place search ordering reads them, so the two never disagree."""
    from search.run import experience_years
    try:
        master = json.loads(paths.MASTER.read_text())
        role = (master.get("experience") or [{}])[0].get("title") or ""
    except (OSError, ValueError, IndexError):
        role = ""
    return role, experience_years(conn)


def _pick_layout(args: dict, conn: sqlite3.Connection) -> tuple[str | None, str]:
    """(layout key, a line for the reply). None when what they asked for
    matches nothing, with the list to offer instead of a guessed look.

    A layout they name becomes their default, so the resume that goes out on
    "apply" looks like the one they chose. If it works against them it is
    built anyway, with the reason said once, which is the rule everywhere
    else too: warn once, then do what they chose.
    """
    from resume import layouts
    said = (args.get("layout") or "").strip()
    if not said:
        return _saved_layout(conn), ""
    key = layouts.resolve(said)
    if key is None:
        return None, (f"ERROR: no layout matches {said!r}. Offer these, or "
                      f"call resume_layouts to show them:\n{layouts.catalogue()}")
    if key != vault.get("resume_layout", conn):
        vault.put("resume_layout", key, source="user", conn=conn)
    warning = layouts.fit_warning(key, _role_years(conn)[1])
    lead = f"Layout: {layouts.get(key)['label']} (now their default).\n"
    return key, lead + (f"Tell them once: {warning}\n" if warning else "") + "\n"


def _first_name(conn: sqlite3.Connection) -> str:
    """Theirs if they set it: a name written surname first would otherwise
    put the surname at the front of every file an employer receives."""
    set_ = vault.get("first_name", conn)
    if set_:
        return set_.strip().split()[0].capitalize()
    from resume import render
    try:
        return render.first_name(json.loads(paths.MASTER.read_text()))
    except (OSError, ValueError):
        return "Resume"


def resume_question(default: str | None, updates: bool = True) -> str:
    """Two choices when nothing has been added since the upload: "updated"
    would be their file again, and a choice between two identical documents
    is not a choice."""
    if updates:
        q = ("Which resume for this one: your file (the one you uploaded, "
             "as-is), updated (your resume plus what you've told me since, "
             "fitted to the page) or tailored (rebuilt for this job)?")
    else:
        q = ("Which resume for this one: your file (the one you uploaded, "
             "as-is) or tailored (rebuilt for this job)?")
    return q + (f" (Last time: {SHOWN_AS[default]}.)" if default else "")


# --- tools -----------------------------------------------------------------

def wants(state: dict, conn: sqlite3.Connection | None = None) -> str:
    """What they are looking for: the long-run profile, plus this session.

    Two sources because they answer different questions. The profile is what
    they have wanted over time; `state["wants"]` is what they said in the
    search that surfaced this job, which is newer and usually sharper.
    """
    bits = []
    profile = reconcile.read_profile(conn)
    if profile:
        bits.append(profile[-800:].strip())
    if state.get("wants"):
        bits.append(state["wants"])
    return "\n".join(b for b in bits if b)


def _by_ref(state: dict, ref: str) -> dict:
    """Find the job a tool was asked for, by name.

    This replaced `picks[n - 1]`. Positions cannot fail: every n from 1 to 5
    returned a job, so the agent renumbering its reply — which it does — meant
    "apply to 1" quietly started a different job. In one recorded run the two
    were a different company.

    Looking up by name can fail, and failing is the feature. The error lists
    what is available rather than guessing, because a guess here fills in a
    real application form.
    """
    known = state.setdefault("by_ref", {})
    ref = (ref or "").strip().strip("[]")
    if ref in known:
        return known[ref]
    # A near miss is usually the model shortening its own slug. Accept it only
    # when exactly one thing matches — two candidates means we do not know.
    hits = [k for k in known if k.startswith(ref) or ref.startswith(k)]
    if len(hits) == 1:
        return known[hits[0]]
    raise KeyError(
        f"no job called {ref!r}. Use one of these exactly: "
        + (", ".join(known) or "(nothing searched yet)")
    )


#: What the model is told an application run ended as. Every line says the
#: outcome in words that cannot be read as anything else.
#:
#: The version this replaces was `f"[{id}] {status}: {message}"`, and `status`
#: was `done` — which the apply agent used to mean "the loop stopped". The
#: model read it as the outcome and told someone five applications were
#: submitted while ALLOW_SUBMIT was off and the apply agent's own message,
#: right there in the same string, said "I'm unable to submit".
_OUTCOME = {
    "submitted": "SUBMITTED — the form was sent and confirmed.",
    "blocked":   "NOT SUBMITTED — the run finished without sending anything.",
    "waiting":   "NOT SUBMITTED — parked, waiting on an answer.",
    "failed":    "NOT SUBMITTED — the run failed.",
    "running":   "NOT SUBMITTED — still running.",
    "queued":    "NOT SUBMITTED — waiting for a free slot to start.",
}


def _application_result(app_id: int, job: dict, r: dict) -> str:
    """One application's outcome, naming the job it belongs to.

    The job is named because the number the person typed and the job the tool
    used have drifted apart before. Saying which one this is makes a wrong
    match visible in the answer instead of only in the database.
    """
    status = r.get("status", "?")
    where = job.get("city") or job.get("location") or ""
    head = (f"Applying to: {job['title']} — {job['company']}"
            + (f", {where}" if where else ""))
    outcome = _OUTCOME.get(status, f"status {status!r}")
    detail = r.get("question") or r.get("message") or r.get("detail", "")
    return f"[{app_id}] {head}\n{outcome}\n{detail}"


def _in_background(state: dict) -> bool:
    """Real applications run beside the chat. A fixture run stays inline: the
    scenario harness replays conversations and needs each turn to hold its own
    result. `JOB_AGENT_BACKGROUND=0` forces inline for anything else."""
    return not state.get("fixture") and os.environ.get("JOB_AGENT_BACKGROUND", "1") != "0"


def _started(app_id: int, job: dict, tailoring: bool, resume: str = "") -> str:
    """What the agent hears when a run was handed off. Worded so it cannot be
    read as done: "All five are submitted" was once said about five runs that
    had submitted nothing."""
    first = ("building a tailored resume first (about two minutes), then "
             "filling the form" if tailoring else "filling the form")
    if resume:
        first += f", with their {resume} resume"
    where = ("in a cloud browser" if os.environ.get("JOB_AGENT_BROWSER") == "cloud"
             else "in a new tab in their own Chrome")
    return (f"[{app_id}] STARTED, NOT SUBMITTED: {job['title']} — {job['company']}. "
            f"Running in the background: {first} {where}. "
            f"Tell them it has started, that they can keep chatting or "
            f"ask for anything else meanwhile, and that you will tell them how "
            f"it went. Do not say it is applied or done.")


def chat_model() -> str:
    """The model that talks. `CHAT_MODEL` in .env swaps only this loop (a
    faster, cheaper model for conversation), leaving search, ranking,
    tailoring and applying on the main one."""
    return os.environ.get("CHAT_MODEL") or llm.MODEL()


def existing_resume(which: str, conn) -> Path | None:
    """A resume already on disk, or None. `file` is their upload (the vault's
    `master_resume_path`); `latest` is the newest PDF built here, preferring
    the upload-named copy the employer would get."""
    if which == "file":
        p = vault.get("master_resume_path", conn)
        return Path(p) if p and Path(p).exists() else None
    built = sorted(paths.RESUMES.glob("*.pdf"), key=lambda f: f.stat().st_mtime)
    if built:
        return built[-1]
    p = vault.get("master_resume_path", conn)
    return Path(p) if p and Path(p).exists() else None


_CLAIMS_FILE = re.compile(r"\b(attached|built it|built your|here it is|here's your|"
                          r"here is your|sent (it|you)|i've sent|i have sent)\b", re.I)


def no_false_attach(text: str, state: dict) -> str:
    """A reply may say a file is attached only when one is queued.

    A tailor failed the ATS gate twice and both replies said "Built it"
    (2026-09-24); the person waited for a PDF that did not exist. The
    prompt already asks for honesty; this holds where it does not.
    """
    if state.get("channel") != "telegram" or state.get("outbox"):
        return text
    if not state.get("build_failed") or not _CLAIMS_FILE.search(text):
        return text
    return ("No file this time: the resume did not build "
            f"({state['build_failed']}).\n\n{text}")


def attach(state: dict, path) -> str:
    """A file for them to see. On Telegram it is queued and sent as a file
    after the reply; asked "can I see the resume?", the bot used to answer
    with a path on this Mac, which nobody on a phone can open."""
    p = Path(str(path))
    if state.get("channel") != "telegram":
        return f"PDF: {p}" if p.suffix == ".pdf" else str(p)
    if p.suffix == ".pdf":
        try:
            from resume import render
            p = render.upload_path(p)        # Firstname_Lastname_Resume.pdf
        except FileNotFoundError:
            pass
    state.setdefault("outbox", []).append(str(p))
    return ("(Sent to them in the chat as a file, right after your reply. Say "
            "it is attached; never write a file path.)")


_URL = re.compile(r"https?://[^\s)\]>]+")
_ITEM = re.compile(r"(?m)^\s*(?:\*\*)?\d{1,2}[.)]")


def fix_links(text: str, jobs: list[dict]) -> str:
    """Every link in a reply is checked against the jobs the tools returned.

    The model writes the links itself, and it put TrueFoundry's link under
    Pocket FM (2026-09-28 evals). A prompt rule cannot stop a copy mistake, so
    code does: inside each numbered item, a link is replaced with the link of
    the job that item names; a link no tool returned is removed.
    """
    if not jobs:
        return text
    link = lambda j: j.get("apply_url") or j.get("url") or ""   # noqa: E731
    known = {link(j) for j in jobs if link(j)}
    starts = [m.start() for m in _ITEM.finditer(text)] + [len(text)]
    out, last = [], 0
    for a, b in zip(starts, starts[1:]):
        out.append(text[last:a])
        item = text[a:b]
        low = item.lower()
        named = [j for j in jobs if j.get("company")
                 and j["company"].lower() in low]
        if len(named) > 1:
            named = [j for j in named if j.get("title")
                     and j["title"].lower()[:25] in low] or named[:1]
        if named and link(named[0]):
            item = _URL.sub(link(named[0]), item)
        out.append(item)
        last = b
    out.append(text[last:])
    fixed = "".join(out)
    return _URL.sub(lambda m: m.group(0) if m.group(0) in known
                    or not _looks_like_job(m.group(0)) else "", fixed)


def _looks_like_job(url: str) -> bool:
    """Job-board links are the ones a model can mix up; anything else (a help
    page it was told about) is left alone."""
    return bool(re.search(r"linkedin\.com/jobs|adzuna\.|greenhouse\.io|lever\.co|"
                          r"ashbyhq\.com|smartrecruiters\.com|workable\.com", url))


_LOCAL_PATH = re.compile(r"!?\[[^\]]*\]\((/[^)]+)\)|(?<![\w/])(/(?:Users|home|private|tmp|var)/\S+)")


def for_phone(text: str) -> str:
    """No file paths on Telegram (the file itself is sent), and no em dashes:
    Sai's rule for anything a person reads."""
    text = _LOCAL_PATH.sub("", text)
    text = text.replace(" — ", ", ").replace("—", "-")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def run_tool(name: str, args: dict, state: dict,
             conn: sqlite3.Connection) -> str:
    picks = state["picks"]

    if name == "search_jobs":
        # Three tries, then hand it back to the person.
        #
        # Left alone the model rephrases its way through the whole budget: one
        # message produced six searches, each an ~18k-token ranking call, and
        # the turn ended on "I got stuck in a loop there" — no answer at all.
        # The first search had been right. There were 16 product roles in the
        # table, none remote, none fintech, so "nothing" was the true answer
        # and it was available immediately.
        #
        # A model that distrusts an empty result will always search again, so
        # the stop has to be a returned error rather than a line in the prompt.
        tried: list[str] = state.setdefault("searched", [])
        # An empty result ends the searching for this message. Asked for
        # "growth roles posted today", the agent found nothing and searched
        # again with Bengaluru added, a city they never named. Sai's call
        # (2026-09-28): ask them instead. The question is theirs to answer.
        if state.get("found_nothing"):
            return ("ERROR: not searched. The last search found nothing, so ask "
                    "them one short question before searching again: offer "
                    "the one or two changes most likely to help (a wider date "
                    "range, a city, a broader title). Do not pick one for "
                    "them.\nTried: " + " | ".join(tried))
        if len(tried) >= 3:
            return ("ERROR: three searches already this message. Stop and tell "
                    "them what you tried and what came back — including the "
                    "counts — then let them narrow it themselves. Do not "
                    "search again.\nTried: " + " | ".join(tried))
        tried.append(args["query"])

        from search.run import experience_years, search
        result = search(args["query"], profile=reconcile.read_profile(conn)[-1500:],
                        conn=conn,
                        # Orders jobs by fit to their experience; never filters.
                        fit_years=experience_years(conn),
                        # One LinkedIn search while they wait, only when the
                        # table comes up short. See search/run.py.
                        live=True,
                        # Their sentence, not the agent's paraphrase of it. The
                        # count and "did they actually ask for Bangalore" are
                        # both read from this, and neither survives the rewrite.
                        said_verbatim=state.get("user_message"),
                        said_recently=" ".join(state.get("said") or []))
        # What they are after, kept for the resume builder. The ranker got
        # this from the first day and `tailor` never did, so the search
        # knew they wanted to own a number and the document written off
        # the back of it did not.
        soft = (result.get("filters") or {}).get("soft_criteria")
        if soft:
            state["wants"] = soft
        state["picks"] = result["picks"]
        # Accumulates, never replaces. Two searches in one message used to
        # leave `picks` holding only the second, while the reply listed both —
        # so a job from the first search resolved to a different company.
        from search.rank import ref as _ref
        state.setdefault("by_ref", {}).update(
            {_ref(p): p for p in result["picks"]})
        # Stashed for the log and the live panel. The model never sees this —
        # it would only spend tokens narrating a funnel the person can read.
        state["last_funnel"] = {
            **result["funnel"],
            "query": args["query"],      # the sentence, so a turn can be replayed
            "picked": [{"source": p["source"], "source_id": str(p["source_id"]),
                        "title": p["title"], "company": p["company"],
                        "city": p.get("city")}
                       for p in result["picks"]],
            "dropped": [{"title": d["title"], "company": d["company"],
                         "why": d.get("why", "")}
                        for d in result.get("dropped", [])],
        }
        save_last(result["picks"])
        if not result["picks"]:
            state["found_nothing"] = True
            return ((result["text_agent"] or "Nothing matched.")
                    + "\n\nDo not search again this message. Tell them it came "
                      "back empty and ask one short question: which change "
                      "would they like (a wider date range, a city, a broader "
                      "title)? Never add a city, date or level they did not say.")
        return result["text_agent"] or "Nothing matched."

    if name == "get_job":
        job = _by_ref(state, args["ref"])
        # Adzuna's terms ask for the credit, and its description is only a
        # snippet: said here so the agent does not present it as the whole ad.
        via = ("\nJobs by Adzuna. The description below is a short snippet, "
               "not the full ad." if job.get("source") == "adzuna" else "")
        return (f"{job['title']} at {job['company']}\n"
                f"{job.get('city') or job.get('location')} · "
                f"{(job.get('posted_at') or '')[:10]}{via}\n"
                f"{job.get('apply_url') or job.get('url')}\n\n"
                f"{(job.get('description') or '')[:3000]}")

    if name == "resume_layouts":
        from resume import generate, layouts
        role, years = _role_years(conn)
        picks = layouts.suggest(role, years)
        out = (f"Layouts (theirs now: {layouts.get(_saved_layout(conn))['label']}):\n"
               f"{layouts.catalogue()}\n\nFor {role or 'them'}"
               f"{f' at {years} years' if years is not None else ''}, suggest: "
               + " and ".join(layouts.get(k)["label"] for k in picks))
        wanted = [layouts.resolve(w) for w in args.get("preview") or []]
        unknown = [w for w, k in zip(args.get("preview") or [], wanted) if not k]
        shown_ = generate.preview_layouts([k for k in wanted if k], conn=conn) \
            if any(wanted) else []
        for p in shown_:
            out += (f"\nPreview {p['label']}: {attach(state, p['png'])}"
                    + (f" (runs to {p['pages']} pages as is)" if p["pages"] > 1 else ""))
        if unknown:
            out += f"\nNo layout matches: {', '.join(unknown)}. Offer the list."
        return out

    if name == "tailor_resume":
        from resume import tailor as t
        job = _by_ref(state, args["ref"])
        layout, heads_up = _pick_layout(args, conn)
        if layout is None:
            return heads_up
        built = t.build(job, instruction=args.get("instruction", ""),
                        conn=conn, wants=wants(state, conn), layout=layout)
        state["resumes"][args["ref"]] = str(built["pdf"])
        # When they last saw it. A tailored resume they read and then said
        # "apply" to is a choice already made — but only once a turn has gone
        # by, or the model could build one and send it in the same breath.
        state.setdefault("tailored_turn", {})[args["ref"]] = state.get("turn")
        return f"{heads_up}{t.preview(built)}\n\n{attach(state, built['pdf'])}"

    if name == "build_resume":
        from resume import generate, tailor as t
        layout, heads_up = _pick_layout(args, conn)
        if layout is None:
            return heads_up
        built = generate.build(conn=conn, layout=layout)
        state["resumes"]["__own__"] = str(built["pdf"])
        return f"{heads_up}{t.preview(built)}\n\n{attach(state, built['pdf'])}"

    if name == "send_resume":
        # "Can I see the resume here?" ran no tool at all (2026-09-24, message
        # 10 in Sai's profile), so nothing was queued and the reply pasted a
        # Mac path. A file they already have needs no build, only a send.
        path = existing_resume(args.get("which", "latest"), conn)
        if path is None:
            return ("ERROR: no resume on file yet. Ask them to upload one as "
                    "a PDF, or offer to build one.")
        return f"{path.name}\n\n{attach(state, path)}"

    if name == "remember_experience":
        import career
        added = career.add(args["kind"], args["text"],
                           company=args.get("company"), source="user",
                           heard_in=state.get("user_message"), conn=conn)
        return ("on the record — it can go on a resume now"
                if added else "already on the record")

    if name == "set_resume_preference":
        # Only the default now. It used to be the decision itself, asked once
        # and kept forever, which needed its own same-turn gate so the model
        # could not save it and send on it in one breath. The decision moved to
        # every apply and is read from what they typed (see `said_resume`), so
        # saving a default can no longer send anything and needs no gate.
        choice = RESUME_MODES[args["choice"]]
        if vault.get("resume_mode", conn) == choice:
            return (f"already saved: {args['choice']} — nothing changed.")
        vault.put("resume_mode", choice, source="user", conn=conn)
        return (f"saved: {args['choice']} is now the default you offer. It does "
                f"not answer the question for any application — still ask "
                f"when they apply.")

    if name == "start_application":
        job = _by_ref(state, args["ref"])

        # Which resume goes out is a decision, not a fallback. It used to be
        # `args or last tailored or master`, so whatever happened to be lying
        # around got sent: asked to "apply to all", the model tailored five
        # resumes nobody had seen and posted them. Nothing chose that.
        #
        # It is now asked on every apply, and the answer is read from the
        # person's own message — never from the model's word for it. Every
        # looser rule let the model answer for them. Inferring "we asked" from
        # this tool having errored failed, because the model asks in prose
        # without calling anything. Allowing it after the opening message
        # failed too: told "just apply to everything, don't ask me anything",
        # the model saved `tailored` and fired five applications in one turn.
        # What the person typed is the one input the model cannot write.
        ref_key = args["ref"]
        turn = state.get("turn")
        default = RESUME_MODES.get(vault.get("resume_mode", conn) or "")
        uploaded = vault.get("master_resume_path", conn)
        note = ""
        if not uploaded and not paths.MASTER.exists():
            # Nothing on file, so there is nothing to choose between. The form
            # asks for a file if it needs one, and the chat already asks them
            # to attach a resume — a three-way question about zero resumes is
            # noise.
            mode = None
        else:
            chosen = said_resume(state, default)
            wanted_mode = RESUME_MODES.get(args.get("resume") or "")
            # A tailored resume for this job that they have already read. "Looks
            # good, apply" after it is an answer — they chose that document by
            # reading it — but only if it was shown on an earlier turn.
            seen = state.get("tailored_turn", {}).get(ref_key)
            if (not chosen and wanted_mode in (None, "tailored")
                    and seen is not None and seen != turn):
                chosen = {"tailored"}
            if wanted_mode is None and len(chosen) == 1:
                wanted_mode = next(iter(chosen))
            if wanted_mode is None or wanted_mode not in chosen:
                # Refusing is what makes the question happen, and the question
                # is written here rather than left to the model, so the batch
                # hears the same one sentence however many jobs refused.
                state["resume_asked_turn"] = turn
                heard = ", ".join(SHOWN_AS[m] for m in sorted(chosen))
                why = (f"they said {heard}, not {SHOWN_AS[wanted_mode]}"
                       if chosen and wanted_mode else
                       "they have not said which resume, in this message")
                return (f"ERROR: not started — {why}. Ask them, once for "
                        f"everything they asked to apply to, and apply when "
                        f"they answer:\n\"{resume_question(default, _has_updates(conn))}\"")
            mode = wanted_mode
            if mode in ("updated", "tailored") and not paths.MASTER.exists():
                return ("ERROR: not started — their resume has not been read "
                        "yet, so only `file` (the one they uploaded) can go out.")
            if mode == "updated" and uploaded and not _has_updates(conn):
                # Nothing told since the upload, so "updated" is their file
                # rebuilt in our layout: a different-looking copy of the same
                # words. Their own file is the truer answer to what they asked.
                mode = "existing"
                note = " (nothing new since your upload, so your file as-is)"
            # What they picked is the default next time. Written by the choice
            # itself, so the "(Last time: …)" in the question is always the
            # truth rather than whatever someone last saved.
            if mode != default:
                vault.put("resume_mode", mode, source="user", conn=conn)

        wanted = wants(state, conn)
        fixture = state.get("fixture")
        prebuilt = (state["resumes"].get(ref_key)
                    if mode == "tailored" and ref_key in state.get("tailored_turn", {})
                    else None)
        if prebuilt and not Path(prebuilt).exists():
            prebuilt = None

        def work(c: sqlite3.Connection) -> dict:
            """Everything slow: building the chosen resume, then the form."""
            if mode == "tailored" and prebuilt:
                # The one they read, and maybe edited. Tailoring again would
                # send a different document from the one they approved.
                path = prebuilt
            elif mode == "tailored":
                from resume import tailor as t
                built = t.build(job, conn=c, wants=wanted,
                                layout=_saved_layout(c))
                path = str(built["pdf"])
                state["resumes"][ref_key] = path
            elif mode == "updated":
                from resume import generate
                path = str(generate.build(conn=c, layout=_saved_layout(c))["pdf"])
            else:
                path = uploaded
            from resume import render
            if mode in ("updated", "tailored"):
                path = str(render.upload_path(Path(path)))
            # What the company sees as the file name: Firstname_Company.pdf
            # (Sai's call, 2026-09-24). Every mode, their own upload included:
            # the copy is renamed, its bytes are not touched.
            if path:
                path = str(render.named_for(Path(path), _first_name(c),
                                            job.get("company")))
            # On the row for every mode, not only tailored. A parked run
            # reloads the path from here when it resumes, and an `existing` run
            # used to come back with no resume to upload.
            c.execute("UPDATE applications SET resume_path=? WHERE id=?",
                      (path, app_id))
            c.commit()
            browser = apply_agent.browser_for(job, app_id, fixture=fixture)
            return apply_agent.start(app_id, job, browser, path, c)

        sent = SHOWN_AS.get(mode, "none on file") + note
        app_id = apply_agent.create(job, None, conn)
        if not _in_background(state):
            return (f"Resume: {sent}\n"
                    + _application_result(app_id, job, work(conn)))
        from apply import worker
        worker.start(app_id, job, work)
        return _started(app_id, job, mode == "tailored" and not prebuilt,
                        resume=sent)

    if name == "answer_application":
        app_id = args["app_id"]
        row = conn.execute("SELECT * FROM applications WHERE id=?",
                           (app_id,)).fetchone()
        if row is None:
            return f"ERROR: there is no application {app_id}"
        if row["status"] != "waiting":
            return (f"application {app_id} is {row['status']}, not waiting on "
                    f"anything — nothing to answer")
        job = {"title": row["title"], "company": row["company"],
               "apply_url": row["apply_url"], "source": row["source"],
               "source_id": row["source_id"]}
        fixture, answer = state.get("fixture"), args["answer"]

        def work(c: sqlite3.Connection) -> dict:
            browser = apply_agent.browser_for(job, app_id, fixture=fixture)
            return apply_agent.resume_run(app_id, answer, browser, c)

        if not _in_background(state):
            return _application_result(app_id, job, work(conn))
        from apply import worker
        worker.start(app_id, job, work)
        return (f"[{app_id}] {job['title']} — {job['company']}: got the answer, "
                f"carrying on in the background. NOT SUBMITTED yet. Tell them it "
                f"is continuing and they can keep chatting.")

    if name == "application_status":
        return open_applications(conn)

    return f"ERROR: no tool named {name!r}"


# --- loop ------------------------------------------------------------------

def _log(conn: sqlite3.Connection, *args, **kw) -> None:
    """Write a log row, but never at the cost of the person's answer.

    A failed log write is worth seeing, so it is printed rather than swallowed.
    It is not worth losing a reply over, so it cannot raise.
    """
    try:
        chatlog.record(conn, *args, **kw)
    except Exception as exc:                           # noqa: BLE001
        print(f"[chatlog] could not record: {type(exc).__name__}: {exc}")


def reply(message: str, history: list, state: dict,
          conn: sqlite3.Connection, on_step=None) -> tuple[str, list]:
    """One user message in, one reply out. History is mutated and returned.

    `on_step` is called as the turn happens, so a surface can show the work
    live instead of leaving the person watching a spinner for 15 seconds. It
    takes the wording from `chatlog.PLAIN`, the same source the written log
    uses, so what you watch and what you read back afterwards cannot drift.
    """
    if not llm.supports_tools():
        raise RuntimeError(
            f"provider {llm.provider()!r} has no tool support in this build — "
            f"set LLM_PROVIDER=anthropic or glm"
        )

    # One extraction call per message: career facts and preferences become
    # rows now, guesses at form facts (notice, CTC) are staged for the nightly
    # pass. Staging used to get the whole message, "hi" included. Best effort
    # by design: a capture that fails costs a fact they can say again, while a
    # turn that dies because the extractor hiccuped costs the conversation.
    import career
    career.capture(message, conn=conn)

    # The transcript is separate from staging on purpose: staging is a memory
    # queue that gets consumed, this is a record that stays.
    session = state.setdefault("session", chatlog.new_session())
    turn = state["turn"] = state.get("turn", 0) + 1
    # Per-message counters. Reset here, where a turn begins, so nothing has to
    # remember to clear them: the searches the model is allowed, and the
    # message a resume choice is read from (see `said_resume`).
    state["searched"] = []
    state["found_nothing"] = False
    state["outbox"] = []
    state["build_failed"] = None
    state["user_message"] = message
    # What they said lately, for "did they ask for Bangalore?" — an answer
    # like "yes" only means something next to the question it answered.
    said = state.setdefault("said", [])
    said.append(message)
    del said[:-4]
    # So a bare "yes" carries the question it answered: "Bangalore only?" /
    # "yes" is asking for Bangalore. Only for a short yes, so a city the bot
    # merely mentioned last turn never becomes one they asked for.
    if re.fullmatch(r"\W*(yes|yeah|yep|ya|haan|ha|ok|okay|sure|y|go ahead|do it)"
                    r"\W*", message.strip(), re.I) and state.get("last_reply"):
        said.append(state["last_reply"][-400:])
    started = time.monotonic()
    calls_before = llm.calls()
    steps: list[dict] = []
    _log(conn, session, turn, "user", message)

    def emit(kind: str, **kw) -> None:
        """A display that breaks must not kill a turn already being paid for.

        If the browser closed mid-answer the reply still lands in the log, so
        the work is not lost — it is just read later instead of watched.
        """
        if on_step is None:
            return
        try:
            on_step({"kind": kind, **kw})
        except Exception as exc:                       # noqa: BLE001
            print(f"[chat] display dropped ({type(exc).__name__}); continuing")

    def finish(text: str) -> tuple[str, list]:
        # Background results are written here by code, not left to the model:
        # told in its context to lead with them, it answered the question and
        # never mentioned that the application had finished.
        if news:
            text = f"{news_lines(news)}\n\n{text}"
        text = fix_links(text, list((state.get("by_ref") or {}).values())
                         + list(state.get("picks") or []))
        if state.get("channel") == "telegram":
            text = for_phone(text)
        text = no_false_attach(text, state)
        state["last_reply"] = text
        # Sampled from llm.py rather than counted in this loop, because a tool
        # makes its own calls: `search_jobs` is two more on its own, and
        # counting only the loop reports half the real bill.
        _log(conn, session, turn, "assistant", text, {
            "ms": round((time.monotonic() - started) * 1000),
            "model_calls": llm.calls() - calls_before,
            "steps": steps,
        })
        return text, history

    from apply import worker
    news = worker.unreported(conn)
    from memory import thread
    carried = ""
    if not history:
        # A new session. It starts from where the last one left off, and those
        # notes seed this session's own, so they survive this one being
        # trimmed too.
        carried = thread.carry_over(session, conn)
        if carried and not thread.get(session, conn)[0]:
            thread.save(session, carried, 0, conn)
    lead = (f"## Where you left off last time\n{carried}\n\n" if carried else "")
    history.append({"role": "user",
                    "content": f"{turn_state(conn, state['picks'], news)}\n\n"
                               f"{lead}{message}"})
    history = compact(history,
                      summarize=lambda: thread.refresh(session, turn - 1, conn))

    client = llm.client()
    system = context(conn)

    for _ in range(6):
        emit("thinking")
        llm.counted()          # this loop calls create() directly, not via llm
        resp = client.messages.create(
            model=chat_model(),
            max_tokens=4096,
            system=[{"type": "text", "text": system,
                     "cache_control": {"type": "ephemeral", "ttl": "1h"}}],
            tools=TOOLS,
            messages=history,
            thinking={"type": "adaptive"},
            output_config={"effort": llm.effort_level("medium")},
        )
        usage.record(resp, model=chat_model(), purpose="chat")
        history.append({"role": "assistant", "content": resp.content})

        if resp.stop_reason != "tool_use":
            text = "".join(b.text for b in resp.content if b.type == "text")
            # So next turn's "yes" can be read as picking the default. Taken
            # from the words they were actually shown, because the question is
            # often asked with no tool call behind it.
            if _asks_which_resume(text):
                state["resume_asked_turn"] = turn
            return finish(text.strip())

        results = []
        for block in resp.content:
            if block.type != "tool_use":
                continue
            t0 = time.monotonic()
            files = TOOL_FILES.get(block.name) or []
            emit("tool", name=block.name,
                 text=chatlog.PLAIN.get(block.name, block.name),
                 files=files)
            try:
                # A search's filter and ranking calls are booked to the search.
                with usage.purpose(block.name):
                    out = run_tool(block.name, block.input, state, conn)
                is_error = False
            except Exception as exc:                   # noqa: BLE001
                out, is_error = f"ERROR: {exc}", True
                if block.name in ("tailor_resume", "build_resume"):
                    state["build_failed"] = str(exc)[:120]
            ms = round((time.monotonic() - t0) * 1000)
            funnel = state.pop("last_funnel", None)
            emit("tool_done", ok=not is_error, ms=ms, funnel=funnel,
                 text=chatlog.PLAIN.get(block.name, block.name))
            steps.append(chatlog.step(
                block.name, block.input, out, not is_error, ms, files=files,
                funnel=funnel))
            results.append({"type": "tool_result", "tool_use_id": block.id,
                            "content": out, "is_error": is_error})
        history.append({"role": "user", "content": results})

    return finish("I got stuck in a loop there. Try rephrasing?")


def terminal_printer():
    """Show the work as it happens, on the same screen as the conversation.

    Written to overwrite its own line, so a turn leaves one tidy summary
    behind rather than a wall of progress that buries the answer.
    """
    import sys

    dim, off = ("\033[2m", "\033[0m") if sys.stdout.isatty() else ("", "")

    def show(ev: dict) -> None:
        kind = ev["kind"]
        if kind == "thinking":
            sys.stdout.write(f"\r{dim}  thinking…{off}\033[K")
        elif kind == "tool":
            sys.stdout.write(f"\r{dim}  {ev['text']}…{off}\033[K")
            for f in ev.get("files", []):
                path, why = (list(f) + [""])[:2] if isinstance(f, (list, tuple)) \
                    else (f, "")
                sys.stdout.write(f"\n{dim}    {path}{off}")
                if why:
                    sys.stdout.write(f"\n{dim}      {why}{off}")
        elif kind == "tool_done":
            secs = ev["ms"] / 1000
            mark = "" if ev["ok"] else "  (it failed)"
            sys.stdout.write(f"\n{dim}    {secs:.1f}s{mark}{off}\n")
        sys.stdout.flush()

    return show


def repl(fixture: str | None = None) -> None:
    conn = db.connect()
    from apply import worker
    worker.recover(conn)      # runs a previous process left behind
    session = chatlog.new_session()
    state = {"picks": load_last(), "resumes": {}, "fixture": fixture,
             "session": session}
    history: list = []
    print(f"job agent · {llm.provider()}/{llm.MODEL()} · ctrl-c to quit")
    print(f"profile: {paths.label()}")
    print(f"logging to session {session} · `python cli.py log` to read it back")
    if state["picks"]:
        print(f"(last search still loaded: {len(state['picks'])} jobs)")
    try:
        while True:
            msg = input("\n> ").strip()
            if not msg:
                continue
            if msg in ("exit", "quit"):
                break
            text, history = reply(msg, history, state, conn,
                                  on_step=terminal_printer())
            print(f"\n{text}")
    except (KeyboardInterrupt, EOFError):
        print()
    finally:
        conn.close()
