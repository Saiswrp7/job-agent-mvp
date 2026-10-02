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


#: Inside the 30-day window whatever day the tests run.
RECENT = (__import__("datetime").date.today()
          - __import__("datetime").timedelta(days=9)).isoformat()


def job(**kw):
    base = dict(source="lever", source_id="1", company="Zupee",
                title="Growth Manager", location="Bangalore, India",
                description="Own the monetisation funnel. 3-5 years.",
                url="https://x", posted_at=RECENT)
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
    assert f["posted_within_days"] == 30          # the table holds 30 days
    f, note = query.relax(f)
    assert "skill" in note and f["must_mention"] is None
    f, note = query.relax(f)
    assert "outside that city" in note and f["posted_within_days"] == 30
    assert query.relax(query.normalize({})) is None


def test_the_posting_window_is_never_widened():
    """Nothing older than 30 days is kept, and "latest" means latest: a sparse
    result is never padded with older jobs."""
    f = query.normalize({"posted_within_days": 7})
    assert query.relax(f) is None


def test_stated_is_what_the_user_said_not_what_normalize_filled_in():
    # count and soft_criteria are excluded: count is the target the cascade
    # chases, not a constraint on which jobs are eligible.
    f = query.normalize({"city": "Bangalore", "count": 5,
                         "soft_criteria": "nothing CRM-heavy"})
    assert query.stated(f) == {"city"}
    assert query.stated(query.normalize({})) == frozenset()


def test_relax_never_drops_a_city_the_user_asked_for():
    """The bug this exists for: "growth roles in bengalore" returned Noida.

    Only 2 growth-titled jobs exist in Bangalore and count defaults to 5, so
    the cascade ran out of levers and deleted the city to pad the list. The
    person is never told, because four results look like four results.
    """
    f = query.normalize({"city": "Bangalore"})
    assert query.relax(f, protect=query.stated(f)) is None, \
        "a city the user named must never be relaxed away"
    # Unprotected — e.g. a city inferred from their profile — may still widen.
    widened, note = query.relax(f)
    assert widened["city"] is None and "outside that city" in note


def test_relax_protects_every_lever_the_user_named():
    f = query.normalize({"must_mention": ["sql"], "posted_within_days": 7,
                         "seniority": "senior", "industry": "fintech",
                         "city": "Pune"})
    assert query.relax(f, protect=query.stated(f)) is None


def test_relax_still_widens_what_the_user_did_not_name():
    # City came from the user; the seniority did not. The seniority gives.
    f = query.normalize({"city": "Pune", "seniority": "senior"})
    widened, note = query.relax(f, protect=frozenset({"city"}))
    assert widened["city"] == "Pune"
    assert widened["seniority"] is None and "seniority" in note


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


def test_submit_works_when_armed_for_this_person_and_approved(browser, monkeypatch):
    monkeypatch.setenv("ALLOW_SUBMIT", "1")
    monkeypatch.setenv("SUBMIT_USERS", "default")

    class Approved:
        def check(self, values, fields, partial=False):
            return None
    browser.approval = Approved()
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


# fill_field must not claim a field it did not fill: see tests/test_jev_browser.py


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


# --- addressing a job by name, not by position -----------------------------
#
# Every one of these failed in a recorded conversation. "apply to 1" started a
# different job, twice at a different company, because the agent renumbers the
# list it writes and the tools indexed the search order. A position cannot be
# wrong — every n from 1 to 5 returns something — so the failure was silent.

def test_two_jobs_never_share_a_name():
    from search.rank import ref
    a = ref({"company": "Paytm", "title": "Growth Management - UPI Growth"})
    b = ref({"company": "Paytm", "title": "Growth Manager - Postpaid"})
    assert a != b
    assert a == "paytm-growth-management-upi-growth"


def test_a_name_from_an_earlier_search_still_resolves():
    """Two searches in one message used to be the worst case: the reply listed
    both sets, `picks` held only the second, so a job from the first resolved
    to whatever sat at that index in the second. Different company."""
    import chat
    state = {"by_ref": {}}
    meesho = {"company": "Meesho", "title": "Manager - DAU Growth"}
    cred = {"company": "CRED", "title": "Capital Partnerships"}
    from search.rank import ref
    state["by_ref"][ref(meesho)] = meesho          # first search
    state["by_ref"][ref(cred)] = cred              # second search, same turn
    assert chat._by_ref(state, "meesho-manager-dau-growth")["company"] == "Meesho"
    assert chat._by_ref(state, "cred-capital-partnerships")["company"] == "CRED"


def test_an_unknown_name_raises_instead_of_returning_a_job():
    import chat
    from search.rank import ref
    job_ = {"company": "Paytm", "title": "Growth Manager - Postpaid"}
    state = {"by_ref": {ref(job_): job_}}
    with pytest.raises(KeyError) as exc:
        chat._by_ref(state, "meesho-manager-dau-growth")
    # The error has to name what IS available — a guess here fills in a real
    # application form.
    assert "paytm-growth-manager-postpaid" in str(exc.value)


def test_the_agents_search_result_carries_no_numbers():
    """A numbered tool result is a second numbered list in the context, in
    search order, competing with the reply the person actually reads."""
    from search import rank
    picks = [{"title": "A", "company": "X", "reason": "r", "url": "u",
              "posted_at": "2026-09-01", "city": "Bangalore"},
             {"title": "B", "company": "Y", "reason": "r", "url": "u",
              "posted_at": "2026-09-01", "city": "Noida"}]
    agent_text = rank.format_results(picks, numbered=False)
    assert not agent_text.lstrip().startswith("1.")
    assert "[x-a]" in agent_text
    # The CLI still numbers, for a person reading a terminal.
    assert rank.format_results(picks).lstrip().startswith("1.")


# --- an application says what happened, never what the loop did ------------

def test_a_run_that_did_not_submit_is_not_called_done(conn):
    """`done` meant "the loop stopped" and was read as "submitted". It reached
    a person as "all five are submitted" while ALLOW_SUBMIT was off and the
    apply agent's own message in the same string said it could not submit."""
    import chat
    job_ = {"title": "Growth Manager", "company": "Paytm", "city": "Noida"}
    out = chat._application_result(3, job_, {"status": "blocked",
                                             "message": "could not submit"})
    assert "NOT SUBMITTED" in out
    assert "done" not in out.lower()
    # and it names the job, so a wrong match is visible in the sentence
    assert "Growth Manager" in out and "Paytm" in out


def test_only_a_real_submit_reads_as_submitted():
    import chat
    job_ = {"title": "Growth Manager", "company": "Paytm"}
    assert "NOT SUBMITTED" in chat._application_result(1, job_, {"status": "waiting"})
    assert "NOT SUBMITTED" in chat._application_result(1, job_, {"status": "failed"})
    sent = chat._application_result(1, job_, {"status": "submitted"})
    assert "SUBMITTED" in sent and "NOT SUBMITTED" not in sent


def test_old_done_rows_become_blocked_not_submitted(tmp_path):
    """Nothing has ever been submitted — ALLOW_SUBMIT has never been on — so
    promoting these rows to `submitted` would invent history."""
    import sqlite3
    p = tmp_path / "old.db"
    c = sqlite3.connect(p)
    c.executescript(db.SCHEMA)
    c.execute("INSERT INTO applications (source, source_id, company, title, "
              "status) VALUES ('lever','1','Paytm','Growth Manager','done')")
    c.commit(); c.close()
    c = db.connect(p)
    rows = [r[0] for r in c.execute("SELECT status FROM applications")]
    assert rows == ["blocked"]
    c.close()


# --- the count they asked for, and filters they did not ---------------------

@pytest.mark.parametrize("said,expected", [
    ("find me 3 growth roles anywhere in India", 3),
    ("show me 5 product jobs in Chennai", 5),
    ("give me 4 jobs", 4),
    # Not a count. This one is why the pattern is anchored to a verb rather
    # than picking up any number in the sentence.
    ("find 5 APM roles for someone with 2 years of experience", 5),
    ("i need a job", None),
    ("growth roles in India", None),
])
def test_the_number_they_asked_for_is_read_from_their_own_sentence(said, expected):
    from search.run import wanted_count
    assert wanted_count(said) == expected


def test_a_filter_they_never_mentioned_is_reported():
    """Say "i need a job" and the agent fills the city in from the vault. That
    is reasonable; doing it silently is not — five Bangalore jobs look exactly
    like five jobs."""
    from search.run import added_without_asking
    assert added_without_asking({"city": "Bangalore"}, "i need a job") == ["city Bangalore"]
    # asked for remote, got a city as well
    assert "city Bangalore" in added_without_asking(
        {"city": "Bangalore", "remote": True},
        "I want remote product roles at fintech companies")
    # they named it themselves — nothing to report
    assert added_without_asking({"city": "Chennai"},
                                "find me 5 product jobs in Chennai") == []


# --- a resume instruction has somewhere to go -------------------------------

def test_what_they_asked_for_reaches_the_resume_builder(monkeypatch):
    """The tool used to take only a job, so "emphasise my CRM work" was
    dropped in transit — not refused, lost."""
    from resume import tailor as t
    seen = {}

    def fake(system, user, **kw):
        seen["user"] = user
        return {"summary": "s", "experience": []}

    monkeypatch.setattr(t.llm, "complete_json", fake)
    master = {"summary": "s", "experience": [], "name": "X"}
    t.tailor({"title": "PM", "company": "Paytm", "description": "d"},
             master, instruction="lean harder on the revenue number")
    assert "lean harder on the revenue number" in seen["user"]


def test_an_instruction_that_needs_an_invented_fact_is_reverted_and_reported():
    """Letting instructions through is only safe because this still fires."""
    master = {"summary": "Growth Manager with 4 years.", "name": "X",
              "experience": [{"company": "Lenskart", "title": "Growth Manager",
                              "bullets": ["Owned the CRM revenue line."]}]}
    patch = {"experience": [
        {"index": 0, "rewrites": {"0": "Owned the CRM revenue line for 8 years."}}]}
    out, notes = verify.apply_patch(master, patch)
    assert out["experience"][0]["bullets"] == ["Owned the CRM revenue line."]
    assert notes, "a reverted bullet must be reported, not silently dropped"


def test_the_summary_is_guarded_like_a_bullet():
    """It was not, and it is where "say I have 8 years" naturally lands: one
    sentence at the top of the page, the first thing a recruiter reads."""
    master = {"summary": "Growth Manager with 4 years.", "name": "X",
              "experience": []}
    out, notes = verify.apply_patch(
        master, {"summary": "Growth Manager with 8 years."})
    assert out["summary"] == "Growth Manager with 4 years."
    assert any("summary" in n for n in notes)
    # an honest rewrite still goes through
    out2, notes2 = verify.apply_patch(
        master, {"summary": "Growth Manager, 4 years, revenue and retention."})
    assert "4 years" in out2["summary"] and not notes2


# --- an instruction that was never attempted is still news ------------------
#
# `apply_patch` reports a rewrite it *caught*. It cannot report one that was
# never tried. Asked to claim eight years, a model that quietly writes
# "4+ years" produces no patch to revert, so the preview showed a clean resume
# and said nothing. Silent compliance and silent refusal looked identical.

def _four_year_resume() -> dict:
    return {"summary": "Growth Manager with 4+ years.", "name": "X",
            "experience": [{"company": "Lenskart", "title": "Growth Manager",
                            "bullets": ["Owned the CRM revenue line."]}]}


def test_an_instruction_quietly_declined_is_reported():
    """The residual on fix 8: nothing to revert, so nothing was said."""
    m = _four_year_resume()
    notes = verify.instruction_outcome("say I have 8 years", m, m, [])
    assert notes, "a declined instruction must be reported, not left silent"
    assert "8" in notes[0]


def test_an_instruction_that_landed_is_not_reported():
    """A guard that fires on success is one nobody reads."""
    before = _four_year_resume()
    after = {**before, "summary": "Growth Manager with 4+ years, 40% lift."}
    assert verify.instruction_outcome(
        "lean harder on the 40% number", before, after, []) == []


def test_a_pointer_at_a_bullet_is_not_read_as_a_claim():
    """"make the 2nd bullet stronger" points at a line; it does not claim a
    fact. Reading the 2 as a claim would fire this on every ordinary edit."""
    before = _four_year_resume()
    after = {**before, "experience": [
        {**before["experience"][0], "bullets": ["Owned the CRM revenue line end to end."]}]}
    assert verify.instruction_outcome(
        "make the 2nd bullet stronger", before, after, []) == []


def test_an_instruction_that_changed_nothing_at_all_is_reported():
    """No revert, no new number, and an identical document. They asked for
    something and got a resume that is byte-for-byte what they had."""
    m = _four_year_resume()
    notes = verify.instruction_outcome("emphasise the CRM work", m, m, [])
    assert notes and "nothing in the resume changed" in notes[0]


def test_a_reverted_number_is_not_reported_twice():
    """apply_patch already told them; saying it again buries both."""
    m = _four_year_resume()
    notes = ["summary: reverted (invented number(s): ['8'])"]
    assert verify.instruction_outcome("say I have 8 years", m, m, notes) == []


def test_no_instruction_means_nothing_to_report():
    m = _four_year_resume()
    assert verify.instruction_outcome("", m, m, []) == []


# --- the five log checks ----------------------------------------------------

def _assertions():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scenario-runs"))
    import assertions
    return assertions


def test_count_check_catches_a_dropped_count():
    """Bug 4: seven sentences with a count produced count=5 seven times."""
    a = _assertions()
    turn = {"user": "find me 3 growth roles", "assistant": "here are five",
            "steps": [{"tool": "search_jobs",
                       "funnel": {"filter": {"count": 5}}}]}
    assert a.a3_count_reaches_filter(turn, None)
    turn["steps"][0]["funnel"]["filter"]["count"] = 3
    assert a.a3_count_reaches_filter(turn, None) == []


def test_submitted_check_ignores_a_future_promise():
    """"I'll submit all five" is a promise, not a claim. Flagging it would
    fire the check on correct behaviour."""
    a = _assertions()
    promise = {"assistant": "Once you give me those, I'll submit all five.",
               "steps": []}
    assert a.a2_no_false_submitted(promise, None) == []
    claim = {"assistant": "All five are submitted with your resume.",
             "steps": []}
    assert a.a2_no_false_submitted(claim, None)


def test_submitted_check_allows_an_honest_negation():
    a = _assertions()
    honest = {"assistant": "Not submitted — two fields are missing.",
              "steps": []}
    assert a.a2_no_false_submitted(honest, None) == []


def test_numbers_resolve_catches_same_company_different_role():
    """The worst real case was Paytm -> Paytm. Comparing company alone hid it."""
    a = _assertions()
    prev = {"assistant": "1. Product Manager — Lending Product, Paytm\n"
                         "2. Growth Manager — Postpaid, Paytm",
            "picks_after": [
                {"i": 1, "title": "Product Management - Paytm Insurance",
                 "company": "Paytm"},
                {"i": 2, "title": "Product Manager Lending Product",
                 "company": "Paytm"}]}
    turn = {"user": "tell me about 1",
            "steps": [{"tool": "get_job", "input": {"n": 1}}]}
    assert a.a1_numbers_resolve(turn, prev), "shown 1 is pick 2, not pick 1"


def test_undisclosed_filter_check_accepts_a_disclosed_one():
    a = _assertions()
    turn = {"user": "i need a job",
            "assistant": "You didn't say where, so I used Bangalore.",
            "steps": [{"tool": "search_jobs",
                       "funnel": {"filter": {"city": "Bangalore"}}}]}
    assert a.a5_no_undisclosed_filter(turn, None) == []
    turn["assistant"] = "Here are five roles."
    assert a.a5_no_undisclosed_filter(turn, None)


# --- the career store: what they did, and where each claim came from --------
#
# master.json is a document and a document cannot answer "did they ever say
# this?" — which is the question the resume guard has to ask once the corpus
# grows past the parsed PDF.

@pytest.fixture
def store(tmp_path, monkeypatch):
    import career as c
    conn = db.connect(tmp_path / "t.db")
    yield c, conn
    conn.close()


def test_a_claim_keeps_the_message_it_came_from(store):
    """Provenance is the whole design: a number is allowed onto a resume
    because a row says who said it, not because a model sounded sure."""
    c, conn = store
    c.add("accomplishment", "Grew activation 18%", company="Paytm",
          source="chat", heard_in="i grew activation 18% last year", conn=conn)
    row = c.all(conn=conn)[0]
    assert row["heard_in"] == "i grew activation 18% last year"
    assert row["source"] == "chat"


def test_the_same_fact_twice_is_not_two_rows(store):
    c, conn = store
    assert c.add("skill", "SQL", conn=conn) is True
    assert c.add("skill", "SQL", conn=conn) is False
    assert len(c.all(conn=conn)) == 1


def test_a_stated_number_stops_being_invented(store):
    """The point of the whole store. Before it, a number they told us in
    conversation was reverted by the same test that blocks a made-up one."""
    c, conn = store
    original = "Owned the CRM revenue line."
    rewritten = "Owned the CRM revenue line, lifting activation 18%."
    assert verify.check_bullet(original, rewritten), "unsupported: must revert"

    c.add("accomplishment", "Grew activation 18%", company="Paytm",
          source="chat", heard_in="i grew activation 18%", conn=conn)
    assert verify.check_bullet(original, rewritten, c.supported(conn)) == [], \
        "they said it; the record backs it"


def test_a_number_nobody_stated_is_still_invented(store):
    """Moving the guard must not remove it."""
    c, conn = store
    c.add("accomplishment", "Grew activation 18%", conn=conn)
    assert verify.check_bullet("Owned the CRM revenue line.",
                               "Owned the CRM revenue line, up 92%.",
                               c.supported(conn))


def test_where_a_number_came_from_can_be_answered(store):
    """A guard that only says no is one nobody can argue with."""
    c, conn = store
    c.add("metric", "activation up 18% in two quarters", company="Paytm",
          source="chat", heard_in="activation went up 18% in two quarters",
          conn=conn)
    p = c.provenance("18%", conn)
    assert p and p["heard_in"] == "activation went up 18% in two quarters"
    assert c.provenance("92%", conn) is None


def test_the_uploaded_resume_goes_on_the_record_too(store):
    """Without seeding, the first generated resume is everything they
    mentioned in chat and nothing they actually did."""
    c, conn = store
    master = {"name": "X", "skills": ["SQL"], "experience": [
        {"company": "Lenskart", "title": "Growth Manager",
         "bullets": ["Owned the CRM revenue line."]}]}
    assert c.seed_from_master(master, conn) >= 3
    kinds = {r["kind"] for r in c.all(conn=conn)}
    assert {"role", "accomplishment", "skill"} <= kinds
    assert all(r["source"] == "resume" for r in c.all(conn=conn))


def test_trivia_never_reaches_the_extractor(store, monkeypatch):
    """vault.stage() saving whole messages is why there are 27 staging rows
    of "hi" and "why did you ask that?". A per-message model call that fires
    on those is the most expensive thing in the product."""
    c, conn = store
    called = []
    monkeypatch.setattr(c.llm, "complete_json",
                        lambda *a, **k: called.append(1) or [])
    for junk in ("hi", "ok", "thanks", "apply to 2", "?"):
        assert c.capture(junk, conn) == []
    assert not called, "no model call for a message that cannot hold a fact"


def test_a_failing_extractor_never_breaks_the_turn(store, monkeypatch):
    c, conn = store

    def boom(*a, **k):
        raise RuntimeError("provider down")

    monkeypatch.setattr(c.llm, "complete_json", boom)
    assert c.capture("i grew activation 18% at Paytm last year", conn) == []


def test_capture_keeps_their_sentence_not_the_paraphrase(store, monkeypatch):
    c, conn = store
    monkeypatch.setattr(c.llm, "complete_json", lambda *a, **k: [
        {"kind": "accomplishment", "text": "Grew activation 18%",
         "company": "Paytm"}])
    said = "so last year i grew activation by about 18% at paytm"
    c.capture(said, conn)
    row = c.all(conn=conn)[0]
    assert row["text"] == "Grew activation 18%"      # goes on the resume
    assert row["heard_in"] == said                   # shown when they ask why


# --- a resume with no job attached ------------------------------------------

def test_a_resume_can_be_built_from_the_record(store):
    c, conn = store
    from resume import generate
    master = {"name": "X", "experience": [
        {"company": "Lenskart", "title": "Growth Manager",
         "bullets": ["Owned the CRM revenue line."]}]}
    c.add("accomplishment", "Grew activation 18%", company="Lenskart",
          source="chat", conn=conn)
    built, unplaced = generate.from_career(master, conn)
    assert built["experience"][0]["bullets"] == [
        "Owned the CRM revenue line.", "Grew activation 18%"]
    assert unplaced == []


def test_an_accomplishment_with_no_company_is_handed_back_not_filed(store):
    """Guessing which job a thing belongs to is inventing employment history."""
    c, conn = store
    from resume import generate
    master = {"name": "X", "experience": [
        {"company": "Lenskart", "title": "GM", "bullets": ["Owned CRM."]}]}
    c.add("accomplishment", "Led a team of six", source="chat", conn=conn)
    built, unplaced = generate.from_career(master, conn)
    assert built["experience"][0]["bullets"] == ["Owned CRM."]
    assert len(unplaced) == 1 and "six" in unplaced[0]["text"]


def test_the_parsed_resume_is_not_duplicated_into_itself(store):
    """Seeded rows are source=resume and must not be appended a second time."""
    c, conn = store
    from resume import generate
    master = {"name": "X", "experience": [
        {"company": "Lenskart", "title": "GM", "bullets": ["Owned CRM."]}]}
    c.seed_from_master(master, conn)
    built, _ = generate.from_career(master, conn)
    assert built["experience"][0]["bullets"] == ["Owned CRM."]


def test_a_tool_name_they_stated_is_allowed_too(store):
    """Names were left out of `supported` at first and the split was
    invisible: the store held the skill, the number path worked, and the
    bullet was still reverted."""
    c, conn = store
    orig = "Owned the CRM revenue line."
    rew = "Owned the CRM revenue line, tracked in Amplitude."
    assert verify.check_bullet(orig, rew, c.supported(conn)), "not stated yet"
    c.add("skill", "Amplitude", source="user",
          heard_in="i used Amplitude daily", conn=conn)
    assert verify.check_bullet(orig, rew, c.supported(conn)) == []


def test_a_company_they_never_worked_at_is_still_blocked(store):
    c, conn = store
    c.add("skill", "Amplitude", source="user", conn=conn)
    assert verify.check_bullet("Owned the CRM revenue line.",
                               "Owned the CRM revenue line at Google.",
                               c.supported(conn))


def test_where_a_name_came_from_can_be_answered(store):
    c, conn = store
    c.add("skill", "Amplitude", source="user",
          heard_in="i used Amplitude daily", conn=conn)
    assert c.provenance("Amplitude", conn)["heard_in"] == "i used Amplitude daily"


def test_the_user_can_overrule_the_guard_by_saying_it(store):
    """The guard is there to stop the model inventing, not to stop the person
    describing their own career. The system has seen one PDF; they lived the
    whole thing, and work that never made that document is the usual reason
    for a gap. Saying so plainly must be enough."""
    c, conn = store
    orig, rew = "Growth Manager with 4+ years.", "Growth Manager with 8 years."
    assert verify.check_bullet(orig, rew, c.supported(conn)), "not stated: blocked"

    c.add("role", "8 years of experience", source="user",
          heard_in="yes, 8 including the years before this job", conn=conn)
    assert verify.check_bullet(orig, rew, c.supported(conn)) == [], \
        "they said it themselves; it goes on"
    assert c.provenance("8", conn)["source"] == "user"


def test_the_decline_note_names_the_way_out(store):
    """A note that says no and stops leaves them arguing with a wall."""
    m = {"summary": "Growth Manager with 4+ years.", "experience": []}
    note = verify.instruction_outcome("say I have 8 years", m, m, [])[0]
    assert "remember_experience" in note
