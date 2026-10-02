"""An invited user's AccorHotel run (2026-10-01): the form was filled and our own switch
(SUBMIT_USERS, sending on only for Sai) refused the submit. The model then
wrote "the site refused the submission for this account", and the chat said
"the site blocked it... retrying". Neither was true.

Now code ends the run at that refusal and writes the reason itself.
No network: an in-memory database and a scripted model.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import chat                                       # noqa: E402
import llm                                        # noqa: E402
from apply import confirm, harness                # noqa: E402
from apply.browser import BaseBrowser             # noqa: E402
from engine import db                             # noqa: E402

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"


class _Filled(BaseBrowser):
    """A filled form whose submit goes through the real guard."""

    def __init__(self):
        super().__init__(1)

    def current_values(self) -> dict:
        return {}

    def submit(self):
        self._guard_submit([])
        return "submitted — the page confirms it (x)"


@pytest.fixture
def only_sai(monkeypatch):
    monkeypatch.setenv(confirm.SUBMIT_ENV, "1")
    monkeypatch.setenv(confirm.USERS_ENV, "1000000001")
    monkeypatch.setattr(confirm, "me", lambda: "1000000003")


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    c.execute("INSERT INTO applications (id, source, source_id, company, title) "
              "VALUES (1, 'smartrecruiters', '744000149239879', 'AccorHotel', "
              "'Director of Sales NSO')")
    c.commit()
    yield c
    c.close()


def _script(monkeypatch, last_words):
    """A model that presses submit, then would explain in its own words."""
    replies = [
        NS(stop_reason="tool_use", content=[NS(type="tool_use", id="t1", name="submit", input={},
                                               model_dump=lambda **k: {"type": "tool_use", "id": "t1",
                                                                        "name": "submit", "input": {}})]),
        NS(stop_reason="end_turn", content=[NS(type="text", text=last_words,
                                               model_dump=lambda **k: {"type": "text", "text": last_words})]),
    ]

    class Msgs:
        def create(self, **kw):
            return replies.pop(0)
    monkeypatch.setattr(llm, "client", lambda p=None: NS(messages=Msgs()))
    monkeypatch.setattr("usage.record", lambda *a, **k: None)
    return replies


def test_the_refusal_names_our_switch_not_the_site(only_sai):
    why = confirm.not_allowed()
    assert confirm.SENDING_OFF in why and "not the site" in why


def test_the_run_stops_at_the_refusal_and_code_says_why(monkeypatch, conn, only_sai):
    left = _script(monkeypatch, "Not sent — the site refused the submission for this account.")
    r = harness.run(1, _Filled(), "s", first_message="go", conn=conn)
    assert r["status"] == "blocked" and not r["submitted"]
    assert r["message"] == confirm.SENDING_OFF_MESSAGE
    assert len(left) == 1          # the model was not asked to explain it
    assert conn.execute("SELECT status FROM applications").fetchone()[0] == "blocked"


def test_the_update_carries_the_whole_reason(only_sai):
    row = {"title": "Director of Sales NSO", "company": "AccorHotel",
           "status": "blocked", "outcome": confirm.SENDING_OFF_MESSAGE}
    line = chat.news_lines([row])
    # The update quotes an outcome's first sentence only: all of it must fit.
    assert confirm.SENDING_OFF_MESSAGE in line
    assert "not the site" in line and "site blocked" not in line


def test_people_with_sending_on_are_not_stopped(monkeypatch, conn, only_sai):
    monkeypatch.setattr(confirm, "me", lambda: "1000000001")
    assert confirm.not_allowed() is None


def test_the_chat_prompt_has_no_canned_retrying_line():
    text = (PROMPTS / "chat.md").read_text()
    assert "Retrying 🔁" not in text and '("Retrying now.")' not in text
