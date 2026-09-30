"""Yes before submit (apply/confirm.py): nothing is sent until the person has
seen the filled answers and said "submit" themselves.

No network, no real table, no real form: a fixture form and an in-memory
database. ALLOW_SUBMIT is set per test and only through monkeypatch.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from apply import confirm                              # noqa: E402
from apply.browser import ManualBrowser, Park, SubmitRefused   # noqa: E402
from engine import db                                  # noqa: E402

JOB = {"title": "Product Manager", "company": "Acme", "source": "lever",
       "source_id": "1", "apply_url": "https://jobs.lever.co/acme/1"}


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    c.execute("INSERT INTO applications (id, source, source_id, company, title) "
              "VALUES (1, 'lever', '1', 'Acme', 'Product Manager')")
    c.commit()
    yield c
    c.close()


@pytest.fixture
def armed(monkeypatch):
    monkeypatch.setenv("ALLOW_SUBMIT", "1")
    monkeypatch.setenv("SUBMIT_USERS", "default")


def filled(conn, value="x"):
    b = ManualBrowser("greenhouse_sample", app_id=1)
    b.approval = confirm.Approval(conn, 1, JOB)
    for f in b.read_form():
        b.filled[f["name"]] = value
    return b


def test_the_first_submit_parks_with_every_answer(conn, armed):
    b = filled(conn)
    with pytest.raises(Park) as park:
        b.submit()
    q = park.value.question
    assert q.startswith(confirm.READY) and "Acme" in q and "First Name: x" in q
    assert "Reply 'submit'" in q


def test_approved_answers_are_sent(conn, armed):
    with pytest.raises(Park):
        filled(conn).submit()
    confirm.approve(conn, 1)
    assert "submitted" in filled(conn).submit()     # the resumed run fills it again


def test_a_form_that_changed_after_approval_asks_again(conn, armed):
    with pytest.raises(Park):
        filled(conn).submit()
    confirm.approve(conn, 1)
    with pytest.raises(Park) as park:
        filled(conn, value="y").submit()
    assert "changed since you said submit" in park.value.question


def test_nobody_submits_unless_named(conn, monkeypatch):
    monkeypatch.setenv("ALLOW_SUBMIT", "1")
    monkeypatch.setenv("SUBMIT_USERS", "1000000001")        # someone else
    with pytest.raises(SubmitRefused, match="only for"):
        filled(conn).submit()
    monkeypatch.delenv("SUBMIT_USERS")
    with pytest.raises(SubmitRefused, match="nobody"):
        filled(conn).submit()


def test_no_approval_step_means_nothing_is_sent(armed):
    b = ManualBrowser("greenhouse_sample", app_id=1)
    for f in b.read_form():
        b.filled[f["name"]] = "x"
    with pytest.raises(SubmitRefused, match="approval"):
        b.submit()


@pytest.mark.parametrize("said,ok", [
    ("submit", True), ("yes send it", True), ("ok go ahead", True),
    ("yes but change the notice period to 30 days", False), ("no wait", False),
    ("don't submit", False), ("what does it say?", False), (None, False)])
def test_only_their_own_submit_counts(said, ok):
    assert confirm.said_submit(said) is ok


def test_the_chat_approves_from_their_words_not_the_models(conn, armed, monkeypatch):
    import chat
    from apply import agent as apply_agent
    conn.execute("UPDATE applications SET status = 'waiting', question = ? WHERE id = 1",
                 (confirm.READY + ": Product Manager at Acme.",))
    conn.commit()
    ran = []
    monkeypatch.setattr(apply_agent, "browser_for", lambda *a, **k: None)
    monkeypatch.setattr(apply_agent, "resume_run",
                        lambda app_id, answer, browser, c: ran.append(answer) or {"status": "blocked"})
    state = {"turn": 3, "picks": [], "resumes": {}, "fixture": "greenhouse_sample",
             "user_message": "hmm what is the notice period there?"}
    chat.run_tool("answer_application", {"app_id": 1, "answer": "yes submit"}, state, conn)
    assert conn.execute("SELECT confirmed_at FROM applications").fetchone()[0] is None
    assert "did not say submit" in ran[-1]
    state["user_message"] = "submit"
    conn.execute("UPDATE applications SET status = 'waiting'")
    chat.run_tool("answer_application", {"app_id": 1, "answer": "ok"}, state, conn)
    assert conn.execute("SELECT confirmed_at FROM applications").fetchone()[0] is not None
