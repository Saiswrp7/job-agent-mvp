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
from apply import confirm, sites
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
            "than a resume *for* something, and no job is being discussed. "
            "Returns the text; the PDF is sent later with send_resume."),
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
        "name": "edit_resume_structure",
        "description": (
            "Change the shape of their resume: section order, which sections "
            "show, section headings, and a one-line headline under the name. "
            "Saved for every resume after this. Then rebuild so they see it."),
        "input_schema": {
            "type": "object",
            "properties": {
                "order": {"type": "array", "items": {"type": "string"},
                          "description": "sections to move to the top, in "
                                         "this order, e.g. [\"skills\", "
                                         "\"experience\"]. The rest follow."},
                "rename": {"type": "object",
                           "additionalProperties": {"type": "string"},
                           "description": "section -> new heading, e.g. "
                                          "{\"experience\": \"Work Experience\"}"},
                "hide": {"type": "array", "items": {"type": "string"}},
                "show": {"type": "array", "items": {"type": "string"}},
                "headline": {"type": "string",
                             "description": "the line under their name, in "
                                          "their words. \"\" removes it."},
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
        "description": ("Where every application got to, finished ones too, "
                        "and whether each was actually SENT."),
        "input_schema": {"type": "object", "properties": {},
                         "additionalProperties": False},
    },
    {
        "name": "company_info",
        "description": ("What a company is and does, for \"what is Konovo?\" or "
                        "\"tell me about CRED\". Reads the company's own job post "
                        "and its website. Answer only from what it returns."),
        "input_schema": {"type": "object",
                         "properties": {"company": {"type": "string"}},
                         "required": ["company"], "additionalProperties": False},
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
    "edit_resume_structure": [
        ["resume/structure.py", "your section order, headings and headline · no AI"],
    ],
    "company_info": [
        ["company.py", "the company's own job post, then its website via Browserbase · no AI"],
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


def applications(conn: sqlite3.Connection) -> str:
    """Every recent application with whether it was SENT, finished ones too.

    This used to list only open ones. Swiggy finished as blocked (not sent),
    the list came back "(none)", and asked to "submit it" the model said the
    submit "already went in" (Sai, 2026-09-30). The update pushed to Telegram
    never enters the chat history, so this list is the only truth it gets.
    """
    rows = conn.execute(
        "SELECT id, company, title, status, question, updated_at FROM applications "
        "ORDER BY id DESC LIMIT 10"
    ).fetchall()
    if not rows:
        return "(none)"
    out = []
    for r in rows:
        st = r["status"]
        sent = ("SENT, the site confirmed it" if st == "submitted" else
                "in progress, NOT SENT yet" if st in ("running", "queued", "pending") else
                "NOT SENT")
        line = (f"[{r['id']}] {r['title']} — {r['company']}: {st} "
                f"({_NEWS.get(st, st)}) · {sent} · {r['updated_at']} UTC")
        if st == "waiting" and r["question"]:
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
        return ("No resume on file. Do not ask for it until you need it (to "
                "tailor, build or apply). Then ask once, in a few words: \"Attach "
                "your resume 📄\". No steps, no explanations.")
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
           f"## Applications, newest first. Only SENT means sent; never "
           f"say applied or submitted otherwise\n{applications(conn)}")
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
    "unconfirmed": "submit pressed, but the site never confirmed it; check your email before trying again",
    "no_form": "no application form found, nothing filled or sent",
    "sign_in": "the site wants a sign-in first, nothing filled or sent",
}


def news_lines(news: list[dict]) -> str:
    out = []
    for r in news:
        detail = (r.get("outcome") or "").strip()
        if detail.startswith(confirm.READY):
            # The answers they are asked to approve: all of them, or the
            # question cannot be answered.
            out.append(f"Update: {r['title']} at {r['company']}: {detail}")
            continue
        if r["status"] == "waiting" and detail:
            # The questions, all of them. Only the first sentence used to go,
            # and a numbered list ends its first sentence at "1." (Loop,
            # 2026-10-01: "I need 4 missing details…: 1." and nothing else).
            out.append(f"Update: {r['title']} at {r['company']}. Paused, it needs "
                       f"answers from you:\n{detail}")
            continue
        first = re.split(r"(?<=[.!?])\s", detail)[0] if detail else ""
        if r["status"] == "submitted" and detail:
            first = detail.split("\n")[0]    # the site's own words, whole
        if "http" in detail and "http" not in first:
            first = detail          # a link later on is the useful part
        said = _NEWS.get(r["status"], r["status"])
        out.append(f"Update: {r['title']} at {r['company']}. "
                   f"{said[:1].upper() + said[1:]}."
                   + (f" {first}" if first else ""))
    return "\n".join(out)


_UPDATE_VOICE = """You rewrite one job-application update for a Telegram chat, as Jojo:
confident, a bit Gen-Z, 1-2 short lines, plain words, at most one emoji (none
if it was not sent). Keep every fact. Never say applied, sent or submitted
unless the facts say SUBMITTED. No technical words (captcha, spam flag, page,
button, form field). No links. No questions unless the facts ask one.
Return only the message."""


def phrase_update(rows: list[dict], conn: sqlite3.Connection | None = None) -> str:
    """A background update in Jojo's voice instead of a fixed template (Sai,
    2026-10-01: "I don't want any static replies"). The facts are the
    template's; a cheap model only rewords them, the false-"sent" check runs
    on the result, and any failure falls back to the template."""
    out = []
    for r in rows:
        facts = news_lines([r])
        detail = (r.get("outcome") or "").strip()
        if detail.startswith(confirm.READY):
            out.append(facts)              # the answers to approve, word for word
            continue
        questions = ""
        if r.get("status") == "waiting" and detail:
            # The model words the lead line only; the questions go verbatim,
            # so none can be dropped or reworded.
            questions, facts = detail, (f"{r['title']} at {r['company']} is paused "
                                        f"and needs a few answers from you.")
        try:
            text = llm.complete(_UPDATE_VOICE, facts, model=llm.models()[1],
                                max_tokens=300, effort="low", timeout=25).strip()
        except Exception as exc:                          # noqa: BLE001
            print(f"[chat] update kept as written ({type(exc).__name__})")
            text = ""
        if not text or (r.get("status") != "submitted" and pagecheck_claims(text)):
            text = facts
        if conn is not None:
            text = no_false_sent(text, conn)
        if questions:
            text = f"{text}\n{questions}"
        out.append(for_phone(text))
    return "\n".join(out)


def pagecheck_claims(text: str) -> bool:
    from apply import pagecheck                           # noqa: PLC0415
    return pagecheck.claims_sent(text)


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
SHOWN_AS = {"existing": "yours", "updated": "updated", "tailored": "tailored"}

#: How each one sounds in a message. Deliberately narrow: a word that only
#: *might* mean a resume ("full", "my resume") is not an answer, it is a reason
#: to ask. Asking once more costs a message; guessing sends a document.
_SAID = [
    ("existing", re.compile(r"\b(initial|original|existing|as[- ]is|uploaded|mine|"
                            r"(my|your|the|own) (own )?file|"
                            r"(current|base|old|my own) (one|resume|cv)|"
                            r"the one i (sent|uploaded|attached))\b", re.I)),
    ("updated", re.compile(r"\b(updated|general|master)\b", re.I)),
    ("tailored", re.compile(r"\btailor(ed|ing)?\b", re.I)),
]
#: At most one word between, and spaces only: punctuation ends the reach, so
#: "not tailored, the one I uploaded" negates tailored and nothing after it.
#: "no" is left out on purpose: "no, updated" is an answer, not a negation.
_NEGATED = re.compile(r"\b(not|don'?t|never|without|instead of)\s+(\w+\s+)?$", re.I)
_SAME = re.compile(r"\b(same( one)?|as before|last time|the usual|default)\b", re.I)
#: "Just apply, stop asking me": their own words handing the choice to the
#: default. Read from what they typed, like every other answer here (Sai's
#: Telegram eval, 2026-10-01: objections get handled, not re-asked).
_DONT_ASK = re.compile(r"\b(stop asking|don'?t ask|no need to ask|just apply|"
                       r"your call|you (decide|choose|pick))\b", re.I)
_YES = re.compile(r"^\W*(yes|yep|yeah|ya|ok(ay)?|sure|go( ahead)?|do it|fine|"
                  r"sounds good|that one|perfect)\b", re.I)


def _asks_which_resume(text: str) -> bool:
    """Whether a reply put the question to them — two of the three names in
    one message. Read off the reply itself because the model often asks in
    prose without touching a tool, and an answer is only an answer next to
    the question it answers."""
    low = text.lower()
    names = (("file", "initial", "yours"), ("updated", "master"), ("tailored",))
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
    # One job only. "Just apply to everything, don't ask me anything" once
    # sent five tailored resumes nobody had seen; a batch still gets the
    # one-word question.
    if _DONT_ASK.search(msg) and not re.search(r"\b(all|everything|every|each)\b", msg, re.I):
        return {default or "tailored"}
    if not default:
        return set()
    asked = state.get("resume_asked_turn") == state.get("turn", 0) - 1
    if _SAME.search(msg) and (asked or re.search(r"\b(resume|cv)\b", msg, re.I)):
        return {default}
    if asked and _YES.search(msg):
        return {default}
    return set()


def _job_resume(conn: sqlite3.Connection, ref_key: str, mode: str | None = None) -> str | None:
    """The resume they chose for one job, kept on their record so a restart
    does not forget it. Read when `mode` is None, written otherwise."""
    try:
        chosen = json.loads(vault.get("resume_by_job", conn) or "{}")
    except (TypeError, ValueError):
        chosen = {}
    if mode is None:
        return chosen.get(ref_key)
    chosen[ref_key] = mode
    vault.put("resume_by_job", json.dumps(chosen), source="user", conn=conn)
    return mode


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
    # Tailored first: a resume built for each job is the point of the product
    # (Sai's eval sheet row 31: "we build each resume for each job to
    # increase the chance"). Still asked; it is their name on the document.
    # Short (Sai's Telegram eval, 2026-10-01: "make it short, which resume?").
    q = ("Which resume: tailored, yours, or updated?" if updates
         else "Which resume: tailored or yours?")
    return q + (f" (last time: {SHOWN_AS[default]})" if default else "")


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


def _key_of(job: dict, ref: str) -> str:
    """One key per job, whatever spelling of its ref the model used. The
    tailored resume Sai approved for Loop was filed under one spelling and
    looked for under another, so applying asked "tailored or yours?" again and
    built it a second time (2026-10-01)."""
    if job and job.get("source") and job.get("source_id") is not None:
        return f"{job['source']}:{job['source_id']}"
    return (ref or "").strip().strip("[]")


def _gone(conn: sqlite3.Connection, job: dict, state: dict) -> str | None:
    """None if the job is still open, else what to tell them instead.

    A result lives in the conversation (`by_ref`) from the search that found
    it, and the engine may have closed it since: the employer took it off
    their careers site, or it passed 30 days. So a tool about to act on a job
    reads the table again first, and nobody gets a resume tailored to, or an
    application started for, a job that no longer exists. The fresh row's
    apply link and apply kind replace the ones from the search.

    A fixture run rehearses against a saved form, where the job's being live
    is not the question, so it is left alone.
    """
    if state.get("fixture") or not job.get("source") or job.get("source_id") is None:
        return None
    row = conn.execute(
        "SELECT closed_at, apply_kind, apply_url, url FROM jobs "
        "WHERE source = ? AND source_id = ?",
        (job["source"], str(job["source_id"]))).fetchone()
    what = f"{job.get('title')} at {job.get('company')}"
    if row is None:
        return (f"GONE: {what} is no longer in the job list: it closed or is "
                f"more than 30 days old. Tell them it is no longer open. Do "
                f"not tailor for it or apply to it.")
    if row["closed_at"]:
        return (f"GONE: {job.get('company')} took {job.get('title')} off their "
                f"careers site ({str(row['closed_at'])[:10]}). Tell them it is "
                f"no longer open. Do not tailor for it or apply to it.")
    job["apply_kind"] = row["apply_kind"]
    job["apply_url"] = row["apply_url"] or row["url"] or job.get("apply_url")
    return None


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
    "unconfirmed": ("MAYBE SENT — submit was pressed but the site never confirmed "
                    "it. Tell them to check their email for a confirmation, and "
                    "not to apply again until they have."),
    "no_form":   "NOT SUBMITTED — no application form on the page; nothing was filled.",
    "sign_in":   "NOT SUBMITTED — the site wants a sign-in first; nothing was filled.",
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


#: "show me those again", "the old ones", "ones I've seen": the seen list is
#: theirs to override.
_SEE_AGAIN = re.compile(r"\b(again|old(er)? ones?|already (seen|shown)|seen (ones|before)|"
                        r"previous( ones)?|same ones|repeat)\b", re.I)


def _seen_jobs(conn: sqlite3.Connection) -> list[str]:
    """Jobs this person was shown or applied to, as "source:source_id"."""
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS main.shown_jobs ("
                     "job_key TEXT PRIMARY KEY, shown_at TEXT DEFAULT CURRENT_TIMESTAMP)")
        keys = [r[0] for r in conn.execute(
            "SELECT job_key FROM main.shown_jobs ORDER BY shown_at DESC LIMIT 600")]
        keys += [f"{r[0]}:{r[1]}" for r in conn.execute(
            "SELECT source, source_id FROM main.applications WHERE source IS NOT NULL")]
        return list(dict.fromkeys(keys))
    except sqlite3.Error as exc:
        print(f"[chat] seen jobs unreadable: {exc}")
        return []


def _mark_seen(conn: sqlite3.Connection, picks: list[dict]) -> None:
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS main.shown_jobs ("
                     "job_key TEXT PRIMARY KEY, shown_at TEXT DEFAULT CURRENT_TIMESTAMP)")
        conn.executemany(
            "INSERT INTO main.shown_jobs (job_key) VALUES (?) ON CONFLICT(job_key) "
            "DO UPDATE SET shown_at = CURRENT_TIMESTAMP",
            [(f"{p.get('source')}:{p.get('source_id')}",) for p in picks if p.get("source")])
        conn.commit()
    except sqlite3.Error as exc:
        print(f"[chat] could not mark jobs seen: {exc}")


def _batch_message(msg: str) -> bool:
    """Several jobs asked for in one message: "apply to all", "both", "5 and
    1", "2, 3, 4". One job is "apply to 2" or "apply for me that"."""
    if re.search(r"\b(all|both|every|everything|each|these|those)\b", msg, re.I):
        return True
    return len(set(re.findall(r"\b\d{1,2}\b", msg))) >= 2


def _is_batch(state: dict, ref: str) -> bool:
    """Part of a several-job request: said so in this message, or the resume
    question this message answers was asked for two or more jobs at once."""
    if _batch_message(state.get("user_message") or ""):
        return True
    asked = state.get("asked_refs") or {}
    last = asked.get(state.get("resume_asked_turn")) or set()
    return len(last) >= 2 and ref in last


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
    # No sentence to copy: a quoted example ("Applying to X 🚀") came back
    # word for word every time and Sai called the bot static (2026-10-01).
    return (f"[{app_id}] STARTED, NOT SUBMITTED: {job['title']} — {job['company']}. "
            f"Running in the background: {first} {where}. "
            f"Tell them in your own words, briefly, that you are on it (name the "
            f"company; say which resume if it was not asked this message). It "
            f"asks them if the form needs anything and shows them the whole form "
            f"before anything is sent. It is FILLING: never say submitting, "
            f"applied or done.")


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


_SENT_CLAIM = re.compile(r"\b(submitted|sent|applied|went (through|in)|gone (through|in)|"
                         r"got in|been (sent|submitted))\b", re.I)
_SENT_NEG = re.compile(r"\bnot\b|n't\b|\bnever\b|\bnothing (was|got|has been|went)\b|"
                       r"\b(once|when|if|until|after|will|going to|unconfirmed)\b|'ll\b", re.I)
_FILE_TALK = re.compile(r"\b(resume|cv|pdf|file|attached|photo|picture)\b", re.I)
_APP_TALK = re.compile(r"\b(application|applied|apply|submit\w*|form)\b", re.I)


def no_false_sent(text: str, conn: sqlite3.Connection) -> str:
    """A reply may say an application went through only when its row says
    submitted (the site confirmed it).

    Swiggy ended blocked, nothing sent, and asked to "submit it" the bot
    twice said it "already went through" (Sai, 2026-09-30). Sentences that
    claim that about an unsent application are dropped and the truth goes
    first. A claim naming no company is about the newest application.
    """
    rows = [dict(r) for r in conn.execute(
        "SELECT company, title, status FROM applications ORDER BY id DESC LIMIT 10")]
    wrong: dict[str, dict] = {}
    kept = []
    for line in text.split("\n"):
        out = []
        for sent in re.split(r"(?<=[.!?])\s+", line):
            claim = (sent and _SENT_CLAIM.search(sent) and not _SENT_NEG.search(sent)
                     and not _FILE_TALK.search(sent) and not sent.rstrip().endswith("?"))
            if claim:
                low = sent.lower()
                named = [r for r in rows if r["company"] and r["company"].lower() in low]
                if named:
                    bad = [] if any(r["status"] == "submitted" for r in named) else named[:1]
                elif _APP_TALK.search(sent):
                    bad = [rows[0]] if rows and rows[0]["status"] != "submitted" else \
                          ([] if rows else [{"company": "", "title": "", "status": "none"}])
                else:
                    bad = []
                if bad:
                    for r in bad:
                        wrong[f"{r['title']}|{r['company']}"] = r
                    continue
            out.append(sent)
        kept.append(" ".join(out))
    if not wrong:
        return text
    truth = []
    for r in wrong.values():
        if r["status"] == "none":
            truth.append("Nothing has been sent: no application has gone out yet.")
        else:
            truth.append(f"{r['title']} at {r['company']} was NOT sent "
                         f"({_NEWS.get(r['status'], r['status'])}).")
    body = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()
    return " ".join(truth) + (f"\n\n{body}" if body else "")


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
            "so in a few words, like \"Here you go, sent 📄\". Nothing else, and "
            "never a file path.)")


#: A build shows text first; the PDF goes when they are happy with it (Sai's
#: eval sheet row 30: "give resume text, and send the final PDF once
#: everything is done"). A PDF per edit was five files for one resume.
NOT_SENT_YET = ("(PDF built, NOT sent yet. Do not paste the resume. Say it is "
                "rebuilt in one line and ask: see it here, or want the PDF? You can "
                "also offer to apply. If they say here, paste the summary and the "
                "changed bullets; for the PDF call send_resume with `latest`.)")
#: Row 32: "if you don't have to change anything, don't build a new resume".
UNCHANGED = ("NOT rebuilt: nothing on their record, layout or structure "
             "changed since the last build, so it is the same resume. Say so "
             "in one line; offer send_resume `latest` if they want the PDF.")


def _record_print(conn: sqlite3.Connection, layout: str) -> str:
    """What a build is made of: the record, the layout, their structure. Two
    builds with the same print produce the same document."""
    import hashlib
    from resume import generate, structure
    try:
        master, _ = generate.from_career(conn=conn)
    except FileNotFoundError:
        master = {}
    blob = json.dumps([master, layout, structure.get(conn)], sort_keys=True,
                      default=str)
    return hashlib.sha1(blob.encode()).hexdigest()


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
    hosts = frozenset(_host(u) for u in known)
    return _URL.sub(lambda m: m.group(0) if m.group(0) in known
                    or not _looks_like_job(m.group(0), hosts) else "", fixed)


_JOB_HOSTS = re.compile(
    r"linkedin\.com/jobs|adzuna\.|greenhouse\.io|lever\.co|ashbyhq\.com|"
    r"smartrecruiters\.com|workable\.com|myworkdayjobs\.com|oraclecloud\.com|"
    r"keka\.com|successfactors\.|jobs2web\.com|icims\.com|recruitee\.com|"
    r"teamtailor\.com|breezy\.hr|bamboohr\.com|pinpointhq\.com|"
    r"recruiterbox\.com|rippling\.com|jobvite\.com|paycomonline\.net|"
    r"dayforcehcm\.com|ultipro\.com|csod\.com", re.I)


def _host(url: str) -> str:
    m = re.match(r"https?://([^/?#]+)", url or "")
    return m.group(1).lower() if m else ""


def _looks_like_job(url: str, hosts: frozenset[str] = frozenset()) -> bool:
    """Job links are the ones a model can mix up; anything else (a help page
    it was told about) is left alone. A job link is one on a careers-site
    host we know, or on the same host as a job this reply is about: many
    employers serve their ATS from their own domain (careers.ey.com)."""
    return bool(_JOB_HOSTS.search(url)) or _host(url) in hosts


_ASKS_LINK = re.compile(r"\b(links?|urls?|apply|send)\b", re.I)
_LINK_LINE = re.compile(r"(?m)^[ \t]*(?:[-*·]\s*)?<?https?://\S+>?[ \t]*\n?")
_MD_LINK = re.compile(r"\[([^\]\n]+)\]\(https?://[^)\s]+\)")


def hide_links(text: str, message: str) -> str:
    """A job list without its links, unless they asked for links.

    Sai's eval sheet row 24: "too overwhelming, don't show links until the
    user asks". Five long ATS links were half of every list on a phone. The
    links are still in the reference table, so "send me the link for 2"
    gets it."""
    if _ASKS_LINK.search(message or ""):
        return text
    text = _MD_LINK.sub(r"\1", _LINK_LINE.sub("", text))
    return re.sub(r"\n{3,}", "\n\n", text)


_LOCAL_PATH = re.compile(r"!?\[[^\]]*\]\((/[^)]+)\)|(?<![\w/])(/(?:Users|home|private|tmp|var)/\S+)")


#: Where a reply becomes two messages on Telegram: the greeting's second
#: line, and a background update ahead of the reply it used to be glued to
#: (Telegram eval, 2026-10-01). Elsewhere it is a paragraph break.
SPLIT = "[[next]]"


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

        # Jobs they already saw or applied to stay out, unless they ask to
        # see them again (Sai: "no repetitive jobs; if we are out, tell me").
        again = bool(_SEE_AGAIN.search(state.get("user_message") or ""))
        exclude = [] if again else _seen_jobs(conn)

        from search.run import experience_years, search
        result = search(args["query"], profile=reconcile.read_profile(conn)[-1500:],
                        exclude=exclude,
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
        _mark_seen(conn, result["picks"])
        if not result["picks"] and result.get("all_seen"):
            state["found_nothing"] = True
            return (f"Nothing new: all {result['all_seen']} jobs that match this were "
                    f"shown to them before. Do not search again this message. Tell "
                    f"them in a few words of your own that they have seen every "
                    f"current match, and ask: see those again, or widen it (another "
                    f"city, a nearby role, older posts)?")
        if not result["picks"]:
            state["found_nothing"] = True
            return ((result["text_agent"] or "Nothing matched.")
                    + "\n\nDo not search again this message. In one line: it came "
                      "back empty right now, and offer one change as a question "
                      "(\"Nothing for APM right now. Try PM roles?\"). No "
                      "explanations. Never add a city, date or level they did not say.")
        return result["text_agent"] or "Nothing matched."

    if name == "get_job":
        job = _by_ref(state, args["ref"])
        if (gone := _gone(conn, job, state)):
            return gone
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
        if (gone := _gone(conn, job, state)):
            return gone
        layout, heads_up = _pick_layout(args, conn)
        if layout is None:
            return heads_up
        ref = _key_of(job, args["ref"])
        fp = _record_print(conn, layout)
        kept = state.setdefault("tailor_prints", {}).get(ref)
        if (not args.get("instruction") and kept and kept[0] == fp
                and Path(state["resumes"].get(ref, "")).exists()):
            return f"{heads_up}{UNCHANGED}\n\n{kept[1]}"
        built = t.build(job, instruction=args.get("instruction", ""),
                        conn=conn, wants=wants(state, conn), layout=layout)
        state["resumes"][ref] = str(built["pdf"])
        # When they last saw it. A tailored resume they read and then said
        # "apply" to is a choice already made — but only once a turn has gone
        # by, or the model could build one and send it in the same breath.
        state.setdefault("tailored_turn", {})[ref] = state.get("turn")
        text = t.preview(built)
        state["tailor_prints"][ref] = (fp, text)
        return f"{heads_up}{text}\n\n{NOT_SENT_YET}"

    if name == "build_resume":
        from resume import generate, tailor as t
        layout, heads_up = _pick_layout(args, conn)
        if layout is None:
            return heads_up
        fp = _record_print(conn, layout)
        last = vault.get("built_print", conn)
        old = state["resumes"].get("__own__") or vault.get("built_pdf", conn)
        if last == fp and old and Path(old).exists():
            state["resumes"]["__own__"] = old
            return (f"{heads_up}{UNCHANGED}\n\n"
                    f"{vault.get('built_preview', conn) or ''}")
        built = generate.build(conn=conn, layout=layout)
        state["resumes"]["__own__"] = str(built["pdf"])
        text = t.preview(built)
        vault.put("built_print", fp, source="system", conn=conn)
        vault.put("built_pdf", str(built["pdf"]), source="system", conn=conn)
        vault.put("built_preview", text, source="system", conn=conn)
        return f"{heads_up}{text}\n\n{NOT_SENT_YET}"

    if name == "edit_resume_structure":
        from resume import structure
        layout = _saved_layout(conn)
        saved, problems = structure.update(
            conn, order=args.get("order"), rename=args.get("rename"),
            hide=args.get("hide"), show=args.get("show"),
            headline=args.get("headline"), layout=layout)
        return ("Saved for every resume from now on.\n"
                + structure.describe(saved, layout)
                + ("\nNOT done, tell them why:\n" + "\n".join(
                    f"  ! {p}" for p in problems) if problems else "")
                + "\nNow rebuild so they see it: tailor_resume for the job "
                  "you are on, else build_resume.")

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
        if (gone := _gone(conn, job, state)):
            return gone
        # Only a form the agent can fill starts a run, and it is decided here,
        # before the resume question, so nobody is asked which resume to send
        # to a site we will not open. The person is on a phone: for the rest,
        # the link and a tailored resume are the whole of what we can give.
        kind = job.get("apply_kind")
        link = job.get("apply_url") or job.get("url")
        if not state.get("fixture") and kind == "account":
            return (f"NOT STARTED: {job.get('company')}'s careers site wants an "
                    f"account before anyone can apply, and signing up for them "
                    f"is not built yet. Give them the link to apply themselves: "
                    f"{link} . Offer a resume tailored to this job for it.")
        if not state.get("fixture") and kind == "link":
            return (f"NOT STARTED: this job is applied to on another site, not "
                    f"a form we can fill. Give them the link: {link} . Offer a "
                    f"resume tailored to this job for it.")
        # A form, but on a site our filler has not passed the form eval on
        # (apply/sites.py). SmartRecruiters and Keka went live untested and
        # the first runs there sent nothing while one was recorded as sent.
        if not state.get("fixture") and kind == "form" and not sites.can_fill(job.get("source")):
            return (f"NOT STARTED: {job.get('company')}'s form is on a site we "
                    f"cannot fill reliably yet. Give them the link to apply "
                    f"themselves: {link} . Offer a resume tailored to this job for it.")

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
        ref_key = _key_of(job, args["ref"])
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
            # With no last answer, a bare "yes" to the question takes
            # tailored: the question says so.
            chosen = said_resume(state, default or "tailored")
            wanted_mode = RESUME_MODES.get(args.get("resume") or "")
            # A tailored resume for this job that they have already read. "Looks
            # good, apply" after it is an answer — they chose that document by
            # reading it — but only if it was shown on an earlier turn.
            # Filed under the job; under the model's spelling of the ref before.
            raw_ref = (args["ref"] or "").strip().strip("[]")
            turns = state.get("tailored_turn", {})
            seen = turns.get(ref_key, turns.get(raw_ref))
            if (not chosen and wanted_mode in (None, "tailored")
                    and seen is not None and seen != turn):
                chosen = {"tailored"}
            # They already chose for this job: "Did my Swiggy application go
            # through?" got "which resume?" again (Telegram eval, 2026-10-01,
            # asked 4 times in one session). Their earlier answer for the same
            # job still stands; a new answer in this message overrides it.
            # Older entries were filed under the model's spelling of the ref.
            before = _job_resume(conn, ref_key) or _job_resume(conn, args["ref"])
            if not chosen and before and wanted_mode in (None, before):
                chosen = {before}
                note = f" (using {SHOWN_AS[before]}, as you chose for this job)"
            if wanted_mode is None and len(chosen) == 1:
                wanted_mode = next(iter(chosen))
            if wanted_mode is None or wanted_mode not in chosen:
                # Refusing is what makes the question happen, and the question
                # is written here rather than left to the model, so the batch
                # hears the same one sentence however many jobs refused.
                state["resume_asked_turn"] = turn
                # Which jobs the question was for: the one-word answer next
                # turn ("tailored") cannot say whether it was one job or five.
                asked = state.setdefault("asked_refs", {})
                asked.setdefault(turn, set()).add(ref_key)
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
            _job_resume(conn, ref_key, mode)

        wanted = wants(state, conn)
        fixture = state.get("fixture")
        raw_ref = (args["ref"] or "").strip().strip("[]")
        turns = state.get("tailored_turn", {})
        prebuilt = ((state["resumes"].get(ref_key) or state["resumes"].get(raw_ref))
                    if mode == "tailored" and (ref_key in turns or raw_ref in turns)
                    else None)
        if prebuilt and not Path(prebuilt).exists():
            prebuilt = None
        # One job, tailored, never seen: build it and show it first, apply
        # after their OK. Sai picked "tailored", heard "applying", asked to see
        # it and got his old upload: the tailored one was being built out of
        # sight (Swiggy, 2026-10-01). A batch ("apply to all", "5 and 1") still
        # builds inside, unseen, as decided on 2026-09-24.
        if (mode == "tailored" and not prebuilt and not fixture
                and not _is_batch(state, ref_key)):
            return (f"NOT STARTED YET: they chose tailored for {job.get('company')}, "
                    f"so build it first. Call tailor_resume for this job, then "
                    f"send_resume with `latest` so they see the PDF, and ask in a "
                    f"few words of your own whether to apply with it. Start the "
                    f"application when they say yes; that same file is what goes.")

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
            # Said "Submit" while their last change was still going in, the
            # bot answered "Submitting to Loop now" and nothing was submitting
            # (Loop, 2026-10-01). Say where it really is, per status.
            if row["status"] in ("running", "queued"):
                return (f"[{app_id}] {row['company']}: NOT submitting. It is still FILLING "
                        f"(their last change is going in). The whole form comes to them in "
                        f"a moment, and only their 'submit' on that sends it. Tell them that "
                        f"in a few words of your own. Never say submitting or sent.")
            if row["status"] == "submitted":
                return (f"[{app_id}] {row['company']}: already SENT, nothing to answer. "
                        f"Say it went through.")
            return (f"[{app_id}] {row['company']}: {row['status']}, NOT sent and not "
                    f"waiting on anything. {row['outcome'] or ''} Say so in one line.")
        job = {"title": row["title"], "company": row["company"],
               "apply_url": row["apply_url"], "source": row["source"],
               "source_id": row["source_id"]}
        fixture, answer = state.get("fixture"), args["answer"]
        approved = False
        if (row["question"] or "").startswith(confirm.READY):
            # The one answer that sends an application is read from what they
            # typed, never from the model's `answer`: a model that passes
            # "yes" for them is exactly what this step exists to stop.
            if confirm.said_submit(state.get("user_message")):
                confirm.approve(conn, app_id)
                approved = True
                answer = "The person said submit. Send it exactly as they saw it."
            else:
                answer = (f"The person did not say submit. Their reply: "
                          f"{state.get('user_message') or answer!r}. Make the change "
                          f"they asked for, then call submit again so they can see it.")

        def work(c: sqlite3.Connection) -> dict:
            browser = apply_agent.browser_for(job, app_id, fixture=fixture, resume=True)
            return apply_agent.resume_run(app_id, answer, browser, c)

        if not _in_background(state):
            return _application_result(app_id, job, work(conn))
        from apply import worker
        worker.start(app_id, job, work)
        # The words must match where it really is. After Sai's answers the bot
        # said "Got it, submitting 🚀" while the form was still being filled,
        # then asked seven more questions five minutes later (Swiggy, 2026-10-01).
        if approved:
            return (f"[{app_id}] {job['title']} — {job['company']}: SUBMITTING now, "
                    f"they said submit. NOT SENT until an update says SUBMITTED. Tell "
                    f"them in your own words, briefly, that it is going out and you "
                    f"will confirm. Nothing else.")
        return (f"[{app_id}] {job['title']} — {job['company']}: their answer is going "
                f"into the form; it is still FILLING, not submitting. Tell them in "
                f"your own words, briefly, that you are adding it and will show them "
                f"the whole form before anything is sent. Never say submitting.")

    if name == "application_status":
        return applications(conn)

    if name == "company_info":
        import company
        return company.about(args.get("company") or "", conn)

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
        text = no_false_sent(text, conn)
        # Background results are written here by code, not left to the model:
        # told in its context to lead with them, it answered the question and
        # never mentioned that the application had finished.
        if news:
            if state.get("channel") == "telegram":
                text = f"{phrase_update(news, conn)}\n{SPLIT}\n{text}"
            else:
                text = f"{news_lines(news)}\n\n{text}"
        if state.get("channel") != "telegram":
            text = text.replace(SPLIT, "\n\n")
        text = fix_links(text, list((state.get("by_ref") or {}).values())
                         + list(state.get("picks") or []))
        if state.get("searched"):
            text = hide_links(text, message)
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
