"""Tests for everything that does not call a model.

Run: python -m pytest tests/ -q

Two standing rules these tests exist to enforce:

- **Nothing here touches live data.** Every test gets its own in-memory
  database and every form is a fixture. A test that could submit a real
  application is not a test.
- **Identity by snapshot id, never by content matching.** Deleting rows by
  matching their text is how a real answer gets destroyed.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import vault                                  # noqa: E402
from apply import agent                       # noqa: E402
from apply.browser import ManualBrowser, SubmitRefused  # noqa: E402
from engine import adapters, db               # noqa: E402
from resume import verify                     # noqa: E402
from search import query                      # noqa: E402


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


def job(**kw):
    base = dict(source="lever", source_id="1", company="Zupee",
                title="Growth Manager", location="Bangalore, India",
                description="Own the monetisation funnel. 3-5 years.",
                url="https://x", posted_at="2026-09-20")
    base.update(kw)
    return adapters._row(**base)


# --- db --------------------------------------------------------------------

def test_upsert_is_idempotent(conn):
    rows = [job(), job(source_id="2", title="Revenue Manager")]
    db.upsert(conn, rows)
    db.upsert(conn, rows)
    assert db.stats(conn)["total"] == 2


def test_upsert_updates_in_place(conn):
    db.upsert(conn, [job(title="Old")])
    db.upsert(conn, [job(title="New")])
    r = conn.execute("SELECT title FROM jobs").fetchone()
    assert db.stats(conn)["total"] == 1 and r["title"] == "New"


def test_close_missing_closes_only_what_vanished(conn):
    db.upsert(conn, [job(source_id="1"), job(source_id="2")])
    db.close_missing(conn, "lever", "Zupee", ["1"])
    assert db.stats(conn)["open"] == 1


def test_a_failed_fetch_closes_nothing(conn):
    """The bug that would quietly empty the corpus: an empty list from a
    timeout must never read as 'they closed every role'."""
    db.upsert(conn, [job(source_id="1"), job(source_id="2")])
    db.close_missing(conn, "lever", "Zupee", [])
    assert db.stats(conn)["open"] == 2


def test_reopening_clears_closed_at(conn):
    db.upsert(conn, [job()])
    db.close_missing(conn, "lever", "Zupee", ["999"])
    db.upsert(conn, [job()])
    assert db.stats(conn)["open"] == 1


# --- normalization ---------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("Bengaluru, India", "Bangalore"),
    ("Bangalore", "Bangalore"),
    ("Gurugram, Haryana", "Gurgaon"),
    ("Remote - India", None),
    ("Mumbai / Pune", "Mumbai"),
])
def test_city_normalization(raw, expected):
    city, _ = adapters.norm_city(raw)
    if expected is None:
        assert city is not None       # falls back to the raw head
    else:
        assert city == expected


def test_remote_is_detected():
    assert adapters.norm_city("Remote (India)")[1] == 1
    assert adapters.norm_city("Bangalore")[1] == 0


def test_seniority_from_title():
    assert adapters.seniority_of("Senior Growth Manager") == "senior"
    assert adapters.seniority_of("Head of Growth") == "head"
    assert adapters.seniority_of("Growth Manager") is None


def test_years_extraction():
    assert adapters.years_of("We want 3-5 years of experience") == (3, 5)
    assert adapters.years_of("4+ years") == (4, None)
    assert adapters.years_of("no numbers here") == (None, None)


def test_content_hash_ignores_cosmetic_change():
    a = adapters.content_hash("Zupee", "Growth", "Bangalore", "body ")
    b = adapters.content_hash("Zupee", "Growth", "Bangalore", "body")
    assert a == b


def test_html_is_stripped():
    out = adapters.strip_html("<p>Own the <b>funnel</b></p><li>3&nbsp;years</li>")
    assert "<" not in out and "Own the funnel" in out


# --- query -----------------------------------------------------------------

def test_null_fields_produce_no_clause():
    sql, params, _ = query.build({"count": 5})
    assert "city =" not in sql and "company_type =" not in sql


def test_skills_order_they_do_not_exclude():
    """A JD saying "split testing" instead of "A/B test" should rank lower,
    not vanish — so must_mention belongs in ORDER BY and must never reach
    WHERE."""
    sql, _, _ = query.build({"must_mention": ["sql"]})
    where = sql.split("WHERE")[1].split("ORDER BY")[0]
    order = sql.split("ORDER BY")[1]
    assert "description" not in where
    assert "hits" in order


def test_overfetch_is_four_times_the_ask():
    _, params, f = query.build({"count": 5})
    assert params[-1] == 20 and f["count"] == 5


def test_remote_counts_as_any_city(conn):
    db.upsert(conn, [job(source_id="r", location="Remote - India")])
    rows, _ = query.search({"city": "Bangalore"}, conn)
    assert len(rows) == 1


def test_closed_jobs_never_returned(conn):
    db.upsert(conn, [job()])
    db.close_missing(conn, "lever", "Zupee", ["999"])
    rows, _ = query.search({}, conn)
    assert rows == []


def test_relax_runs_in_a_fixed_order():
    f = query.normalize({"must_mention": ["sql"], "posted_within_days": 45,
                         "city": "Bangalore"})
    f, note = query.relax(f)
    assert "skill" in note and f["must_mention"] is None
    f, note = query.relax(f)
    assert "90" in note
    assert query.relax(query.normalize({})) is None


# --- vault -----------------------------------------------------------------

@pytest.mark.parametrize("label,key", [
    ("First Name", "first_name"),
    ("Last Name", "last_name"),
    ("Full Name", "full_name"),
    ("Expected CTC", "expected_ctc"),
    ("Notice period", "notice_period"),
    ("Are you authorized to work in India?", "work_authorization"),
])
def test_label_matching(label, key):
    assert vault.match_key(label) == key


def test_first_name_does_not_match_full_name():
    """The alias 'name' is a substring of 'first name'. Getting this wrong
    types the full name into a first-name box on a real application."""
    assert vault.match_key("First Name") != "full_name"


def test_unknown_label_returns_none():
    """None means the agent must ask. That is the contract."""
    assert vault.match_key("What is your favourite colour?") is None


def test_slice_only_returns_asked_for_keys(conn):
    vault.put("email", "a@b.com", conn=conn)
    vault.put("expected_ctc", "30 LPA", conn=conn)
    out = vault.slice_for(["Email"], conn)
    assert out == {"Email": "a@b.com"}


def test_staged_facts_do_not_reach_the_vault(conn):
    vault.stage("mentioned 30 LPA while reading a posting", conn=conn)
    assert vault.get("expected_ctc", conn) is None


# --- resume ----------------------------------------------------------------

def test_honest_reword_passes():
    assert verify.check_bullet("Grew repeat purchase by 18%",
                               "Drove an 18% lift in repeat purchase") == []


def test_invented_number_is_caught():
    assert verify.check_bullet("Grew repeat purchase by 18%",
                               "Drove a 45% lift") != []


def test_invented_tool_is_caught():
    assert verify.check_bullet("Owned the email channel",
                               "Owned email via Braze") != []


def test_failing_bullet_reverts_to_original():
    master = {"experience": [{"company": "X", "title": "T", "dates": "D",
                              "bullets": ["Grew revenue by 18%", "Shipped a thing"]}]}
    patch = {"experience": [{"index": 0, "keep": [0],
                             "rewrites": {"0": "Grew revenue by 90%"}}]}
    out, notes = verify.apply_patch(master, patch)
    assert out["experience"][0]["bullets"] == ["Grew revenue by 18%"]
    assert notes


def test_locked_fields_cannot_change():
    master = {"experience": [{"company": "Lenskart", "title": "T", "dates": "D",
                              "bullets": ["a"]}]}
    out, _ = verify.apply_patch(master, {"experience": []})
    assert out["experience"][0]["company"] == "Lenskart"


def test_keep_reorders_bullets():
    master = {"experience": [{"company": "X", "title": "T", "dates": "D",
                              "bullets": ["one", "two", "three"]}]}
    out, _ = verify.apply_patch(master, {"experience": [{"index": 0, "keep": [2, 0]}]})
    assert out["experience"][0]["bullets"] == ["three", "one"]


# --- apply -----------------------------------------------------------------

@pytest.fixture
def browser():
    return ManualBrowser("greenhouse_sample", app_id=1)


def test_preflight_finds_exactly_the_gaps(conn, browser):
    for k, v in {"first_name": "Sai", "last_name": "S",
                 "email": "a@b.com", "phone": "+91"}.items():
        vault.put(k, v, conn=conn)
    pre = agent.preflight(browser, conn)
    assert pre["missing"] == ["Notice period", "Expected CTC"]


def test_submit_refuses_on_empty_required(browser):
    with pytest.raises(SubmitRefused, match="required fields"):
        browser.submit()


def test_submit_refuses_without_the_env_flag(browser, monkeypatch):
    monkeypatch.delenv("ALLOW_SUBMIT", raising=False)
    for f in browser.read_form():
        browser.filled[f["name"]] = "x"
    with pytest.raises(SubmitRefused, match="ALLOW_SUBMIT"):
        browser.submit()


def test_submit_works_when_armed(browser, monkeypatch):
    monkeypatch.setenv("ALLOW_SUBMIT", "1")
    for f in browser.read_form():
        browser.filled[f["name"]] = "x"
    assert "submitted" in browser.submit()


def test_fill_unknown_field_returns_an_error_not_an_exception(browser):
    """A tool error is information the model can recover from. A crash is not."""
    assert browser.fill_field("nope", "x").startswith("ERROR")


def test_ask_user_parks(browser):
    from apply.browser import Park
    with pytest.raises(Park):
        browser.ask_user("notice period?")


# --- resume links: anchor text is not an address ---------------------------

def test_anchor_text_never_survives_as_a_linkedin_url():
    """"Portfolio | LinkedIn" was typed into a real form's LinkedIn field.

    A URL field gets a URL or nothing: blank makes the agent ask, junk makes it
    confidently type junk.
    """
    from resume.parse import _fix_links
    master = {"contact": {"linkedin": "Portfolio | LinkedIn"}}
    assert _fix_links(master, [])["contact"]["linkedin"] is None


def test_a_real_link_beats_whatever_the_model_read():
    from resume.parse import _fix_links
    master = {"contact": {"linkedin": "LinkedIn"}}
    out = _fix_links(master, ["https://sai-swaroop.netlify.app",
                              "https://www.linkedin.com/in/bugata-sai-swaroop"])
    assert out["contact"]["linkedin"] == "https://www.linkedin.com/in/bugata-sai-swaroop"
    assert out["contact"]["website"] == "https://sai-swaroop.netlify.app"


def test_vault_never_stores_a_non_url_as_a_url():
    from resume.parse import vault_rows
    rows = vault_rows({"name": "A B", "contact": {"linkedin": "LinkedIn",
                                                  "email": "a@b.com"}})
    assert "linkedin" not in rows
    assert rows["email"] == "a@b.com"


# --- fill_field must not claim a field it did not actually fill ------------

class _DeadInput:
    """An input that accepts a value and silently drops it, like Lever's
    location autocomplete."""
    def __init__(self): self.typed = None
    def fill(self, v): self.typed = v
    def input_value(self): return ""
    def click(self): pass
    def type(self, v, delay=None): pass
    def press(self, k): pass
    def count(self): return 0
    def is_visible(self): return False
    def select_option(self, **kw): pass
    def check(self): pass


def test_a_field_that_drops_the_value_is_reported_as_an_error():
    """The bug this came from: the trace said 'filled location' while the form
    showed it empty, so the submit guard counted a required field as done."""
    from apply.browser import PlaywrightBrowser

    b = PlaywrightBrowser.__new__(PlaywrightBrowser)   # no real browser
    b.filled = {}
    b._fields = [{"name": "location", "label": "Current location",
                  "type": "text", "required": True}]
    dead = _DeadInput()
    b._locator = lambda name: dead
    b.page = type("P", (), {"wait_for_timeout": lambda self, ms: None,
                            "locator": lambda self, sel: dead})()

    result = b.fill_field("location", "Bengaluru")
    assert result.startswith("ERROR")
    assert "location" not in b.filled        # the guard can now catch it


# --- chat transcript -------------------------------------------------------
# Written because the chat agent logged nothing at all: the `messages` table
# shipped in the schema and stayed empty, so a turn could only ever be
# reconstructed from timestamps in other tables.


def test_migration_adds_the_columns_to_an_old_messages_table(tmp_path):
    """A jobs.db built before these columns existed must not stay broken.

    CREATE TABLE IF NOT EXISTS leaves an existing table exactly as it was, so
    without the migration every write to it would fail forever.
    """
    import sqlite3

    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "role TEXT NOT NULL, content TEXT NOT NULL, "
                "created_at TEXT DEFAULT CURRENT_TIMESTAMP)")
    old.commit()
    old.close()

    c = db.connect(path)
    cols = {r[1] for r in c.execute("PRAGMA table_info(messages)")}
    assert {"session", "turn", "meta"} <= cols
    db._migrate(c)                       # idempotent: running twice is a no-op
    c.close()


def test_a_turn_records_what_ran_not_just_what_was_said(conn):
    import chatlog

    s = chatlog.new_session()
    chatlog.record(conn, s, 1, "user", "find me growth jobs")
    chatlog.record(conn, s, 1, "assistant", "Here are five.", {
        "ms": 4120, "model_calls": 4,
        "steps": [chatlog.step("search_jobs", {"query": "growth"},
                               "1. Growth Manager — Paytm", True, 3980)],
    })

    rows = chatlog.turns(conn)
    assert [r["role"] for r in rows] == ["user", "assistant"]
    assert rows[0]["meta"] is None                     # user rows carry none
    assert rows[1]["meta"]["model_calls"] == 4
    assert rows[1]["meta"]["steps"][0]["tool"] == "search_jobs"


def test_a_reply_with_no_tool_call_is_visible_as_such(conn):
    """The failure this exists to catch: the agent said an application was in
    when it had run no tool at all. An empty `steps` list is the evidence."""
    import chatlog

    s = chatlog.new_session()
    chatlog.record(conn, s, 1, "assistant", "That's in, I'll let you know.",
                   {"ms": 900, "model_calls": 1, "steps": []})
    row = chatlog.turns(conn)[0]
    assert row["meta"]["steps"] == []
    # Said in words a person reads, not as an absent line they'd never notice.
    assert "Nothing" in chatlog.render([row], color=False)


def test_turns_are_ordered_by_id_not_timestamp(conn):
    """Several messages land in the same second routinely, and their order is
    the only thing a log is for."""
    import chatlog

    s = chatlog.new_session()
    for i in range(1, 6):
        chatlog.record(conn, s, i, "user", f"message {i}")
    rows = chatlog.turns(conn, limit=3)
    assert [r["content"] for r in rows] == ["message 3", "message 4", "message 5"]


def test_sessions_are_listed_newest_first(conn):
    import chatlog

    chatlog.record(conn, "aaa", 1, "user", "first")
    chatlog.record(conn, "bbb", 1, "user", "second")
    rows = chatlog.sessions(conn)
    assert [r["session"] for r in rows] == ["bbb", "aaa"]


def test_a_long_tool_result_is_truncated_not_stored_whole(conn):
    """A 20-job search result must not turn the log into a second copy of the
    jobs table."""
    import chatlog

    s = chatlog.step("search_jobs", {}, "x" * 5000, True, 10)
    assert len(s["result"]) == chatlog.RESULT_CHARS


def test_a_broken_log_write_never_costs_the_person_their_reply(conn):
    """A reply the model already produced must survive a failed log write."""
    import chat

    chat._log(conn, "sess", 1, "user", None)   # NOT NULL violation, on purpose
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


def test_model_calls_are_counted_globally_not_per_loop():
    """A turn's cost includes the calls its tools make.

    `search_jobs` is two model calls on its own, so a turn that counted only
    its own loop reported half the real bill.
    """
    import llm

    before = llm.calls()
    llm.counted()
    llm.counted()
    assert llm.calls() - before == 2


def test_the_log_says_which_files_ran(conn):
    """"Which file did that?" is the first question asked of any trace, and a
    tool name alone does not answer it."""
    import chat
    import chatlog

    # Every tool the chat agent has must say what it runs.
    assert set(chat.TOOL_FILES) == {t["name"] for t in chat.TOOLS}

    s = chatlog.new_session()
    chatlog.record(conn, s, 1, "assistant", "here you go", {
        "ms": 100, "model_calls": 2,
        "steps": [chatlog.step("search_jobs", {}, "1. ...", True, 90,
                               files=chat.TOOL_FILES["search_jobs"])],
    })
    shown = chatlog.render(chatlog.turns(conn), color=False)
    assert "search/rank.py" in shown
    # parse.py is unreachable from chat; the log must not imply otherwise.
    assert "parse.py" not in shown


def test_the_browser_page_javascript_actually_parses():
    """The page is a Python string, so `\\n` in the source reaches the browser
    as a real newline. Inside a JS string literal that is a syntax error, and
    it kills the entire script silently: no handlers attach, the form falls
    back to a native GET, and the app looks like it navigated away for no
    reason. Nothing else in this suite can see that.
    """
    import shutil
    import subprocess
    import tempfile

    import web

    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed; cannot parse-check the page")

    js = web.PAGE.split("<script>")[1].split("</script>")[0]
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
        fh.write(js)
        path = fh.name
    r = subprocess.run([node, "--check", path], capture_output=True, text=True)
    assert r.returncode == 0, f"page JS does not parse:\n{r.stderr}"


# --- the funnel ------------------------------------------------------------
# "Here are five jobs" reads identically whether the other fifteen were junk
# or whether the one they wanted was among them. These make the difference
# visible.


def test_rank_returns_picks_and_what_it_dropped(monkeypatch):
    from search import rank

    rows = [
        {"source": "lever", "source_id": "1", "company": "Paytm",
         "title": "Growth Manager", "description": "own growth"},
        {"source": "lever", "source_id": "2", "company": "Paytm",
         "title": "Growth Lead", "description": "wants 10 years"},
    ]
    monkeypatch.setattr(rank.llm, "prompt", lambda n: "{profile}{soft_criteria}")
    monkeypatch.setattr(rank.llm, "RANK_MODEL", lambda: "x")
    monkeypatch.setattr(rank.llm, "complete_json", lambda *a, **k: {
        "picks": [{"source": "lever", "source_id": "1", "reason": "fits"}],
        "dropped": [{"source": "lever", "source_id": "2", "why": "wants 10 yrs"}],
    })

    chosen, cut = rank.pick(rows, {"count": 5}, "growth jobs")
    assert [c["source_id"] for c in chosen] == ["1"]
    assert cut[0]["why"] == "wants 10 yrs"


def test_a_job_is_never_both_shown_and_rejected(monkeypatch):
    """A model that lists the same job twice must not produce a panel saying
    'here it is' and 'we dropped it' at once."""
    from search import rank

    rows = [{"source": "lever", "source_id": "1", "company": "Paytm",
             "title": "Growth Manager", "description": "own growth"}]
    monkeypatch.setattr(rank.llm, "prompt", lambda n: "{profile}{soft_criteria}")
    monkeypatch.setattr(rank.llm, "RANK_MODEL", lambda: "x")
    monkeypatch.setattr(rank.llm, "complete_json", lambda *a, **k: {
        "picks": [{"source": "lever", "source_id": "1", "reason": "fits"}],
        "dropped": [{"source": "lever", "source_id": "1", "why": "too junior"}],
    })

    chosen, cut = rank.pick(rows, {"count": 5}, "growth jobs")
    assert len(chosen) == 1 and cut == []


def test_a_provider_that_ignores_the_new_schema_still_searches(monkeypatch):
    """Losing the reject list is acceptable. Losing the search is not."""
    from search import rank

    rows = [{"source": "lever", "source_id": "1", "company": "Paytm",
             "title": "Growth Manager", "description": "own growth"}]
    monkeypatch.setattr(rank.llm, "prompt", lambda n: "{profile}{soft_criteria}")
    monkeypatch.setattr(rank.llm, "RANK_MODEL", lambda: "x")
    monkeypatch.setattr(rank.llm, "complete_json", lambda *a, **k: [
        {"source": "lever", "source_id": "1", "reason": "fits"}])

    chosen, cut = rank.pick(rows, {"count": 5}, "growth jobs")
    assert len(chosen) == 1 and cut == []


def test_the_filter_is_described_in_words_a_person_would_use():
    """The sentence-to-filter step is the first place a search goes wrong, and
    it is invisible unless it is shown."""
    from search.run import describe

    out = describe(query.normalize(
        {"title_keywords": ["growth"], "city": "Bangalore",
         "posted_within_days": 7}))
    assert 'title has "growth"' in out
    assert "city is Bangalore" in out
    assert "posted in the last 7 days" in out
    # An unset filter must read as absent, never be silently omitted.
    assert "anywhere" in describe(query.normalize({"title_keywords": ["growth"]}))


def test_a_flat_funnel_is_called_out(conn):
    """matched == shown means the ranking step had nothing to choose between.
    That is a different answer from 'these were the best five'."""
    import chatlog

    s = chatlog.new_session()
    chatlog.record(conn, s, 1, "assistant", "two jobs", {
        "ms": 100, "model_calls": 4,
        "steps": [chatlog.step("search_jobs", {}, "...", True, 90,
                               funnel={"open": 670, "matched": 2, "shown": 2,
                                       "looked_for": ["city is Bangalore"]})],
    })
    shown = chatlog.render(chatlog.turns(conn), color=False)
    assert "670 open" in shown
    assert "no choice to make" in shown
