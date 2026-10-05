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


#: In the refusal when sending is off for this person. The harness looks for it
#: and writes the outcome itself (see SENDING_OFF_MESSAGE).
SENDING_OFF = "SENDING IS OFF FOR THIS ACCOUNT"
#: What the person is told, decided by code: the model once turned this refusal
#: into "the site blocked it... retrying" (an invited user, AccorHotel, 2026-10-01).
#: One sentence: the update quotes only an outcome's first sentence.
SENDING_OFF_MESSAGE = ("Filled but not sent: sending isn't switched on for your account "
                       "yet (a beta setting on our side, not the site), so nothing goes "
                       "out until Sai turns it on.")


def not_allowed() -> str | None:
    """None when this profile may submit, else why not."""
    if os.environ.get(SUBMIT_ENV) != "1":
        return (f"{SUBMIT_ENV} is not set — refusing to submit a real "
                f"application. Everything up to this point ran.")
    allowed = {x.strip() for x in os.environ.get(USERS_ENV, "").split(",") if x.strip()}
    if me() not in allowed:
        return (f"{SENDING_OFF} (SUBMIT_USERS is {sorted(allowed) or 'empty'}): our own "
                f"setting, not the site. The form is filled, nothing was sent. Stop "
                f"here and say exactly that; do not retry.")
    return None


def said_submit(message: str | None) -> bool:
    """Their own message approves sending: "submit", "yes send it". A reply
    that also asks for a change ("yes but fix the notice period") does not."""
    text = message or ""
    return (bool(_YES.search(text)) or _typed_submit(text)) and not _NO.search(text)


def _typed_submit(text: str) -> bool:
    """"Sumbit", "submti", "yea", "haan": Sai's approvals on a phone keyboard.
    "Yea" to a ready Swiggy form was read as a change request, so the whole
    form was filled again and shown again (2026-10-05)."""
    import difflib
    words = re.findall(r"[a-z]+", text.lower())
    return any(w in _YES_TYPED or (5 <= len(w) <= 7 and
               difflib.SequenceMatcher(None, w, "submit").ratio() >= 0.8)
               for w in words)


_YES_TYPED = {"yea", "yeah", "ya", "yup", "haan", "han", "sure"}


def shown(values: dict, fields: list[dict]) -> dict[str, str]:
    """The answers as the person would read them: by label, filled ones only."""
    labels = {f["name"]: (f.get("label") or f["name"]) for f in fields}
    out, files = {}, set()
    for name, value in values.items():
        if value in (None, "", []):
            continue
        v = str(value)
        # One resume in two upload boxes is one answer: Keka's second box
        # came back named after the page heading and the person saw their
        # resume twice, once as "Apply for this job" (Loop, 2026-10-01).
        if re.search(r"\.(pdf|docx?|rtf|odt|txt)$", v, re.I):
            if v.lower() in files:
                continue
            files.add(v.lower())
        out[labels.get(name, name)] = v
    return out


def blank(values: dict, fields: list[dict]) -> list[str]:
    """Boxes on this page still empty. Shown with the answers: Swiggy's page 2
    was approved with salary, notice and relocation silently empty."""
    return [f.get("label") or f["name"] for f in fields
            if values.get(f["name"]) in (None, "", [])]


def _plain(value: str) -> str:
    """An answer as it is compared, not as it is shown: case and spacing
    ignored, and a phone-like answer by its last 10 digits. "+91-9876543210"
    and "9876543210" are the same answer; re-asking for approval over that
    (Loop, 2026-10-01) taught nothing and cost a round trip."""
    v = re.sub(r"\s+", " ", str(value or "")).strip().lower()
    digits = re.sub(r"\D", "", v)
    if len(digits) >= 10 and re.fullmatch(r"[\d\s+()\-.]+", v):
        return digits[-10:]
    return v


def same(before: dict | None, now: dict) -> bool:
    if before is None or set(before) != set(now):
        return False
    return all(_plain(before[k]) == _plain(now[k]) for k in now)


def wants_answers(message: str | None) -> bool:
    """They asked to see what was filled ("show answers", "what did you put")
    instead of approving or changing it."""
    return bool(re.search(r"\b(show|see|list|read)\b.*\b(answers?|it|all|everything|form)\b"
                          r"|\bwhat (did|have) you (put|fill|enter|type)", message or "", re.I))


def full_list(answers: dict[str, str]) -> str:
    """Every filled answer, whole, for "show answers"."""
    budget = 3400 - sum(len(k) + 4 for k in answers)
    cap = 1200
    while cap > 150 and sum(min(len(v), cap) for v in answers.values()) > budget:
        cap -= 50
    return "\n".join(f"- {k}: {v if len(v) <= cap else v[:cap].rstrip() + '…'}"
                     for k, v in answers.items())


def question(job: dict, answers: dict[str, str], changed: list[str] | None = None,
             empty: list[str] | None = None, notes: list[str] | None = None) -> str:
    """The one last question: good to go? Short on purpose (Sai, 2026-10-04:
    ask everything first, then only "is it good to go?"). The answers are
    stored with the question and a resumed run must match them exactly, so
    approval still means these answers; "show answers" lists them."""
    lines = [f"{READY}: {job.get('title')} at {job.get('company')}.",
             f"Filled: {len(answers)} answers."]
    if changed:
        lines.append("These changed since you said submit: " + ", ".join(changed) + ".")
    if empty:
        lines.append("Left blank: " + "; ".join(e[:80] for e in empty) + ".")
    if notes:
        lines.append("Check these (a look at the page flagged them): "
                     + "; ".join(n[:120] for n in notes) + ".")
    lines.append("Good to go? Reply 'submit' to send it, 'show answers' to see "
                 "everything, or tell me what to change.")
    return "\n".join(lines)


class Approval:
    """Attached to a browser by the apply agent; the submit guard calls it."""

    def __init__(self, conn: sqlite3.Connection, app_id: int, job: dict):
        self.conn, self.app_id, self.job = conn, app_id, job

    def check(self, values: dict, fields: list[dict], partial: bool = False,
              notes: list[str] | None = None) -> None:
        """Returns when the person approved exactly these answers. Otherwise
        stores them as the ones to approve and parks with the question.

        `partial`: a button that may send but may also open another page (a
        bare Continue). The answers so far need only agree with what they
        approved, or a refilled first page could never get past it.
        `notes`: what a look at the page flagged, shown with the answers."""
        from apply.browser import Park
        now = shown(values, fields)
        row = self.conn.execute(
            "SELECT confirm_values, confirmed_at FROM applications WHERE id = ?",
            (self.app_id,)).fetchone()
        before = json.loads(row["confirm_values"]) if row and row["confirm_values"] else None
        if row and row["confirmed_at"] and same(before, now):
            return
        if partial and row and row["confirmed_at"] and before is not None \
                and all(_plain(before.get(k)) == _plain(v) for k, v in now.items()):
            return
        changed = None
        if row and row["confirmed_at"] and before is not None:
            changed = sorted(k for k in set(before) | set(now)
                             if _plain(before.get(k)) != _plain(now.get(k)))
        self.conn.execute(
            "UPDATE applications SET confirm_values = ?, confirmed_at = NULL WHERE id = ?",
            (json.dumps(now), self.app_id))
        self.conn.commit()
        raise Park(question(self.job, now, changed, blank(values, fields), notes))


def approve(conn: sqlite3.Connection, app_id: int) -> None:
    conn.execute("UPDATE applications SET confirmed_at = CURRENT_TIMESTAMP WHERE id = ?",
                 (app_id,))
    conn.commit()
