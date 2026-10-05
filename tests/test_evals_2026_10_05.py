"""Sai's Telegram evals of 2026-10-05: the fixes, one test or more each.
Fake values only (public repo)."""

from __future__ import annotations

import datetime
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import vault                                       # noqa: E402


def _conn(**values):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE vault (key TEXT PRIMARY KEY, value TEXT, source TEXT, "
              "updated_at TEXT)")
    for k, v in values.items():
        c.execute("INSERT INTO vault (key, value, source) VALUES (?, ?, 'user')", (k, v))
    return c


# --- salary questions go to the right salary ---------------------------------

def test_expected_and_current_pay_questions_are_told_apart():
    m = vault.match_key
    assert m("What is your expected fixed CTC?") == "expected_ctc"
    assert m("Expected fixed CTC") == "expected_ctc"
    assert m("What is your expected annual salary?") == "expected_ctc"
    assert m("Salary expectation") == "expected_ctc"
    assert m("Desired salary") == "expected_ctc"
    assert m("What is your current annual salary?") == "current_ctc"
    assert m("Current fixed CTC") == "current_fixed_ctc"
    assert m("Current variable pay") == "current_variable_ctc"
    assert m("What is your current CTC") == "current_ctc"
    assert m("Current Salary (currency)") is None
    assert m("Salary") is None                       # can't tell: ask


def test_joining_date_is_not_the_notice_period():
    assert vault.match_key("Earliest joining date") == "joining_date"
    assert vault.match_key("Notice period") == "notice_period"


def test_fixed_pay_falls_back_to_total_when_there_is_no_variable():
    c = _conn(current_ctc="9 LPA", current_variable_ctc="0", expected_ctc="12 LPA")
    got = vault.slice_for(["Current fixed CTC", "Expected fixed CTC"], c)
    assert got == {"Current fixed CTC": "9 LPA", "Expected fixed CTC": "12 LPA"}


def test_joining_date_comes_from_the_notice_period():
    assert vault._join_date("30 days", datetime.date(2026, 1, 1)) == "2026-01-31"
    assert vault._join_date("within 2 weeks", datetime.date(2026, 1, 1)) == "2026-01-15"
    assert vault._join_date("immediately") is None


def test_an_expected_salary_answer_never_lands_in_the_current_salary():
    from apply import harness
    c = _conn(current_ctc="9 LPA")
    history = [{"role": "user", "content": "12 lpa rupees"}]
    harness.remember_answer(c, history, {"name": "Expected fixed CTC", "value": "12"})
    assert vault.get("current_ctc", c) == "9 LPA"
    assert vault.get("current_fixed_ctc", c) is None


# --- years in one field are not total years ----------------------------------

def test_years_in_a_named_field_are_asked_not_taken_from_the_total():
    m = vault.match_key
    assert m("How many years of experience do you have in Software Development?") is None
    assert m("Years of experience in Product Management") is None
    assert m("Years in sales") is None
    assert m("Years of experience") == "years_experience"
    assert m("Total years of experience") == "years_experience"
    assert m("How many years of experience do you have?") == "years_experience"
    assert m("Experience (in years)") == "years_experience"
    assert m("Relevant experience") == "years_experience"


# --- shared fixture: one in-memory profile with jobs and applications --------

import pytest                                       # noqa: E402

from engine import db                               # noqa: E402


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    for sid, desc in (("A1", "Meridian team"), ("A2", "Retention team")):
        c.execute("INSERT INTO jobs (source, source_id, company, title, description, "
                  "apply_url, apply_kind, first_seen, last_seen) VALUES "
                  "('lever', ?, 'Acme', 'Product Manager II', ?, ?, 'form', "
                  "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                  (sid, desc, f"https://jobs.lever.co/acme/{sid}/apply"))
    c.execute("INSERT INTO applications (id, source, source_id, company, title, status) "
              "VALUES (6, 'lever', 'A2', 'Acme', 'Product Manager II', 'failed')")
    c.commit()
    yield c
    c.close()


# --- a retry is the same job; same-title jobs get different names ------------

def test_two_jobs_with_one_title_get_different_names():
    from search.rank import name_refs
    picks = [{"source": "lever", "source_id": "A1", "company": "Acme", "title": "PM II"},
             {"source": "lever", "source_id": "A2", "company": "Acme", "title": "PM II"}]
    name_refs(picks)
    assert [p["ref"] for p in picks] == ["acme-pm-ii", "acme-pm-ii-2"]
    later = [{"source": "lever", "source_id": "A3", "company": "Acme", "title": "PM II"}]
    name_refs(later, {"acme-pm-ii": "lever:A1", "acme-pm-ii-2": "lever:A2"})
    assert later[0]["ref"] == "acme-pm-ii-3"
    again = [{"source": "lever", "source_id": "A1", "company": "Acme", "title": "PM II"}]
    name_refs(again, {"acme-pm-ii": "lever:A1"})
    assert again[0]["ref"] == "acme-pm-ii"               # the same job keeps its name


def test_a_retry_resolves_to_the_applications_own_job(conn):
    import chat
    state = {"by_ref": {}}
    ref = chat._ref_of_application(conn, state, 6)
    assert state["by_ref"][ref]["source_id"] == "A2"
    assert chat._ref_of_application(conn, state, 6) == ref     # not added twice
    with pytest.raises(KeyError):
        chat._ref_of_application(conn, state, 99)


# --- a run that ends asking is waiting, not blocked ---------------------------

class _Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Reply:
    def __init__(self, content, stop):
        self.content, self.stop_reason = content, stop


def _scripted(monkeypatch, replies):
    from apply import harness
    import llm
    it = iter(replies)

    class Client:
        class messages:
            @staticmethod
            def create(**kw):
                return next(it)
    monkeypatch.setattr(llm, "client", lambda: Client)
    monkeypatch.setattr(harness, "save", lambda *a, **k: None)
    monkeypatch.setattr(harness, "trace", lambda *a, **k: None)
    import usage
    monkeypatch.setattr(usage, "record", lambda *a, **k: None)
    return harness


def test_questions_in_the_last_words_park_the_run(conn, monkeypatch):
    harness = _scripted(monkeypatch, [_Reply(
        [_Block(type="text", text="1. What is your expected fixed CTC?\n2. Joining date?")],
        "end_turn")])
    r = harness.run(6, browser=None, system="s", first_message="go", conn=conn)
    assert r["status"] == "waiting" and "expected fixed CTC" in r["question"]


def test_a_cancelled_run_stops_before_its_next_step(conn, monkeypatch):
    from apply import worker
    harness = _scripted(monkeypatch, [])
    assert worker.cancel(conn, 6) == "failed"
    r = harness.run(6, browser=None, system="s", first_message="go", conn=conn)
    assert r["status"] == "cancelled"
    row = conn.execute("SELECT status FROM applications WHERE id = 6").fetchone()
    assert row[0] == "cancelled"
    worker._cancelled.discard(6)


def test_a_sent_application_cannot_be_cancelled(conn):
    from apply import worker
    conn.execute("UPDATE applications SET status = 'submitted' WHERE id = 6")
    assert worker.cancel(conn, 6) == "submitted"
    assert conn.execute("SELECT status FROM applications").fetchone()[0] == "submitted"


# --- approvals typed on a phone -----------------------------------------------

def test_typed_approvals_count_and_questions_do_not():
    from apply import confirm
    for said in ("Yea", "Sumbit", "submti", "haan", "Submit all"):
        assert confirm.said_submit(said), said
    for said in ("Submitted?", "Done?", "yes but change the notice period", "no"):
        assert not confirm.said_submit(said), said


def test_an_update_the_reply_already_covers_is_not_sent_twice():
    import chat
    from apply import confirm
    news = [{"company": "Zeta", "status": "failed", "outcome": "interrupted"},
            {"company": "Swiggy", "status": "waiting", "outcome": confirm.READY + ": x"},
            {"company": "Paytm", "status": "blocked", "outcome": "not sent"}]
    left = chat.untold(news, "Not yet, Zeta never went out. Swiggy is ready.")
    assert [r["company"] for r in left] == ["Swiggy", "Paytm"]


# --- a restart never cuts someone off -----------------------------------------

def test_busy_names_what_a_restart_would_cut_off(tmp_path):
    import ops
    p = tmp_path / "tg-1"
    p.mkdir()
    c = sqlite3.connect(p / "jobs.db")
    c.execute("CREATE TABLE applications (id INTEGER, company TEXT, status TEXT, "
              "question TEXT, updated_at TEXT)")
    c.execute("CREATE TABLE messages (role TEXT, created_at TEXT)")
    c.commit()
    assert ops.busy(tmp_path) == []
    c.execute("INSERT INTO applications VALUES (1, 'Acme', 'running', NULL, CURRENT_TIMESTAMP)")
    c.execute("INSERT INTO applications VALUES (2, 'Beta', 'waiting', 'READY TO SUBMIT: x', "
              "CURRENT_TIMESTAMP)")
    c.execute("INSERT INTO messages VALUES ('user', CURRENT_TIMESTAMP)")
    c.commit()
    got = ops.busy(tmp_path)
    assert len(got) == 3 and "Acme" in got[0] and "submit" in got[1]
