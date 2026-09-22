"""The six functions the apply agent sees. The browser sits behind them.

**Why this file exists at all.** The agent's tool list must never contain a
Jev call or a Playwright selector. Keep the seam here and swapping the backend
is one file; skip it and the agent is welded to whatever drove it first.

**Two backends.**

- `PlaywrightBrowser` — what the CLI runs. A standalone process needs its own
  browser; Jev only exists while Claude Code is attached.
- `ManualBrowser` — a recorder for building fixtures. It prints what it would
  do and returns canned answers, so the loop can be exercised with no browser
  at all.

`submit()` is the one function with a hard refusal. It is deliberate and it
stays: a careless afternoon otherwise fires real applications at real
companies, and that is not recoverable.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
SHOTS = Path(__file__).resolve().parent.parent / "artifacts" / "screenshots"

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

    def ask_user(self, question: str) -> str:
        # Never returns. The harness catches Park, saves the log and exits.
        raise Park(question)

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


class PlaywrightBrowser(BaseBrowser):
    """Real browser. `pip install playwright && playwright install chromium`.

    Headed on purpose: a captcha is solved by the user in the same window the
    agent is driving, which is the only captcha strategy that works in week one.
    """

    def __init__(self, url: str, app_id: int | None = None, headless: bool = False):
        super().__init__(app_id)
        from playwright.sync_api import sync_playwright  # noqa: PLC0415

        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.launch(headless=headless)
        self.page = self.browser.new_page()
        self.page.goto(url, wait_until="domcontentloaded")
        self._fields: list[dict] = []

    def read_form(self) -> list[dict]:
        """Structural, not selector-based. Boards ship hashed class names, so
        anything keyed on CSS names breaks on their next deploy."""
        js = """() => {
          const out = [];
          for (const el of document.querySelectorAll('input,select,textarea')) {
            if (el.type === 'hidden' || el.disabled) continue;
            const id = el.id || el.name;
            let label = '';
            if (id) {
              const l = document.querySelector(`label[for="${CSS.escape(id)}"]`);
              if (l) label = l.innerText.trim();
            }
            if (!label && el.closest('label')) label = el.closest('label').innerText.trim();
            if (!label) label = el.getAttribute('aria-label') || el.placeholder || '';
            const opts = el.tagName === 'SELECT'
              ? [...el.options].map(o => o.innerText.trim()).filter(Boolean) : [];
            out.push({
              name: el.name || el.id,
              label: label.replace(/\\s+/g,' ').replace(/\\*$/,'').trim(),
              type: el.tagName === 'SELECT' ? 'select'
                  : el.tagName === 'TEXTAREA' ? 'textarea' : el.type,
              required: el.required || /\\*/.test(label),
              options: opts,
            });
          }
          return out.filter(f => f.name);
        }"""
        self._fields = self.page.evaluate(js)
        return self._fields

    def _locator(self, name: str):
        return self.page.locator(f'[name="{name}"], #{name}').first

    #: Widgets that clear themselves unless a suggestion is chosen. Lever's
    #: "Current location" is one, which is how a required field reached the
    #: submit guard looking filled while the form showed it empty.
    _OPTION_SELECTORS = ('[role="option"]', '[class*="suggestion"]',
                         '[class*="dropdown"] li', '[class*="autocomplete"] li')

    def current_values(self) -> dict:
        """Read every field straight off the page."""
        js = """() => {
          const o = {};
          for (const el of document.querySelectorAll('input,select,textarea')) {
            if (el.type === 'hidden' || el.disabled) continue;
            const k = el.name || el.id;
            if (!k) continue;
            if (el.type === 'checkbox' || el.type === 'radio') {
              if (el.checked) o[k] = 'on';
            } else if (el.type === 'file') {
              o[k] = el.files && el.files.length ? el.files[0].name : '';
            } else {
              o[k] = (el.value || '').trim();
            }
          }
          return o;
        }"""
        try:
            return self.page.evaluate(js)
        except Exception:                             # noqa: BLE001
            return dict(self.filled)

    def _value_of(self, loc) -> str:
        """Read the value back *after blurring*.

        Reading it immediately is what made the first fix useless: an
        autocomplete still holds the typed text until focus leaves, and only
        then throws it away. Checking before blur measures the wrong moment.
        """
        try:
            loc.blur()
            self.page.wait_for_timeout(120)
        except Exception:                             # noqa: BLE001
            pass
        try:
            return (loc.input_value() or "").strip()
        except Exception:                             # noqa: BLE001
            return ""

    def _pick_suggestion(self, loc, value: str) -> None:
        """Type it and choose from the dropdown, for autocomplete inputs."""
        loc.click()
        loc.fill("")
        loc.type(value, delay=45)
        self.page.wait_for_timeout(700)
        for sel in self._OPTION_SELECTORS:
            opt = self.page.locator(sel).first
            try:
                if opt.count() and opt.is_visible():
                    opt.click()
                    self.page.wait_for_timeout(200)
                    return
            except Exception:                         # noqa: BLE001
                continue
        loc.press("Enter")                            # last resort
        self.page.wait_for_timeout(200)

    def fill_field(self, name: str, value: str) -> str:
        try:
            loc = self._locator(name)
            spec = next((f for f in self._fields if f["name"] == name), None)
            kind = spec["type"] if spec else "text"

            if kind == "select":
                loc.select_option(label=value)
            elif kind in ("checkbox", "radio"):
                loc.check()
            else:
                loc.fill(value)
                # Read it back. `fill()` succeeding means Playwright typed it,
                # not that the widget kept it.
                if not self._value_of(loc):
                    self._pick_suggestion(loc, value)

            if kind not in ("checkbox", "radio"):
                got = self._value_of(loc)
                if not got:
                    # Do NOT record it as filled — the submit guard counts on
                    # `self.filled` to know which required fields are done.
                    return (f"ERROR: {name} would not keep a value. It is "
                            f"probably a widget that needs a choice from a "
                            f"list. Ask the user, or try an exact option name.")
                if kind not in ("select",) and value.lower() not in got.lower():
                    self.filled[name] = got
                    return (f"filled {name}, but it now reads {got!r} rather "
                            f"than {value!r} — the field rewrote it.")

            self.filled[name] = value
            return f"filled {name}"
        except Exception as exc:                      # noqa: BLE001
            return f"ERROR filling {name}: {exc}"

    def upload_file(self, name: str, path: str) -> str:
        try:
            self._locator(name).set_input_files(path)
            self.filled[name] = path
            return f"uploaded {Path(path).name}"
        except Exception as exc:                      # noqa: BLE001
            return f"ERROR uploading to {name}: {exc}"

    def submit(self) -> str:
        self._guard_submit(self._fields or self.read_form())
        self.page.get_by_role("button", name="Submit").first.click()
        self.page.wait_for_load_state("networkidle")
        return f"submitted — now at {self.page.url}"

    def screenshot(self) -> str:
        SHOTS.mkdir(parents=True, exist_ok=True)
        p = SHOTS / f"app_{self.app_id or 0}.png"
        self.page.screenshot(path=str(p), full_page=True)
        return str(p)

    def save_fixture(self, name: str) -> Path:
        """Freeze this form so the eval suite never needs the live site."""
        FIXTURES.mkdir(parents=True, exist_ok=True)
        p = FIXTURES / f"{name}.json"
        p.write_text(json.dumps(
            {"url": self.page.url, "fields": self.read_form()}, indent=1))
        (FIXTURES / f"{name}.html").write_text(self.page.content())
        return p

    def close(self) -> None:
        self.browser.close()
        self._pw.stop()
