"""Applications run here, beside the chat — never inside its turn.

Before this, "apply to CRED" held the chat for four minutes: two and a half
building a tailored resume, then the form, and the person saw nothing until
all of it was over. AGENTS.md always said `start_application` "hands off,
returns immediately"; it never did.

Now the chat starts a run and replies at once. The run lives in a thread with
its own database connection and its own Chrome tab, parks or finishes on its
own, and leaves its outcome on the `applications` row. The chat reads that row
on the next turn and says how it went.

Rules this file keeps:

- **At most `MAX_PARALLEL` at once.** Each is a tab in the person's Chrome and
  a stream of model calls. The rest wait as `queued`.
- **An outcome is reported once.** `reported = 0` marks one the person has not
  been told about; the chat flips it when it tells them.
- **A run that dies says so.** Any exception becomes `failed` with its reason,
  never a row stuck at `running`.
- **A restart cannot leave ghosts.** Runs live in this process, so on start-up
  anything still `running` or `queued` was cut off; `recover` marks it.
"""

from __future__ import annotations

import threading
from typing import Callable

from engine import db

MAX_PARALLEL = 3
_slots = threading.BoundedSemaphore(MAX_PARALLEL)
_threads: dict[int, threading.Thread] = {}
#: Runs the person called off. Checked by the harness before every step, so a
#: run stops at its next step and never reaches Submit (Sai's "not from this"
#: left four unwanted runs going, 2026-10-05).
_cancelled: set[int] = set()


def cancel(conn, app_id: int) -> str:
    """Call off one application. Returns its status before."""
    row = conn.execute("SELECT status FROM applications WHERE id = ?", (app_id,)).fetchone()
    if row is None:
        return "missing"
    before = row[0]
    if before in ("submitted", "unconfirmed"):
        return before                       # gone or maybe gone: nothing to stop
    _cancelled.add(app_id)
    conn.execute("UPDATE applications SET status = 'cancelled', question = NULL, "
                 "outcome = 'cancelled by them, nothing sent', reported = 1, "
                 "updated_at = CURRENT_TIMESTAMP WHERE id = ?", (app_id,))
    conn.commit()
    return before


def cancelled(app_id: int) -> bool:
    return app_id in _cancelled


def _set(conn, app_id: int, status: str, outcome: str | None = None) -> None:
    conn.execute(
        "UPDATE applications SET status = ?, outcome = COALESCE(?, outcome), "
        "reported = CASE WHEN ? IS NULL THEN reported ELSE 0 END, "
        "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (status, outcome, outcome, app_id))
    conn.commit()


def _run(app_id: int, job: dict, work: Callable, connect: Callable) -> None:
    conn = connect()
    try:
        _set(conn, app_id, "queued")
        with _slots:
            _set(conn, app_id, "running")
            r = work(conn)
        status = r.get("status", "failed")
        if status == "cancelled" or cancelled(app_id):
            return                          # cancel() already wrote the row
        message = r.get("question") or r.get("message") or r.get("detail") or ""
        # The harness has already written the status; this adds what the
        # person should hear, and marks it unreported.
        _set(conn, app_id, status, message or f"finished: {status}")
    except Exception as exc:                          # noqa: BLE001
        # A RuntimeError is ours and already says it in words; others keep
        # their type, which is the useful part of an unexpected crash.
        why = str(exc) if type(exc) is RuntimeError else f"{type(exc).__name__}: {exc}"
        _set(conn, app_id, "failed", f"stopped: {why}")
    finally:
        conn.close()
        _threads.pop(app_id, None)


def start(app_id: int, job: dict, work: Callable,
          connect: Callable = db.connect) -> threading.Thread:
    """Run `work(conn) -> result dict` for this application in the background.

    `connect` opens the thread's own database connection — a sqlite
    connection belongs to the thread that made it. Tests pass a temporary one.
    """
    t = threading.Thread(target=_run, args=(app_id, job, work, connect),
                         name=f"apply-{app_id}", daemon=True)
    _threads[app_id] = t
    t.start()
    return t


def running() -> list[int]:
    return [i for i, t in _threads.items() if t.is_alive()]


def recover(conn) -> int:
    """Mark runs a restart cut off. Called once when a surface starts."""
    cur = conn.execute(
        "UPDATE applications SET status = 'failed', reported = 0, "
        "outcome = 'interrupted — the app restarted while this was running. "
        "Say retry to start it again.' "
        "WHERE status IN ('running', 'queued')")
    conn.commit()
    return cur.rowcount


def unreported(conn) -> list[dict]:
    """Finished or parked runs the person has not been told about. Reading
    them marks them told — the chat puts every one it reads in front of the
    agent, which is told to say them first."""
    rows = [dict(r) for r in conn.execute(
        "SELECT id, company, title, status, outcome FROM applications "
        "WHERE reported = 0 AND outcome IS NOT NULL "
        "AND status NOT IN ('running', 'queued') ORDER BY id")]
    if rows:
        conn.execute(f"UPDATE applications SET reported = 1 WHERE id IN "
                     f"({', '.join('?' for _ in rows)})", [r["id"] for r in rows])
        conn.commit()
    return rows
