"""Background applications. A temporary database file (threads need their own
connections, so :memory: cannot be shared), and `work` is a stub — no browser,
no model."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from apply import worker    # noqa: E402
from engine import db       # noqa: E402


@pytest.fixture
def dbfile(tmp_path):
    path = tmp_path / "apps.db"
    c = db.connect(path)
    for i in range(1, 6):
        c.execute("INSERT INTO applications (id, company, title) VALUES (?, ?, 'PM')",
                  (i, f"Co{i}"))
    c.commit()
    c.close()
    return path


def connect(path):
    return lambda: db.connect(path)


def row(path, app_id):
    c = db.connect(path)
    r = dict(c.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone())
    c.close()
    return r


def test_start_returns_at_once_and_the_outcome_lands_on_the_row(dbfile):
    gate = threading.Event()

    def work(c):
        gate.wait(5)
        c.execute("UPDATE applications SET status='blocked' WHERE id=1")
        c.commit()
        return {"status": "blocked", "message": "filled, submit refused"}
    t0 = time.monotonic()
    t = worker.start(1, {}, work, connect(dbfile))
    assert time.monotonic() - t0 < 0.5                 # the chat is not held
    time.sleep(0.1)
    assert row(dbfile, 1)["status"] == "running"
    gate.set(); t.join(5)
    r = row(dbfile, 1)
    assert (r["status"], r["outcome"], r["reported"]) == ("blocked", "filled, submit refused", 0)


def test_an_outcome_is_reported_once(dbfile):
    worker.start(2, {}, lambda c: {"status": "blocked", "message": "done"},
                 connect(dbfile)).join(5)
    c = db.connect(dbfile)
    assert [r["id"] for r in worker.unreported(c)] == [2]
    assert worker.unreported(c) == []


def test_a_crash_becomes_failed_with_its_reason(dbfile):
    def boom(c):
        raise RuntimeError("tab vanished")
    worker.start(3, {}, boom, connect(dbfile)).join(5)
    r = row(dbfile, 3)
    assert r["status"] == "failed" and "tab vanished" in r["outcome"]


def test_no_more_than_max_parallel_run_at_once(dbfile, monkeypatch):
    monkeypatch.setattr(worker, "_slots", threading.BoundedSemaphore(2))
    live, peak, lock, gate = [0], [0], threading.Lock(), threading.Event()

    def work(c):
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        gate.wait(5)
        with lock:
            live[0] -= 1
        return {"status": "blocked"}
    threads = [worker.start(i, {}, work, connect(dbfile)) for i in (1, 2, 3, 4)]
    time.sleep(0.3)
    statuses = sorted(row(dbfile, i)["status"] for i in (1, 2, 3, 4))
    assert statuses == ["queued", "queued", "running", "running"]
    gate.set()
    for t in threads:
        t.join(5)
    assert peak[0] == 2


def test_a_restart_marks_cut_off_runs(dbfile):
    c = db.connect(dbfile)
    c.execute("UPDATE applications SET status='running' WHERE id=4")
    c.execute("UPDATE applications SET status='queued' WHERE id=5")
    c.commit()
    assert worker.recover(c) == 2
    assert {r["id"] for r in worker.unreported(c)} == {4, 5}
    assert "interrupted" in row(dbfile, 4)["outcome"]


def test_fixture_runs_stay_inline_for_the_scenario_harness(monkeypatch):
    import chat
    monkeypatch.delenv("JOB_AGENT_BACKGROUND", raising=False)
    assert chat._in_background({}) is True
    assert chat._in_background({"fixture": "greenhouse_sample"}) is False
    monkeypatch.setenv("JOB_AGENT_BACKGROUND", "0")
    assert chat._in_background({}) is False


def test_the_started_message_cannot_be_read_as_done():
    import chat
    msg = chat._started(7, {"title": "PM", "company": "CRED"}, tailoring=True)
    assert "NOT SUBMITTED" in msg and "Do not say it is applied" in msg


def test_finished_runs_are_written_into_the_reply_by_code(dbfile):
    """The model was told to lead with it and did not; code writes it now."""
    import chat
    worker.start(1, {}, lambda c: {"status": "blocked",
                                   "message": "All 8 fields filled. Submit was off."},
                 connect(dbfile)).join(5)
    c = db.connect(dbfile)
    news = worker.unreported(c)
    line = chat.news_lines(news)
    assert line == "Update — PM at Co1: form filled, not submitted. All 8 fields filled."
    assert "do not repeat" in chat.turn_state(c, [], news)
    assert worker.unreported(c) == []


def test_resaving_the_same_resume_preference_does_not_block_applying():
    import chat
    from engine import db as _db
    conn = _db.connect(":memory:")
    conn.execute("INSERT INTO vault (key, value) VALUES ('resume_mode', 'existing')")
    state = {"turn": 3, "picks": []}
    out = chat.run_tool("set_resume_preference", {"choice": "existing"}, state, conn)
    assert out.startswith("already saved")
    assert "resume_pref_turn" not in state        # the same-turn gate stays open
