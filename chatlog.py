"""What happened underneath each chat turn.

**Why this file had to be written on purpose.** The apply agent logs every step
it takes, but nobody chose that: it parks mid-form and resumes hours later, so
the conversation *had* to survive the process, and the log fell out of the
feature. The chat agent never stops and comes back, so its history stayed a
Python list in the server process and died on every restart. The `messages`
table was in the schema from day one and stayed empty.

The lesson generalises: **you only log what a feature forced you to log.** When
nothing forces it, it has to be deliberate, which is this.

One row per message. The assistant's row also carries `meta`:

    {"ms": 4120, "model_calls": 4, "steps": [
        {"tool": "search_jobs", "ok": true, "ms": 3980, "result": "1. ..."}
    ]}

The text alone tells you what was *said* and nothing about what was *done* —
and the gap between those two is exactly where this system has failed before.
An agent once reported a submitted application while the database said
`waiting`. A transcript would have recorded the lie word for word. `steps`
records that no tool ran.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone

#: How much of a tool result to keep. Enough to recognise what came back,
#: not enough to turn the log into a second copy of the database.
RESULT_CHARS = 400


def new_session() -> str:
    """Short, sortable, and unique enough for one machine."""
    return f"{datetime.now():%m%d-%H%M}-{uuid.uuid4().hex[:4]}"


def record(conn: sqlite3.Connection, session: str, turn: int, role: str,
           content: str, meta: dict | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO messages (session, turn, role, content, meta) "
        "VALUES (?,?,?,?,?)",
        (session, turn, role, content,
         json.dumps(meta, default=str) if meta else None),
    )
    conn.commit()
    return cur.lastrowid


def step(tool: str, args: dict, result: str, ok: bool, ms: int,
         files: list | None = None, funnel: dict | None = None) -> dict:
    """One tool call, as it goes into `meta["steps"]`.

    `files` is recorded here rather than resolved when the log is printed, so
    a turn from last week still reports what ran last week after a refactor.

    `funnel` is what a search narrowed and what it threw away. Without it,
    "here are five jobs" reads the same whether fifteen were rejected for good
    reasons or the right one was among them.
    """
    out = {"tool": tool, "input": args, "ok": ok, "ms": ms,
           "files": files or [],
           "result": (result or "")[:RESULT_CHARS]}
    if funnel:
        out["funnel"] = funnel
    return out


def turns(conn: sqlite3.Connection, session: str | None = None,
          limit: int = 20) -> list[dict]:
    """The most recent `limit` messages, oldest first.

    Ordered by id, never by timestamp: two messages in the same second are
    common and their order is the whole point of a log.
    """
    sql = "SELECT * FROM messages"
    params: list = []
    if session:
        sql += " WHERE session = ?"
        params.append(session)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    rows = [dict(r) for r in conn.execute(sql, params)]
    for r in rows:
        r["meta"] = json.loads(r["meta"]) if r.get("meta") else None
    return list(reversed(rows))


def sessions(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT session, COUNT(*) AS messages, MAX(turn) AS turns, "
        "MIN(created_at) AS started, MAX(created_at) AS ended "
        "FROM messages WHERE session IS NOT NULL "
        "GROUP BY session ORDER BY MIN(id) DESC LIMIT ?", (limit,))]


def local_clock(ts: str | None) -> str:
    """SQLite's CURRENT_TIMESTAMP is UTC; a person reading a log is not.

    Stored UTC so every table in this database agrees with every other one,
    converted here so the printed time matches the wall clock you remember.
    """
    if not ts:
        return ""
    try:
        dt = datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return ts[11:19]
    return dt.replace(tzinfo=timezone.utc).astimezone().strftime("%H:%M:%S")


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


#: Tool names are written for the model. These are written for the person
#: reading the log afterwards. A tool with no entry falls back to its raw
#: name rather than being guessed at or hidden.
PLAIN = {
    "search_jobs":        "Searched the job table",
    "get_job":            "Opened the full job description",
    "tailor_resume":      "Built a resume tailored to that job",
    "build_resume":       "Built your updated resume",
    "send_resume":        "Sent the resume already on file",
    "set_resume_preference": "Saved which resume to apply with",
    "start_application":  "Started filling in the application form",
    "answer_application": "Passed your answer to the parked application",
    "application_status": "Checked where your applications stand",
}


def _took(ms: int) -> str:
    if ms < 150:
        return "instantly"
    if ms < 1500:
        return "took under a second"
    return f"took {ms / 1000:.0f} seconds"


def _when(ts: str | None) -> str:
    """'Monday 22 September, 3:51 pm' — a date a person recognises."""
    if not ts:
        return "unknown time"
    try:
        dt = datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return ts
    dt = dt.replace(tzinfo=timezone.utc).astimezone()
    hour = dt.hour % 12 or 12
    return (f"{dt:%A} {dt.day} {dt:%B}, {hour}:{dt:%M} "
            f"{'am' if dt.hour < 12 else 'pm'}")


def _wrap(text: str, width: int, indent: str) -> str:
    import textwrap
    out = []
    for para in (text or "").splitlines() or [""]:
        out.extend(textwrap.wrap(para, width, initial_indent=indent,
                                 subsequent_indent=indent) or [indent.rstrip()])
    return "\n".join(out)


def render(rows: list[dict], width: int = 76, color: bool | None = None) -> str:
    """Your words, what it actually did, and what it said back.

    Written to be read by the person whose job search this is, not by whoever
    wrote the tools — so no function names, no milliseconds, no jargon. The
    mechanism still shows through; it is just in words.

    `color` defaults to on only when writing to a terminal, so piping this to
    a file or another command does not fill it with escape codes.
    """
    if not rows:
        return ("Nothing recorded yet. Start a chat with `python cli.py chat` "
                "or `python cli.py web`, then run this again.")

    if color is None:
        import sys
        color = sys.stdout.isatty()

    def dim(s: str) -> str:
        return f"\033[2m{s}\033[0m" if color else s

    def bold(s: str) -> str:
        return f"\033[1m{s}\033[0m" if color else s

    def head(s: str) -> str:
        return f"\033[36m{s}\033[0m" if color else s

    out: list[str] = []
    session = None
    for r in rows:
        meta = r.get("meta") or {}

        if r.get("session") != session:
            session = r.get("session")
            out.append("")
            out.append(dim("─" * width))
            out.append(dim(f" {_when(r.get('created_at'))}"
                           f"    (session {session or 'unknown'})"))
            out.append(dim("─" * width))

        if r["role"] == "user":
            out.append("")
            out.append(head("YOU ASKED"))
            out.append(_wrap(r["content"], width, "  "))
            continue

        out.append("")
        out.append(head("WHAT IT DID"))
        steps = meta.get("steps") or []
        if not steps:
            out.append("  Nothing. It answered from what it already knew.")
        for s in steps:
            what = PLAIN.get(s["tool"], s["tool"])
            if not s.get("ok"):
                out.append(f"  Tried to: {what.lower()} — and it failed")
            else:
                out.append(f"  {what}, {_took(s.get('ms', 0))}")
            fn = s.get("funnel") or {}
            if fn:
                for line in fn.get("looked_for") or []:
                    out.append(dim(f"      looked for  {line}"))
                for note in fn.get("relaxed") or []:
                    out.append(dim(f"      loosened    {note}"))
                out.append(dim(
                    f"      {fn.get('open', 0)} open  ->  "
                    f"{fn.get('matched', 0)} matched  ->  "
                    f"{fn.get('shown', 0)} shown"))
                if fn.get("matched") and fn["matched"] == fn.get("shown"):
                    out.append("      nothing was filtered out; the AI had "
                               "no choice to make")
                for d in (fn.get("dropped") or [])[:8]:
                    out.append(dim(f"      not shown   {d.get('title','')[:38]}"
                                   f" — {d.get('company','')}"))
                    if d.get("why"):
                        out.append(dim(f"                  {d['why']}"))
            for f in s.get("files") or []:
                # Rows written before files carried a description are plain
                # strings. An old log stays readable rather than crashing.
                path, why = (list(f) + [""])[:2] if isinstance(f, (list, tuple)) \
                    else (f, "")
                out.append(dim(f"      ran  {path}"))
                if why:
                    out.append(dim(f"           {why}"))
            detail = (s.get("result") or "").strip().splitlines()
            if detail:
                out.append(dim(f"      got  {detail[0][:width - 18]}"))

        out.append("")
        out.append(head("IT REPLIED"))
        out.append(_wrap(r["content"], width, "  "))

        cost = []
        if meta.get("ms"):
            cost.append(f"{meta['ms'] / 1000:.1f} seconds")
        if meta.get("model_calls"):
            cost.append(_plural(meta["model_calls"], "call") + " to the AI")
        if cost:
            out.append("")
            out.append(dim("  " + " · ".join(cost)))

    return "\n".join(out)
