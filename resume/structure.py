"""Their resume's shape: which sections, in what order, under which heading,
and a one-line headline under the name.

Sai's eval sheet (rows 34-35, 2026-09-28): "the user should be able to make
necessary changes in structure, headline and what not, it's his resume". A
layout is the look; this is the shape, and it is theirs to set.

Stored as one vault row, so it applies to every build, tailored or not, until
they change it. Headings are picked from a short list of names recruiters'
software already reads (Research B, rule 9): "My Journey" as a heading is a
section an ATS files under nothing.
"""

from __future__ import annotations

import json
import re
import sqlite3

import vault
from resume import layouts as L

KEY = "resume_structure"

#: Words people use for a section, mapped to its key.
_WORDS = [
    ("summary", r"summary|profile|about|objective|intro"),
    ("experience", r"experience|work|employment|jobs?|career history"),
    ("projects", r"projects?"),
    ("education", r"education|degree|college|academic"),
    ("positions", r"positions?|responsibility|leadership|por"),
    ("achievements", r"achievements?|awards?|honou?rs?"),
    ("certifications", r"certifications?|certificates?|courses?|licen[cs]es?"),
    ("skills", r"skills?|tools|tech stack"),
]

#: Headings an ATS files correctly, per section. The first is the default.
TITLES = {
    "summary": ["Summary", "Profile", "Professional Summary", "Career Summary",
                "About Me"],
    "experience": ["Experience", "Work Experience", "Professional Experience",
                   "Employment History", "Career History"],
    "projects": ["Projects", "Key Projects", "Personal Projects", "Side Projects"],
    "education": ["Education", "Academic Background"],
    "positions": ["Positions of Responsibility", "Leadership",
                  "Leadership Experience"],
    "achievements": ["Achievements", "Awards", "Awards and Achievements",
                     "Honors"],
    "certifications": ["Certifications", "Licenses and Certifications",
                       "Courses"],
    "skills": ["Skills", "Core Skills", "Key Skills", "Technical Skills",
               "Tools"],
}

HEADLINE_MAX = 90


def section(word: str) -> str | None:
    w = (word or "").strip().lower()
    if w in L.STANDARD_ORDER:
        return w
    for key, rx in _WORDS:
        if re.search(rf"\b(?:{rx})\b", w):
            return key
    return None


def get(conn: sqlite3.Connection | None) -> dict:
    """{} when nothing is set, or no database was given (a test building a
    resume from a dict must never read a real profile)."""
    if conn is None:
        return {}
    try:
        return json.loads(vault.get(KEY, conn) or "{}")
    except ValueError:
        return {}


def apply(master: dict, conn: sqlite3.Connection | None) -> dict:
    """The master with their shape attached, for render to read."""
    s = get(conn)
    if not s:
        return master
    out = dict(master)
    if s.get("order"):
        out["_order"] = list(s["order"])
    if s.get("titles"):
        out["_titles"] = dict(s["titles"])
    if s.get("headline"):
        out["headline"] = s["headline"]
    return out


def update(conn: sqlite3.Connection, *, order: list[str] | None = None,
           rename: dict[str, str] | None = None, hide: list[str] | None = None,
           show: list[str] | None = None, headline: str | None = None,
           layout: str | None = None) -> tuple[dict, list[str]]:
    """Merge a change into what is saved. Returns (saved, problems): a
    problem is something they asked for that was not done, with why, so the
    reply can say so instead of claiming it moved."""
    s = get(conn)
    problems: list[str] = []
    current = list(s.get("order") or L.get(layout).get("sections")
                   or L.STANDARD_ORDER)

    def keys(words, what):
        out = []
        for w in words or []:
            k = section(w)
            if k is None:
                problems.append(f"{what} {w!r}: no such section. Sections: "
                                + ", ".join(L.STANDARD_ORDER))
            elif k not in out:
                out.append(k)
        return out

    for k in keys(show, "show"):
        if k not in current:
            current.append(k)
    for k in keys(hide, "hide"):
        if k in current:
            current.remove(k)
    first = [k for k in keys(order, "move") if k in current]
    if first:
        current = first + [k for k in current if k not in first]
    if order or hide or show:
        s["order"] = current

    titles = dict(s.get("titles") or {})
    for word, title in (rename or {}).items():
        k = section(word)
        if k is None:
            problems.append(f"rename {word!r}: no such section")
            continue
        ok = {t.lower(): t for t in TITLES[k]}
        if (title or "").strip().lower() not in ok:
            problems.append(f"heading {title!r} for {k}: not used, because "
                            f"recruiters' software may not read it. Options: "
                            + ", ".join(TITLES[k]))
            continue
        titles[k] = ok[title.strip().lower()]
    if titles:
        s["titles"] = titles

    if headline is not None:
        h = re.sub(r"\s+", " ", headline).strip().replace("—", "|")
        if len(h) > HEADLINE_MAX:
            problems.append(f"headline is {len(h)} characters; keep it under "
                            f"{HEADLINE_MAX} so it stays one line")
        elif h:
            s["headline"] = h
        else:
            s.pop("headline", None)

    vault.put(KEY, json.dumps(s), source="user", conn=conn)
    return s, problems


def describe(s: dict, layout: str | None = None) -> str:
    """The shape in one line each, as the next build will print it."""
    spec = L.get(layout)
    order = s.get("order") or spec.get("sections") or L.STANDARD_ORDER
    titles = {**L.TITLES, **spec.get("titles", {}), **(s.get("titles") or {})}
    hidden = [k for k in L.STANDARD_ORDER if k not in order]
    return ("Sections, top to bottom: " + " → ".join(titles[k] for k in order)
            + (f"\nHidden: {', '.join(titles[k] for k in hidden)}" if hidden else "")
            + f"\nHeadline under the name: {s.get('headline') or '(none)'}"
            + "\nSections with nothing in them are skipped when printed.")
