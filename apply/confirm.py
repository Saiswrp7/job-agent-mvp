"""Yes before submit: nothing is sent until the person has seen it.

When the agent presses submit, the browser stops and parks the run instead:
the person gets the filled answers and a screenshot, and is asked to reply
"submit". Three things make that hold, all in code:

- **Their word, never the model's.** Approval is read from the person's own
  message (`said_submit`), the way the resume choice is. The model cannot
  approve on their behalf by passing "yes" to a tool.
- **What they saw is what goes.** The approved answers are stored. A resumed
  run fills the form again (a parked cloud session is closed), and if the form
  then holds anything different, it parks again and says what changed.
- **Only for people it is switched on for.** `ALLOW_SUBMIT=1` and the profile
  in `SUBMIT_USERS` (Telegram ids, comma-separated; `default` for the Mac).
"""

from __future__ import annotations

import json
import os
import re
import sqlite3

import paths

#: How a parked approval question starts. The chat and the Telegram push look
#: for it, so it must not change without them.
READY = "READY TO SUBMIT"
SUBMIT_ENV = "ALLOW_SUBMIT"
USERS_ENV = "SUBMIT_USERS"

_YES = re.compile(r"\b(submit|send( it)?|go ahead|confirm(ed)?|approved?|yes|yep|ok(ay)?)\b", re.I)
_NO = re.compile(r"\b(no|not|don'?t|do not|wait|stop|change|hold|cancel|instead|but)\b", re.I)


def me() -> str:
    """This profile's id for SUBMIT_USERS: the Telegram id, or `default`."""
    return "default" if paths.IS_DEFAULT else paths.HOME.name.removeprefix("tg-")


def not_allowed() -> str | None:
    """None when this profile may submit, else why not."""
    if os.environ.get(SUBMIT_ENV) != "1":
        return (f"{SUBMIT_ENV} is not set — refusing to submit a real "
                f"application. Everything up to this point ran.")
    allowed = {x.strip() for x in os.environ.get(USERS_ENV, "").split(",") if x.strip()}
    if me() not in allowed:
        return (f"submitting is switched on only for {sorted(allowed) or 'nobody'}, "
                f"not this person — the form is filled, nothing was sent.")
    return None


def said_submit(message: str | None) -> bool:
    """Their own message approves sending: "submit", "yes send it". A reply
    that also asks for a change ("yes but fix the notice period") does not."""
    text = message or ""
    return bool(_YES.search(text)) and not _NO.search(text)


def shown(values: dict, fields: list[dict]) -> dict[str, str]:
    """The answers as the person would read them: by label, filled ones only."""
    labels = {f["name"]: (f.get("label") or f["name"]) for f in fields}
    out = {}
    for name, value in values.items():
        if value in (None, "", []):
            continue
        out[labels.get(name, name)] = str(value)
    return out


def question(job: dict, answers: dict[str, str], changed: list[str] | None = None) -> str:
    lines = [f"{READY}: {job.get('title')} at {job.get('company')}."]
    if changed:
        lines.append("These changed since you said submit: " + ", ".join(changed) + ".")
    lines.append("It is filled like this:")
    lines += [f"- {k}: {v[:120]}" for k, v in answers.items()]
    lines.append("Reply 'submit' to send it, or tell me what to change.")
    return "\n".join(lines)


class Approval:
    """Attached to a browser by the apply agent; the submit guard calls it."""

    def __init__(self, conn: sqlite3.Connection, app_id: int, job: dict):
        self.conn, self.app_id, self.job = conn, app_id, job

    def check(self, values: dict, fields: list[dict]) -> None:
        """Returns when the person approved exactly these answers. Otherwise
        stores them as the ones to approve and parks with the question."""
        from apply.browser import Park
        now = shown(values, fields)
        row = self.conn.execute(
            "SELECT confirm_values, confirmed_at FROM applications WHERE id = ?",
            (self.app_id,)).fetchone()
        before = json.loads(row["confirm_values"]) if row and row["confirm_values"] else None
        if row and row["confirmed_at"] and before == now:
            return
        changed = None
        if row and row["confirmed_at"] and before is not None:
            changed = sorted(k for k in set(before) | set(now) if before.get(k) != now.get(k))
        self.conn.execute(
            "UPDATE applications SET confirm_values = ?, confirmed_at = NULL WHERE id = ?",
            (json.dumps(now), self.app_id))
        self.conn.commit()
        raise Park(question(self.job, now, changed))


def approve(conn: sqlite3.Connection, app_id: int) -> None:
    conn.execute("UPDATE applications SET confirmed_at = CURRENT_TIMESTAMP WHERE id = ?",
                 (app_id,))
    conn.commit()
