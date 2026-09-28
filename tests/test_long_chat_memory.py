"""A long conversation keeps what the person said, and its thread.

Three things are covered: preferences become rows on the turn they are said,
trimmed turns leave running notes behind, and a new session starts from the
last one's notes. Every model call is stubbed; nothing reads the real profile.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import career    # noqa: E402
import chat      # noqa: E402
import chatlog   # noqa: E402
from engine import db               # noqa: E402
from memory import prefs, reconcile, thread   # noqa: E402


@pytest.fixture
def conn(tmp_path, monkeypatch):
    c = db.connect(tmp_path / "t.db")
    # The nightly prose file, pointed somewhere empty: the real one is theirs.
    monkeypatch.setattr(reconcile, "PROFILE", tmp_path / "profile.md")
    yield c
    c.close()


def extractor(monkeypatch, result):
    calls = []
    monkeypatch.setattr(career.llm, "complete_json",
                        lambda system, user, **k: calls.append(user) or result)
    return calls


# --- preferences are rows, written on the turn they are said --------------

def test_a_stated_preference_is_a_row(conn, monkeypatch):
    extractor(monkeypatch, {"preferences": [
        {"kind": "avoid", "text": "consulting companies", "stated": True},
        {"kind": "avoid", "text": "agency roles", "stated": False}]})
    career.capture("no consulting please, and ugh these agency roles", conn)
    rows = {r["text"]: r["stated"] for r in prefs.active(conn)}
    assert rows == {"consulting companies": 1, "agency roles": 0}
    assert "- avoid: agency roles (inferred)" in prefs.render(conn)


def test_staging_gets_the_guess_not_the_message(conn, monkeypatch):
    """The old bug: every whole message, "hi" included, went to staging."""
    extractor(monkeypatch, {"vault_guesses": [
        {"key": "notice_period", "value": "2 months", "stated": False}]})
    career.capture("I'd have to serve two months at Lenskart", conn)
    facts = [r["fact"] for r in conn.execute("SELECT fact FROM staging")]
    assert facts == ["notice_period: 2 months (inferred)"]


def test_a_message_with_nothing_in_it_stages_nothing(conn, monkeypatch):
    extractor(monkeypatch, {})
    career.capture("show me the next few please", conn)
    assert conn.execute("SELECT COUNT(*) FROM staging").fetchone()[0] == 0


def test_taking_one_back_is_by_id_and_only_a_live_id(conn, monkeypatch):
    prefs.add("avoid", "consulting companies", conn=conn)
    keep = prefs.add("want", "B2C fintech", conn=conn)
    pid = prefs.active(conn)[0]["id"]
    calls = extractor(monkeypatch, {"retract": [pid, 999]})
    career.capture("actually consulting is fine now", conn)
    assert f"{pid} · avoid · consulting companies" in calls[0]   # it saw the ids
    assert [r["text"] for r in prefs.active(conn)] == ["B2C fintech"] and keep


def test_saying_it_again_brings_it_back_and_upgrades_a_guess(conn):
    prefs.add("avoid", "agency roles", stated=False, conn=conn)
    prefs.retract([prefs.active(conn)[0]["id"]], conn=conn)
    assert prefs.active(conn) == []
    assert prefs.add("avoid", "agency roles", stated=True, conn=conn)
    assert prefs.active(conn)[0]["stated"] == 1


def test_the_old_list_shape_still_writes_career_facts(conn, monkeypatch):
    extractor(monkeypatch, [{"kind": "skill", "text": "SQL"}])
    assert career.capture("I use SQL every day at work", conn)[0]["text"] == "SQL"


def test_preferences_reach_what_the_agents_read(conn):
    prefs.add("constraint", "remote only", conn=conn)
    assert "- constraint: remote only" in reconcile.read_profile(conn)
    assert "remote only" in chat.wants({}, conn)


# --- trimmed turns leave notes ---------------------------------------------

def say(conn, session, turn, them, you="ok"):
    chatlog.record(conn, session, turn, "user", them)
    chatlog.record(conn, session, turn, "assistant", you)


def summarizer(monkeypatch, reply="- compared Meesho and CRED"):
    seen = []
    monkeypatch.setattr(thread.llm, "complete",
                        lambda system, user, **k: seen.append(user) or reply)
    return seen


def test_notes_fold_in_only_the_turns_not_yet_read(conn, monkeypatch):
    seen = summarizer(monkeypatch)
    for t in (1, 2, 3):
        say(conn, "s1", t, f"message {t}")
    assert thread.refresh("s1", 2, conn) == "- compared Meesho and CRED"
    assert "message 1" in seen[0] and "message 3" not in seen[0]
    thread.refresh("s1", 3, conn)
    assert "message 1" not in seen[1] and "message 3" in seen[1]
    assert "compared Meesho" in seen[1]                  # the notes so far
    thread.refresh("s1", 3, conn)
    assert len(seen) == 2                                # nothing new, no call


def test_a_failed_refresh_keeps_the_notes_it_had(conn, monkeypatch):
    thread.save("s1", "- liked CRED", 1, conn)
    say(conn, "s1", 2, "hmm")

    def down(*a, **k):
        raise RuntimeError("provider down")
    monkeypatch.setattr(thread.llm, "complete", down)
    assert thread.refresh("s1", 2, conn) == "- liked CRED"


def test_the_trim_note_carries_the_notes():
    history = [{"role": "user", "content": "x" * 2000}] * 40
    out = chat.compact(history, summarize=lambda: "- applied to CRED (tailored)")
    assert "applied to CRED" in out[0]["content"] and len(out) <= chat.KEEP_TURNS + 1


def test_no_trim_means_no_summary_call():
    called = []
    history = [{"role": "user", "content": "short"}]
    assert chat.compact(history, summarize=lambda: called.append(1)) == history
    assert called == []


def test_a_summary_that_fails_still_trims():
    def boom():
        raise RuntimeError("down")
    out = chat.compact([{"role": "user", "content": "x" * 2000}] * 40, summarize=boom)
    assert out[0]["content"].startswith("[earlier conversation trimmed")


# --- a new session starts where the last one stopped -----------------------

def test_a_new_session_gets_the_last_ones_notes(conn, monkeypatch):
    """No model call while they wait for the first reply (it made "Hie" take
    16 s): turns the notes have not folded in go over as they were said."""
    seen = summarizer(monkeypatch, "- thinking about Meesho")
    thread.save("old", "- asked about growth roles", 0, conn)
    say(conn, "old", 1, "Meesho looks good, let me think")
    carried = thread.carry_over("new", conn)
    assert carried.startswith("- asked about growth roles")
    assert "Meesho looks good, let me think" in carried
    assert seen == []


def test_the_very_first_session_has_nothing_to_carry(conn, monkeypatch):
    seen = summarizer(monkeypatch)
    assert thread.carry_over("first", conn) == "" and seen == []


# --- the long chat: said on turn 3, still known on turn 40 -----------------

def test_said_on_turn_3_still_known_on_turn_40(conn, monkeypatch):
    """What the whole change is for. Turn 3 says no consulting; 37 turns of
    other talk push it out of the history; turn 40 must still know it, from
    the row, and the thread must survive as notes."""
    extractor(monkeypatch, {"preferences": [
        {"kind": "avoid", "text": "consulting companies", "stated": True}]})
    career.capture("no consulting companies please", conn)
    extractor(monkeypatch, {})
    summarizer(monkeypatch, "- they ruled out consulting on turn 3")

    history = []
    for t in range(1, 41):
        say(conn, "long", t, "no consulting companies please" if t == 3
            else f"filler question number {t} " + "y" * 900)
        history.append({"role": "user", "content": f"turn {t} " + "y" * 900})
        history.append({"role": "assistant", "content": "z" * 900})
        history = chat.compact(
            history, summarize=lambda t=t: thread.refresh("long", t - 1, conn))

    flat = str(history)
    assert "turn 3 " not in flat                         # the turn itself is gone
    assert "ruled out consulting" in flat                # its thread is in the notes
    assert "avoid: consulting companies" in reconcile.read_profile(conn)
