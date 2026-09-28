"""The Jev browser, against a fake bridge. No Chrome, no extension, no network.

The fake answers the same methods the real bridge does (snapshot, type, select,
click, listUploads, upload, screenshot) with the same shapes, recorded from
the practice form on 2026-09-24.
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from apply.browser import SubmitRefused     # noqa: E402
from apply.jev import JevBrowser            # noqa: E402


class FakeBridge:
    """A form held in memory. `sticky=False` on a field makes it drop whatever
    is typed, like Lever's location autocomplete."""

    def __init__(self, elements, uploads=None):
        self.elements = elements
        self.uploads = uploads or []
        self.calls = []

    def _page(self):
        return {"page": {"elements": [dict(e) for e in self.elements],
                         "url": "https://form"}}

    def _el(self, ref):
        return next(e for e in self.elements if e["ref"] == ref)

    def call(self, method, **p):
        self.calls.append((method, p))
        if method == "navigate":
            return {"tabId": 7, **self._page()}
        if method == "snapshot":
            return {"tabId": 7, **self._page()}
        if method == "listUploads":
            return {"inputs": self.uploads}
        if method == "type":
            e = self._el(p["ref"])
            e["value"], e["chosen"] = p["text"], False
            if e.get("suggestions"):
                # Lever: the open list is glued onto the field's own label.
                e.setdefault("base_label", e["label"])
                e["label"] = e["base_label"] + " " + " ".join(e["suggestions"])
            return {"typed": p["text"], **self._page()}
        if method == "key":
            e = self._el(p["ref"])
            if p["key"] == "Enter" and e.get("suggestions"):
                e["value"], e["chosen"] = e["suggestions"][0], True
            if p["key"] == "Tab":
                if not e.get("sticky", True) and not e.get("chosen"):
                    e["value"] = ""          # dropped on blur, unless chosen
                if e.get("base_label"):
                    e["label"] = e["base_label"]
            return self._page()
        if method == "select":
            self._el(p["ref"])["value"] = p["option"]
            return {"selected": p["option"], **self._page()}
        if method == "click":
            e = self._el(p["ref"])
            if e.get("role") == "option":
                target = self._el(e["fills"])
                target["value"], target["chosen"] = e["label"], True
            if e.get("role") == "checkbox":
                e["checked"] = "true"
            return self._page()
        if method == "upload":
            return {"uploaded": p["paths"]}
        if method == "screenshot":
            return {"mimeType": "image/jpeg", "data": base64.b64encode(b"jpg").decode()}
        if method == "tabs.close":
            return {}
        raise AssertionError(f"unexpected {method}")

    def close(self):
        pass


def form(overrides=None):
    els = [
        {"ref": 1, "role": "textbox", "label": "First Name *", "editable": True},
        {"ref": 2, "role": "textbox", "label": "Email *", "editable": True,
         "inputType": "email"},
        {"ref": 3, "role": "combobox", "label": "Gender", "options": [
            {"label": "Select…", "value": "", "selected": True},
            {"label": "Female", "value": "Female"}]},
        {"ref": 4, "role": "checkbox", "label": "I agree *", "checked": "false"},
        {"ref": 5, "role": "button", "label": "Submit application"},
    ]
    for e in els:
        e.update((overrides or {}).get(e["ref"], {}))
    return FakeBridge(els, uploads=[{"ref": 9, "label": "resume", "hidden": True}])


def browser(bridge):
    return JevBrowser("https://form", app_id=1, bridge=bridge)


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr("apply.jev.time.sleep", lambda s: None)


def test_read_form_gives_the_agent_clean_fields():
    fields = {f["name"]: f for f in browser(form()).read_form()}
    assert fields["First Name"]["required"] is True
    assert fields["Gender"]["type"] == "select"
    assert fields["Gender"]["options"] == ["Female"]
    assert fields["I agree"]["type"] == "checkbox"
    assert fields["upload: resume"]["type"] == "file"
    assert "Submit application" not in fields          # buttons are not fields


def test_fill_reads_the_value_back():
    b = browser(form())
    b.read_form()
    assert b.fill_field("First Name", "Asha") == "filled First Name"
    assert b.fill_field("Gender", "Female") == "filled Gender"
    assert b.filled == {"First Name": "Asha", "Gender": "Female"}


def test_a_field_that_drops_the_value_is_reported_as_an_error():
    """The bug this came from: the trace said 'filled location' while the form
    showed it empty, so the submit guard counted a required field as done."""
    b = browser(form({1: {"sticky": False}}))
    b.read_form()
    result = b.fill_field("First Name", "Asha")
    assert result.startswith("ERROR")
    assert "First Name" not in b.filled


def test_an_autocomplete_is_resolved_by_choosing_the_suggestion():
    bridge = form({1: {"sticky": False}})
    bridge.elements.append({"ref": 6, "role": "option", "label": "Bengaluru, India",
                            "fills": 1})
    b = browser(bridge)
    b.read_form()
    assert b.fill_field("First Name", "Bengaluru").startswith("filled")
    assert ("click", {"ref": 6, "tabId": 7}) in bridge.calls


def test_upload_goes_to_the_file_input(tmp_path):
    cv = tmp_path / "cv.pdf"
    cv.write_text("x")
    bridge = form()
    b = browser(bridge)
    b.read_form()
    assert b.upload_file("upload: resume", str(cv)) == "uploaded cv.pdf"
    method, params = bridge.calls[-1]
    assert method == "upload" and params["ref"] == 9


def test_submit_is_refused_while_a_required_field_is_empty(monkeypatch):
    monkeypatch.setenv("ALLOW_SUBMIT", "1")
    bridge = form()
    b = browser(bridge)
    b.read_form()
    b.fill_field("First Name", "Asha")
    with pytest.raises(SubmitRefused, match="Email"):
        b.submit()
    assert not any(m == "click" for m, _ in bridge.calls)


def test_submit_is_refused_without_allow_submit_even_when_complete(monkeypatch):
    monkeypatch.delenv("ALLOW_SUBMIT", raising=False)
    bridge = form()
    b = browser(bridge)
    b.read_form()
    b.fill_field("First Name", "Asha")
    b.fill_field("Email", "a@b.com")
    b.fill_field("I agree", "yes")
    with pytest.raises(SubmitRefused, match="ALLOW_SUBMIT"):
        b.submit()
    assert not any(m == "click" and p["ref"] == 5 for m, p in bridge.calls)


def test_a_closed_tab_is_reopened_once_and_forgets_what_was_typed():
    bridge = form()
    real = bridge.call
    state = {"gone": False}

    def call(method, **p):
        if state["gone"] and p.get("tabId") == 7 and method != "navigate":
            raise RuntimeError("No tab with id 7. Call browser_tabs to list tabs.")
        r = real(method, **p)
        if method == "navigate" and state["gone"]:
            return {**r, "tabId": 8}
        return r
    bridge.call = call
    b = browser(bridge)
    b.read_form()
    b.fill_field("First Name", "Asha")
    state["gone"] = True
    with pytest.raises(RuntimeError, match="opened again"):
        b.read_form()
    assert b.tab == 8 and b.filled == {}
    state["gone"] = False
    assert b.read_form()                      # works on the new tab


def test_a_parked_application_saves_reply_blocks_as_objects():
    """GLM 5.3 always returns thinking blocks. Saved as their repr, a resumed
    application sent strings where the API needs objects: a 400."""
    import sqlite3
    from anthropic.types import TextBlock, ThinkingBlock
    from apply import harness
    from engine import db

    conn = db.connect(":memory:")
    conn.execute("INSERT INTO applications (id, company) VALUES (1, 'X')")
    history = [{"role": "user", "content": "fill it"},
               {"role": "assistant", "content": [
                   ThinkingBlock(type="thinking", thinking="hm", signature="s"),
                   TextBlock(type="text", text="asking")]}]
    harness.save(conn, 1, history, "waiting", "notice period?")
    back = harness.load(conn, 1)
    assert back[1]["content"][0] == {"type": "thinking", "thinking": "hm",
                                     "signature": "s"}
    assert back[1]["content"][1]["text"] == "asking"


def test_a_dropdown_that_updates_a_moment_later_is_not_reported_as_failed(monkeypatch):
    """The live bug: select answered before the page updated, so Gender read
    empty and the agent said it would not accept the value. It had."""
    monkeypatch.setattr("apply.jev.time.sleep", lambda s: None)
    bridge = form()
    real = bridge.call
    pending = {}

    def call(method, **p):
        if method == "select":                       # the page catches up later
            pending["v"] = p["option"]
            return {"selected": p["option"]}
        if method == "snapshot" and "v" in pending and pending.get("seen"):
            bridge._el(3)["value"] = pending.pop("v")
        if method == "snapshot" and "v" in pending:
            pending["seen"] = True
        return real(method, **p)
    bridge.call = call
    b = browser(bridge)
    b.read_form()
    assert b.fill_field("Gender", "Female") == "filled Gender"
    assert b.filled["Gender"] == "Female"


def test_a_dropdown_value_can_come_from_the_selected_option():
    bridge = form({3: {"options": [{"label": "Select…", "value": "", "selected": False},
                                    {"label": "Female", "value": "F", "selected": True}]}})
    b = browser(bridge)
    b.read_form()
    assert b._value(bridge.elements, "Gender") == "Female"


def test_lever_stars_mean_required_and_never_reach_the_name():
    bridge = form({1: {"label": "Full name ✱"},
                   2: {"label": "Current location No location found. Try entering a different location Loading"}})
    bridge.uploads = [{"ref": 9, "label": "Resume/CV ✱ ATTACH RESUME/CV Couldn't auto-read resume."}]
    fields = {f["name"]: f for f in browser(bridge).read_form()}
    assert fields["Full name"]["required"] is True
    assert "Current location" in fields and fields["Current location"]["required"] is False
    assert fields["upload: Resume/CV"]["required"] is True


def test_lever_location_is_chosen_from_its_suggestions_with_the_keyboard():
    """The live CRED run: typed Bengaluru, reported filled, and Lever emptied it
    on blur because nothing was chosen from the list."""
    bridge = form({1: {"label": "Current location", "sticky": False,
                       "suggestions": ["Bengaluru, Bengaluru Urban, Karnataka, IND"]}})
    b = browser(bridge)
    b.read_form()
    out = b.fill_field("Current location", "Bengaluru")
    assert "picked 'Bengaluru, Bengaluru Urban, Karnataka, IND'" in out
    assert b.current_values()["Current location"].startswith("Bengaluru, Bengaluru Urban")


def test_enter_is_never_pressed_in_an_ordinary_field():
    """Enter in a plain field submits the form, and the guard cannot stop it."""
    bridge = form()
    b = browser(bridge)
    b.read_form()
    b.fill_field("First Name", "Asha")
    assert not any(m == "key" and p["key"] == "Enter" for m, p in bridge.calls)


def test_the_agent_can_never_ask_for_a_password():
    from apply.browser import ManualBrowser, Park
    b = ManualBrowser.__new__(ManualBrowser)
    for q in ("Could you provide the password for Saiswrp7@live.com?",
              "What is the OTP sent to your phone?"):
        with pytest.raises(Park) as got:
            b.ask_user(q)
        assert "password" not in got.value.question.split("never")[0].lower()
        assert "sign in" in got.value.question
    with pytest.raises(Park) as got:
        b.ask_user("What is your notice period?")
    assert got.value.question == "What is your notice period?"
