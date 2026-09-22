"""Typed facts, retrieved by key.

The rule this file exists for: **if a wrong value causes an irreversible
action, it is a database row, not prose.** A markdown blob is a fine place for
"prefers early-stage" and a terrible place for expected CTC, because a model
reads prose and paraphrases it, and a paraphrased salary lands on a submitted
application.

Two write paths, split by who decided:

- `put()`   — the user stated it. Writes now, and the caller echoes it back.
- `stage()` — the model inferred it. Waits for the nightly reconciler.

The echo is the safety mechanism, not the delay. Delay only protects the facts
the user never actually said.
"""

from __future__ import annotations

import re
import sqlite3

from engine import db

#: Canonical keys. Form fields are matched against these aliases, so a new ATS
#: wording is one line here rather than a code change.
FIELDS: dict[str, list[str]] = {
    "full_name":        ["name", "full name", "first and last"],
    "first_name":       ["first name", "given name"],
    "last_name":        ["last name", "surname", "family name"],
    "email":            ["email", "e-mail"],
    "phone":            ["phone", "mobile", "contact number"],
    "location":         ["location", "current location", "where are you based"],
    "linkedin":         ["linkedin"],
    "portfolio":        ["portfolio", "website", "personal site"],
    "current_company":  ["current company", "present employer"],
    "current_title":    ["current title", "current role", "designation"],
    "years_experience": ["years of experience", "total experience", "yoe"],
    "notice_period":    ["notice period", "when can you join", "availability"],
    "current_ctc":      ["current ctc", "current salary", "present compensation"],
    "expected_ctc":     ["expected ctc", "expected salary", "compensation expectation"],
    "work_authorization": ["work authorization", "authorized to work", "visa",
                           "right to work"],
    "sponsorship":      ["sponsorship", "require sponsorship"],
    "gender":           ["gender"],
    "ethnicity":        ["ethnicity", "race"],
    "veteran_status":   ["veteran"],
    "disability":       ["disability"],
    "referral":         ["how did you hear", "referral", "source"],
}

_NORM = re.compile(r"[^a-z0-9 ]+")


def _norm(s: str) -> str:
    return _NORM.sub(" ", (s or "").lower()).strip()


#: (alias, key) longest-first. Order is the whole correctness story here:
#: "name" is a substring of "first name", so a dict-order scan maps "First
#: Name" onto full_name and types the wrong value into a real form.
_ALIASES: list[tuple[str, str]] = sorted(
    ([(_norm(a), k) for k, al in FIELDS.items() for a in al]
     + [(_norm(k), k) for k in FIELDS]),
    key=lambda t: -len(t[0]),
)


def match_key(label: str) -> str | None:
    """Map a form field's label onto a canonical vault key, or None.

    None is the important return: it means the agent must ask rather than
    guess, which is the whole contract of the apply loop.
    """
    n = _norm(label)
    if not n:
        return None
    for alias, key in _ALIASES:
        if alias == n:
            return key
    for alias, key in _ALIASES:
        if re.search(rf"\b{re.escape(alias)}\b", n):
            return key
    return None


def put(key: str, value: str, *, source: str = "user",
        conn: sqlite3.Connection | None = None) -> None:
    close_after = conn is None
    conn = conn or db.connect()
    try:
        conn.execute(
            "INSERT INTO vault (key, value, source, updated_at) "
            "VALUES (?, ?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "source=excluded.source, updated_at=CURRENT_TIMESTAMP",
            (key, str(value), source),
        )
        conn.commit()
    finally:
        if close_after:
            conn.close()


def get(key: str, conn: sqlite3.Connection | None = None) -> str | None:
    close_after = conn is None
    conn = conn or db.connect()
    try:
        row = conn.execute("SELECT value FROM vault WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None
    finally:
        if close_after:
            conn.close()


def slice_for(labels: list[str], conn: sqlite3.Connection | None = None) -> dict:
    """Only the keys this form actually asked about.

    Never hand the whole vault to an agent — it is both a context cost and a
    way for an unrelated value to end up in a box nobody checked.
    """
    close_after = conn is None
    conn = conn or db.connect()
    try:
        out = {}
        for label in labels:
            key = match_key(label)
            if key:
                val = get(key, conn)
                if val is not None:
                    out[label] = val
        return out
    finally:
        if close_after:
            conn.close()


def one_pager(conn: sqlite3.Connection | None = None) -> str:
    """Four lines for the chat agent's context. Not the whole vault."""
    keys = ["full_name", "current_title", "current_company", "location",
            "years_experience", "notice_period", "expected_ctc"]
    close_after = conn is None
    conn = conn or db.connect()
    try:
        bits = [f"{k}: {get(k, conn)}" for k in keys if get(k, conn)]
        return " · ".join(bits) if bits else "(nothing known yet)"
    finally:
        if close_after:
            conn.close()


def stage(fact: str, context: str = "", conn: sqlite3.Connection | None = None) -> None:
    """An inferred fact. Goes nowhere near the vault until reconcile() runs."""
    close_after = conn is None
    conn = conn or db.connect()
    try:
        conn.execute("INSERT INTO staging (fact, context) VALUES (?, ?)",
                     (fact, context))
        conn.commit()
    finally:
        if close_after:
            conn.close()


def all_keys(conn: sqlite3.Connection | None = None) -> dict:
    close_after = conn is None
    conn = conn or db.connect()
    try:
        return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM vault")}
    finally:
        if close_after:
            conn.close()
