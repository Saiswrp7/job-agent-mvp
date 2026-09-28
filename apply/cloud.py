"""The cloud browser: the six functions, in a Browserbase Chrome.

Why a second real backend. `JevBrowser` drives the person's own Chrome, so an
application runs where their Chrome runs. The 3-4 people trying this from a
chat app have no Chrome of ours to drive. Browserbase gives each application
its own Chrome on their machines, reached over CDP with Playwright.

Why not Jev inside Browserbase. The Jev extension dials 127.0.0.1:10522, which
in a cloud Chrome is Browserbase's machine, not ours. Its only other way out is
the jevbrowsercontrol.com relay, and that holds every submit-type click for a
60 s OK in the extension's side panel — which nobody can press in the cloud.

What it costs: one fresh session per run (about 1 s to start), billed by the
minute it is open. So a run closes its session when it ends or parks. A parked
run that gets its answer hours later opens a new session on an empty form and
fills it again; paying for a browser that waits on a person is the wrong trade.
Free plan: 1 browser-hour a month, 3 at once, 15 minutes a session. $20: 100 h.

Fields are found by a `data-ja-ref` attribute stamped on each one when the form
is read, never by CSS class — boards ship hashed class names and change them.
Labels come from the page's own structure: `<label for>`, the wrapping label,
Lever's `.application-label`, a fieldset legend, aria-label. A radio group is
one field whose options are the choices. On Lever the radio's own label is
"Yes"; the question is two levels up, and a field called "Yes" is useless.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

import httpx

from apply.browser import SHOTS, BaseBrowser
from apply.jev import _SUBMIT, _clean, _required, _short

API = "https://api.browserbase.com/v1"
#: How long a session may live. The free plan's cap is 15 minutes, and a form
#: that takes longer has gone wrong somewhere else.
SESSION_SECONDS = int(os.environ.get("CLOUD_SESSION_SECONDS", "900"))
REPLAY = "https://www.browserbase.com/sessions/{}"
#: Where the cloud Chrome runs. Every action is a round trip from wherever this
#: code runs, and Playwright spends several per action. Measured from India on
#: 2026-09-24: us-west-2 (their default) fill 5.5 s, click 3.0 s; ap-southeast-1
#: fill 1.1 s, click 1.0 s. Move it when the code moves to a server.
REGION = os.environ.get("CLOUD_REGION", "ap-southeast-1")
_PLACE = re.compile(r"\b(location|city)\b", re.I)


def _keys() -> tuple[str, str] | None:
    import llm  # noqa: F401,PLC0415  (importing llm loads .env)
    key, project = (os.environ.get("BROWSERBASE_API_KEY"),
                    os.environ.get("BROWSERBASE_PROJECT_ID"))
    return (key, project) if key and project else None


def configured() -> bool:
    return _keys() is not None


def probe() -> tuple[bool, str]:
    """For `doctor`: is the key good? Reads the project; opens no browser, so
    it spends none of the month's browser time."""
    keys = _keys()
    if keys is None:
        return False, "BROWSERBASE_API_KEY / BROWSERBASE_PROJECT_ID not in .env"
    key, project = keys
    try:
        r = httpx.get(f"{API}/projects", headers={"X-BB-API-Key": key}, timeout=15)
        r.raise_for_status()
    except Exception as exc:                          # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    mine = next((p for p in r.json() if p.get("id") == project), None)
    if mine is None:
        return False, "the key works, but BROWSERBASE_PROJECT_ID is not one of its projects"
    return True, f"project ok, {mine.get('concurrency', '?')} browsers at once"


class Session:
    """One Browserbase session: created, connected to, released."""

    def __init__(self, app_id: int | None = None, tries: int = 6):
        keys = _keys()
        if keys is None:
            raise RuntimeError("cloud browser not set up: put BROWSERBASE_API_KEY "
                               "and BROWSERBASE_PROJECT_ID in .env")
        self.key, self.project = keys
        body = {"projectId": self.project, "timeout": SESSION_SECONDS,
                "region": REGION, "userMetadata": {"app": str(app_id or 0)}}
        # 429 = every browser the plan allows is busy (3 on the free plan).
        # The worker runs at most 3 at once, so this is rare; wait, don't fail.
        for attempt in range(tries):
            r = httpx.post(f"{API}/sessions", json=body, timeout=30,
                           headers={"X-BB-API-Key": self.key})
            if r.status_code != 429:
                break
            time.sleep(5 * (attempt + 1))
        if r.status_code == 429:
            raise RuntimeError("every cloud browser is busy; try again in a minute")
        r.raise_for_status()
        data = r.json()
        self.id, self.connect_url = data["id"], data["connectUrl"]
        self.replay_url = REPLAY.format(self.id)

    def release(self) -> None:
        try:
            httpx.post(f"{API}/sessions/{self.id}", timeout=15,
                       json={"projectId": self.project, "status": "REQUEST_RELEASE"},
                       headers={"X-BB-API-Key": self.key})
        except Exception:                             # noqa: BLE001
            pass     # it times out on its own; never fail a run over this


# One pass over the page. Stamps a ref on every control and returns what each
# one is. Radios and multi-box checkboxes come back per option with a `group`,
# and Python folds them into one field.
_READ = r"""() => {
  const txt = n => (n ? (n.innerText || n.textContent || '') : '').replace(/\s+/g, ' ').trim();
  const bare = lab => {           // a wrapping label's text without its controls
    const c = lab.cloneNode(true);
    c.querySelectorAll('input,select,textarea,option,button').forEach(x => x.remove());
    return txt(c);
  };
  const byIds = ids => ids.split(/\s+/).map(i => txt(document.getElementById(i))).join(' ').trim();
  const visible = el => !!(el.offsetParent || el.getClientRects().length);
  const question = el => {
    const q = el.closest('.application-question');
    if (q && q.querySelector('.application-label')) return txt(q.querySelector('.application-label'));
    const g = el.closest('fieldset,[role=radiogroup],[role=group]');
    if (g) {
      const lg = g.querySelector('legend');
      if (lg) return txt(lg);
      if (g.getAttribute('aria-labelledby')) return byIds(g.getAttribute('aria-labelledby'));
      if (g.getAttribute('aria-label')) return g.getAttribute('aria-label');
    }
    // The nearest text before the block holding the control.
    let n = el;
    for (let i = 0; i < 6 && n.parentElement; i++) {
      const p = n.parentElement;
      for (const c of p.children) {
        if (c === n) break;
        if (c.querySelector('input,select,textarea')) continue;
        const t = txt(c);
        if (t.length > 2 && t.length < 400) return t;
      }
      n = p;
    }
    return '';
  };
  const own = el => {
    if (el.getAttribute('aria-labelledby')) return byIds(el.getAttribute('aria-labelledby'));
    if (el.labels && el.labels.length) return bare(el.labels[0]);
    return el.getAttribute('aria-label') || '';
  };
  const out = [];
  let i = 0;
  const seen = {};
  for (const el of document.querySelectorAll('input,select,textarea,[role=combobox]')) {
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || (tag === 'input' ? 'text' : tag)).toLowerCase();
    if (['hidden', 'submit', 'button', 'image', 'reset'].includes(type) || el.disabled) continue;
    if (tag !== 'input' && tag !== 'select' && tag !== 'textarea' && el.querySelector('input')) continue;
    if (type !== 'file' && !visible(el)) continue;
    // react-select's twin that only exists for the browser's own validation
    // (Greenhouse, 2026-09-24): every dropdown was read twice.
    if (type !== 'file' && el.getAttribute('aria-hidden') === 'true') continue;
    const ref = String(++i);
    el.setAttribute('data-ja-ref', ref);
    const item = {ref, tag, type, role: el.getAttribute('role') || '',
                  required: !!(el.required || el.getAttribute('aria-required') === 'true'),
                  name: el.getAttribute('name') || el.id || ''};
    if (type === 'radio' || type === 'checkbox') {
      const key = item.name || ref;
      seen[key] = (seen[key] || 0) + 1;
      item.group = key;
      item.option = own(el) || el.value;
      item.label = question(el);
      if (!item.label || item.label === item.option) item.label = item.option;
    } else if (type === 'file') {
      item.label = question(el) || own(el) || 'file';
    } else {
      const q = el.closest('.application-question');
      item.label = (q && q.querySelector('.application-label') && txt(q.querySelector('.application-label')))
                   || own(el) || question(el) || el.getAttribute('placeholder') || item.name;
    }
    if (tag === 'select')
      item.options = [...el.options].filter(o => !o.disabled && o.value !== '').map(o => txt(o));
    item.suggests = !!(item.role === 'combobox' || el.getAttribute('aria-autocomplete')
                       || el.getAttribute('list') || el.getAttribute('aria-haspopup'));
    out.push(item);
  }
  for (const it of out) if (it.group) it.multi = seen[it.group] > 1;
  return out;
}"""

# What each ref holds right now, as the page shows it.
_VALUES = r"""() => {
  const txt = n => (n ? (n.innerText || n.textContent || '') : '').replace(/\s+/g, ' ').trim();
  const out = {};
  for (const el of document.querySelectorAll('[data-ja-ref]')) {
    const ref = el.getAttribute('data-ja-ref');
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (type === 'radio' || type === 'checkbox') out[ref] = el.checked ? 'on' : '';
    else if (type === 'file') out[ref] = el.files && el.files.length ? el.files[0].name : '';
    else if (el.tagName === 'SELECT') out[ref] = el.value ? txt(el.selectedOptions[0]) : '';
    else {
      let v = (el.value || '').trim();
      // A react-select keeps the choice beside the input, not in it.
      if (!v && el.getAttribute('role') === 'combobox') {
        let n = el;
        for (let i = 0; i < 5 && n && !v; i++, n = n.parentElement)
          v = txt(n.querySelector('[class*="singleValue"],[class*="single-value"]'));
      }
      out[ref] = v;
    }
  }
  return out;
}"""

# Suggestion lists, wherever a board puts them.
_OPTIONS = ('[role="option"], .dropdown-results > *, [class*="suggestion"] li, '
            '[class*="autocomplete"] li, li[class*="option"], div[class*="option"]')

_VISIBLE_OPTIONS = r"""sel => {
  // Stale marks from an earlier look would be clicked instead of the fresh one.
  document.querySelectorAll('[data-ja-opt]').forEach(e => e.removeAttribute('data-ja-opt'));
  return [...document.querySelectorAll(sel)]
    .filter(e => (e.offsetParent || e.getClientRects().length) && !e.querySelector('[role=option]'))
    .map((e, i) => { e.setAttribute('data-ja-opt', String(i));
                     return {i: String(i), text: (e.innerText || '').replace(/\s+/g, ' ').trim()}; })
    .filter(o => o.text);
}"""


def _match(options: list[str], value: str) -> str | None:
    """The option the value names: exact, then starts-with, then contains."""
    v = value.strip().lower()
    for test in (lambda o: o.lower() == v, lambda o: o.lower().startswith(v),
                 lambda o: v in o.lower()):
        hit = next((o for o in options if test(o)), None)
        if hit is not None:
            return hit
    return None


class CloudBrowser(BaseBrowser):
    """The six functions in a Browserbase Chrome. One session per run."""

    #: The session is billed while open; agent.start / resume_run close it.
    close_after_run = True
    #: Nobody can log in to a browser they cannot see.
    SIGN_IN = ("This job's page wants an account and a sign-in before it shows "
               "the application, and I can't sign in for you from here — I never "
               "ask for or type passwords. Best to skip this one; I can find "
               "others that take applications directly.")

    def __init__(self, url: str, app_id: int | None = None, page=None):
        super().__init__(app_id)
        self.url = url
        self.session = None
        self._pw = self._browser = None
        if page is None:                  # tests hand in a local page
            from playwright.sync_api import sync_playwright  # noqa: PLC0415
            self.session = Session(app_id)
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.connect_over_cdp(self.session.connect_url)
            ctx = self._browser.contexts[0]
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            try:
                page.wait_for_selector("input,textarea,select", timeout=10_000)
            except Exception:                         # noqa: BLE001
                pass     # no form: read_form says so, the agent decides
        self.page = page
        self.replay_url = self.session.replay_url if self.session else None
        self._fields: list[dict] = []
        #: The suggestions a field showed when none matched what was typed.
        self.offered: list[str] = []
        #: name -> {"kind", "ref" | "options": {label: ref}}
        self._where: dict[str, dict] = {}

    # --- reading ---------------------------------------------------------

    def read_form(self) -> list[dict]:
        raw = self.page.evaluate(_READ)
        fields, where, seen, groups = [], {}, {}, {}

        def name_for(short: str) -> str:
            n = seen[short] = seen.get(short, 0) + 1
            return short if n == 1 else f"{short} ({n})"

        for it in raw:
            # A lone checkbox is a yes/no field; a radio is always a choice.
            if it.get("group") and (it.get("multi") or it["type"] == "radio"):
                key = it["group"]
                if key not in groups:
                    short = _short(it["label"]) or _clean(it["option"])
                    name = name_for(short)
                    kind = it["type"]              # radio or checkbox
                    groups[key] = name
                    where[name] = {"kind": kind, "options": {}}
                    fields.append({"name": name, "label": short, "type": kind,
                                   "required": it["required"] or _required(it["label"]),
                                   "options": []})
                name = groups[key]
                opt = _clean(it["option"])
                where[name]["options"][opt] = it["ref"]
                next(f for f in fields if f["name"] == name)["options"].append(opt)
                continue
            if it["type"] == "file":
                short = _short(it["label"]) or "file"
                name = f"upload: {short}"
                where[name] = {"kind": "file", "ref": it["ref"]}
                fields.append({"name": name, "label": short, "type": "file",
                               "required": it["required"] or _required(it["label"])})
                continue
            short = _short(it["label"])
            if not short:
                continue
            name = name_for(short)
            kind = ("select" if it["tag"] == "select" else
                    "combobox" if it["role"] == "combobox" else
                    "checkbox" if it["type"] == "checkbox" else
                    "textarea" if it["tag"] == "textarea" else it["type"] or "text")
            # A place field autocompletes even when its markup does not say
            # so: Lever's location input has no role and no aria.
            where[name] = {"kind": kind, "ref": it["ref"],
                           "suggests": it["suggests"] or bool(_PLACE.search(short))}
            # A combobox is a dropdown to the agent. Its choices only exist
            # once it is opened, so they are not listed here: fill_field types
            # the answer and clicks the match, and names the choices if none.
            f = {"name": name, "label": short,
                 "type": "select" if kind == "combobox" else kind,
                 "required": it["required"] or _required(it["label"])}
            if it.get("options"):
                f["options"] = [_clean(o) for o in it["options"]]
            fields.append(f)
        self._fields, self._where = fields, where
        return fields

    def _loc(self, ref: str):
        return self.page.locator(f'[data-ja-ref="{ref}"]').first

    def _spot(self, name: str) -> dict | None:
        if name not in self._where:
            self.read_form()
        return self._where.get(name)

    # --- the six -------------------------------------------------------

    def fill_field(self, name: str, value: str) -> str:
        try:
            spot = self._spot(name)
            if spot is None:
                return (f"ERROR: no field named {name!r}. "
                        f"Fields: {sorted(self._where)}")
            kind = spot["kind"]
            if kind in ("radio", "checkbox") and "options" in spot:
                return self._choose(name, spot, value)
            loc = self._loc(spot["ref"])
            if kind == "checkbox":
                off = value.strip().lower() in ("no", "false", "off", "0", "")
                loc.set_checked(not off, force=True)
                if off:
                    self.filled.pop(name, None)
                    return f"unchecked {name}"
                self.filled[name] = "on"
                return f"checked {name}"
            if kind == "select":
                return self._select(name, spot, loc, value)
            got, picked = self._type(spot, loc, value)
            if not got and self.offered:
                return (f"ERROR: {name} only takes a choice from its list, and "
                        f"none matched {value!r}. It offered: {self.offered}. "
                        f"Fill it again with one of these exactly if it is the "
                        f"same place, or ask the user.")
            if not got:
                return (f"ERROR: {name} would not keep a value. It is probably "
                        f"a widget that needs a choice from a list. Ask the "
                        f"user, or try an exact option name.")
            self.filled[name] = got
            if picked:
                shows = "" if got == picked else f" (the field shows {got!r})"
                return f"filled {name} — picked {picked!r} from its suggestions{shows}"
            if value.lower() not in got.lower():
                return (f"filled {name}, but it now reads {got!r} rather than "
                        f"{value!r} — the field rewrote it.")
            self.filled[name] = value
            return f"filled {name}"
        except Exception as exc:                      # noqa: BLE001
            return f"ERROR filling {name}: {exc}"

    def _choose(self, name: str, spot: dict, value: str) -> str:
        opts = spot["options"]
        wanted = ([v.strip() for v in value.split(",")] if spot["kind"] == "checkbox"
                  else [value])
        picked = []
        for w in wanted:
            hit = _match(list(opts), w)
            if hit is None:
                return (f"ERROR: {w!r} is not a choice for {name}. "
                        f"Choices: {list(opts)}")
            self._loc(opts[hit]).check(force=True)
            picked.append(hit)
        self.filled[name] = ", ".join(picked)
        return f"chose {self.filled[name]!r} for {name}"

    def _select(self, name: str, spot: dict, loc, value: str) -> str:
        options = next((f.get("options", []) for f in self._fields
                        if f["name"] == name), [])
        hit = _match(options, value) or value
        loc.select_option(label=hit)
        got = self._settled(spot["ref"])
        if not got:
            return f"ERROR: {name} did not take {value!r}. Choices: {options}"
        self.filled[name] = got
        return f"filled {name}" + ("" if got == value else f" with {got!r}")

    def _type(self, spot: dict, loc, value: str) -> tuple[str, str]:
        """Type, choose from a suggestion list if one opened, then read the
        value back after the field has lost focus.

        After blur, because an autocomplete holds the typed text until focus
        leaves and only then throws it away (Lever's location, 2026-09-24).
        Only suggestions that appeared *after* typing are candidates, so a menu
        left open elsewhere on the page is never clicked. Enter is never
        pressed: in an ordinary field it submits the form, and the submit
        guard cannot stop a keypress.
        """
        before = {o["text"] for o in self.page.evaluate(_VISIBLE_OPTIONS, _OPTIONS)}
        # Each Playwright action costs about a second over the wire, so no
        # extra click or clear: fill() focuses and replaces on its own.
        suggests = spot.get("suggests")
        if suggests:
            loc.fill("")                              # focus, empty
            loc.press_sequentially(value, delay=40)   # real keys wake the lookup
        else:
            loc.fill(value)
        picked, offered = "", []
        # A suggestion box looks its list up online: Lever's took 1.1 s, and
        # its first list can be for a half-typed word. So keep looking until
        # one matches. Never take the first one that doesn't: on 2026-09-24
        # that turned "Bengaluru" into "Luru, Chainpur, Gumla, Jharkhand".
        for _ in range(16 if suggests else 1):
            self.page.wait_for_timeout(250)
            fresh = [o for o in self.page.evaluate(_VISIBLE_OPTIONS, _OPTIONS)
                     if o["text"] not in before]
            if not fresh:
                continue
            offered = [o["text"] for o in fresh]
            hit = _match(offered, value)
            if hit is not None:
                i = next(o["i"] for o in fresh if o["text"] == hit)
                self.page.locator(f'[data-ja-opt="{i}"]').first.click()
                picked = hit
                break
        self.page.wait_for_timeout(200)
        loc.press("Tab")
        if suggests:
            # The throw-away runs a beat after blur; read too soon and a value
            # about to vanish is reported as filled.
            self.page.wait_for_timeout(400)
        got = self._settled(spot["ref"])
        if not (picked or got) and not offered and spot["kind"] == "combobox":
            offered = self._open_list(loc)
        self.offered = [] if picked or got else offered[:30]
        return got, picked

    def _open_list(self, loc) -> list[str]:
        """Every choice a dropdown has: opened empty, read, closed."""
        before = {o["text"] for o in self.page.evaluate(_VISIBLE_OPTIONS, _OPTIONS)}
        loc.fill("")
        self.page.wait_for_timeout(600)
        texts = [o["text"] for o in self.page.evaluate(_VISIBLE_OPTIONS, _OPTIONS)
                 if o["text"] not in before]
        loc.press("Escape")
        return texts

    def _settled(self, ref: str, tries: int = 3) -> str:
        """Read again after the page has had a moment: a change handler runs
        after `select` returns, and the first read can come back empty."""
        for _ in range(tries):
            got = _clean(self.page.evaluate(_VALUES).get(ref, ""))
            if got:
                return got
            self.page.wait_for_timeout(250)
        return ""

    def upload_file(self, name: str, path: str) -> str:
        try:
            if not Path(path).exists():
                return f"ERROR: file not found: {path}"
            spot = self._spot(name)
            if spot is None or spot["kind"] != "file":
                files = [n for n, s in self._where.items() if s["kind"] == "file"]
                if len(files) != 1:
                    return f"ERROR: no file input named {name!r}. File inputs: {files}"
                spot = self._where[files[0]]
            self._loc(spot["ref"]).set_input_files(str(Path(path).resolve()))
            self.page.wait_for_timeout(500)
            if not self.page.evaluate(_VALUES).get(spot["ref"]):
                return f"ERROR: {name} did not keep the file"
            self.filled[name] = path
            return f"uploaded {Path(path).name}"
        except Exception as exc:                      # noqa: BLE001
            return f"ERROR uploading to {name}: {exc}"

    def current_values(self) -> dict:
        """Every field as the page holds it — not what we believe we typed."""
        try:
            vals = self.page.evaluate(_VALUES)
        except Exception:                             # noqa: BLE001
            return dict(self.filled)
        out = {}
        for name, spot in self._where.items():
            if "options" in spot:
                on = [o for o, ref in spot["options"].items() if vals.get(ref)]
                if on:
                    out[name] = ", ".join(on)
            elif vals.get(spot["ref"]):
                out[name] = _clean(vals[spot["ref"]])
        return out

    def submit(self) -> str:
        # The guard first, always: required fields, then ALLOW_SUBMIT.
        self._guard_submit(self._fields or self.read_form())
        buttons = self.page.locator("button, input[type=submit]")
        for i in range(buttons.count()):
            b = buttons.nth(i)
            text = (b.inner_text() if b.evaluate("e => e.tagName") == "BUTTON"
                    else b.get_attribute("value")) or ""
            if _SUBMIT.search(text) and b.is_visible():
                before = self.page.url
                b.click()
                try:
                    self.page.wait_for_load_state("networkidle", timeout=15_000)
                except Exception:                     # noqa: BLE001
                    pass
                if self.page.url == before and self.page.locator(
                        "iframe[src*=captcha]").count():
                    return ("ERROR: the page showed a captcha after submit; "
                            "the application is probably not sent")
                return f"submitted — now at {self.page.url}"
        return "ERROR: no submit button found on the page"

    def screenshot(self) -> str:
        SHOTS.mkdir(parents=True, exist_ok=True)
        p = SHOTS / f"app_{self.app_id or 0}.png"
        self.page.screenshot(path=str(p), full_page=True)
        return str(p)

    def close(self) -> None:
        # The last look at the form, taken before the session goes: the only
        # picture of it a person in a chat app will ever get.
        if self.session and self.app_id:
            try:
                self.screenshot()
            except Exception:                         # noqa: BLE001
                pass
        try:
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
        except Exception:                             # noqa: BLE001
            pass
        finally:
            if self.session:
                self.session.release()
                self.session = None
