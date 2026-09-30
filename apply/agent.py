"""The apply agent: one application, start to finish.

Pre-flight is the part that matters. Reading the whole form and diffing it
against the vault *before* the loop starts turns six questions over twenty
minutes into one question, which is the difference between a tool someone uses
and one they abandon.
"""

from __future__ import annotations

import json
import os
import sqlite3

import llm
import vault
from apply import confirm, harness, pagecheck
from apply.browser import BaseBrowser, ManualBrowser
from apply.jev import JevBrowser

#: Which real browser applies: "jev" (the person's own Chrome, this Mac) or
#: "cloud" (a Browserbase Chrome per run — for people who are not at this Mac).
BROWSER_ENV = "JOB_AGENT_BROWSER"
from engine import db


def create(job: dict, resume_path: str | None = None,
           conn: sqlite3.Connection | None = None) -> int:
    close_after = conn is None
    conn = conn or db.connect()
    try:
        # Asking twice must not start twice. Without this a second "apply to 3"
        # opens a parallel row, and once ALLOW_SUBMIT is on that is two real
        # applications to the same company.
        # `submitted` is excluded too: that one is finished, and a second row
        # for it would be a duplicate application rather than a retry.
        # `no_form` and `sign_in` never opened a form, so a retry is fresh.
        # `unconfirmed` is NOT excluded: it may have been sent, and a second
        # row would be a second application.
        existing = conn.execute(
            "SELECT id FROM applications WHERE source=? AND source_id=? "
            "AND status NOT IN ('submitted', 'failed', 'no_form', 'sign_in') "
            "ORDER BY id LIMIT 1",
            (job.get("source"), str(job.get("source_id")))).fetchone()
        if existing is not None:
            return existing[0]

        cur = conn.execute(
            "INSERT INTO applications (source, source_id, company, title, "
            "apply_url, resume_path) VALUES (?,?,?,?,?,?)",
            (job.get("source"), str(job.get("source_id")), job.get("company"),
             job.get("title"), job.get("apply_url") or job.get("url"),
             resume_path),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        if close_after:
            conn.close()


def preflight(browser: BaseBrowser, conn: sqlite3.Connection | None = None) -> dict:
    """Read the form once, and say what is known and what is missing.

    Deterministic — no model. The agent gets the answer handed to it rather
    than discovering gaps one field at a time.
    """
    fields = browser.read_form()
    labels = [f["label"] or f["name"] for f in fields]
    known = vault.slice_for(labels, conn)

    missing = []
    for f in fields:
        label = f["label"] or f["name"]
        if not f.get("required"):
            continue
        if label in known:
            continue
        if f.get("type") == "file":          # the resume, supplied separately
            continue
        missing.append(label)

    return {"fields": fields, "known": known, "missing": missing}


def system_prompt(job: dict, known: dict, resume_path: str | None) -> str:
    parts = [llm.prompt("apply"), "", "## This job",
             f"{job.get('title')} at {job.get('company')}",
             str(job.get("apply_url") or job.get("url") or ""), "",
             "## Vault (the only values you may use)",
             json.dumps(known, indent=1) if known else "(empty)"]
    if resume_path:
        parts += ["", f"## Resume to upload\n{resume_path}"]
    return "\n".join(parts)


def start(app_id: int, job: dict, browser: BaseBrowser,
          resume_path: str | None = None,
          conn: sqlite3.Connection | None = None) -> dict:
    close_after = conn is None
    conn = conn or db.connect()
    try:
        _note_replay(conn, app_id, browser)
        browser.approval = confirm.Approval(conn, app_id, job)
        stop = _no_form_here(conn, app_id, job, browser)
        if stop:
            return stop
        pre = preflight(browser, conn)
        system = system_prompt(job, pre["known"], resume_path)

        first = (
            f"Fill this application. {len(pre['fields'])} fields, "
            f"{len(pre['missing'])} required values are not in the vault"
            + (f": {pre['missing']}" if pre["missing"] else ".")
            + "\nIf anything is missing, ask for all of it in one question."
        )
        return harness.run(app_id, browser, system, first_message=first, conn=conn)
    finally:
        _close(browser)
        if close_after:
            conn.close()


def resume_run(app_id: int, answer: str, browser: BaseBrowser,
               conn: sqlite3.Connection | None = None) -> dict:
    close_after = conn is None
    conn = conn or db.connect()
    try:
        row = conn.execute("SELECT * FROM applications WHERE id=?",
                           (app_id,)).fetchone()
        if row is None:
            raise ValueError(f"no application {app_id}")
        job = {"title": row["title"], "company": row["company"],
               "apply_url": row["apply_url"]}
        _note_replay(conn, app_id, browser)
        browser.approval = confirm.Approval(conn, app_id, job)
        stop = _no_form_here(conn, app_id, job, browser)
        if stop:
            return stop
        pre = preflight(browser, conn)
        system = system_prompt(job, pre["known"], row["resume_path"])
        # Every resume opens the form afresh, so what was typed before the
        # question is gone. Said here, or the agent believes its history and
        # meets it at the submit guard as "required fields still empty".
        answer = (f"{answer}\n\n(The form was opened again to carry on, so it "
                  f"is empty now: read_form and fill it again.)")
        return harness.run(app_id, browser, system, answer=answer, conn=conn)
    finally:
        _close(browser)
        if close_after:
            conn.close()


def _no_form_here(conn: sqlite3.Connection, app_id: int, job: dict,
                  browser: BaseBrowser) -> dict | None:
    """Decided in code before the model starts: is there a form to fill?

    First the page's own Apply button is pressed if it shows a job
    description. Then a password field means a sign-in wall, and no fields at
    all means no form. Either ends the run here with the link. Left to the
    model, an empty page became "you need to sign in" (Clickpost) and a
    press of "Apply With Indeed" recorded as submitted (Swiggy)."""
    opened = browser.open_form()
    if opened:
        harness.trace(conn, app_id, -1, "browser", {"opened": opened})
    st = browser.page_state()
    if not st:
        return None                   # this backend cannot tell; the agent reads
    link = job.get("apply_url") or job.get("url")
    harness.trace(conn, app_id, -1, "page", {k: st.get(k) for k in
                                             ("url", "fields", "password", "captcha", "buttons")})
    if st.get("password"):
        msg = f"{browser.SIGN_IN} Link: {link}"
        status = "sign_in"
    elif st.get("fields", 0) == 0:
        msg, status = pagecheck.no_form(job.get("company"), link), "no_form"
    else:
        return None
    harness.save(conn, app_id, [], status, None)
    return {"status": status, "message": msg}


def _note_replay(conn: sqlite3.Connection, app_id: int, browser: BaseBrowser) -> None:
    """A cloud run's recording, in the run's log: the one way to see what
    happened on a form nobody here was watching."""
    url = getattr(browser, "replay_url", None)
    if url:
        harness.trace(conn, app_id, -1, "browser", {"replay": url})


def _close(browser: BaseBrowser) -> None:
    """Close a cloud session when its run ends or parks: it is billed while
    open, and a parked run can wait hours. Jev's tab stays — it is the person's
    own Chrome, and they may want to look at the form."""
    if getattr(browser, "close_after_run", False):
        browser.close()


def browser_for(job: dict, app_id: int, fixture: str | None = None) -> BaseBrowser:
    if fixture:
        return ManualBrowser(fixture, app_id)
    url = job.get("apply_url") or job.get("url")
    if not url:
        raise ValueError("job has no apply URL")
    if os.environ.get(BROWSER_ENV, "jev").lower() == "cloud":
        from apply.cloud import CloudBrowser  # noqa: PLC0415
        return CloudBrowser(url, app_id)
    return JevBrowser(url, app_id)


def pending(conn: sqlite3.Connection | None = None) -> list[dict]:
    """Applications parked on a question.

    The chat agent reads this and asks once, even when two applications are
    waiting — otherwise two agents message the user separately about the same
    missing field.
    """
    close_after = conn is None
    conn = conn or db.connect()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT id, company, title, question FROM applications "
            "WHERE status='waiting' ORDER BY id")]
    finally:
        if close_after:
            conn.close()
