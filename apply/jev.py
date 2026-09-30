"""The Jev browser: the six functions, driven through Jev Browser Control.

Jev Browser Control is a Chrome extension plus a small local bridge
(`jev-browser-control/mcp/server.mjs`). Claude Code uses it through MCP, but the
bridge itself is a plain websocket on 127.0.0.1 that any local program can join
as a peer — which is what this file does. No Claude Code involved.

It is the only real browser the apply agent has (Playwright was removed on
2026-09-24). What it gives:

- **Speed.** Measured on the practice form: type 0.07 s, select 0.03 s, upload
  0.05 s, screenshot 0.09 s, and every action returns the updated page, so
  checking a value stuck costs no extra round trip.
- **The person's own Chrome, with their logins.** The only kind of browser that
  could ever finish a LinkedIn Easy Apply.
- **Labels are read properly**, including `<label for>`, which is where another
  Jev browser tool was reviewed as failing.

What it costs: it needs Chrome open with the extension on this machine, so an
application runs where the person's Chrome runs. A WhatsApp user applying from
a server needs a cloud Chrome with the extension, which is not built.

The agent still decides every value (GLM, with the vault and the guards).
Jev's own decision model is not used here yet — this is Jev as the hands.
"""

from __future__ import annotations

import base64
import itertools
import json
import os
import re
import subprocess
import time
import urllib.request
from pathlib import Path

import paths
from apply import pagecheck
from apply.browser import SHOTS, BaseBrowser

PORT = int(os.environ.get("JBC_PORT", "10522"))
#: Where the extension's bridge lives. Next to this checkout by default.
JEV_DIR = Path(os.environ.get("JEV_BROWSER_DIR")
               or paths.ROOT.parent / "jev-browser-control")
TOKEN = Path(os.environ.get("JBC_HOME") or Path.home() / ".jev-browser-control") \
    / "bridge-token"

#: A submit button, by what it says. Never clicked unless the guard passes.
_SUBMIT = re.compile(r"\b(submit|send application|apply( now)?)\b", re.I)


#: Every mark a form uses for "required". Lever uses ✱, not *: the first real
#: Lever form read every required field as optional, and the agent, copying a
#: name with ✱ in it, typed ✡ and got "no field named" three times.
_STARS = "*✱✳✴✶✷✸★☆＊⁎∗"
_STAR_RE = re.compile(f"[{re.escape(_STARS)}]")
#: Where helper text starts once it is glued onto a label. Jev reads a field's
#: accessible name, and on Lever that is "Current location No location found.
#: Try entering a different location Loading". Only the display name is cut;
#: fields are found again by ref, so a wrong cut costs readability, not a field.
_HELPER = re.compile(r"\s(No |Try |Loading|Couldn|Analyzing|ATTACH|Please |e\.g\.|"
                     r"Select |Choose |Upload |Drag |Max |Optional|Required)")


def _clean(label: str) -> str:
    return re.sub(r"\s+", " ", _STAR_RE.sub("", label or "")).strip()


def _required(label: str) -> bool:
    return bool(_STAR_RE.search(label or ""))


def _short(label: str) -> str:
    """The field's name as the agent sees it: the label without its star or the
    helper text stuck to it."""
    raw = label or ""
    m = _STAR_RE.search(raw)
    if m:
        raw = raw[:m.start()]
    raw = _clean(raw)
    h = _HELPER.search(raw)
    if h and h.start() > 0:
        raw = raw[:h.start()]
    return raw.split(". ")[0].strip() or _clean(label)


class Bridge:
    """A peer connection to the local Jev bridge. Starts the bridge if nobody
    has — outside Claude Code there is no MCP server to own the port."""

    def __init__(self, timeout: float = 60.0):
        self._proc = None
        if not self._healthy():
            self._start_owner()
        from websocket import create_connection
        token = TOKEN.read_text().strip()
        # suppress_origin: the bridge refuses any peer that sends an Origin,
        # because only web pages send one.
        self.ws = create_connection(f"ws://127.0.0.1:{PORT}/peer?token={token}",
                                    suppress_origin=True, timeout=timeout)
        self._ids = itertools.count(1)

    @staticmethod
    def _healthy() -> bool:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health",
                                        timeout=2) as r:
                return bool(json.loads(r.read()).get("extension"))
        except Exception:                             # noqa: BLE001
            return False

    def _start_owner(self) -> None:
        server = JEV_DIR / "mcp" / "server.mjs"
        if not server.exists():
            raise RuntimeError(f"Jev Browser Control not found at {JEV_DIR} "
                               f"(set JEV_BROWSER_DIR)")
        # A stdio MCP server lives as long as its stdin is open, so hold it.
        self._proc = subprocess.Popen(["node", str(server)], stdin=subprocess.PIPE,
                                      stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL)
        for _ in range(40):
            time.sleep(0.25)
            if self._healthy():
                return
        raise RuntimeError("Jev bridge started, but the Chrome extension did not "
                           "connect. Open Chrome and click the Jev icon.")

    def call(self, method: str, **params):
        rid = f"job-agent-{os.getpid()}-{next(self._ids)}"
        self.ws.send(json.dumps({"type": "request", "id": rid, "method": method,
                                 "params": params, "timeoutMs": 45_000}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") != rid or msg.get("type") != "response":
                continue
            if msg.get("error"):
                raise RuntimeError(msg["error"].get("message", "Jev error"))
            return msg["result"]

    def close(self) -> None:
        try:
            self.ws.close()
        finally:
            if self._proc:
                self._proc.terminate()


class JevBrowser(BaseBrowser):
    """The six functions over the Jev bridge. Opens the form in a new tab."""

    def __init__(self, url: str, app_id: int | None = None,
                 bridge: Bridge | None = None):
        super().__init__(app_id)
        self.bridge = bridge or Bridge()
        self.url = url
        self.tab = self.bridge.call("navigate", url=url, newTab=True)["tabId"]
        self._reopened = False
        self._fields: list[dict] = []
        #: name -> how to find it again: (kind, cleaned label, occurrence)
        self._where: dict[str, tuple[str, str, int]] = {}

    def _call(self, method: str, **params):
        """Every call on the form's tab goes through here.

        It is the person's own Chrome, and they are using it: the first live
        run lost its tab seconds after opening it, and the agent spent three
        steps reading "No tab with id" before asking them to reopen it. The
        tab is reopened once, and everything typed so far is forgotten —
        a fresh page holds none of it, and the submit guard must not think
        otherwise.
        """
        try:
            return self.bridge.call(method, tabId=self.tab, **params)
        except RuntimeError as exc:
            if "No tab with id" not in str(exc) or self._reopened:
                raise
            self._reopened = True
            self.tab = self.bridge.call("navigate", url=self.url, newTab=True)["tabId"]
            self.filled.clear()
            raise RuntimeError("the form's tab was closed, so it was opened "
                               "again and is empty now — read_form and fill "
                               "it again") from exc

    # --- reading ---------------------------------------------------------

    def _elements(self) -> list[dict]:
        return self._call("snapshot", full=True)["page"]["elements"]

    def _uploads(self) -> list[dict]:
        return self._call("listUploads").get("inputs", [])

    @staticmethod
    def _kind(e: dict) -> str | None:
        if e.get("options"):
            return "select"
        if e.get("role") in ("checkbox", "radio"):
            return e["role"]
        if e.get("editable"):
            return e.get("inputType") or ("textarea" if e.get("role") == "textarea"
                                          else "text")
        return None

    def read_form(self) -> list[dict]:
        fields, where, seen = [], {}, {}
        for e in self._elements():
            kind = self._kind(e)
            if not kind:
                continue
            short = _short(e.get("label"))
            if not short:
                continue
            n = seen[short] = seen.get(short, 0) + 1
            name = short if n == 1 else f"{short} ({n})"
            where[name] = (kind, short, n, e["ref"])
            f = {"name": name, "label": short, "type": kind,
                 "required": _required(e.get("label"))}
            if kind == "select":
                f["options"] = [o["label"] for o in e["options"]
                                if not o.get("disabled") and o.get("value") != ""]
            fields.append(f)
        for u in self._uploads():
            short = _short(u.get("label")) or "file"
            name = f"upload: {short}"
            where[name] = ("file", short, 1, u["ref"])
            fields.append({"name": name, "label": short, "type": "file",
                           "required": _required(u.get("label"))})
        self._fields, self._where = fields, where
        return fields

    def _find(self, elements: list[dict], name: str) -> dict | None:
        """The element for a field name. By the ref Jev tagged it with first —
        a label changes under you (Lever's location label grows the whole
        suggestion list while it is open) — then by its name, in order."""
        if name not in self._where:
            self.read_form()
        kind, short, n, ref = self._where.get(name, (None, name, 1, None))
        for e in elements:
            if e.get("ref") == ref and _short(e.get("label")).split(" ")[:1] == short.split(" ")[:1]:
                return e
        hits = [e for e in elements if _short(e.get("label")) == short]
        return hits[n - 1] if len(hits) >= n else None

    # --- the six -------------------------------------------------------

    def fill_field(self, name: str, value: str) -> str:
        try:
            el = self._find(self._elements(), name)
            if el is None:
                return (f"ERROR: no field named {name!r}. "
                        f"Fields: {sorted(self._where)}")
            kind = self._kind(el)
            picked = ""
            if kind == "select":
                page = self._call("select", ref=el["ref"], option=value).get("page")
                elements = (page or {}).get("elements") or self._elements()
                got = self._value(elements, name) or self._settled(name)
            elif kind in ("checkbox", "radio"):
                if el.get("checked") != "true":
                    self._call("click", ref=el["ref"])
                self.filled[name] = "on"
                return f"checked {name}"
            else:
                got, picked = self._type(el, name, value)
            if not got:
                return (f"ERROR: {name} would not keep a value. It is probably "
                        f"a widget that needs a choice from a list. Ask the "
                        f"user, or try an exact option name.")
            if picked:
                self.filled[name] = got
                return f"filled {name} — picked {got!r} from its suggestions"
            if value.lower() not in got.lower():
                self.filled[name] = got
                return (f"filled {name}, but it now reads {got!r} rather than "
                        f"{value!r} — the field rewrote it.")
            self.filled[name] = value
            return f"filled {name}"
        except Exception as exc:                      # noqa: BLE001
            return f"ERROR filling {name}: {exc}"

    def _type(self, el: dict, name: str, value: str) -> tuple[str, bool]:
        """Type, then read the value back *after focus has left the field*.

        Reading straight after typing is what reported Lever's location as
        filled while the form, a second later, showed it empty: an autocomplete
        holds the typed text until blur and only then throws it away. So the
        field is blurred (Tab) and read after. When a suggestion list opened —
        as separate options, or glued onto the field's own label, which is how
        Jev sees Lever's — the match is chosen first: clicked when it is its
        own element, ArrowDown + Enter when it is not. Enter is pressed only
        when a list was seen, because Enter in an ordinary field submits the
        form, and the submit guard cannot stop a keypress.
        """
        before = el.get("label") or ""
        self._call("type", ref=el["ref"], text=value)
        low = value.lower()
        # A suggestion box looks up its list online: Lever's took 1.1 s to
        # appear, and the first version gave up at 0.6 s. Only a field that
        # looks like one waits that long; an ordinary field would spend 3 s a
        # field on nothing.
        waits = 12 if self._suggests(el) else 1
        for _ in range(waits):
            time.sleep(0.25)
            elements = self._elements()
            now = self._find(elements, name) or el
            option = next((e for e in elements if e.get("role") == "option"
                           and low in (e.get("label") or "").lower()), None)
            glued = ((now.get("label") or "") != before
                     and low in (now.get("label") or "")[len(_short(before)):].lower())
            if option is not None or glued:
                break
        picked = False
        if option is not None:
            self._call("click", ref=option["ref"])
            picked = True
        elif glued:
            self._call("key", key="ArrowDown", ref=now["ref"])
            time.sleep(0.3)
            self._call("key", key="Enter", ref=now["ref"])
            picked = True
        time.sleep(0.3)
        self._call("key", key="Tab", ref=now["ref"])
        got = self._value(self._elements(), name) or self._settled(name)
        return got, picked

    @staticmethod
    def _suggests(el: dict) -> bool:
        """Does this field look like it opens a suggestion list?"""
        label = el.get("label") or ""
        return (el.get("role") == "combobox" or "expanded" in el
                or bool(re.search(r"Loading|No \w+ found|Try entering|Start typing",
                                  label)))

    def _value(self, elements: list[dict], name: str) -> str:
        """What the field shows: its value, or for a dropdown the option marked
        selected — whichever the snapshot carries."""
        el = self._find(elements, name) or {}
        got = _clean(el.get("value", ""))
        if not got and el.get("options"):
            got = next((_clean(o["label"]) for o in el["options"]
                        if o.get("selected") and o.get("value") != ""), "")
        return got

    def _settled(self, name: str, tries: int = 3, wait: float = 0.25) -> str:
        """Read again after the page has had a moment.

        `select` answers before the page's change handler has run, so the first
        live run read Gender as empty straight after choosing "Decline to
        self-identify" — the form showed it chosen, and the agent told the
        person it would not accept the value.
        """
        for _ in range(tries):
            time.sleep(wait)
            got = self._value(self._elements(), name)
            if got:
                return got
        return ""

    def upload_file(self, name: str, path: str) -> str:
        try:
            if not Path(path).exists():
                return f"ERROR: file not found: {path}"
            _kind, short, _n, ref = self._where.get(
                name, ("file", name.replace("upload: ", ""), 1, None))
            inputs = self._uploads()
            hit = next((u for u in inputs if u.get("ref") == ref), None) \
                or next((u for u in inputs if _short(u.get("label")) == short), None) \
                or (inputs[0] if len(inputs) == 1 else None)
            if hit is None:
                return f"ERROR: no file input named {name!r}"
            self._call("upload", ref=hit["ref"], paths=[str(Path(path).resolve())])
            self.filled[name] = path
            return f"uploaded {Path(path).name}"
        except Exception as exc:                      # noqa: BLE001
            return f"ERROR uploading to {name}: {exc}"

    def current_values(self) -> dict:
        """Read every field straight off the page. `self.filled` is only what we
        believe we typed; the submit guard must see what the form holds."""
        try:
            elements = self._elements()
        except Exception:                             # noqa: BLE001
            return dict(self.filled)
        out = {}
        for name, (kind, *_rest) in self._where.items():
            if kind == "file":
                if name in self.filled:
                    out[name] = self.filled[name]
                continue
            el = self._find(elements, name)
            if not el:
                continue
            if kind in ("checkbox", "radio"):
                if el.get("checked") == "true":
                    out[name] = "on"
            else:
                out[name] = _clean(el.get("value", ""))
        return out

    # --- the page, for code ----------------------------------------------

    def page_state(self) -> dict:
        try:
            snap = self._call("snapshot", full=True)["page"]
        except Exception:                             # noqa: BLE001
            return {}
        els = snap.get("elements", [])
        try:
            uploads = len(self._uploads())
        except Exception:                             # noqa: BLE001
            uploads = 0
        try:
            text = self._call("read", format="text", limit=6000).get("content") or ""
        except Exception:                             # noqa: BLE001
            text = ""
        buttons = [e for e in els if e.get("role") in ("button", "link") and e.get("label")]
        return {"url": snap.get("url", ""), "title": snap.get("title", ""),
                "fields": sum(1 for e in els if self._kind(e)) + uploads,
                "password": any((e.get("inputType") or "").lower() == "password" for e in els),
                "captcha": bool(re.search(r"captcha", text, re.I)) and not buttons,
                "buttons": [b["label"] for b in buttons], "refs": [b["ref"] for b in buttons],
                "text": text}

    def open_form(self, tries: int = 2) -> str:
        """See CloudBrowser.open_form: a job page's own Apply button."""
        done = []
        for _ in range(tries):
            st = self.page_state()
            if not st or st.get("fields", 0) >= 2 or st.get("password"):
                break
            i = pagecheck.entry_button(st["buttons"])
            if i is None:
                break
            self._call("click", ref=st["refs"][i])
            done.append(st["buttons"][i])
            time.sleep(3)
        return (f"pressed {' then '.join(repr(d) for d in done)} to open the form"
                if done else "")

    def describe(self, st: dict | None = None, limit: int = 700) -> str:
        st = st if st is not None else self.page_state()
        if not st:
            return "(could not read the page)"
        bits = [f"URL: {st.get('url')}", f"Form fields a person could fill: {st.get('fields', 0)}"]
        if st.get("password"):
            bits.append("It has a PASSWORD field: this is a sign-in or sign-up page.")
        if st.get("buttons"):
            bits.append(f"Buttons: {st['buttons'][:15]}")
        bits.append(f"Text: {(st.get('text') or '')[:limit]}")
        return "\n".join(bits)

    def submit(self) -> str:
        # The guard first, always: required fields, then ALLOW_SUBMIT.
        self._guard_submit(self._fields or self.read_form())
        st = self.page_state()
        i = pagecheck.submit_button(st.get("buttons", []))
        if i is None:
            nxt = pagecheck.next_button(st.get("buttons", []))
            if nxt:
                return (f"ERROR: NOT SENT. This form goes on to another page (it "
                        f"has a {nxt!r} button), and I can only fill one page. "
                        f"Nothing was sent. Stop and say so.")
            return "ERROR: NOT SENT. No submit button found on the page."
        label = st["buttons"][i]
        self._call("click", ref=st["refs"][i])
        after = {}
        for _ in range(10):
            time.sleep(1)
            after = self.page_state()
            if pagecheck.confirmed(st.get("text", ""), after.get("text", ""),
                                   st.get("url", ""), after.get("url", "")):
                return f"submitted — the page confirms it ({after.get('url')})"
        if after.get("url") == st.get("url") and after.get("fields", 0) >= max(1, st.get("fields", 0) - 1):
            said = pagecheck.complaints(after.get("text", ""))
            return (f"ERROR: NOT SENT. Pressed {label!r} and the form is still there"
                    + (f"; the page says: {said}" if said else ", with no confirmation")
                    + ". Fix what it says, or stop and say so.")
        return (f"unconfirmed — pressed {label!r} and the page changed, but it does "
                f"not say the application was received. It may or may not have gone "
                f"through: do not submit again. Page now: {(after.get('text') or '')[:300]}")

    def screenshot(self) -> str:
        SHOTS.mkdir(parents=True, exist_ok=True)
        shot = self._call("screenshot")
        p = SHOTS / f"app_{self.app_id or 0}.jpg"
        p.write_bytes(base64.b64decode(shot["data"]))
        return f"saved {p}\nWhat the page shows:\n{self.describe()}"

    def close(self) -> None:
        try:
            self.bridge.call("tabs.close", tabId=self.tab)
        except Exception:                             # noqa: BLE001
            pass
        self.bridge.close()
