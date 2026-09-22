"""The apply agent: one application, start to finish.

Pre-flight is the part that matters. Reading the whole form and diffing it
against the vault *before* the loop starts turns six questions over twenty
minutes into one question, which is the difference between a tool someone uses
and one they abandon.
"""

from __future__ import annotations

import json
import sqlite3

import llm
import vault
from apply import harness
from apply.browser import BaseBrowser, ManualBrowser, PlaywrightBrowser
from engine import db


def create(job: dict, resume_path: str | None = None,
           conn: sqlite3.Connection | None = None) -> int:
    close_after = conn is None
    conn = conn or db.connect()
    try:
        # Asking twice must not start twice. Without this a second "apply to 3"
        # opens a parallel row, and once ALLOW_SUBMIT is on that is two real
        # applications to the same company.
        existing = conn.execute(
            "SELECT id FROM applications WHERE source=? AND source_id=? "
            "AND status != 'done' ORDER BY id LIMIT 1",
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
        pre = preflight(browser, conn)
        system = system_prompt(job, pre["known"], row["resume_path"])
        return harness.run(app_id, browser, system, answer=answer, conn=conn)
    finally:
        if close_after:
            conn.close()


def browser_for(job: dict, app_id: int, fixture: str | None = None) -> BaseBrowser:
    if fixture:
        return ManualBrowser(fixture, app_id)
    url = job.get("apply_url") or job.get("url")
    if not url:
        raise ValueError("job has no apply URL")
    return PlaywrightBrowser(url, app_id)


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
