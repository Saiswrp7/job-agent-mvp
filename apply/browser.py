"""The six functions the apply agent sees. The browser sits behind them.

**Why this file exists at all.** The agent's tool list must never contain a
Jev call or a Playwright selector. Keep the seam here and swapping the backend
is one file; skip it and the agent is welded to whatever drove it first.

**Two backends.**

- `JevBrowser` (apply/jev.py) — the real one. Drives the person's own Chrome
  through Jev Browser Control: fast per action, their own logins, and it needs
  Chrome open with the extension on this machine.
- `ManualBrowser` — a recorder for building fixtures. It prints what it would
  do and returns canned answers, so the loop can be exercised with no browser
  at all.

Playwright was the first real backend and was removed on 2026-09-24 in favour
of Jev. The seam is why that was one file.

`submit()` is the one function with a hard refusal. It is deliberate and it
stays: a careless afternoon otherwise fires real applications at real
companies, and that is not recoverable.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import paths

FIXTURES = paths.FIXTURES          # ships with the code
SHOTS = paths.SHOTS                # evidence of this user's runs

SUBMIT_ENV = "ALLOW_SUBMIT"


class SubmitRefused(RuntimeError):
    pass


class Park(Exception):
    """Raised by ask_user. Not an error — the run is suspended, not failed."""

    def __init__(self, question: str):
        super().__init__(question)
        self.question = question


@dataclass
class Field:
    name: str
    label: str
    type: str = "text"
    required: bool = False
    options: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = {"name": self.name, "label": self.label, "type": self.type,
             "required": self.required}
        if self.options:
            d["options"] = self.options
        return d


class BaseBrowser:
    """The interface. Six methods, nothing else."""

    def __init__(self, app_id: int | None = None):
        self.app_id = app_id
        self.filled: dict[str, str] = {}

    # --- the six -------------------------------------------------------
    def read_form(self) -> list[dict]:
        raise NotImplementedError

    def fill_field(self, name: str, value: str) -> str:
        raise NotImplementedError

    def upload_file(self, name: str, path: str) -> str:
        raise NotImplementedError

    #: A question the agent must never put to anyone. On 2026-09-23 a LinkedIn
    #: job link landed on a sign-in page and the agent asked the person for
    #: their LinkedIn password "to fill in this form's password field".
    _SECRET = re.compile(r"pass ?(word|code)|\botp\b|one[- ]time (code|password)"
                         r"|verification code|2fa|two[- ]factor|\bpin\b|cvv|card number",
                         re.I)

    def ask_user(self, question: str) -> str:
        # Never returns. The harness catches Park, saves the log and exits.
        if self._SECRET.search(question or ""):
            # Replaced in code, not discouraged in a prompt: a returned rule
            # holds where a prompt line does not.
            raise Park(self.SIGN_IN)
        raise Park(question)

    #: What a sign-in page turns into. Overridden where the person cannot
    #: reach the browser (the cloud one).
    SIGN_IN = ("This page wants you to sign in before it shows the "
               "application. Log in to that site yourself in Chrome, "
               "then tell me to try again — I never ask for or type "
               "passwords.")

    def submit(self) -> str:
        raise NotImplementedError

    def screenshot(self) -> str:
        raise NotImplementedError

    # --- shared guard --------------------------------------------------
    def current_values(self) -> dict:
        """What the form actually holds right now.

        Overridden by the real browser to read the live DOM. `self.filled` is
        only what we *believe* we typed, and a widget that silently clears
        itself makes those two disagree — which is exactly how a required
        field once reached this guard looking done while the page showed it
        empty.
        """
        return dict(self.filled)

    def _guard_submit(self, fields: list[dict]) -> None:
        values = self.current_values()
        missing = [f["label"] for f in fields
                   if f.get("required") and not values.get(f["name"])]
        if missing:
            raise SubmitRefused(f"required fields still empty: {missing}")
        if os.environ.get(SUBMIT_ENV) != "1":
            raise SubmitRefused(
                f"{SUBMIT_ENV} is not set — refusing to submit a real "
                f"application. Everything up to this point ran."
            )


class ManualBrowser(BaseBrowser):
    """Replays a saved form. Used for tests and for building fixtures.

    Tests run against this, never against a live site, so a test can never
    submit a real application no matter what the env says.
    """

    def __init__(self, fixture: str | Path, app_id: int | None = None):
        super().__init__(app_id)
        path = Path(fixture)
        if not path.exists():
            path = FIXTURES / f"{fixture}.json"
        self.spec = json.loads(path.read_text())
        self.fields = self.spec["fields"]

    def read_form(self) -> list[dict]:
        return self.fields

    def fill_field(self, name: str, value: str) -> str:
        known = {f["name"] for f in self.fields}
        if name not in known:
            return f"ERROR: no field named {name!r}. Fields: {sorted(known)}"
        self.filled[name] = value
        return f"filled {name}"

    def upload_file(self, name: str, path: str) -> str:
        if not Path(path).exists():
            return f"ERROR: file not found: {path}"
        self.filled[name] = path
        return f"uploaded {Path(path).name} to {name}"

    def submit(self) -> str:
        self._guard_submit(self.fields)
        return "submitted (fixture)"

    def screenshot(self) -> str:
        SHOTS.mkdir(parents=True, exist_ok=True)
        p = SHOTS / f"app_{self.app_id or 0}_fixture.txt"
        p.write_text(json.dumps(self.filled, indent=1))
        return str(p)
