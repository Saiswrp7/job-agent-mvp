"""What they want and what they avoid, as rows.

A preference said in chat used to live only in the conversation. The nightly
pass that was meant to lift it into `profile.md` has never run, so "no
consulting" on turn 5 was gone by turn 40, while the trim note told the model
it was "in the profile". These rows are written on the turn it is said, by the
same extraction call that writes career facts (`career.capture`).

Taking one back is by id, never by matching text. The model is shown the live
rows with their ids and names the one to retract; a content match could
remove a line nobody asked to remove.
"""

from __future__ import annotations

import sqlite3

from engine import db

KINDS = ("want", "avoid", "constraint")


def _conn(conn):
    return (conn, False) if conn is not None else (db.connect(), True)


def add(kind: str, text: str, *, stated: bool = True, heard_in: str | None = None,
        conn: sqlite3.Connection | None = None) -> bool:
    """False when it was already on record. Saying it again un-retracts it, and
    an inferred line they now state outright is upgraded to stated."""
    if kind not in KINDS or not (text or "").strip():
        return False
    conn, close = _conn(conn)
    try:
        row = conn.execute("SELECT id, stated, retracted_at FROM preferences "
                           "WHERE kind=? AND text=?", (kind, text.strip())).fetchone()
        if row is None:
            conn.execute("INSERT INTO preferences (kind, text, stated, heard_in) "
                         "VALUES (?, ?, ?, ?)",
                         (kind, text.strip(), int(stated), heard_in))
            conn.commit()
            return True
        if row["retracted_at"] or (stated and not row["stated"]):
            conn.execute("UPDATE preferences SET retracted_at=NULL, "
                         "stated=MAX(stated, ?), heard_in=COALESCE(?, heard_in), "
                         "said_at=CURRENT_TIMESTAMP WHERE id=?",
                         (int(stated), heard_in, row["id"]))
            conn.commit()
            return True
        return False
    finally:
        if close:
            conn.close()


def retract(ids: list[int], conn: sqlite3.Connection | None = None) -> int:
    """Kept, marked. A preference they dropped is still worth knowing they once
    held, and a mistaken retraction is undone by saying it again."""
    ids = [int(i) for i in ids if str(i).isdigit()]
    if not ids:
        return 0
    conn, close = _conn(conn)
    try:
        n = conn.execute(
            f"UPDATE preferences SET retracted_at=CURRENT_TIMESTAMP "
            f"WHERE retracted_at IS NULL AND id IN ({','.join('?' * len(ids))})",
            ids).rowcount
        conn.commit()
        return n
    finally:
        if close:
            conn.close()


def active(conn: sqlite3.Connection | None = None) -> list[dict]:
    conn, close = _conn(conn)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT id, kind, text, stated FROM preferences "
            "WHERE retracted_at IS NULL ORDER BY id")]
    finally:
        if close:
            conn.close()


def render(conn: sqlite3.Connection | None = None) -> str:
    """For a prompt. Inferred lines say so, so the model treats them as a lean
    rather than a rule."""
    rows = active(conn)
    return "\n".join(f"- {r['kind']}: {r['text']}"
                     + ("" if r["stated"] else " (inferred)") for r in rows)
