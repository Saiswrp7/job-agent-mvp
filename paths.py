"""Where *this user's* data lives. One switch, in one file.

Separating the code from the data it accumulates, so a second profile can exist
without a second checkout:

    JOB_AGENT_HOME=profiles/fresh python3 cli.py web

**Why this is one constant and not seven.** The obvious version of this feature
is an override for the database alone, and it is wrong in the specific way
every bug on this project has been wrong: two individually-correct things
meeting at a seam. Point `jobs.db` somewhere new and the vault is empty — but
`resume/master.json` is still on disk, so the agent greets a brand-new user
with "a resume is on file, no need to send it again", reads the old one, and
tailors it. Half a reset is worse than none, because the half that survived is
invisible.

So everything the user accumulates derives from `HOME`, and nothing else does.
The split is by *origin*, not by file type:

  data (moves)   the database, the parsed resume, the learned profile,
                 generated PDFs, screenshots, uploads, the last search
  code (stays)   prompts/, engine/boards.json, resume/layouts/,
                 fixtures/, .env

`.env` stays with the code on purpose: an API key is a property of the machine,
not of the person being searched for, and copying a profile should never copy a
secret.

Read from the real environment only, never from `.env`. `.env` is parsed by
`llm.py` at import, which is *after* several modules have already resolved
their paths — a home that took effect in some modules and not others is the
exact failure this file exists to prevent. It has to be set before the process
starts, where it is also visible in the shell history that ran it.
"""

from __future__ import annotations

import os
from pathlib import Path

#: The checkout. Code and shared reference data live here, always.
ROOT = Path(__file__).resolve().parent


def _home() -> Path:
    raw = (os.environ.get("JOB_AGENT_HOME") or "").strip()
    if not raw:
        return ROOT
    p = Path(raw).expanduser()
    # A relative home resolves against the checkout, not the shell's cwd, so
    # `cd` between commands cannot silently move a profile.
    return p if p.is_absolute() else (ROOT / p).resolve()


HOME = _home()

#: True when running against the default profile — the one holding real
#: applications and a real resume. Surfaces print this; see `label()`.
IS_DEFAULT = HOME == ROOT

DB = HOME / "jobs.db"

ARTIFACTS = HOME / "artifacts"
LAST_SEARCH = ARTIFACTS / "last_search.json"
RESUMES = ARTIFACTS / "resumes"
SHOTS = ARTIFACTS / "screenshots"
UPLOADS = ARTIFACTS / "uploads"

MASTER = HOME / "resume" / "master.json"
PROFILE = HOME / "memory" / "profile.md"
PLATFORMS = HOME / "memory" / "platforms"

#: Code and shared reference data. Never per-user.
#: The jobs every user searches. It is the default profile's database, so the
#: default profile needs no change; every other profile attaches it.
#: `JOB_AGENT_SHARED` moves it, for tests only — so a two-user test never
#: attaches the real jobs table.
SHARED_DB = Path(os.environ.get("JOB_AGENT_SHARED") or ROOT / "jobs.db")
PROMPTS = ROOT / "prompts"
BOARDS = ROOT / "engine" / "boards.json"
TEMPLATE = ROOT / "resume" / "layouts" / "base.tex"
FIXTURES = ROOT / "fixtures"


def ensure() -> None:
    """Create the directories a profile needs. Idempotent, cheap, safe to call
    on every connect — a missing parent must never be the reason a tailored
    resume or a screenshot is lost."""
    for d in (ARTIFACTS, RESUMES, SHOTS, UPLOADS, MASTER.parent, PROFILE.parent):
        d.mkdir(parents=True, exist_ok=True)


def label() -> str:
    """Which profile is live, for anything a person reads.

    Worth printing everywhere: this process can submit real applications, and
    "which data am I in" must never be a question you answer by guessing.
    """
    return "default" if IS_DEFAULT else f"{HOME.name} ({HOME})"
