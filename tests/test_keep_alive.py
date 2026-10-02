"""A parked run keeps its cloud browser open, so the answer carries on on the
same filled page instead of filling the form again.

Apply eval, 2026-10-01: one SmartRecruiters form stopped twice to ask, and each
answer opened a fresh browser on an empty form: 3 sessions, 11 minutes."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from apply import agent, cloud, harness             # noqa: E402
from apply.browser import ManualBrowser             # noqa: E402
from engine import db                               # noqa: E402


@pytest.fixture
def conn(tmp_path, monkeypatch):
    path = str(tmp_path / "jobs.db")
    real = db.connect
    monkeypatch.setattr(db, "connect", lambda p=None: real(path))
    c = db.connect()
    c.execute("INSERT INTO applications (id, source, source_id, company, title, apply_url) "
              "VALUES (1, 'lever', '1', 'Co', 'PM', 'https://jobs.lever.co/co/1/apply')")
    c.commit()
    yield c
    c.close()


def test_sessions_are_created_kept_alive_with_room_to_wait(monkeypatch):
    sent = {}

    def post(url, json=None, **kw):
        sent.update(json or {})
        return NS(status_code=201, json=lambda: {"id": "s1", "connectUrl": "wss://x"},
                  raise_for_status=lambda: None)
    monkeypatch.setattr(cloud, "_keys", lambda: ("k", "p"))
    monkeypatch.setattr(cloud.httpx, "post", post)
    s = cloud.Session(1)
    assert sent["keepAlive"] is True
    assert sent["timeout"] == cloud.SESSION_SECONDS + cloud.KEEP_SECONDS


class _Page:
    def screenshot(self, **kw):
        pass


def _kept_browser(conn, monkeypatch, tmp_path, started=None):
    monkeypatch.setattr(cloud, "SHOTS", tmp_path)
    b = cloud.CloudBrowser.__new__(cloud.CloudBrowser)
    b.app_id, b._pw, b._browser, b._earlier = 1, None, None, {"Email": "a@example.com"}
    b.page = _Page()
    b.describe = lambda: ""
    b.session = NS(keep_alive=True, started=started or time.time(), timeout=1800,
                   id="s1", connect_url="wss://x",
                   release=lambda: pytest.fail("a kept browser must not be released"))
    return b


def test_a_parked_run_keeps_its_browser_and_saves_where_it_was(conn, monkeypatch, tmp_path):
    b = _kept_browser(conn, monkeypatch, tmp_path)
    assert b.keep(conn) is True and b.session is None
    kept = json.loads(conn.execute("SELECT kept_session FROM applications").fetchone()[0])
    assert kept["id"] == "s1" and kept["earlier"] == {"Email": "a@example.com"}
    assert kept["until"] > time.time()


def test_too_little_time_left_is_not_kept(conn, monkeypatch, tmp_path):
    b = _kept_browser(conn, monkeypatch, tmp_path, started=time.time() - 1790)
    assert b.keep(conn) is False


def test_a_kept_browser_is_taken_once_and_an_expired_one_released(conn, monkeypatch):
    released = []
    monkeypatch.setattr(cloud.Session, "release", lambda self: released.append(self.id))
    conn.execute("UPDATE applications SET kept_session = ?",
                 (json.dumps({"id": "s1", "connect_url": "wss://x", "until": time.time() + 600}),))
    conn.commit()
    assert cloud._take_kept(1)["id"] == "s1"
    assert cloud._take_kept(1) is None                   # used once
    conn.execute("UPDATE applications SET kept_session = ?",
                 (json.dumps({"id": "s2", "connect_url": "wss://y", "until": time.time() - 1}),))
    conn.commit()
    assert cloud._take_kept(1) is None and released == ["s2"]


class _Cloudish:
    close_after_run = True

    def __init__(self):
        self.kept = self.closed = False

    def keep(self, conn):
        self.kept = True
        return True

    def close(self):
        self.closed = True


def test_only_a_waiting_run_keeps_its_browser(conn):
    b = _Cloudish()
    agent._close(b, conn, parked={"status": "waiting"})
    assert b.kept and not b.closed
    b = _Cloudish()
    agent._close(b, conn, parked={"status": "blocked"})
    assert b.closed and not b.kept


@pytest.mark.parametrize("resumed,said", [(True, "same page you left"),
                                           (False, "empty now")])
def test_the_agent_is_told_whether_the_form_is_still_filled(conn, monkeypatch, resumed, said):
    got = {}
    monkeypatch.setattr(harness, "run", lambda *a, **k: got.update(k) or {"status": "blocked"})
    b = ManualBrowser("greenhouse_sample", app_id=1)
    b.resumed = resumed
    agent.resume_run(1, "Bengaluru", b, conn)
    assert said in got["answer"]


def test_a_keka_session_asks_browserbase_to_solve_its_captcha(monkeypatch):
    sent = {}

    def post(url, json=None, **kw):
        sent.update(json or {})
        return NS(status_code=201, json=lambda: {"id": "s1", "connectUrl": "wss://x"},
                  raise_for_status=lambda: None)
    monkeypatch.setattr(cloud, "_keys", lambda: ("k", "p"))
    monkeypatch.setattr(cloud.httpx, "post", post)
    cloud.Session(1, url="https://acme.keka.com/careers/applyjob/1")
    assert sent["browserSettings"] == {"captchaImageSelector": "#imgCaptcha",
                                       "captchaInputSelector": "#captcha"}
