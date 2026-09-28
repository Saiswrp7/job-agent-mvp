"""The thread of a conversation, kept when its turns are not.

Before this, a long chat lost its early turns outright: past ~30k characters
`chat.compact` dropped them and left a note saying the durable facts were
elsewhere. Facts were; the thread was not. "We were comparing 2 and 4", "you
said you'd think about Meesho" went with the turns. And a new session started
blank.

So each session keeps a few lines of running notes (`summaries` table), made
from the transcript in `messages`, not from the agent's history: the
transcript holds what was said, without the tool payloads and reference tables
the history carries. Refreshed only when turns are trimmed, and only over the
turns it has not read yet. A new session starts from the last one's notes.

A failed refresh keeps the notes it had. Losing a refresh costs some thread;
failing a turn over it would cost the conversation.
"""

from __future__ import annotations

import sqlite3

import llm


def get(session: str, conn: sqlite3.Connection) -> tuple[str, int]:
    row = conn.execute("SELECT summary, upto_turn FROM summaries WHERE session=?",
                       (session,)).fetchone()
    return (row["summary"], row["upto_turn"]) if row else ("", 0)


def save(session: str, summary: str, upto_turn: int,
         conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO summaries (session, summary, upto_turn) VALUES (?, ?, ?) "
        "ON CONFLICT(session) DO UPDATE SET summary=excluded.summary, "
        "upto_turn=excluded.upto_turn, updated_at=CURRENT_TIMESTAMP",
        (session, summary, upto_turn))
    conn.commit()


def _turns(session: str, after: int, upto: int, conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        "SELECT turn, role, content FROM messages WHERE session=? "
        "AND turn > ? AND turn <= ? ORDER BY id", (session, after, upto))
    return "\n".join(f"[{r['turn']}] {'them' if r['role'] == 'user' else 'you'}: "
                     f"{r['content'][:1500]}" for r in rows)


def refresh(session: str, upto_turn: int, conn: sqlite3.Connection) -> str:
    """Fold turns (seen, upto_turn] into the notes. Returns the notes."""
    notes, seen = get(session, conn)
    if upto_turn <= seen:
        return notes
    new = _turns(session, seen, upto_turn, conn)
    if not new:
        return notes
    try:
        updated = llm.complete(
            llm.prompt("summary"),
            f"Notes so far:\n{notes or '(none)'}\n\nTurns since:\n{new}",
            max_tokens=800, effort="low").strip()
    except Exception:                                  # noqa: BLE001
        return notes
    if updated:
        save(session, updated, upto_turn, conn)
        return updated
    return notes


def carry_over(session: str, conn: sqlite3.Connection) -> str:
    """Notes from the last session before this one, for its first turn.

    A session that ended without ever being trimmed has no notes yet, so they
    are made here from its transcript, once. Web sessions have no "ended"
    moment to hook, and the start of the next one is the first time anyone
    needs them.
    """
    row = conn.execute(
        "SELECT session, MAX(turn) AS last FROM messages WHERE session != ? "
        "GROUP BY session ORDER BY MAX(id) DESC LIMIT 1", (session,)).fetchone()
    if row is None or not row["session"]:
        return ""
    # No model call here: this runs while they wait for the first reply, and
    # the summary call made "Hie" take 16 s instead of 7 (2026-09-28). Turns
    # the notes have not folded in yet go in as they were said, clipped; the
    # next compaction summarises them properly.
    notes, seen = get(row["session"], conn)
    new = _turns(row["session"], seen, row["last"] or 0, conn)
    if not new:
        return notes
    return (f"{notes}\n\nLast turns, as said:\n{new[-2500:]}").strip()
