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
    "middle_name":      ["middle name"],
    "last_name":        ["last name", "surname", "family name"],
    "date_of_birth":    ["date of birth", "dob", "birth date", "birthday"],
    "email":            ["email", "e-mail"],
    "phone":            ["phone", "mobile", "contact number"],
    "location":         ["location", "current location", "where are you based", "city",
                         "current city"],
    "country":          ["country", "country region", "country of residence",
                         "which country", "where are you from"],
    "linkedin":         ["linkedin"],
    "portfolio":        ["portfolio", "website", "personal site"],
    "current_company":  ["current company", "present employer"],
    "current_title":    ["current title", "current role", "designation"],
    "years_experience": ["years of experience", "total experience", "yoe",
                         "relevant experience", "experience in years", "work experience",
                         "experience years"],
    "notice_period":    ["notice period", "when can you join", "availability",
                         "available to join", "time to join", "days to join",
                         "how soon can you join", "earliest joining"],
    "current_ctc":      ["current ctc", "current salary", "present compensation",
                         "total ctc"],
    "current_fixed_ctc": ["fixed salary", "current fixed", "fixed ctc", "fixed pay"],
    "current_variable_ctc": ["variable salary", "current variable", "variable pay",
                             "variable ctc"],
    "expected_ctc":     ["expected ctc", "expected salary", "compensation expectation"],
    "work_authorization": ["work authorization", "authorized to work", "visa",
                           "right to work"],
    "sponsorship":      ["sponsorship", "require sponsorship"],
    "relocate":         ["relocate", "relocation"],
    "gender":           ["gender"],
    "ethnicity":        ["ethnicity", "race"],
    "veteran_status":   ["veteran"],
    "disability":       ["disability"],
    "referral":         ["how did you hear", "referral", "source"],
}

_NORM = re.compile(r"[^a-z0-9 ]+")


def _norm(s: str) -> str:
    # Runs of space are one: "Experience (in years)" left "experience  in
    # years" and matched no alias (Keka, 2026-10-01).
    return re.sub(r"\s+", " ", _NORM.sub(" ", (s or "").lower())).strip()


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
    # The question decides, not the longest word in it: "Are you willing to
    # relocate if you are in a different location?" matched `location` and
    # was answered with the city (apply eval, 2026-10-01).
    if re.search(r"\breloca", n):
        return "relocate"
    # The small boxes beside a real answer are not that answer: "Current
    # Salary (currency)" is INR, not 12 LPA; "Mobile Phone (country code)" is
    # +91, not the number; "Experience (months)" is not 5 years (Keka, Loop).
    if re.search(r"\b(currency|country code|dial(ing)? code|isd)\b", n):
        return None
    if re.search(r"\bmonths?\b", n) and "experience" in n:
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
            n = _norm(label)
            if re.search(r"\b(country code|dial(ing)? code|isd)\b", n):
                code = _country_code(get("phone", conn))
                if code:
                    out[label] = code
                continue
            key = match_key(label)
            if key:
                if key in _NAME_PARTS and _guessed_name(key, conn):
                    continue                  # ask once; a guess is not their answer
                val = get(key, conn)
                if key == "country" and not val:
                    val = country_of(get("phone", conn))      # +91 is India
                if key == "location" and val and is_country(val):
                    continue                  # a country is not a city: ask
                if val is not None:
                    out[label] = val
        return out
    finally:
        if close_after:
            conn.close()


_NAME_PARTS = ("first_name", "middle_name", "last_name")


def _guessed_name(key: str, conn: sqlite3.Connection) -> bool:
    """A name part the resume reader guessed, for a name it cannot split.

    "BUGATA SAI SWAROOP" became first name BUGATA, last name SWAROOP, and
    the form got "Middle Name: BUGATA SAI SWAROOP" (Loop, 2026-10-01). With
    three or more words nobody can tell first from last from the letters
    alone, so such a guess is treated as unknown and the person is asked."""
    row = conn.execute("SELECT source FROM vault WHERE key = ?", (key,)).fetchone()
    if not row or row["source"] != "resume":
        return False
    full = get("full_name", conn) or ""
    return len(full.split()) >= 3


#: Countries a person answers "where are you from" with. Kept apart from the
#: city: Konovo asked the country, "India" was saved over "Bengaluru", and
#: Swiggy's next form went out with City: India (2026-10-01).
_COUNTRIES = {"india", "united states", "usa", "us", "united kingdom", "uk", "canada",
              "singapore", "uae", "united arab emirates", "germany", "australia",
              "netherlands", "france", "ireland", "japan", "bharat"}


def is_country(value: str | None) -> bool:
    return _norm(value or "") in _COUNTRIES


def country_of(phone: str | None) -> str | None:
    """The country a phone number on file belongs to, for "Country" boxes."""
    return "India" if _country_code(phone) == "+91" else None


def _country_code(phone: str | None) -> str | None:
    """+91 for an Indian number on file, the way a country-code list names it."""
    d = re.sub(r"\D", "", phone or "")
    if (phone or "").strip().startswith("+91") or (len(d) == 12 and d.startswith("91")):
        return "+91"
    return None


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
