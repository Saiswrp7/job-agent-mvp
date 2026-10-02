"""The agent eval's pure helpers: counting questions on a page, spotting the
ones the reader dropped, reading a run's log, and the pass rule. No model, no
Browserbase; the page test uses a local headless Chromium."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evals import apply_agent as ev             # noqa: E402

REFUSAL = "ALLOW_SUBMIT is not set — refusing to submit a real application. Everything up to this point ran."


def _row(i, kind, payload):
    return {"id": i, "step": 0, "kind": kind,
            "payload": payload if isinstance(payload, str) else json.dumps(payload)}


def test_radio_group_is_one_question_and_dropped_needs_no_kept_ref():
    controls = [
        {"ref": "1", "type": "text", "name": "name", "hint": "Name", "required": True, "value": "x"},
        {"ref": "2", "type": "radio", "group": "reloc", "name": "reloc", "hint": "Yes", "value": ""},
        {"ref": "3", "type": "radio", "group": "reloc", "name": "reloc", "hint": "No", "value": "on"},
        {"ref": "4", "type": "text", "name": "salary", "hint": "", "required": False, "value": ""},
        {"ref": "5", "type": "checkbox", "name": "li", "hint": "Apply with LinkedIn", "value": ""},
    ]
    qs = ev.questions(controls)
    assert len(qs) == 4
    reloc = qs[1]
    assert reloc["refs"] == ["2", "3"] and reloc["value"] == "on"
    kept = ev.kept_refs({"Name": {"kind": "text", "ref": "1"},
                         "Relocate": {"kind": "radio", "options": {"Yes": "2", "No": "3"}}})
    assert kept == {"1", "2", "3"}
    drop = ev.dropped(qs, kept)
    assert [q["name"] for q in drop] == ["salary"]      # the LinkedIn hand-off is not a drop


def test_read_log_finds_the_submit_off_refusal_pages_and_replay():
    rows = [
        _row(1, "browser", {"replay": "https://www.browserbase.com/sessions/abc"}),
        _row(2, "prompt", {"messages": 1}),
        _row(3, "tool_call", {"name": "next_page", "input": {}}),
        _row(4, "tool_result", "on the next page now: 5 fields."),
        _row(5, "prompt", {"messages": 3}),
        _row(6, "tool_call", {"name": "submit", "input": {}}),
        _row(7, "tool_result", f"REFUSED: {REFUSAL}"),
    ]
    log = ev.read_log(rows, REFUSAL)
    assert log["reached_submit"] and not log["sent"]
    assert log["pages"] == 2 and log["steps"] == 2
    assert log["replays"] == ["https://www.browserbase.com/sessions/abc"]


def test_required_empty_refusal_is_not_reaching_submit():
    rows = [_row(1, "tool_call", {"name": "submit", "input": {}}),
            _row(2, "tool_result", "REFUSED: required fields still empty: ['Phone']")]
    assert not ev.read_log(rows, REFUSAL)["reached_submit"]


def test_a_send_is_flagged():
    rows = [_row(1, "tool_call", {"name": "next_page", "input": {}}),
            _row(2, "tool_result", "submitted — pressing Next sent it.")]
    log = ev.read_log(rows, REFUSAL)
    assert log["sent"]
    assert ev.classify("blocked", log, {})[0] == "SENT"


def test_pass_needs_submit_reached_nothing_dropped_nothing_required_empty():
    reached = {"reached_submit": True}
    assert ev.classify("blocked", reached, {"dropped": [], "required_empty": []})[1]
    klass, ok, why = ev.classify("blocked", reached, {"dropped": [{"hint": "*", "name": "s"}],
                                                      "required_empty": []})
    assert klass == "reached_submit" and not ok and "not read" in why[0]
    assert not ev.classify("blocked", reached, {"dropped": [], "required_empty": ["Phone"]})[1]
    assert ev.classify("waiting", {}, {})[0] == "parked"
    assert ev.classify("sign_in", {}, {})[0] == "sign_in"
    assert ev.classify("failed", {}, {})[0] == "failed"


def test_sign_in_and_captcha_questions_are_not_answered():
    assert ev.resumable("What is your notice period and expected CTC?")
    assert not ev.resumable("This page wants you to sign in before it shows the application.")
    assert not ev.resumable("I see a captcha on the page.")


def test_vault_keys_are_real_vault_fields():
    import vault
    assert set(ev.VAULT) <= set(vault.FIELDS)
    assert ev.VAULT["email"].endswith("@example.com")


pw = pytest.importorskip("playwright.sync_api")

PAGE = """<html><body>
<div class="cookie-banner"><input type="checkbox" name="cookies"></div>
<input type="search" name="q">
<form>
  <label for="n">Full name *</label><input id="n" name="n" required>
  <div><span>*</span><input name="salary"></div>
  <input type="hidden" name="token" value="t">
  <input name="hp_trap" style="position:absolute;left:-9999px">
  <input name="gone" style="display:none">
  <fieldset><legend>Relocate?</legend>
    <label><input type="radio" name="r" value="y">Yes</label>
    <label><input type="radio" name="r" value="n">No</label></fieldset>
  <input type="file" name="resume" style="display:none">
  <button type="submit">Submit</button>
</form></body></html>"""


def test_count_js_counts_what_a_person_would_answer():
    with pw.sync_playwright() as p:
        try:
            b = p.chromium.launch(headless=True)
        except Exception as exc:                      # noqa: BLE001
            pytest.skip(f"no local chromium: {exc}")
        page = b.new_page()
        page.set_content(PAGE)
        qs = ev.questions(page.evaluate(ev.count_js()))
        b.close()
    names = [q["name"] for q in qs]
    # name, the "*"-only salary box, the radio group, the resume file box;
    # not the cookie switch, search, hidden, honeypot, display:none, or button.
    assert names == ["n", "salary", "r", "resume"]
    assert qs[0]["required"] and qs[0]["hint"].startswith("Full name")


def test_unnamed_boxes_are_found_and_fail_only_when_empty():
    fields = [{"name": "Email", "label": "Email", "type": "email"},
              {"name": "field 12", "label": "field 12", "type": "text"},
              {"name": "cards[a][field0]", "label": "cards[a][field0]", "type": "text"},
              {"name": "First name (2)", "label": "First name", "type": "text"},
              {"name": "upload: file", "label": "file", "type": "file"}]
    assert ev.unlabeled(fields) == ["field 12", "cards[a][field0]"]
    reached = {"reached_submit": True}
    base = {"dropped": [], "required_empty": []}
    assert ev.classify("blocked", reached, {**base, "unlabeled": ["field 12"], "unlabeled_empty": []})[1]
    _, ok, why = ev.classify("blocked", reached, {**base, "unlabeled_empty": ["field 12"]})
    assert not ok and "unnamed" in why[0]
