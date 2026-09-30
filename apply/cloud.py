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

import json
import os
import re
import time
from pathlib import Path

import httpx

from apply import pagecheck
from apply.browser import SHOTS, BaseBrowser
from apply.jev import _clean, _required, _short

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


# Helpers every page script starts with. `deepAll` walks into shadow roots in
# document order: SmartRecruiters builds its whole form from web components
# (spl-input, spl-dropzone...), and `document.querySelectorAll` saw none of its
# 11 fields (2026-09-30). Playwright's CSS locators already pierce open shadow
# roots, so a field found here is filled by its `data-ja-ref` like any other.
_DEEP = r"""
  const deepAll = (sel, root = document, out = []) => {
    for (const el of root.querySelectorAll('*')) {
      if (el.matches(sel)) out.push(el);
      if (el.shadowRoot) deepAll(sel, el.shadowRoot, out);
    }
    return out;
  };
  const hostOf = n => { const r = n.getRootNode && n.getRootNode(); return r && r.host || null; };
  const closestDeep = (el, sel) => {
    for (let n = el; n; n = hostOf(n)) { const hit = n.closest(sel); if (hit) return hit; }
    return null;
  };
  const hasControl = c => deepAll('input,select,textarea', c).length > 0
                          || !!(c.shadowRoot && deepAll('input,select,textarea', c.shadowRoot).length);
  const deepText = () => {
    const parts = [document.body ? document.body.innerText : ''];
    for (const h of deepAll('*')) if (h.shadowRoot)
      for (const c of h.shadowRoot.children) if (c.innerText) parts.push(c.innerText);
    return parts.join('\n');
  };
  const btnText = el => {
    const h = hostOf(el);
    return ((el.innerText || el.value || el.getAttribute('aria-label') || '').trim()
            || (h ? (h.innerText || h.getAttribute('aria-label') || '').trim() : '')).replace(/\s+/g, ' ');
  };
  const shown = el => !!(el.offsetParent || el.getClientRects().length);
  // Not a question for the person: a spam trap parked off-screen (Breezy's
  // hp_* box at left:-9999px, filling it marks the application as a bot),
  // a cookie-banner switch, or a site search box (a 404 page on an
  // employer's own domain read as a 7-field form, form eval 2026-09-30).
  const notAsked = el => {
    const t = (el.getAttribute('type') || '').toLowerCase();
    if (t === 'search' || el.getAttribute('role') === 'searchbox') return true;
    if (closestDeep(el, '[id*=cookie i],[class*=cookie i],[id*=consent i],[class*=consent i],'
                        + '[id*=onetrust i],[class*=onetrust i],[id*=gdpr i],'
                        + '[class*=wcc-],[class*=cky-],[class*=cmplz],[class*=cc-window]')) return true;
    if (t === 'file') return false;
    const r = el.getBoundingClientRect();
    if (r.right <= 0 && r.width > 0) return true;
    if (el.tabIndex === -1 && closestDeep(el, '[aria-hidden=true]')) return true;
    return /^(hp_|honeypot|_gotcha)/i.test(el.getAttribute('name') || '');
  };
"""

# One pass over the page. Stamps a ref on every control and returns what each
# one is. Radios and multi-box checkboxes come back per option with a `group`,
# and Python folds them into one field.
_READ = r"""() => {""" + _DEEP + r"""
  const txt = n => (n ? (n.innerText || n.textContent || '') : '').replace(/\s+/g, ' ').trim();
  const bare = lab => {           // a wrapping label's text without its controls
    const c = lab.cloneNode(true);
    c.querySelectorAll('input,select,textarea,option,button').forEach(x => x.remove());
    return txt(c);
  };
  const byIds = (ids, el) => {
    const root = el.getRootNode();
    return ids.split(/\s+/).map(i => txt((root.getElementById && root.getElementById(i))
                                            || document.getElementById(i))).join(' ').trim();
  };
  const visible = el => !!(el.offsetParent || el.getClientRects().length);
  const question = el => {
    const q = closestDeep(el, '.application-question');
    if (q && q.querySelector('.application-label')) return txt(q.querySelector('.application-label'));
    const g = closestDeep(el, 'fieldset,[role=radiogroup],[role=group]');
    if (g) {
      const lg = g.querySelector('legend');
      if (lg) return txt(lg);
      if (g.getAttribute('aria-labelledby')) return byIds(g.getAttribute('aria-labelledby'), g);
      if (g.getAttribute('aria-label')) return g.getAttribute('aria-label');
    }
    // The nearest text before the block holding the control.
    // Across a shadow boundary the walk carries on from the host.
    let n = el;
    for (let i = 0; i < 8 && n; i++) {
      const p = n.parentElement;
      if (!p) { n = hostOf(n); continue; }
      for (const c of p.children) {
        if (c === n) break;
        if (hasControl(c)) continue;
        const t = txt(c);
        // "Choose a file or drop it here" names every upload box alike; the
        // question is further up (SmartRecruiters' "Resume *").
        if (GENERIC.test(t)) continue;
        if (t.length > 2 && t.length < 400) return t;
      }
      n = p;
    }
    return '';
  };
  const GENERIC = /^(choose|select|browse|attach|upload|drag)[^.]{0,40}(file|here)|drop (it|them|files?|here)|^\d+ ?mb/i;
  // Breezy's inputs have no label, only "Full Name" as a placeholder; the
  // nearest text above them is the section heading "Personal Details".
  const usefulPh = el => {
    const ph = (el.getAttribute('placeholder') || '').trim();
    return ph.length > 2 && ph.length < 60
      && !/^(type|enter|select|choose|search|start typing|e\.?g\.?|ex:|your answer)|here$|\.\.\.$|^[\d\s\/.-]+$|\b(mm|dd|yyyy)\b/i.test(ph) ? ph : '';
  };
  const own = el => {
    if (el.getAttribute('aria-labelledby')) return byIds(el.getAttribute('aria-labelledby'), el);
    if (el.labels && el.labels.length) return bare(el.labels[0]);
    const h = hostOf(el);
    return el.getAttribute('aria-label') || (h && (h.getAttribute('label') || h.getAttribute('aria-label'))) || '';
  };
  const out = [];
  let i = 0;
  const seen = {};
  // A page that re-renders (SmartRecruiters' next page, its resume autofill)
  // can keep old stamped nodes; a stale ref must not be found first.
  deepAll('[data-ja-ref]').forEach(e => e.removeAttribute('data-ja-ref'));
  for (const el of deepAll('input,select,textarea,[role=combobox]')) {
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || (tag === 'input' ? 'text' : tag)).toLowerCase();
    if (['hidden', 'submit', 'button', 'image', 'reset'].includes(type) || el.disabled) continue;
    if (tag !== 'input' && tag !== 'select' && tag !== 'textarea' && el.querySelector('input')) continue;
    if (type !== 'file' && !visible(el)) continue;
    // react-select's twin that only exists for the browser's own validation
    // (Greenhouse, 2026-09-24): every dropdown was read twice.
    if (type !== 'file' && el.getAttribute('aria-hidden') === 'true') continue;
    if (notAsked(el)) continue;
    const ref = String(++i);
    el.setAttribute('data-ja-ref', ref);
    const host = hostOf(el);
    const item = {ref, tag, type, role: el.getAttribute('role') || '',
                  required: !!(el.required || el.getAttribute('aria-required') === 'true'
                               || (host && host.hasAttribute('required') && host.getAttribute('required') !== 'false')),
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
      const q = closestDeep(el, '.application-question');
      item.label = (q && q.querySelector('.application-label') && txt(q.querySelector('.application-label')))
                   || own(el) || usefulPh(el) || question(el) || el.getAttribute('placeholder') || item.name;
    }
    if (tag === 'select')
      item.options = [...el.options].filter(o => !o.disabled && o.value !== '').map(o => txt(o));
    item.placeholder = el.getAttribute('placeholder') || '';
    item.datepicker = !!closestDeep(el, '[class*=datepicker],[class*=date-picker],[data-role=datepicker]');
    item.suggests = !!(item.role === 'combobox' || el.getAttribute('aria-autocomplete')
                       || el.getAttribute('list') || el.getAttribute('aria-haspopup'));
    out.push(item);
  }
  for (const it of out) if (it.group) it.multi = seen[it.group] > 1;
  return out;
}"""

# What each ref holds right now, as the page shows it.
_VALUES = r"""() => {""" + _DEEP + r"""
  // A file box that took its file often empties the input and shows a chip
  // with the name (Greenhouse, SmartRecruiters): read the name near it.
  const subText = n => [n.innerText || ''].concat(deepAll('*', n).filter(x => x.shadowRoot)
      .map(x => [...x.shadowRoot.children].map(c => c.innerText || '').join(' '))).join(' ')
      + (n.shadowRoot ? [...n.shadowRoot.children].map(c => c.innerText || '').join(' ') : '');
  const chip = el => {
    let n = el;
    for (let i = 0; i < 4 && n; i++) {
      n = n.parentElement || hostOf(n);
      if (!n) break;
      const m = subText(n).match(/[^\s\/\\]{1,80}\.(pdf|docx?|rtf|txt|odt)\b/i);
      if (m) return m[0].trim();
    }
    return '';
  };
  const txt = n => (n ? (n.innerText || n.textContent || '') : '').replace(/\s+/g, ' ').trim();
  const out = {};
  for (const el of deepAll('[data-ja-ref]')) {
    const ref = el.getAttribute('data-ja-ref');
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (type === 'radio' || type === 'checkbox') out[ref] = el.checked ? 'on' : '';
    else if (type === 'file') out[ref] = el.files && el.files.length ? el.files[0].name : chip(el);
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

# What the page is, in one look: how many fields a person could fill, whether
# it wants a password, the buttons it offers (stamped so one can be clicked by
# index), and its text. Used before the agent starts, at submit, and by
# `screenshot` so a text-only model is not guessing from a file name.
_STATE = r"""() => {""" + _DEEP + r"""
  const fields = deepAll('input,select,textarea').filter(el => {
    const t = (el.getAttribute('type') || el.tagName).toLowerCase();
    if (['hidden', 'submit', 'button', 'image', 'reset', 'search'].includes(t) || el.disabled) return false;
    if (notAsked(el)) return false;
    return t === 'file' || shown(el);
  });
  deepAll('[data-ja-btn]').forEach(e => e.removeAttribute('data-ja-btn'));
  const buttons = [];
  for (const el of deepAll('button,input[type=submit],input[type=button],a,[role=button]')) {
    if (!shown(el)) continue;
    const t = btnText(el);
    if (!t || t.length > 60) continue;
    el.setAttribute('data-ja-btn', String(buttons.length));
    buttons.push(t);
  }
  const heads = deepAll('h1,h2,h3').filter(shown).map(h => (h.innerText || '').trim()).filter(Boolean).slice(0, 6);
  // Only checkboxes and radios is not an application form (a cookie
  // preference panel, a filter list): every real one asks a name, an email
  // or a file.
  const typing = fields.filter(el => !['checkbox', 'radio'].includes((el.getAttribute('type') || '').toLowerCase()));
  return {url: location.href, title: document.title, fields: typing.length ? fields.length : 0,
          filled: fields.filter(el => el.type !== 'file' && (el.value || '').trim()).length,
          password: fields.some(el => (el.getAttribute('type') || '').toLowerCase() === 'password'),
          captcha: !!document.querySelector('iframe[src*=captcha],iframe[src*=hcaptcha],iframe[src*=recaptcha]'),
          invalid: deepAll('[aria-invalid=true]').filter(shown).length,
          buttons, heads, text: deepText().replace(/[ \t]+/g, ' ').replace(/\n\s*\n+/g, '\n').trim()};
}"""

_TEXT = r"""() => {""" + _DEEP + r""" return deepText(); }"""

# Suggestion lists, wherever a board puts them.
_OPTIONS = ('[role="option"], .dropdown-results > *, [class*="suggestion"] li, '
            '[class*="autocomplete"] li, li[class*="option"], div[class*="option"]')

_VISIBLE_OPTIONS = r"""sel => {""" + _DEEP + r"""
  // Stale marks from an earlier look would be clicked instead of the fresh one.
  deepAll('[data-ja-opt]').forEach(e => e.removeAttribute('data-ja-opt'));
  return deepAll(sel)
    .filter(e => (e.offsetParent || e.getClientRects().length) && !e.querySelector('[role=option]'))
    // react-select's "No options" notice matched div[class*=option] and was
    // handed to the agent as the only choice (Greenhouse, 2026-09-30).
    .filter(e => !/notice|no-options|loading/i.test(String(e.className))
                 && !/^(no (options|results|matches)|loading)/i.test((e.innerText || '').trim()))
    .map((e, i) => { e.setAttribute('data-ja-opt', String(i));
                     return {i: String(i), text: (e.innerText || '').replace(/\s+/g, ' ').trim()}; })
    .filter(o => o.text);
}"""


_JUNK = re.compile(r"SVGs? (are )?not supported by this browser\.?", re.I)
_DATE_HINT = re.compile(r"\b(dd|mm|yyyy|yy)\b.*[/.-]", re.I)


def _date_format(placeholder: str, itype: str, datepicker: bool) -> str | None:
    """The format a date field wants, or None if it is not a date field."""
    if itype == "date":
        return "YYYY-MM-DD"
    ph = (placeholder or "").strip()
    if _DATE_HINT.search(ph):
        return ph.upper()
    return "DD/MM/YYYY" if datepicker else None


def _parse_date(value: str):
    from datetime import datetime  # noqa: PLC0415
    v = value.strip()
    for f in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y", "%b %d, %Y",
              "%B %d, %Y", "%Y/%m/%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(v, f).date()
        except ValueError:
            continue
    return None


def _format_date(d, fmt: str) -> str:
    out = fmt.upper()
    for token, val in (("YYYY", f"{d.year:04d}"), ("YY", f"{d.year % 100:02d}"),
                       ("MM", f"{d.month:02d}"), ("DD", f"{d.day:02d}")):
        out = out.replace(token, val)
    return out


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
            try:
                self._pw = sync_playwright().start()
                self._browser = self._pw.chromium.connect_over_cdp(self.session.connect_url)
                ctx = self._browser.contexts[0]
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            except Exception:
                # A page that fails to load must not keep a billed session
                # open: the plan allows 3 at once, and a leaked one blocked
                # every run until it timed out (2026-09-30).
                self.session.release()
                if self._pw:
                    self._pw.stop()
                raise
            try:
                page.wait_for_selector("input,textarea,select", timeout=10_000)
            except Exception:                         # noqa: BLE001
                pass     # no form yet: open_form looks for its Apply button
        self.page = page
        self.replay_url = self.session.replay_url if self.session else None
        self._fields: list[dict] = []
        #: Answers on pages already left through next_page, by field name.
        #: They are part of what the person approves at submit.
        self._earlier: dict[str, str] = {}
        #: The suggestions a field showed when none matched what was typed.
        self.offered: list[str] = []
        #: name -> {"kind", "ref" | "options": {label: ref}}
        self._where: dict[str, dict] = {}

    # --- the page --------------------------------------------------------

    def page_state(self) -> dict:
        try:
            return self.page.evaluate(_STATE)
        except Exception:                             # noqa: BLE001
            return {}

    def _press(self, index: int) -> None:
        """Click a button `page_state` stamped, following it to a new tab if
        it opened one."""
        ctx = self.page.context
        before = len(ctx.pages)
        self.page.locator(f'[data-ja-btn="{index}"]').first.click(timeout=10_000)
        self.page.wait_for_timeout(1500)
        if len(ctx.pages) > before:
            self.page = ctx.pages[-1]
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=15_000)
        except Exception:                             # noqa: BLE001
            pass

    def open_form(self, tries: int = 2) -> str:
        """A job page with no form gets its own Apply button pressed.

        Keka's stored link is the job description (`/careers/jobdetails/N`);
        the 38-field form is at `/careers/applyjob/N`, one "Apply for this
        job" click away. Never an apply-with-Indeed/LinkedIn button."""
        done = []
        for _ in range(tries):
            st = self.page_state()
            if not st or st.get("fields", 0) >= 2 or st.get("password"):
                break
            i = pagecheck.entry_button(st.get("buttons", []))
            if i is None:
                break
            label = st["buttons"][i]
            try:
                self._press(i)
            except Exception:                         # noqa: BLE001
                break
            done.append(label)
            try:
                self.page.wait_for_selector("input,textarea,select", timeout=10_000)
            except Exception:                         # noqa: BLE001
                pass
        return (f"pressed {' then '.join(repr(d) for d in done)} to open the form"
                if done else "")

    def describe(self, st: dict | None = None, limit: int = 700) -> str:
        """The page in words, for a model that cannot see a picture."""
        st = st if st is not None else self.page_state()
        if not st:
            return "(could not read the page)"
        bits = [f"URL: {st.get('url')}", f"Title: {st.get('title')}",
                f"Form fields a person could fill: {st.get('fields', 0)}"]
        if st.get("password"):
            bits.append("It has a PASSWORD field: this is a sign-in or sign-up page.")
        if st.get("captcha"):
            bits.append("It shows a captcha.")
        if st.get("heads"):
            bits.append(f"Headings: {st['heads']}")
        if st.get("buttons"):
            bits.append(f"Buttons: {st['buttons'][:15]}")
        bits.append(f"Text: {(st.get('text') or '')[:limit]}")
        return "\n".join(bits)

    # --- reading ---------------------------------------------------------

    def read_form(self) -> list[dict]:
        raw = self.page.evaluate(_READ)
        fields, where, seen, groups = [], {}, {}, {}

        def name_for(short: str) -> str:
            n = seen[short] = seen.get(short, 0) + 1
            return short if n == 1 else f"{short} ({n})"

        for it in raw:
            # "Apply with LinkedIn" boxes hand the application to LinkedIn.
            if it["type"] in ("checkbox", "radio") and re.search(
                    r"\bapply (with|using)\b", f"{it.get('label')} {it.get('option')}", re.I):
                continue
            # Workable's labels carry its icon fallback text.
            for k in ("label", "option"):
                if it.get(k):
                    it[k] = _JUNK.sub("", it[k]).strip()
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
                # Two boxes both called "Choose a file..." were one name, and
                # the second overwrote the first (SmartRecruiters).
                name = name_for(f"upload: {short}")
                where[name] = {"kind": "file", "ref": it["ref"]}
                fields.append({"name": name, "label": short, "type": "file",
                               "required": it["required"] or _required(it["label"])})
                continue
            short = _short(it["label"])
            if not short:
                continue
            name = name_for(short)
            fmt = _date_format(it.get("placeholder", ""), it["type"], it.get("datepicker"))
            kind = ("date" if fmt else
                    "select" if it["tag"] == "select" else
                    "combobox" if it["role"] == "combobox" else
                    "checkbox" if it["type"] == "checkbox" else
                    "textarea" if it["tag"] == "textarea" else it["type"] or "text")
            # A place field autocompletes even when its markup does not say
            # so: Lever's location input has no role and no aria.
            where[name] = {"kind": kind, "ref": it["ref"], "format": fmt,
                           "suggests": (it["suggests"] or bool(_PLACE.search(short))) and not fmt}
            # A combobox is a dropdown to the agent. Its choices only exist
            # once it is opened, so they are not listed here: fill_field types
            # the answer and clicks the match, and names the choices if none.
            f = {"name": name, "label": short,
                 "type": "select" if kind == "combobox" else kind,
                 "required": it["required"] or _required(it["label"])}
            if it.get("options"):
                f["options"] = [_clean(o) for o in it["options"]]
            if fmt:
                f["format"] = fmt
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
                self._tick(loc, not off)
                if off:
                    self.filled.pop(name, None)
                    return f"unchecked {name}"
                self.filled[name] = "on"
                return f"checked {name}"
            if kind == "select":
                return self._select(name, spot, loc, value)
            if kind == "date":
                return self._date(name, spot, loc, value)
            got, picked = self._type(spot, loc, value)
            if not got and self.offered:
                return (f"ERROR: {name} only takes a choice from its list, and "
                        f"none matched {value!r}. It offered: {self.offered}. "
                        f"Fill it again with one of these exactly if it is the "
                        f"same place, or ask the user.")
            if not got:
                if not re.fullmatch(r"[\d.,\s]+", value):
                    return (f"ERROR: {name} would not keep {value!r}. It may take "
                            f"numbers only (no words or currency): try the number "
                            f"alone, or ask the user.")
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
            self._tick(self._loc(opts[hit]))
            picked.append(hit)
        self.filled[name] = ", ".join(picked)
        return f"chose {self.filled[name]!r} for {name}"

    def _date(self, name: str, spot: dict, loc, value: str) -> str:
        """A date field: typed in the format it shows, calendar closed after.

        Workable's start date is a react-datepicker; its day cells are
        role=option, so the suggestion-picking path clicked calendar days and
        timed out (form eval, 2026-09-30)."""
        when = _parse_date(value)
        if when is None:
            return (f"ERROR: {name} takes a date ({spot['format']}); {value!r} is not "
                    f"one. Give a date like 2026-10-15.")
        text = _format_date(when, spot["format"])
        if spot["format"] == "YYYY-MM-DD" and loc.get_attribute("type") == "date":
            loc.fill(text)                            # a native date input
        else:
            # A calendar widget parses keystrokes; a pasted value is thrown
            # away when it closes. Tab closes it and keeps the date.
            loc.fill("")
            loc.press_sequentially(text, delay=30)
            loc.press("Tab")
        self.page.wait_for_timeout(300)
        got = self._settled(spot["ref"])
        if not got:
            return f"ERROR: {name} would not keep the date {text!r}"
        self.filled[name] = got
        return f"filled {name} with {got!r}"

    def _tick(self, loc, on: bool = True) -> None:
        """Check a radio or checkbox however the page lets us.

        Teamtailor styles its Yes/No over the real input and a click on the
        input "did not change its state" (form eval, 2026-09-30). Then the
        label; then set it and fire the events the page listens for."""
        try:
            loc.set_checked(on, force=True, timeout=5_000)
            return
        except Exception:                             # noqa: BLE001
            pass
        try:
            loc.evaluate("e => e.labels && e.labels[0] && e.labels[0].click()")
            if loc.is_checked() == on:
                return
        except Exception:                             # noqa: BLE001
            pass
        loc.evaluate("""(e, on) => { e.checked = on;
            for (const t of ['input', 'change', 'click']) e.dispatchEvent(new Event(t, {bubbles: true})); }""", on)

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
            before_filled = self.page_state().get("filled")
            spot = self._spot(name)
            if spot is None or spot["kind"] != "file":
                files = [n for n, s in self._where.items() if s["kind"] == "file"]
                if len(files) != 1:
                    return f"ERROR: no file input named {name!r}. File inputs: {files}"
                spot = self._where[files[0]]
            self._loc(spot["ref"]).set_input_files(str(Path(path).resolve()))
            self.page.wait_for_timeout(500)
            if not self.page.evaluate(_VALUES).get(spot["ref"]):
                # Greenhouse takes the file and swaps its input for a chip
                # naming it, so the input reads empty (gone) after a good upload.
                for _ in range(12):               # its upload takes 1-5 s
                    self.page.wait_for_timeout(500)
                    if Path(path).name in self.page.evaluate(_TEXT):
                        break
                else:
                    return f"ERROR: {name} did not keep the file"
                spot["shown"] = Path(path).name
            self.filled[name] = path
            note = ""
            if before_filled is not None:
                self.page.wait_for_timeout(1500)
                after = self.page_state().get("filled", 0)
                if after >= before_filled + 2:
                    # SmartRecruiters' "Easy Apply" box fills name, email,
                    # phone, city, experience and education from the resume.
                    note = (f" The page filled {after - before_filled} more fields "
                            f"from the resume: call read_form again and check them "
                            f"before filling anything.")
            return f"uploaded {Path(path).name}" + note
        except Exception as exc:                      # noqa: BLE001
            return f"ERROR uploading to {name}: {exc}"

    def current_values(self) -> dict:
        """Every field as the page holds it — not what we believe we typed —
        plus the answers on pages already left."""
        try:
            vals = self.page.evaluate(_VALUES)
        except Exception:                             # noqa: BLE001
            return {**self._earlier, **self.filled}
        out = dict(self._earlier)
        text = None
        for name, spot in self._where.items():
            if spot.get("shown") and not vals.get(spot["ref"]):
                if text is None:
                    text = self.page.evaluate(_TEXT)
                if spot["shown"] in text:
                    out[name] = spot["shown"]
                continue
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
        st = self.page_state()
        labels = st.get("buttons", [])
        i = pagecheck.submit_button(labels)
        if i is None:
            nxt = pagecheck.next_button(labels)
            if nxt:
                return (f"ERROR: NOT SENT. This form goes on to another page (it "
                        f"has a {nxt!r} button), and I can only fill one page. "
                        f"Nothing was sent. Stop and say so.")
            return f"ERROR: NOT SENT. No submit button on the page. Buttons: {labels[:12]}"
        label = labels[i]
        self._press(i)
        try:
            self.page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:                             # noqa: BLE001
            pass
        return self._after_submit(st, label)

    def next_page(self) -> str:
        """Press Next/Continue on a form that goes on to another page.

        Refused while a required field here is empty, and on a page that has
        a submit button (that is `submit`'s job, behind the approval)."""
        fields = self._fields or self.read_form()
        vals = self.current_values()
        empty = [f["label"] for f in fields if f.get("required") and f["type"] != "file"
                 and not vals.get(f["name"])]
        if empty:
            return f"ERROR: fill these first: {empty}"
        st = self.page_state()
        labels = st.get("buttons", [])
        if pagecheck.submit_button(labels) is not None:
            return "ERROR: this page has a submit button, so it is the last page: call submit."
        i = next((k for k, t in enumerate(labels) if t and pagecheck.NEXT.search(t)), None)
        if i is None:
            return f"ERROR: no Next or Continue button on the page. Buttons: {labels[:12]}"
        here = {k: v for k, v in vals.items() if k not in self._earlier or k in self._where}
        names_before = {f["name"] for f in fields}
        self._press(i)
        self.page.wait_for_timeout(2500)
        after = self.page_state()
        if pagecheck.confirmed(st.get("text", ""), after.get("text", ""),
                               st.get("url", ""), after.get("url", "")):
            return "submitted — pressing Next sent it, and the page confirms it"
        new = self.read_form()
        if {f["name"] for f in new} == names_before and after.get("url") == st.get("url"):
            said = pagecheck.complaints(after.get("text", ""))
            return ("ERROR: still on the same page after pressing Next"
                    + (f"; the page says: {said}" if said else "") + ".")
        self._earlier.update(here)
        return (f"on the next page now: {len(new)} fields. The answers so far are kept. "
                f"Fields here: {json.dumps(new)[:3000]}")

    def _after_submit(self, before: dict, label: str, wait_ms: int = 10_000) -> str:
        """Only a page that says so is "submitted". The old check was "no
        captcha appeared", and a button that did nothing passed it."""
        after = {}
        for _ in range(max(1, wait_ms // 1000)):
            after = self.page_state()
            if pagecheck.confirmed(before.get("text", ""), after.get("text", ""),
                                   before.get("url", ""), after.get("url", "")):
                return f"submitted — the page confirms it ({after.get('url')})"
            self.page.wait_for_timeout(1000)
        if after.get("captcha") and not before.get("captcha"):
            return ("ERROR: NOT SENT. A captcha appeared after pressing "
                    f"{label!r}. Stop and say so.")
        same_page = after.get("url") == before.get("url") and after.get("fields", 0) >= max(1, before.get("fields", 0) - 1)
        if same_page:
            said = pagecheck.complaints(after.get("text", ""))
            return (f"ERROR: NOT SENT. Pressed {label!r} and the form is still "
                    f"there" + (f"; the page says: {said}" if said else ", with no "
                                "confirmation") + ". Fix what it says, or stop and say so.")
        return (f"unconfirmed — pressed {label!r} and the page changed, but it does "
                f"not say the application was received. It may or may not have "
                f"gone through: do not submit again. Page now: "
                f"{(after.get('text') or '')[:300]}")

    def screenshot(self) -> str:
        SHOTS.mkdir(parents=True, exist_ok=True)
        p = SHOTS / f"app_{self.app_id or 0}.png"
        self.page.screenshot(path=str(p), full_page=True)
        # The apply model reads text only. Handed a bare path it guessed a
        # sign-in page where there was none (Clickpost, 2026-09-29).
        return f"saved {p}\nWhat the page shows:\n{self.describe()}"

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
