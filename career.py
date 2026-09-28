"""Everything they did, as rows — and where each row came from.

`master.json` is the resume they uploaded: fixed, trusted, and finished. This
is the part that grows. Every accomplishment they mention in a conversation
lands here with the message it came from, so a claim can always be traced back
to the person who made it.

**Why provenance is the whole design.** `resume/verify.py` blocks a number that
was not in the original bullet, which is exactly right while `master.json` is
the only source — anything new is invented by definition. The moment the corpus
grows, that test blocks the person's own words. Replacing it with trust blocks
nothing at all. So the test becomes "did they tell us this, and when", which is
a row lookup, not a judgment.

Nothing here calls a model. `capture()` does, and it is the only thing that
does, so everything below is testable without a provider.
"""

from __future__ import annotations

import re
import sqlite3

import llm
from engine import db

KINDS = ("accomplishment", "skill", "metric", "role", "education")

#: Imported, not copied. A mirrored regex that drifts would let a fact be
#: supported here and reverted there, which reads as the guard being broken
#: rather than strict. `verify` imports nothing from this module, so there is
#: no cycle.
from resume import verify as _verify        # noqa: E402


def _conn(conn: sqlite3.Connection | None) -> tuple[sqlite3.Connection, bool]:
    return (conn, False) if conn is not None else (db.connect(), True)


def add(kind: str, text: str, *, company: str | None = None,
        source: str = "chat", heard_in: str | None = None,
        conn: sqlite3.Connection | None = None) -> bool:
    """One claim. Returns whether it was new.

    Duplicates are ignored rather than rejected: the same fact arriving twice
    is ordinary — they mention a number again, or a resume is re-parsed — and
    it is not worth an error.
    """
    text = (text or "").strip()
    if not text:
        return False
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}; one of {KINDS}")
    c, close = _conn(conn)
    try:
        cur = c.execute(
            "INSERT OR IGNORE INTO career (kind, text, company, source, heard_in) "
            "VALUES (?, ?, ?, ?, ?)", (kind, text, company, source, heard_in))
        c.commit()
        return cur.rowcount > 0
    finally:
        if close:
            c.close()


def all(kind: str | None = None, company: str | None = None,
        conn: sqlite3.Connection | None = None) -> list[dict]:
    c, close = _conn(conn)
    try:
        sql = "SELECT * FROM career"
        where, args = [], []
        if kind:
            where.append("kind = ?")
            args.append(kind)
        if company:
            where.append("LOWER(company) = LOWER(?)")
            args.append(company)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY said_at, id"
        return [dict(r) for r in c.execute(sql, args).fetchall()]
    finally:
        if close:
            c.close()


def _facts(text: str) -> set[str]:
    """Everything in one claim that `verify` would call a fact.

    Both kinds, because both get blocked. A number is the obvious one; a tool
    or product name is the one that bit — told "I used Amplitude daily", the
    store held the skill and the guard still reverted the bullet, because
    `supported` had only been threaded into the number check.
    """
    return _verify._numbers(text) | _verify._propers(text)


def supported(conn: sqlite3.Connection | None = None) -> set[str]:
    """Every fact they have ever stated, normalized the way verify.py reads it.

    This is what lets a resume say something the uploaded PDF does not: they
    said it, it is on the record, and the record can be shown back to them.
    """
    out: set[str] = set()
    for row in all(conn=conn):
        out |= _facts(row["text"])
    return out


def provenance(fact: str, conn: sqlite3.Connection | None = None) -> dict | None:
    """Which claim backs this number or name, and what they actually typed.

    A guard that only says no is a guard nobody can argue with. This is how the
    agent answers "where did 40% come from?" with their own sentence.
    """
    want = str(fact).replace(" ", "").lower().rstrip(".")
    for row in all(conn=conn):
        facts = _facts(row["text"])
        if want in facts or want in {f.lower() for f in facts}:
            return row
    return None


def seed_from_master(master: dict, conn: sqlite3.Connection | None = None) -> int:
    """Put the uploaded resume on the record too.

    Without this the store starts empty and the first generated resume would be
    everything they mentioned in chat and nothing they actually did. Sourced
    `resume`, so a later read can always tell the parsed document from the
    conversation.
    """
    n = 0
    c, close = _conn(conn)
    try:
        for job in master.get("experience", []) or []:
            company = job.get("company")
            if job.get("title"):
                n += add("role", f"{job['title']} at {company}", company=company,
                         source="resume", conn=c)
            for bullet in job.get("bullets", []) or []:
                n += add("accomplishment", bullet, company=company,
                         source="resume", conn=c)
        for skill in master.get("skills", []) or []:
            n += add("skill", str(skill), source="resume", conn=c)
        for e in master.get("education", []) or []:
            text = e if isinstance(e, str) else " ".join(
                str(v) for v in e.values() if v)
            n += add("education", text, source="resume", conn=c)
        return n
    finally:
        if close:
            c.close()


# --- the one part that calls a model ---------------------------------------

#: Messages that cannot carry a durable fact. Checked before the model, because
#: a conversation is mostly these and paying for "ok" is how a per-message call
#: becomes the most expensive thing in the product.
_TRIVIAL = re.compile(
    r"^\s*(?:hi|hey|hello|ok|okay|k|yes|no|yeah|yep|nope|thanks|ty|sure|cool|"
    r"got it|nice|great|hmm+|\?+|apply to \d+|tell me about \d+|more|next)\s*[.!?]*\s*$",
    re.I)


def capture(message: str, conn: sqlite3.Connection | None = None) -> list[dict]:
    """Pull what is worth keeping out of one message. Best effort, never raises.

    One model call reads the message for three things: career facts (rows
    here, a resume is built from them), preferences (`memory/prefs.py`, a
    search is steered by them) and guesses at form facts like notice period
    (staged for the nightly pass, never straight to the vault). One call, not
    three, because this runs before every reply and each call is seconds.

    Returns the career facts added, as before.

    Best effort on purpose: this runs on every user message, and a capture that
    fails must cost the person nothing. A dropped fact can be said again; a turn
    that dies because the extractor hiccuped cannot be undone.
    """
    text = (message or "").strip()
    if not text or len(text) < 6 or _TRIVIAL.match(text):
        return []
    from memory import prefs
    import vault
    on_record = prefs.active(conn)
    ask = text
    if on_record:
        # With ids, so "actually consulting is fine now" can take back the
        # exact row. A retraction by matching text could remove the wrong one.
        ask += ("\n\n---\nPreferences on record (id · kind · text):\n"
                + "\n".join(f"{r['id']} · {r['kind']} · {r['text']}"
                             for r in on_record))
    try:
        found = llm.complete_json(llm.prompt("career"), ask, max_tokens=1024)
    except Exception:                                  # noqa: BLE001
        return []
    if isinstance(found, list):            # the shape before preferences
        found = {"career": found}
    if not isinstance(found, dict):
        return []

    out = []
    for item in found.get("career") or []:
        if not isinstance(item, dict) or item.get("kind") not in KINDS:
            continue
        claim = str(item.get("text") or "").strip()
        if not claim:
            continue
        # `heard_in` is their message, not the model's paraphrase of it. The
        # paraphrase is what goes on the resume; the raw sentence is what gets
        # shown back when they ask where a number came from.
        if add(item["kind"], claim, company=item.get("company") or None,
               source="chat", heard_in=text, conn=conn):
            out.append({**item, "text": claim})

    for item in found.get("preferences") or []:
        if isinstance(item, dict):
            prefs.add(str(item.get("kind") or ""), str(item.get("text") or ""),
                      stated=item.get("stated", True) is not False,
                      heard_in=text, conn=conn)
    live = {r["id"] for r in on_record}
    prefs.retract([i for i in found.get("retract") or [] if i in live], conn=conn)

    # A guess at a form fact, as "key: value". It used to be the whole message
    # (27 rows of "hi" and "why did you ask that?"), which left the nightly
    # pass nothing to promote and plenty to misread.
    for g in found.get("vault_guesses") or []:
        if isinstance(g, dict) and g.get("key") and g.get("value"):
            said = " (stated)" if g.get("stated") else " (inferred)"
            vault.stage(f"{g['key']}: {g['value']}{said}", context=text, conn=conn)
    return out
