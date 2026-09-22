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
import sqlite3
import time
from pathlib import Path

import chatlog
import llm
import vault
from apply import agent as apply_agent
from engine import db
from memory import reconcile

HERE = Path(__file__).resolve().parent
LAST = HERE / "artifacts" / "last_search.json"
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
        "description": "Full detail on one result from the last search, by its number.",
        "input_schema": {
            "type": "object",
            "properties": {"n": {"type": "integer"}},
            "required": ["n"], "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "tailor_resume",
        "description": ("Build a tailored resume for one result. Returns the "
                        "bullets as text. Show them before applying."),
        "input_schema": {
            "type": "object",
            "properties": {"n": {"type": "integer"}},
            "required": ["n"], "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "start_application",
        "description": ("Begin applying to one result. Only after the person "
                        "explicitly said to apply."),
        "input_schema": {
            "type": "object",
            "properties": {
                "n": {"type": "integer"},
                "resume_path": {"type": "string",
                                "description": "from tailor_resume, or omit for the master"},
            },
            "required": ["n"], "additionalProperties": False,
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
    "tailor_resume": [
        ["resume/tailor.py", "rewrites your bullets for this job · asks the AI"],
        ["resume/verify.py", "reverts any bullet that gained a fact · no AI"],
        ["resume/render.py", "builds the PDF"],
    ],
    "start_application": [
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
    """Ids, titles, companies. Never descriptions — that is the point."""
    if not picks:
        return "(no search yet)"
    return "\n".join(
        f"{i}. {p['title']} — {p['company']}" for i, p in enumerate(picks, 1)
    )


def open_applications(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        "SELECT id, company, title, status, question FROM applications "
        "WHERE status != 'done' ORDER BY id DESC LIMIT 10").fetchall()
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
        return ("No resume on file. Ask them to attach a PDF — there is an "
                "attach button in the chat.")
    try:
        master = json.loads(MASTER.read_text())
    except Exception:                                  # noqa: BLE001
        return f"A resume is on file at {path}."
    roles = ", ".join(
        f"{e.get('title')} at {e.get('company')}" for e in master.get("experience", [])
    )
    return (f"**A resume is on file** — parsed and ready, no need to ask for "
            f"it again.\nFile: {path}\nRoles: {roles or '(none parsed)'}\n"
            f"Summary: {master.get('summary', '')[:400]}")


def context(conn: sqlite3.Connection) -> str:
    """The stable prefix. Cached; everything volatile goes after it."""
    profile = reconcile.read_profile()
    return "\n\n".join([
        llm.prompt("chat"),
        "## Known about them\n" + vault.one_pager(conn),
        "## Resume\n" + resume_status(conn),
        "## Profile\n" + (profile[-2000:] if profile else "(nothing learned yet)"),
    ])


def turn_state(conn: sqlite3.Connection, picks: list[dict]) -> str:
    """Rebuilt every turn. Never cached, never trusted from memory."""
    return (f"## Last results\n{reference_table(picks)}\n\n"
            f"## Open applications\n{open_applications(conn)}")


def compact(history: list) -> list:
    """Drop old turns, keep the tail.

    This loses less than it looks like: anything durable already left the
    conversation into the vault or the profile, so the dropped turns were
    thread, not facts.
    """
    if len(json.dumps(history, default=str)) < COMPACT_AT:
        return history
    kept = history[-KEEP_TURNS:]
    # Never start on a tool_result — it would reference a tool_use that is gone.
    while kept and isinstance(kept[0].get("content"), list) and any(
            isinstance(b, dict) and b.get("type") == "tool_result"
            for b in kept[0]["content"]):
        kept = kept[1:]
    note = {"role": "user",
            "content": "[earlier conversation trimmed — durable facts are in "
                       "the profile and vault above]"}
    return [note, *kept]


# --- tools -----------------------------------------------------------------

def _nth(picks: list[dict], n: int) -> dict:
    if not 1 <= n <= len(picks):
        raise IndexError(f"there is no result {n}; the last search returned "
                         f"{len(picks)}")
    return picks[n - 1]


def run_tool(name: str, args: dict, state: dict,
             conn: sqlite3.Connection) -> str:
    picks = state["picks"]

    if name == "search_jobs":
        from search.run import search
        result = search(args["query"], profile=reconcile.read_profile()[-1500:],
                        conn=conn)
        state["picks"] = result["picks"]
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
        return result["text"] or "Nothing matched."

    if name == "get_job":
        job = _nth(picks, args["n"])
        return (f"{job['title']} at {job['company']}\n"
                f"{job.get('city') or job.get('location')} · "
                f"{(job.get('posted_at') or '')[:10]}\n"
                f"{job.get('apply_url') or job.get('url')}\n\n"
                f"{(job.get('description') or '')[:3000]}")

    if name == "tailor_resume":
        from resume import tailor as t
        job = _nth(picks, args["n"])
        built = t.build(job)
        state["resumes"][args["n"]] = str(built["pdf"])
        return f"{t.preview(built)}\n\nPDF: {built['pdf']}"

    if name == "start_application":
        job = _nth(picks, args["n"])
        resume_path = (args.get("resume_path")
                       or state["resumes"].get(args["n"])
                       or vault.get("master_resume_path", conn))
        app_id = apply_agent.create(job, resume_path, conn)
        browser = apply_agent.browser_for(job, app_id, fixture=state.get("fixture"))
        r = apply_agent.start(app_id, job, browser, resume_path, conn)
        if r["status"] == "waiting":
            return (f"[{app_id}] started, now waiting on: {r['question']}")
        return f"[{app_id}] {r['status']}: {r.get('message') or r.get('detail','')}"

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
        browser = apply_agent.browser_for(job, app_id,
                                          fixture=state.get("fixture"))
        r = apply_agent.resume_run(app_id, args["answer"], browser, conn)
        detail = r.get("question") or r.get("message") or r.get("detail", "")
        return f"[{app_id}] {r['status']}: {detail}"

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

    # Inferred facts go to staging, never straight to the vault. Facts the
    # person states outright are written immediately, elsewhere, and echoed.
    vault.stage(message, context="chat", conn=conn)

    # The transcript is separate from staging on purpose: staging is a memory
    # queue that gets consumed, this is a record that stays.
    session = state.setdefault("session", chatlog.new_session())
    turn = state["turn"] = state.get("turn", 0) + 1
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
        # Sampled from llm.py rather than counted in this loop, because a tool
        # makes its own calls: `search_jobs` is two more on its own, and
        # counting only the loop reports half the real bill.
        _log(conn, session, turn, "assistant", text, {
            "ms": round((time.monotonic() - started) * 1000),
            "model_calls": llm.calls() - calls_before,
            "steps": steps,
        })
        return text, history

    history.append({"role": "user",
                    "content": f"{turn_state(conn, state['picks'])}\n\n{message}"})
    history = compact(history)

    client = llm.client()
    system = context(conn)

    for _ in range(6):
        emit("thinking")
        llm.counted()          # this loop calls create() directly, not via llm
        resp = client.messages.create(
            model=llm.MODEL(),
            max_tokens=4096,
            system=[{"type": "text", "text": system,
                     "cache_control": {"type": "ephemeral", "ttl": "1h"}}],
            tools=TOOLS,
            messages=history,
            thinking={"type": "adaptive"},
            output_config={"effort": "medium"},
        )
        history.append({"role": "assistant", "content": resp.content})

        if resp.stop_reason != "tool_use":
            text = "".join(b.text for b in resp.content if b.type == "text")
            return finish(text.strip())

        results = []
        for block in resp.content:
            if block.type != "tool_use":
                continue
            t0 = time.monotonic()
            files = TOOL_FILES.get(block.name) or []
            emit("tool",
                 text=chatlog.PLAIN.get(block.name, block.name),
                 files=files)
            try:
                out = run_tool(block.name, block.input, state, conn)
                is_error = False
            except Exception as exc:                   # noqa: BLE001
                out, is_error = f"ERROR: {exc}", True
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
    session = chatlog.new_session()
    state = {"picks": load_last(), "resumes": {}, "fixture": fixture,
             "session": session}
    history: list = []
    print(f"job agent · {llm.provider()}/{llm.MODEL()} · ctrl-c to quit")
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
