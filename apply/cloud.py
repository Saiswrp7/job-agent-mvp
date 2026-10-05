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

from apply import pagecheck, stage
from apply.browser import SHOTS, BaseBrowser
from apply.jev import _clean, _required, _short

API = "https://api.browserbase.com/v1"
#: How long a session may live. The free plan's cap is 15 minutes, and a form
#: that takes longer has gone wrong somewhere else.
SESSION_SECONDS = int(os.environ.get("CLOUD_SESSION_SECONDS", "900"))
#: How long a parked run's browser waits for the person's answer. Within it the
#: run carries on on the same filled page; after it, the form is filled again.
#: Billed while it waits, so 0 turns it off (CLOUD_KEEP_SECONDS=0).
KEEP_SECONDS = int(os.environ.get("CLOUD_KEEP_SECONDS", "900"))
#: Image captchas Browserbase solves once told where the picture and the answer
#: box are (browserSettings captchaImageSelector / captchaInputSelector).
#: Keka: every form has one, and the agent asked the person to type it (apply
#: eval 2026-10-01: 5 of 5 Keka forms stopped there). Keyed by host suffix.
CUSTOM_CAPTCHAS = {"keka.com": ("#imgCaptcha", "#captcha")}


def custom_captcha(url: str) -> tuple[str, str] | None:
    host = re.sub(r"^https?://", "", url or "").split("/")[0].lower()
    return next((v for k, v in CUSTOM_CAPTCHAS.items() if host == k or host.endswith("." + k)), None)
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

    def __init__(self, app_id: int | None = None, tries: int = 6, url: str = "",
                 extension_id: str | None = None):
        keys = _keys()
        if keys is None:
            raise RuntimeError("cloud browser not set up: put BROWSERBASE_API_KEY "
                               "and BROWSERBASE_PROJECT_ID in .env")
        self.key, self.project = keys
        self.keep_alive = KEEP_SECONDS > 0
        self.started = time.time()
        self.timeout = SESSION_SECONDS + (KEEP_SECONDS if self.keep_alive else 0)
        body = {"projectId": self.project, "timeout": self.timeout,
                "region": REGION, "userMetadata": {"app": str(app_id or 0)}}
        if self.keep_alive:
            # Survives the disconnect when a run parks (Browserbase keepAlive,
            # paid plans); released by hand when the run ends.
            body["keepAlive"] = True
        if extension_id:
            body["extensionId"] = extension_id      # Stagehand's (apply/stage.py)
        cap = custom_captcha(url)
        if cap:
            body["browserSettings"] = {"captchaImageSelector": cap[0],
                                       "captchaInputSelector": cap[1]}
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
        if r.status_code == 402:
            # The plan's browser minutes are used up (each session bills at
            # least a full minute). Said in words the person can act on.
            raise RuntimeError("the cloud browser is out of minutes for this month, so "
                               "nothing was filled or sent; apply on the job link yourself")
        r.raise_for_status()
        data = r.json()
        self.id, self.connect_url = data["id"], data["connectUrl"]
        self.replay_url = REPLAY.format(self.id)

    @classmethod
    def attached(cls, kept: dict) -> "Session":
        """A kept session, to reconnect to and later release."""
        me = cls.__new__(cls)
        keys = _keys() or ("", "")
        me.key, me.project = keys
        me.id, me.connect_url = kept["id"], kept["connect_url"]
        me.replay_url = REPLAY.format(me.id)
        me.keep_alive, me.started = True, kept.get("started", time.time())
        me.timeout = kept.get("timeout", SESSION_SECONDS)
        return me

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
    if (closestDeep(el, '[id*=cookie i],[class*=cookie i],'
                        + '[id*=onetrust i],[class*=onetrust i],[id*=gdpr i],'
                        + '[class*=wcc-],[class*=cky-],[class*=cmplz],[class*=cc-window]')) return true;
    // "consent" alone is also the application's own consent box: Keka keeps
    // Apply Now greyed out until #candidateConsent is ticked, and skipping
    // every "consent" hid it from the agent (Loop, 2026-10-01). Only a
    // cookie banner's consent is not a question.
    const cons = closestDeep(el, '[id*=consent i],[class*=consent i]');
    if (cons && /cookie/i.test(`${cons.id} ${cons.className} ${(cons.innerText || '').slice(0, 300)}`)) return true;
    // A trap box, by what it tells a person to do with it (Workday's
    // "Enter website. This input is for robots only").
    const said = `${el.getAttribute('aria-label') || ''} ${el.getAttribute('placeholder') || ''} `
               + `${el.labels && el.labels.length ? el.labels[0].innerText : ''}`;
    if (/robots only|leave (this )?(field )?(blank|empty)|do not fill/i.test(said)) return true;
    // Inside an open calendar: its month and year lists are not questions
    // (they came back as "Feb" and "Current Salary: 1990" on Keka).
    if (closestDeep(el, '#ui-datepicker-div,.ui-datepicker,.datepicker-dropdown,.react-datepicker-popper,'
                        + '.flatpickr-calendar,.bs-datepicker,.daterangepicker,.pika-single')) return true;
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
    // ...and without a dropdown's list: Workable's phone label carried its
    // whole country list ("Phone+65United States+1United Kingdom+44...").
    c.querySelectorAll('input,select,textarea,option,button,[role=listbox],[role=option],'
                       + '[role=menu],[role=combobox],ul,ol').forEach(x => x.remove());
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
        // A hidden <select>'s option text ("INR USD") is not a question, and
        // a link or button is not one either ("Back to all job openings").
        if (['SELECT', 'A', 'BUTTON'].includes(c.tagName) || hasControl(c)) continue;
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
  // A label needs words. SmartRecruiters' question boxes carry a label of
  // just "*", which cleaned to nothing, and the box was dropped (Swiggy
  // page 2, 2026-10-01): salary, notice and relocation never reached the agent.
  const good = t => !!t && /[A-Za-z\u0900-\u097F]{2,}/.test(t.replace(/\*/g, ''))
                    && !GENERIC.test(t) && !/^\d+\s*\/\s*\d+$/.test(t.trim());
  // SmartRecruiters keeps each screening question on the form as JSON
  // (`definition`), keyed by the id inside the box's id: question_<uuid>.
  let defs = null;
  const defLabel = el => {
    const h = hostOf(el);
    const ids = [el.id, el.getAttribute('name'), h && h.id, h && h.getAttribute('name')].join(' ');
    const m = ids.match(/question_([0-9a-f-]{8,})/i);
    if (!m) return '';
    if (defs === null) {
      defs = [];
      for (const d of deepAll('[definition]')) { try { defs.push(JSON.parse(d.getAttribute('definition'))); } catch (e) {} }
    }
    const find = o => {
      if (!o || typeof o !== 'object') return '';
      if (!Array.isArray(o) && o.id === m[1])
        for (const k of ['label', 'title', 'text', 'question', 'name'])
          if (typeof o[k] === 'string' && good(o[k])) return o[k];
      for (const v of Object.values(o)) { const r = find(v); if (r) return r; }
      return '';
    };
    for (const d of defs) { const r = find(d); if (r) return txt({innerText: r}); }
    return '';
  };
  // The text a person sees just above the box, the way they read a form.
  let texts = null;
  const above = el => {
    if (texts === null) {
      texts = [];
      for (const e of deepAll('*')) {
        if (/^(SCRIPT|STYLE|OPTION|BUTTON)$/.test(e.tagName) || hasControl(e)) continue;
        const own = [...e.childNodes].filter(c => c.nodeType === 3).map(c => c.textContent).join(' ').replace(/\s+/g, ' ').trim();
        if (!good(own) || own.length > 300) continue;
        const r = e.getBoundingClientRect();
        if (r.width && r.height) texts.push({t: own, r});
      }
    }
    const b = el.getBoundingClientRect();
    let best = null;
    for (const c of texts)
      if (c.r.bottom <= b.top + 4 && c.r.bottom >= b.top - 90 && c.r.left < b.right && c.r.right > b.left
          && (!best || c.r.bottom > best.r.bottom)) best = c;
    return best ? best.t : '';
  };
  const first = (...xs) => { for (const x of xs) { const t = typeof x === 'function' ? x() : x; if (good(t)) return t; } return ''; };
  // The words right after a tick box, where a checkbox's text usually is.
  // Keka's consent box has no <label>; its sentence is the next span, and it
  // was named after the page heading instead (Loop, 2026-10-01).
  const after = el => {
    for (let n = el.nextElementSibling, k = 0; n && k < 3; n = n.nextElementSibling, k++) {
      if (hasControl(n)) break;
      const t = txt(n);
      if (good(t) && t.length < 400) return t;
    }
    const loose = [...(el.parentElement ? el.parentElement.childNodes : [])]
      .filter(c => c.nodeType === 3).map(c => c.textContent).join(' ').replace(/\s+/g, ' ').trim();
    return good(loose) && loose.length < 400 ? loose : '';
  };
  // A calendar box: Keka's date of birth is a read-only input with jQuery
  // UI's `hasDatepicker` class and a calendar icon, and the old check
  // (lower-case "datepicker" only) missed it (Loop, 2026-10-01).
  const isDate = el => /datepicker|date-picker/i.test(String(el.className))
    || !!closestDeep(el, '[class*=datepicker i],[class*=date-picker i],[data-role=datepicker],[class*=input-calendar i]')
    || (el.readOnly && /date|dob|birth/i.test(`${el.id} ${el.getAttribute('name') || ''}`));
  // The real list behind a styled dropdown. select2 hides the <select> just
  // before its box; Keka's own widget keeps both in one container. The box a
  // person clicks says "Select" or "INR", which is not a question.
  const backing = el => {
    if (['INPUT', 'SELECT', 'TEXTAREA'].includes(el.tagName)) return null;
    let n = el;
    for (let k = 0; k < 4 && n; k++) {
      n = n.parentElement;
      if (!n) break;
      const prev = n.previousElementSibling;
      if (prev && prev.tagName === 'SELECT') return prev;
      const s = n.querySelector('select');
      if (s) return s;
    }
    return null;
  };
  const out = [];
  let i = 0;
  const seen = {};
  // A page that re-renders (SmartRecruiters' next page, its resume autofill)
  // can keep old stamped nodes; a stale ref must not be found first.
  deepAll('[data-ja-ref]').forEach(e => e.removeAttribute('data-ja-ref'));
  for (const box of deepAll('input,select,textarea,[role=combobox]')) {
    // Stamped already as the list behind a styled dropdown read earlier.
    if (box.getAttribute('data-ja-ref')) continue;
    let el = box;
    let tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || (tag === 'input' ? 'text' : tag)).toLowerCase();
    if (['hidden', 'submit', 'button', 'image', 'reset'].includes(type) || el.disabled) continue;
    if (tag !== 'input' && tag !== 'select' && tag !== 'textarea' && el.querySelector('input')) continue;
    if (type !== 'file' && !visible(el)) continue;
    // react-select's twin that only exists for the browser's own validation
    // (Greenhouse, 2026-09-24): every dropdown was read twice.
    if (type !== 'file' && el.getAttribute('aria-hidden') === 'true') continue;
    if (notAsked(el)) continue;
    const date = tag === 'input' && isDate(el);
    // A read-only box nobody can type in is a label in disguise: Keka puts
    // "Years", "Months" and "INR" beside the real boxes, and the agent tried
    // to fill them (Loop, 2026-10-01). A calendar box is the exception.
    if (tag === 'input' && type !== 'file' && (el.readOnly || el.hasAttribute('readonly')) && !date) continue;
    const behind = el.getAttribute('role') === 'combobox' ? backing(el) : null;
    if (behind && behind.getAttribute('data-ja-ref')) continue;   // already read
    const ref = String(++i);
    if (behind) { el = behind; tag = 'select'; }
    el.setAttribute('data-ja-ref', ref);
    const host = hostOf(el);
    const item = {ref, tag, type: behind ? 'select' : type, role: behind ? '' : (el.getAttribute('role') || ''),
                  required: !!(el.required || el.getAttribute('aria-required') === 'true'
                               || box.getAttribute('aria-required') === 'true'
                               || (host && host.hasAttribute('required') && host.getAttribute('required') !== 'false')),
                  name: el.getAttribute('name') || el.id || '',
                  backing: !!behind, readonly: !!(el.readOnly || el.hasAttribute('readonly')),
                  maxlength: parseInt(el.getAttribute('maxlength') || '0', 10) || 0};
    if (behind) {
      // Labelled by the block it sits in ("Mobile Phone", "Current Salary"),
      // never by what the box shows. Choices from the real list.
      const shows = txt(box);
      const mine = own(box);
      item.label = first(() => question(box), () => (mine && mine !== shows && !/^select\b/i.test(mine)) ? mine : '',
                         () => above(box)) || item.name;
      item.options = [...behind.options].filter(o => !o.disabled && o.value !== '')
        .map(o => { const t = txt(o), ti = (o.getAttribute('title') || '').trim();
                    return ti && ti !== t ? `${ti} (${t})` : t; });
      item.placeholder = '';
      item.datepicker = false;
      item.suggests = false;
      out.push(item);
      continue;
    }
    if (type === 'radio' || type === 'checkbox') {
      const key = item.name || ref;
      seen[key] = (seen[key] || 0) + 1;
      item.group = key;
      item.option = own(el) || after(el) || el.value;
      item.label = first(() => question(el), () => defLabel(el), () => above(el));
      if (!item.label || item.label === item.option) item.label = item.option;
    } else if (type === 'file') {
      item.label = question(el) || own(el) || 'file';
    } else {
      const q = closestDeep(el, '.application-question');
      item.label = first(q && q.querySelector('.application-label') && txt(q.querySelector('.application-label')),
                         () => own(el), () => defLabel(el), () => usefulPh(el), () => question(el),
                         () => above(el), () => el.getAttribute('placeholder')) || item.name;
    }
    if (tag === 'select')
      item.options = [...el.options].filter(o => !o.disabled && o.value !== '').map(o => txt(o));
    item.placeholder = el.getAttribute('placeholder') || '';
    item.datepicker = date;
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
      // A react-select keeps the choice beside the input, not in it. A
      // multi-select keeps chips: Konovo's (Greenhouse) consent list took
      // "Acknowledge and confirm" as a chip, this read it as empty, and the
      // agent retried for nine minutes and said the site refused it (2026-10-01).
      if (!v && el.getAttribute('role') === 'combobox') {
        let n = el;
        for (let i = 0; i < 5 && n && !v; i++, n = n.parentElement) {
          v = txt(n.querySelector('[class*="singleValue"],[class*="single-value"]'));
          if (!v) {
            const chips = [...n.querySelectorAll('[class*="multi-value__label"],[class*="multiValue"] > div:first-child')]
              .map(c => txt(c)).filter(Boolean);
            if (chips.length) v = [...new Set(chips)].join(', ');
          }
        }
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
  const buttons = [], disabled = [];
  for (const el of deepAll('button,input[type=submit],input[type=button],a,[role=button]')) {
    if (!shown(el)) continue;
    const t = btnText(el);
    if (!t || t.length > 60) continue;
    if (el.disabled || el.getAttribute('aria-disabled') === 'true') disabled.push(buttons.length);
    el.setAttribute('data-ja-btn', String(buttons.length));
    buttons.push(t);
  }
  // What the page says is wrong: red messages under boxes. Keka keeps Submit
  // greyed out until they are fixed and the run never knew why (Loop).
  const errors = [...new Set(deepAll('.error,.invalid-feedback,.text-danger,.field-error,[class*=error-message i],[role=alert]')
      .filter(shown).map(e => (e.innerText || '').replace(/\s+/g, ' ').trim())
      .filter(t => t.length > 2 && t.length < 160 && t !== '*'))].slice(0, 8);
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
          buttons, disabled, errors, heads, text: deepText().replace(/[ \t]+/g, ' ').replace(/\n\s*\n+/g, '\n').trim()};
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


def _same(s: str) -> str:
    """Text as a person compares it: any dash is "-", runs of space are one.
    SmartRecruiters listed "3–4 weeks" (en dash); the agent's "3-4 weeks"
    matched nothing and the form said "This field is required" (apply eval,
    2026-10-01)."""
    return re.sub(r"\s+", " ", re.sub(r"[\u2010-\u2015\u2212]", "-", s or "")).strip().lower()


def _match(options: list[str], value: str) -> str | None:
    """The option the value names: exact, then starts-with, then contains."""
    v = _same(value)
    for test in (lambda o: _same(o) == v, lambda o: _same(o).startswith(v),
                 lambda o: v in _same(o)):
        hit = next((o for o in options if test(o)), None)
        if hit is not None:
            return hit
    return None


def _number(value: str) -> str:
    """What a number box takes: "12 LPA" is 1200000 rupees, not 12. Lakh and
    crore are written out; anything else keeps its digits."""
    v = (value or "").replace(",", "").strip()
    m = re.match(r"^\s*([\d.]+)\s*(lpa|lakhs?|lacs?|l|crores?|cr|k)\b", v, re.I)
    if m:
        n = float(m.group(1)) * {"k": 1e3, "cr": 1e7}.get(
            m.group(2).lower()[:2] if m.group(2).lower().startswith("cr") else m.group(2).lower(), 1e5)
        return str(int(round(n)))
    # The first number only: "5 years 2 months" is 5, never "52".
    first = re.search(r"\d+(?:\.\d+)?", v)
    return first.group(0) if first else v


_GENERIC_PART = {"answer", "value", "field", "input", "text", "data"}


def _qualifier(name: str) -> str:
    """What a box's own name says it holds, in words: "workExperience.months"
    is "months", "mobilePhone.countryCode" is "country code". Generic tails
    ("standardFields.dateOfBirth.answer") are skipped."""
    parts = [p for p in re.split(r"[.\[\]_\-:]+", name or "") if p and not p.isdigit()]
    for p in reversed(parts):
        words = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", p).lower().strip()
        if words and words not in _GENERIC_PART:
            return words
    return ""


_MONEY = re.compile(r"\b(salary|ctc|compensation|package|remuneration|pay|stipend)\b", re.I)
_LAKH_BOX = re.compile(r"\b(lpa|lakhs?|lacs?)\b", re.I)
_MONTHLY = re.compile(r"\b(month|monthly|per month|p\.?m\.?)\b", re.I)
#: A pay box that is not a yearly rupee amount: a US form's "Desired pay
#: rate" of 30 (dollars an hour) must stay 30 (form-widget test).
_NOT_INR = re.compile(r"\b(rate|hour|hourly|usd|dollars?|eur|euros?|gbp|pounds?)\b|\$", re.I)
_YEARLY = re.compile(r"\b(ctc|lpa|lakhs?|lacs?|salary|compensation|package|annual)\b", re.I)
_PHONE = re.compile(r"\b(phone|mobile|contact number|whatsapp|cell)\b", re.I)
_CODE_BOX = re.compile(r"\b(country code|dial(ing)? code|isd|calling code)\b", re.I)
_NAME_BOX = re.compile(r"\b(first|middle|last|full|given|family|sur)\s*name\b|^name$", re.I)
_MIDDLE = re.compile(r"\bmiddle\s*name\b", re.I)
_CONSENT = re.compile(r"consent|\bagree|terms|privacy|declar|acknowledg|i confirm|certify", re.I)


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value or "")


#: Gap between key presses, in ms. Every text box is typed key by key, never
#: pasted: Google's invisible check on Greenhouse and Ashby watches for key
#: presses, and Ashby flagged a pasted application as spam (metaforms,
#: 2026-10-01).
KEY_DELAY = 40
#: Past this many characters (a cover letter, a "why us"), no added gap: the
#: round trip to the cloud browser already spaces the keys.
LONG_TEXT = 200
#: Time allowed per key, in ms. Each key is a round trip to the cloud browser,
#: so a long answer outlasts the page's 8 s action limit: 39 of 259 keys got
#: in before it (Browserbase Singapore, 2026-10-04).
KEY_TIME = 600


def _type_keys(loc, text: str) -> None:
    """Empty a box, then type `text` into it with real key presses."""
    loc.fill("")                                  # focus, empty
    loc.press_sequentially(text, delay=0 if len(text) > LONG_TEXT else KEY_DELAY,
                           timeout=max(8_000, len(text) * KEY_TIME))


def _money(value: str, label: str) -> str | None:
    """A yearly salary as the box wants it: full rupees, or lakhs when the
    label asks in lakhs. None when it is not a salary the code can read.

    "12 LPA" typed into Keka's salary box became "12", read as 12 rupees, and
    the vault held expected CTC as a bare "18" (Loop, 2026-10-01). A bare
    number under 1000 for a yearly salary is lakhs."""
    if _MONTHLY.search(label or "") or _NOT_INR.search(label or ""):
        return None       # monthly, hourly or not rupees: the agent decides
    v = (value or "").replace(",", "").replace("₹", "").strip()
    v = re.sub(r"\b(inr|rs\.?|rupees?|per annum|p\.?a\.?|annual(ly)?)\b", "", v, flags=re.I).strip()
    m = re.match(r"^([\d.]+)\s*(lpa|lakhs?|lacs?|l|crores?|cr|k)?(?![a-z])", v, re.I)
    if not m:
        return None
    try:
        n = float(m.group(1))
    except ValueError:
        return None
    unit = (m.group(2) or "").lower()
    if unit.startswith("cr"):
        rupees = n * 1e7
    elif unit == "k":
        rupees = n * 1e3
    elif unit:
        rupees = n * 1e5
    elif n >= 1000:
        rupees = n
    elif _YEARLY.search(label or ""):
        rupees = n * 1e5              # "20" for a yearly CTC is 20 lakh
    else:
        return None
    if _LAKH_BOX.search(label or ""):
        return f"{rupees / 1e5:.2f}".rstrip("0").rstrip(".")
    return str(int(round(rupees)))


def _phone(value: str, maxlength: int, has_code_box: bool) -> str:
    """A phone number as the box takes it. With a country-code list beside
    it, or too long for the box, only the 10-digit number: Keka's box holds
    13 characters and "+91-9876543210" became "+91987654321" (Loop, 2026-10-01)."""
    v = (value or "").strip()
    d = _digits(v)
    national = d[2:] if len(d) == 12 and d.startswith("91") else d[1:] if len(d) == 11 and d.startswith("0") else d
    if has_code_box or (maxlength and len(v) > maxlength):
        return national
    return v


def _take_kept(app_id: int) -> dict | None:
    """The browser a parked run left open for this application, if it is
    still within its time; cleared from the row either way, so it is used
    (or released) once."""
    try:
        from engine import db                         # noqa: PLC0415
        conn = db.connect()
        try:
            row = conn.execute("SELECT kept_session FROM applications WHERE id = ?",
                               (app_id,)).fetchone()
            if not row or not row[0]:
                return None
            conn.execute("UPDATE applications SET kept_session = NULL WHERE id = ?", (app_id,))
            conn.commit()
            kept = json.loads(row[0])
        finally:
            conn.close()
    except Exception as exc:                          # noqa: BLE001
        print(f"[cloud] could not read the kept browser: {exc}")
        return None
    if kept.get("until", 0) <= time.time():
        Session.attached(kept).release()
        return None
    return kept


class CloudBrowser(BaseBrowser):
    """The six functions in a Browserbase Chrome. One session per run."""

    #: The session is billed while open; agent.start / resume_run close it.
    close_after_run = True
    #: Stagehand (apply/stage.py): off until a session is made with its extension.
    _stage = None
    _with_stage = False
    #: Nobody can log in to a browser they cannot see.
    SIGN_IN = ("This job's page wants an account and a sign-in before it shows "
               "the application, and I can't sign in for you from here — I never "
               "ask for or type passwords. Best to skip this one; I can find "
               "others that take applications directly.")

    def __init__(self, url: str, app_id: int | None = None, page=None,
                 resume: bool = False):
        super().__init__(app_id)
        self.url = url
        self.session = None
        self._pw = self._browser = None
        #: The session carries Stagehand's extension (a kept one does too).
        self._with_stage = False
        #: Carrying on in the browser a parked run left open: the form is
        #: still filled, so the agent must not fill it again.
        self.resumed = False
        kept = _take_kept(app_id) if page is None and app_id else None
        if kept and resume:
            page = self._reconnect(kept)
        elif kept:
            Session.attached(kept).release()  # a fresh start never reuses one
        if page is None:                  # tests hand in a local page
            from playwright.sync_api import sync_playwright  # noqa: PLC0415
            ext = None
            if stage.available():
                try:
                    ext = stage.extension_id()
                except Exception as exc:              # noqa: BLE001
                    print(f"[apply] Stagehand is off for this run: {exc}")
            self.session = Session(app_id, url=url, extension_id=ext)
            self._with_stage = bool(ext)
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
        #: Stagehand tries per box: [(field, held, seconds)], for the eval.
        self.stage_log: list[tuple[str, bool, float]] = []
        # A box that will not take an action gives up in 8 s, not Playwright's
        # 30: six read-only boxes on Keka's form cost three minutes (Loop,
        # 2026-10-01). Page loads keep their own longer timeouts.
        try:
            page.set_default_timeout(8_000)
        except Exception:                             # noqa: BLE001
            pass
        #: Glances at the filled form before approval (apply/vision.py).
        self._looked = 0
        self._warnings: list[str] = []
        #: Browserbase's captcha solver: None, "solving" or "solved".
        self._captcha: str | None = None
        try:
            page.on("console", self._on_console)
        except Exception:                             # noqa: BLE001
            pass
        self.replay_url = self.session.replay_url if self.session else None
        self._fields: list[dict] = []
        #: Answers on pages already left through next_page, by field name.
        #: They are part of what the person approves at submit.
        self._earlier: dict[str, str] = dict((kept or {}).get("earlier") or {}) \
            if self.resumed else {}
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
        # Boxes that share one label get told apart by what the page calls
        # them: Keka's "Experience (in years)" is a years box and a months
        # list, and the agent saw four boxes of the same name (Loop, 2026-10-01).
        shared: dict[str, int] = {}
        for it in raw:
            if not it.get("group") and it["type"] != "file":
                k = _short(it.get("label") or "")
                shared[k] = shared.get(k, 0) + 1

        def name_for(short: str) -> str:
            n = seen[short] = seen.get(short, 0) + 1
            return short if n == 1 else f"{short} ({n})"

        for it in raw:
            base = _short(it.get("label") or "")
            q = _qualifier(it.get("name") or "")
            if shared.get(base, 0) > 1 and q and q.lower() not in base.lower():
                it["label"] = f"{base} ({q})"
            # "Apply with LinkedIn" boxes hand the application to LinkedIn. By
            # the box's own words only: Teamtailor's consent boxes sit under a
            # heading that reads "Personal information Apply with LinkedIn" and
            # were thrown away with it (regression run, 2026-10-01).
            own_words = it.get("option") or it.get("label") or ""
            if it["type"] in ("checkbox", "radio") and re.search(
                    r"\bapply (with|using)\b", own_words, re.I):
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
            # A lone tick box is named by its own sentence ("By applying, you
            # accept the data processing terms..."), not the heading above it.
            opt = (it.get("option") or "").strip()
            if it["type"] == "checkbox" and len(opt) > 3 and opt.lower() not in ("on", "yes", "true"):
                it["label"] = opt
            # A box with no readable label still goes to the agent, by its
            # page id: dropping it silently hid Swiggy's whole page 2.
            short = _short(it["label"]) or _clean(it.get("name") or "") or f"field {it['ref']}"
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
                           "suggests": (it["suggests"] or bool(_PLACE.search(short))) and not fmt,
                           "backing": bool(it.get("backing")), "readonly": bool(it.get("readonly")),
                           "maxlength": it.get("maxlength") or 0, "label": short}
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
        cap = custom_captcha(self.url) if self.session else None
        if cap:
            ref = self.page.evaluate("s => { const e = document.querySelector(s); "
                                     "return e && e.getAttribute('data-ja-ref') || ''; }", cap[1])
            # Solved by the cloud browser, so never asked of the person.
            if ref:
                fields = [f for f in fields if (where.get(f["name"]) or {}).get("ref") != ref]
                where = {k: v for k, v in where.items() if v.get("ref") != ref}
        self._fields, self._where = fields, where
        return fields

    def _captcha_ready(self, wait_s: int = 40) -> str:
        """For a site whose captcha the cloud browser solves: wait until its
        answer box is filled. "" when ready (or none), else why not."""
        cap = custom_captcha(self.url) if self.session else None
        if not cap:
            return ""
        for _ in range(wait_s):
            got = self.page.evaluate("s => { const e = document.querySelector(s); "
                                     "return e ? (e.value || '').trim() : null; }", cap[1])
            if got is None or got:
                return ""                             # no captcha here, or solved
            self.page.wait_for_timeout(1000)
        return "the captcha on the page was not solved"

    def _loc(self, ref: str):
        return self.page.locator(f'[data-ja-ref="{ref}"]').first

    def _spot(self, name: str) -> dict | None:
        if name not in self._where:
            self.read_form()
        return self._where.get(name)

    # --- the six -------------------------------------------------------

    def _shape(self, name: str, spot: dict, value: str) -> tuple[str, str]:
        """The value as this box takes it, decided in code: (value, how), or
        ("ERROR: ...", "") for one that must not go in. The model picks which
        fact goes where; how a salary or a phone is written is not its call."""
        label = spot.get("label") or name
        value = (value or "").strip()
        if spot["kind"] in ("text", "number", "tel", "textarea") and _MONEY.search(label) \
                and not _PHONE.search(label):
            shaped = _money(value, label)
            if shaped is not None:
                # "money" even when unchanged: Keka shows 2500000 as
                # "25,00,000", which is the same amount, read back as digits.
                return shaped, "money"
        if spot["kind"] in ("text", "tel", "number") and _PHONE.search(label) \
                and not _CODE_BOX.search(label) and len(_digits(value)) >= 8:
            code_box = any(_CODE_BOX.search(f"{n} {s.get('label', '')}") for n, s in self._where.items())
            shaped = _phone(value, spot.get("maxlength") or 0, code_box)
            return shaped, ("phone" if shaped != value else "")
        if spot["kind"] == "text" and _NAME_BOX.search(label):
            if _MIDDLE.search(label):
                words = value.split()
                others = {v.strip().lower() for n, v in self.filled.items()
                          if _NAME_BOX.search(n) and not _MIDDLE.search(n)}
                if len(words) >= 3 or value.lower() in others or \
                        (others and all(o in value.lower() for o in others)):
                    return (f"ERROR: {value!r} looks like the whole name, not a middle "
                            f"name. Leave {name} blank unless the person has told you "
                            f"a middle name."), ""
            if value.isupper() and len(value) > 1:
                return value.title(), "name"
        return value, ""

    def _fill_ours(self, name: str, value: str) -> str:
        try:
            spot = self._spot(name)
            if spot is None:
                return (f"ERROR: no field named {name!r}. "
                        f"Fields: {sorted(self._where)}")
            kind = spot["kind"]
            if kind in ("radio", "checkbox") and "options" in spot:
                return self._choose(name, spot, value)
            asked = value
            value, how = self._shape(name, spot, value)
            if value.startswith("ERROR:"):
                return value
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
            if how in ("money", "phone") or (kind == "number" and _digits(value)):
                # Read back as digits: a box that drops a digit or a unit is a
                # wrong application, not a cosmetic difference.
                want = _number(value) if kind == "number" else value
                if _digits(got) != _digits(want):
                    self.filled.pop(name, None)
                    return (f"ERROR: {name} now shows {got!r} but should hold {value!r}. "
                            f"The box changed it; fill it again with digits only, or ask.")
                why = {"money": f" ({asked!r} written in rupees)",
                       "phone": " (the number without the country code; the box has its own)"}
                return f"filled {name} with {got!r}{why.get(how, '')}"
            if value.lower() not in got.lower():
                return (f"filled {name}, but it now reads {got!r} rather than "
                        f"{value!r} — the field rewrote it.")
            self.filled[name] = value
            return f"filled {name}" + (f" with {value!r}" if how == "name" else "")
        except Exception as exc:                      # noqa: BLE001
            return f"ERROR filling {name}: {exc}"

    def fill_field(self, name: str, value: str) -> str:
        """One box. Our code by default; Stagehand under it or before it when
        APPLY_FILLER says so (apply/stage.py). The value is shaped here either
        way, and what counts is what the page then holds."""
        mode = stage.filler() if self._with_stage else "ours"
        if mode == "stagehand":
            done = self._fill_stage(name, value)
            return done if done is not None else self._fill_ours(name, value)
        out = self._fill_ours(name, value)
        if mode == "fallback" and (out.startswith("ERROR") or "rather than" in out):
            done = self._fill_stage(name, value)
            if done is not None and not done.startswith("ERROR"):
                return done
        return out

    def _stage_open(self):
        if self._stage is None and self.session is not None:
            self._stage = stage.Stage(self.session.id)
        return self._stage

    def _stage_close(self) -> None:
        if self._stage is not None:
            self._stage.close()
            self._stage = None

    def _fill_stage(self, name: str, value: str) -> str | None:
        """Stagehand fills one box; the text to return when the page now holds
        the value, None when it does not (so the other filler still gets a go)."""
        t0 = time.time()
        if not hasattr(self, "stage_log"):
            self.stage_log = []
        try:
            spot = self._spot(name)
            if spot is None:
                return None
            label = spot.get("label") or name
            shaped, how = self._shape(name, spot, value)
            if shaped.startswith("ERROR:"):
                return shaped
            if spot["kind"] in ("radio", "checkbox") and "options" in spot:
                instruction = (f'For the question "{label}", choose the option "{value}". '
                               f"Tick only that one.")
            elif spot["kind"] == "checkbox":
                instruction = f'Tick the checkbox "{label}".'
            elif spot["kind"] in ("select", "date"):
                instruction = f'Set the "{label}" box to "{shaped}".'
            else:
                instruction = f'Type "{shaped}" into the "{label}" box.'
            ok, said = self._stage_open().act(instruction)
            got = self.current_values().get(name, "")
            held = bool(got) and (
                _same(shaped) in _same(got) or _same(value) in _same(got)
                or (bool(_digits(shaped)) and _digits(got) == _digits(shaped)))
            self.stage_log.append((name, held, round(time.time() - t0, 1)))
            if not held:
                return None
            self.filled[name] = shaped
            return f"filled {name} (read from the page)"
        except Exception as exc:                      # noqa: BLE001
            self.stage_log.append((name, False, round(time.time() - t0, 1)))
            print(f"[apply] Stagehand could not fill {name}: {exc}")
            return None

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
        if spot.get("readonly"):
            # A calendar box that takes no typing (Keka's date of birth, a
            # jQuery UI datepicker on a read-only input): set it through the
            # calendar's own API when the page has one, else set the value
            # the way the page's framework sees it and fire its events. Every
            # attempt to type into it waited 30 s and failed (Loop, 2026-10-01).
            got = loc.evaluate("""(el, a) => {
                const [y, m, d, text] = a;
                const $ = window.jQuery;
                if ($ && $(el).hasClass('hasDatepicker') && $.fn.datepicker) {
                  $(el).datepicker('setDate', new Date(y, m - 1, d));
                  $(el).trigger('change');
                  $(el).datepicker('hide');      // a calendar left open reads as questions
                } else {
                  const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
                  set.call(el, text);
                  for (const t of ['input', 'change', 'blur']) el.dispatchEvent(new Event(t, {bubbles: true}));
                }
                return el.value;
            }""", [when.year, when.month, when.day, text])
            self.page.wait_for_timeout(200)
            got = self._settled(spot["ref"]) or (got or "")
            if not got:
                return f"ERROR: {name} would not keep the date {text!r}"
            self.filled[name] = got
            return f"filled {name} with {got!r}"
        if spot["format"] == "YYYY-MM-DD" and loc.get_attribute("type") == "date":
            # A native date input: its keys go to day/month/year boxes in the
            # browser's own order, so "2026-10-15" typed would land wrong.
            loc.fill(text)
        else:
            # A calendar widget parses keystrokes; a pasted value is thrown
            # away when it closes. Tab closes it and keeps the date.
            _type_keys(loc, text)
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
        hit = _match(options, value) or _match(options, f"({value})") or value
        if spot.get("backing"):
            # The real list behind a styled dropdown (select2, Keka's own):
            # chosen on the list and announced the way the widget listens,
            # so the box a person sees shows it too.
            if not self._pick_hidden(loc, hit, value):
                return f"ERROR: {name} did not take {value!r}. Choices: {options[:40]}"
        else:
            try:
                loc.select_option(label=hit)
            except Exception:                         # noqa: BLE001
                if not self._pick_hidden(loc, hit, value):
                    raise
        got = self._settled(spot["ref"])
        if not got:
            return f"ERROR: {name} did not take {value!r}. Choices: {options}"
        self.filled[name] = got
        return f"filled {name}" + ("" if got == value else f" with {got!r}")

    def _pick_hidden(self, loc, hit: str, value: str) -> bool:
        """Choose an option on a <select> a person never sees, by its text,
        its value or its title ("India (+91)" is the title India, text +91).
        Through jQuery when the page has it: select2 only redraws on a jQuery
        change event."""
        return bool(loc.evaluate("""(el, want) => {
            const norm = s => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
            const keys = want.map(norm);
            const label = o => { const t = (o.textContent || '').trim(), ti = (o.getAttribute('title') || '').trim();
                                 return ti && ti !== t ? `${ti} (${t})` : t; };
            const o = [...el.options].find(o => keys.includes(norm(label(o))) || keys.includes(norm(o.textContent))
                                                || keys.includes(norm(o.value)) || keys.includes(norm(o.getAttribute('title'))));
            if (!o) return false;
            const $ = window.jQuery;
            if ($) { $(el).val(o.value).trigger('change'); }
            else { el.value = o.value; for (const t of ['input', 'change']) el.dispatchEvent(new Event(t, {bubbles: true})); }
            return el.value === o.value;
        }""", [hit, value]))

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
        suggests = spot.get("suggests")
        # Real keys also wake a lookup. A number box takes digits only, no
        # "LPA" (SmartRecruiters' salary box went back to 0, Swiggy 2026-10-01).
        number = spot["kind"] == "number" and not suggests
        _type_keys(loc, _number(value) if number else value)
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
            offered, picked = self._open_list(loc, value)
            if picked:
                got = self._settled(spot["ref"])
        self.offered = [] if picked or got else offered[:30]
        return got, picked

    def _open_list(self, loc, want: str = "") -> tuple[list[str], str]:
        """Every choice a dropdown has: opened empty, read, closed.

        Emptied, then clicked, then the down arrow: SmartRecruiters' dropdowns
        open on a click or a key, not on focus, and the agent was left
        guessing "B.Tech" / "Bachelor's Degree" with no list (apply eval,
        2026-10-01). The way browser-use does it: show the choices, then pick."""
        before = {o["text"] for o in self.page.evaluate(_VISIBLE_OPTIONS, _OPTIONS)}
        fresh: list[dict] = []
        loc.fill("")
        for act in ("click", "ArrowDown"):
            try:
                loc.click() if act == "click" else loc.press(act)
            except Exception:                         # noqa: BLE001
                continue
            for _ in range(4):
                self.page.wait_for_timeout(300)
                fresh = [o for o in self.page.evaluate(_VISIBLE_OPTIONS, _OPTIONS)
                         if o["text"] not in before]
                if fresh:
                    break
            if fresh:
                break
        texts = [o["text"] for o in fresh]
        # The list is open: pick the match now, the way a person would.
        hit = _match(texts, want) if want else None
        if hit is not None:
            i = next(o["i"] for o in fresh if o["text"] == hit)
            try:
                self.page.locator(f'[data-ja-opt="{i}"]').first.click()
                self.page.wait_for_timeout(300)
                return texts, hit
            except Exception:                         # noqa: BLE001
                pass
        try:
            loc.press("Escape")
        except Exception:                             # noqa: BLE001
            pass
        return texts, ""

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
        # The last page first: asking for approval on page 1 of a longer form
        # showed the person half the answers, then stopped at Next once they
        # said yes (Swiggy, 2026-09-30). Nothing is pressed here.
        st = self.page_state()
        labels = st.get("buttons", [])
        if pagecheck.submit_button(labels) is None:
            nxt = pagecheck.next_button(labels)
            if nxt and pagecheck.may_send(nxt, st.get("text", "")):
                # It may send: approval first, then press it and see.
                fields = self.read_form()             # as it is now, not as last read
                self._guard_submit(fields, partial=True)
                st = self.page_state()
                labels = st.get("buttons", [])
                if nxt not in labels:
                    return f"ERROR: NOT SENT. {nxt!r} is gone from the page. Buttons: {labels[:12]}"
                return self._advance(labels.index(nxt), st, fields, self.current_values(),
                                     sending=True)
            if nxt:
                return (f"ERROR: NOT SENT, not the last page yet: this form goes on (it has "
                        f"a {nxt!r} button). Call next_page, fill that page, and call "
                        f"submit on the page that has the submit button.")
            return f"ERROR: NOT SENT. No submit button on the page. Buttons: {labels[:12]}"
        why = self._captcha_ready()
        if why:
            return f"ERROR: NOT SENT. {why}. Stop and say so."
        # Then the guard, always: required fields, ALLOW_SUBMIT, approval.
        # The page as it is now: a list read before the last section opened
        # passed Lever's signature "Name"/"Date" as filled when they were
        # empty (apply eval, 2026-10-01).
        fields = self.read_form()
        stuck = self._greyed_out(self.page_state(), fields)
        if stuck:
            return stuck
        seen = self._look(fields)
        if seen:
            return seen
        self._guard_submit(fields)
        st = self.page_state()
        labels = st.get("buttons", [])
        i = pagecheck.submit_button(labels)
        if i is None:
            return f"ERROR: NOT SENT. No submit button on the page. Buttons: {labels[:12]}"
        label = labels[i]
        self._press(i)
        try:
            self.page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:                             # noqa: BLE001
            pass
        return self._after_submit(st, label)

    def _greyed_out(self, st: dict, fields: list[dict]) -> str:
        """A Submit the page keeps greyed out means the page thinks something
        is missing or wrong. Asking the person to approve it, then clicking a
        dead button, is what happened on Loop's Keka form (2026-10-01). Say
        what the page says instead, before anyone is asked."""
        labels = st.get("buttons", [])
        i = pagecheck.submit_button(labels)
        if i is None or i not in (st.get("disabled") or []):
            return ""
        vals = self.current_values()
        empty = [f["label"] for f in fields if f.get("required") and not vals.get(f["name"])]
        said = [e for e in (st.get("errors") or []) if e.strip() != "*"]
        # Keka enables Apply Now only once its consent box is ticked (Loop).
        unticked = [f["name"] for f in fields if f.get("type") == "checkbox"
                    and _CONSENT.search(f"{f['name']} {f.get('label', '')}")
                    and not vals.get(f["name"])]
        return (f"ERROR: NOT SENT. The {labels[i]!r} button is greyed out: the page thinks "
                f"something is missing or wrong."
                + (f" Tick these consent boxes: {unticked}." if unticked else "")
                + (f" It says: {said}." if said else "")
                + (f" Still empty: {empty}." if empty else "")
                + " Fix those (read_form, then fill), then call submit again.")

    def _look(self, fields: list[dict]) -> str:
        """glm-4.6v compares a picture of the filled form with the answers
        before the person is asked (apply/vision.py). The first time it finds
        something, the agent hears it and gets to fix it; after that its
        findings travel with the approval as warnings. Only in the cloud
        browser: tests hand in a local page and never call out."""
        if not self.session or self._looked >= 2:
            return ""
        vals = self.current_values()
        if any(f.get("required") and not vals.get(f["name"]) for f in fields):
            return ""                       # the guard refuses this anyway
        from apply import confirm, vision   # noqa: PLC0415
        answers = confirm.shown(vals, fields)
        if not answers:
            return ""
        self._looked += 1
        try:
            shot = self.page.screenshot(full_page=True, type="jpeg", quality=70)
        except Exception as exc:                      # noqa: BLE001
            print(f"[cloud] no picture for the look: {exc}")
            return ""
        problems = vision.look(shot, answers)
        if not problems:
            self._warnings = []
            return ""
        if self._looked == 1:
            return ("ERROR: NOT SENT YET. A look at the filled form found: "
                    + "; ".join(problems)
                    + ". Fix what is really wrong (read_form, then fill_field), then call "
                      "submit again. If a finding is wrong, just call submit again.")
        self._warnings = problems
        return ""

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
        # next_page never passes the approval, so it never presses a button
        # that may be the send (apply eval, 2026-10-01).
        if pagecheck.may_send(labels[i], st.get("text", "")):
            return (f"ERROR: NOT PRESSED. {labels[i]!r} may send the application (the page "
                    f"shows no step count). Call submit: it asks the person first, then "
                    f"presses it.")
        return self._advance(i, st, fields, vals)

    def _advance(self, i: int, st: dict, fields: list[dict], vals: dict,
                 sending: bool = False) -> str:
        """Press a Next/Continue button and say where it led. `sending`: the
        button may have been the send, so a page that did not move on is
        judged as a submit."""
        label = st.get("buttons", [])[i]
        here = {k: v for k, v in vals.items() if k not in self._earlier or k in self._where}
        names_before = {f["name"] for f in fields}
        self._press(i)
        self.page.wait_for_timeout(2500)
        after = self.page_state()
        if pagecheck.confirmed(st.get("text", ""), after.get("text", ""),
                               st.get("url", ""), after.get("url", "")):
            return f"submitted — pressing {label!r} sent it. {self._proof(st, after)}"
        new = self.read_form()
        moved = {f["name"] for f in new} != names_before or after.get("url") != st.get("url")
        if not moved or (sending and not new):
            if sending:
                return self._after_submit(st, label)
            said = pagecheck.complaints(after.get("text", ""))
            return ("ERROR: still on the same page after pressing Next"
                    + (f"; the page says: {said}" if said else "") + ".")
        self._earlier.update(here)
        # The vault was matched against page 1 only, so Swiggy's page 2
        # (salary, notice, relocation) was asked again with every answer on
        # file (2026-10-01). Each new page gets its own known answers.
        known = {}
        try:
            import vault                                  # noqa: PLC0415
            known = vault.slice_for([f.get("label") or f["name"] for f in new])
        except Exception as exc:                          # noqa: BLE001
            print(f"[cloud] vault lookup for the next page failed: {exc}")
        return (("Nothing was sent: it opened another page. " if sending else "")
                + f"on the next page now: {len(new)} fields. The answers so far are kept. "
                f"Fields here: {json.dumps(new)[:3000]}"
                + (f"\nAlready known for this page (use these, do not ask): "
                   f"{json.dumps(known)}" if known else ""))

    def _on_console(self, msg) -> None:
        """Browserbase solves captchas itself and says so in the console."""
        t = getattr(msg, "text", "")
        t = t() if callable(t) else t
        if t == "browserbase-solving-started":
            self._captcha = "solving"
        elif t == "browserbase-solving-finished":
            self._captcha = "solved"

    def _after_submit(self, before: dict, label: str, wait_ms: int = 10_000) -> str:
        """Only a page that says so is "submitted". The old check was "no
        captcha appeared", and a button that did nothing passed it.

        A captcha after the click is Browserbase's to solve, which takes up to
        30 s; this used to give up after 10 (Lever, apply eval 2026-10-01). So
        the wait runs on while it is solving, and 10 s more once it is solved."""
        after = {}
        start = time.monotonic()
        deadline, solved_at = start + wait_ms / 1000, None
        while True:
            after = self.page_state()
            if pagecheck.confirmed(before.get("text", ""), after.get("text", ""),
                                   before.get("url", ""), after.get("url", "")):
                return f"submitted — {self._proof(before, after)}"
            no = pagecheck.refused(before.get("text", ""), after.get("text", ""))
            if no:
                return (f"ERROR: NOT SENT. The site refused it: \"{no}\". Stop and "
                        f"say in one line that it was not sent.")
            now = time.monotonic()
            if self._captcha == "solving":
                deadline = max(deadline, now + 2)
            elif self._captcha == "solved" and solved_at is None:
                solved_at, deadline = now, max(deadline, now + 10)
            if now >= deadline or now - start > 75:
                break
            self.page.wait_for_timeout(1000)
        if after.get("captcha") and not before.get("captcha"):
            tried = " The cloud browser tried to solve it and could not." if self._captcha else ""
            return ("ERROR: NOT SENT. A captcha appeared after pressing "
                    f"{label!r}.{tried} Stop and say so.")
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

    def _proof(self, before: dict, after: dict) -> str:
        """The confirmation page, kept: a picture taken the moment the site
        confirmed (app_<id>_sent.png, sent with the "sent" update), and the
        words that confirmed it."""
        said = pagecheck.proof(before.get("text", ""), after.get("text", ""),
                               before.get("url", ""), after.get("url", ""))
        try:
            SHOTS.mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(SHOTS / f"app_{self.app_id or 0}_sent.png"),
                                 full_page=True)
        except Exception as exc:                      # noqa: BLE001
            print(f"[cloud] no picture of the confirmation page: {exc}")
        return (f'The page confirms it: "{said}" ({after.get("url")})' if said
                else f"The page confirms it ({after.get('url')})")

    def screenshot(self) -> str:
        SHOTS.mkdir(parents=True, exist_ok=True)
        p = SHOTS / f"app_{self.app_id or 0}.png"
        self.page.screenshot(path=str(p), full_page=True)
        # The apply model reads text only. Handed a bare path it guessed a
        # sign-in page where there was none (Clickpost, 2026-09-29).
        return f"saved {p}\nWhat the page shows:\n{self.describe()}"

    def _reconnect(self, kept: dict):
        """The page a parked run left, or None if it is gone (timed out,
        released): then the caller opens the form afresh."""
        from playwright.sync_api import sync_playwright  # noqa: PLC0415
        try:
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.connect_over_cdp(kept["connect_url"], timeout=20_000)
            ctx = self._browser.contexts[0]
            page = ctx.pages[0]
            page.evaluate("1")                        # alive, not a dead tab
            self.session = Session.attached(kept)
            self._with_stage = bool(kept.get("stage"))
            self.resumed = True
            return page
        except Exception as exc:                      # noqa: BLE001
            print(f"[cloud] kept browser for app {self.app_id} is gone ({type(exc).__name__}); "
                  f"opening the form again")
            try:
                if self._pw:
                    self._pw.stop()
            except Exception:                         # noqa: BLE001
                pass
            self._pw = self._browser = None
            Session.attached(kept).release()
            return None

    def fresh_form(self) -> None:
        """Back to the job's form page from wherever a kept browser was left.
        After a refused submit it sat on the error page, and the retry ended
        "couldn't find an application form" (metaforms, 2026-10-01)."""
        self.page.goto(self.url, wait_until="domcontentloaded", timeout=45_000)
        try:
            self.page.wait_for_selector("input,textarea,select", timeout=10_000)
        except Exception:                             # noqa: BLE001
            pass                  # no form yet: open_form presses its Apply
        self.resumed = False
        self._earlier = {}
        self._fields, self._where = [], {}

    def keep(self, conn) -> bool:
        """A parked run: leave the browser open, filled, for the answer.
        Saved on the application row; the connection is dropped, the session
        is not. False when there is nothing to keep (keep-alive off)."""
        s = self.session
        if not (s and getattr(s, "keep_alive", False) and self.app_id):
            return False
        until = s.started + s.timeout - 60
        if until - time.time() < 120:
            return False                              # too little left to be worth it
        try:
            self.screenshot()
        except Exception:                             # noqa: BLE001
            pass
        self._stage_close()
        conn.execute("UPDATE applications SET kept_session = ? WHERE id = ?",
                     (json.dumps({"id": s.id, "connect_url": s.connect_url,
                                  "until": until, "started": s.started, "timeout": s.timeout,
                                  "earlier": self._earlier, "stage": self._with_stage}),
                      self.app_id))
        conn.commit()
        try:
            if self._pw:
                self._pw.stop()                       # drop the connection only
        except Exception:                             # noqa: BLE001
            pass
        self._pw = self._browser = self.session = None
        return True

    def close(self) -> None:
        # The last look at the form, taken before the session goes: the only
        # picture of it a person in a chat app will ever get.
        if self.session and self.app_id:
            try:
                self.screenshot()
            except Exception:                         # noqa: BLE001
                pass
        self._stage_close()
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
