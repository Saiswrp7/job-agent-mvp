"""Labels, label-aware search, saved searches, the live fallback, and the
choices offered when nothing fits. No model and no network: both are faked,
and the database is in memory.
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import adapters, db, labels, linkedin    # noqa: E402
from search import query, rank                       # noqa: E402
from search import run as run_mod                    # noqa: E402

from test_linkedin import FakeLinkedIn, card_html    # noqa: E402


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


def add(conn, sid, title="Growth Manager", city="Bangalore, India", source="lever",
        description="Own the funnel.", **labels_):
    row = adapters._row(source=source, source_id=sid, company=f"Co{sid}",
                        title=title, location=city, description=description,
                        url=f"https://x/{sid}", posted_at="2026-09-20")
    db.upsert(conn, [row])
    if labels_:
        sets = ", ".join(f"{k} = ?" for k in labels_)
        conn.execute(f"UPDATE jobs SET {sets}, label_hash = content_hash "
                     f"WHERE source_id = ?", [*labels_.values(), sid])
        conn.commit()


def ids(rows):
    return {r["source_id"] for r in rows}


# --- labels: cleaning what the model returns --------------------------------

def test_clean_keeps_only_values_the_columns_accept():
    got = labels.clean({"role_family": "Growth", "level": "wizard",
                        "years_min": "3", "owns_pnl": "true",
                        "country": " India ", "work_mode": "remote",
                        "company_type": "b2c"})
    # company_type is not a label at all: a guess from a company name hid jobs.
    assert got == {"role_family": "growth", "level": None, "years_min": 3,
                   "owns_pnl": 1, "country": "India", "work_mode": "remote"}


def test_clean_rejects_nonsense_years():
    assert labels.clean({"years_min": 45})["years_min"] is None
    assert labels.clean({"years_min": True})["years_min"] is None


# --- labels: the run --------------------------------------------------------

def fake_labeller(monkeypatch, answer):
    """answer(rendered_user_text) -> list of label dicts."""
    calls = []

    def fake(system, user, **kw):
        calls.append(kw)
        return answer(user)
    monkeypatch.setattr(labels.llm, "complete_json", fake)
    monkeypatch.setattr(labels.llm, "prompt", lambda n: "")
    monkeypatch.setattr(labels, "provider", lambda: "openrouter")
    return calls


def test_run_writes_labels_and_never_overwrites_known_facts(conn, monkeypatch):
    add(conn, "1", description="Own the P&L. 3-5 years.")      # regex years = 3
    add(conn, "2")
    conn.execute("UPDATE jobs SET company_type = 'b2b' WHERE source_id = '1'")
    calls = fake_labeller(monkeypatch, lambda u: [
        {"id": i, "role_family": "growth", "level": "mid", "years_min": 9,
         "owns_pnl": True, "country": "India", "work_mode": "remote",
         "company_type": "b2c"} for i in range(2)])
    r = labels.run(conn, verbose=False)
    assert r["labelled"] == 2
    job = dict(conn.execute("SELECT * FROM jobs WHERE source_id = '1'").fetchone())
    other = dict(conn.execute("SELECT * FROM jobs WHERE source_id = '2'").fetchone())
    assert other["company_type"] is None          # the model's guess is ignored
    assert (job["role_family"], job["level"], job["owns_pnl"]) == ("growth", "mid", 1)
    assert job["years_min"] == 3 and job["company_type"] == "b2b"
    assert job["remote"] == 1
    assert calls[0]["temperature"] == 0 and calls[0]["via"] == "openrouter"


def test_a_labelled_job_is_not_labelled_again_until_its_text_changes(conn, monkeypatch):
    add(conn, "1")
    fake_labeller(monkeypatch, lambda u: [{"id": 0, "role_family": "growth"}])
    labels.run(conn, verbose=False)
    assert labels.pending(conn) == []
    add(conn, "1", description="A completely different job now.")
    assert len(labels.pending(conn)) == 1


def test_a_job_the_reply_skipped_stays_unlabelled(conn, monkeypatch):
    add(conn, "1"); add(conn, "2")
    fake_labeller(monkeypatch, lambda u: [{"id": 0, "role_family": "growth"}])
    r = labels.run(conn, verbose=False)
    assert r["labelled"] == 1 and r["skipped"] == 1
    assert len(labels.pending(conn)) == 1


def test_a_dead_provider_stops_the_run(conn, monkeypatch):
    for i in range(60):
        add(conn, str(i))

    def boom(u):
        raise RuntimeError("429 out of credit")
    fake_labeller(monkeypatch, boom)
    r = labels.run(conn, verbose=False, workers=1)
    assert r["stopped"] and r["failed_batches"] == labels.GIVE_UP_AFTER
    assert r["labelled"] == 0


# --- search reads the labels ------------------------------------------------

def test_a_wrong_label_does_not_hide_a_job_whose_title_fits(conn):
    # The real case: a cheap model labelled "Data Product Manager" as `data`.
    add(conn, "1", title="Data Product Manager", role_family="data")
    rows, _ = query.search({"title_keywords": ["product manager"],
                            "role_family": ["product"]}, conn)
    assert ids(rows) == {"1"}


def test_the_label_finds_a_job_the_title_keyword_misses(conn):
    add(conn, "1", title="Product Designer", role_family="design")
    add(conn, "2", title="Sales Lead", role_family="sales")
    rows, _ = query.search({"title_keywords": ["ux"], "role_family": ["design"]}, conn)
    assert ids(rows) == {"1"}


def test_country_filters_but_unknown_and_global_pass(conn):
    add(conn, "in", country="India")
    add(conn, "kr", country="South Korea")
    add(conn, "gl", country="Global")
    add(conn, "unlabelled")
    rows, _ = query.search({"country": "india"}, conn)
    assert ids(rows) == {"in", "gl", "unlabelled"}


def test_level_filters_only_when_asked(conn):
    add(conn, "jr", level="junior")
    add(conn, "sr", level="senior")
    assert ids(query.search({}, conn)[0]) == {"jr", "sr"}
    assert ids(query.search({"level": ["senior"]}, conn)[0]) == {"sr"}


def test_unknown_filter_values_are_dropped_not_sent_to_sql():
    f = query.normalize({"role_family": ["growth", "wizardry"], "level": "boss",
                         "owns_pnl": "yes please"})
    assert f["role_family"] == ["growth"] and f["level"] is None
    assert f["owns_pnl"] is None


def test_owning_a_pnl_sorts_first_and_removes_nothing(conn):
    add(conn, "support", owns_pnl=0)
    add(conn, "owner", owns_pnl=1)
    rows, _ = query.search({"owns_pnl": True}, conn)
    assert [r["source_id"] for r in rows] == ["owner", "support"]


def test_experience_orders_too_senior_last_but_keeps_it(conn):
    add(conn, "director", title="Director Growth", level="leadership")
    add(conn, "ten", description="Needs 10+ years.")
    add(conn, "fit", level="mid", years_min=3)
    rows, _ = query.search({}, conn, fit_years=4)
    order = [r["source_id"] for r in rows]
    assert order[0] == "fit" and set(order) == {"director", "ten", "fit"}


def test_no_experience_known_means_no_reordering(conn):
    sql, params, _ = query.build({}, fit_years=None)
    assert "1 AS fit" in " ".join(sql.split())


# --- saved searches ----------------------------------------------------------

def test_an_ask_becomes_a_saved_search_the_daily_fetch_runs(conn):
    linkedin.remember(conn, "UX Designer", "Kochi, India", False)
    linkedin.remember(conn, "ux designer", "Kochi, India", False)
    row = conn.execute("SELECT keywords, times_asked FROM saved_searches").fetchone()
    assert tuple(row) == ("ux designer", 2)
    keys = {linkedin.search_key(s) for s in linkedin.load_searches(conn=conn)}
    assert "ux designer @ Kochi, India" in keys


def test_a_search_nobody_asked_for_in_months_is_not_fetched(conn):
    linkedin.remember(conn, "florist", "Kochi, India", False)
    conn.execute("UPDATE saved_searches SET last_asked = '2020-01-01'")
    keys = {linkedin.search_key(s) for s in linkedin.load_searches(conn=conn)}
    assert "florist @ Kochi, India" not in keys


# --- live -------------------------------------------------------------------

def sync_client(fake: FakeLinkedIn) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(fake))


def test_live_stores_new_jobs_and_skips_ones_already_here(conn):
    add(conn, "7", source="linkedin")
    fake = FakeLinkedIn(pages={0: [card_html("7"), card_html("8"), card_html("9")]})
    conn.execute("INSERT INTO linkedin_queue (source_id, card) VALUES ('9', '{}')")
    added = linkedin.live(conn, "growth manager", "Kochi, India",
                          client=sync_client(fake), pause=0)
    assert added == [("linkedin", "8"), ("linkedin", "9")]
    # Queued links are fetched now instead of waiting for tomorrow.
    assert conn.execute("SELECT COUNT(*) FROM linkedin_queue").fetchone()[0] == 0
    assert not any("sortBy" in u for u in fake.calls)     # relevance, not date


def test_live_respects_its_cap(conn):
    fake = FakeLinkedIn(pages={0: [card_html(str(i)) for i in range(10)]})
    added = linkedin.live(conn, "pm", "India", client=sync_client(fake),
                          pause=0, max_new=3)
    assert len(added) == 3


def test_live_target_is_india_only_and_needs_a_role():
    assert run_mod.live_target({"title_keywords": ["ux designer"], "city": "Kochi"}) \
        == ("ux designer", "Kochi, India", False)
    assert run_mod.live_target({"title_keywords": ["pm"], "remote": True}) \
        == ("pm", "India", True)
    assert run_mod.live_target({"title_keywords": ["pm"], "country": "Germany"}) is None
    assert run_mod.live_target({"city": "Kochi"}) is None


# --- the whole search flow ---------------------------------------------------

@pytest.fixture
def offline_search(monkeypatch):
    """search() with the filter model and the ranker faked. Returns a dict the
    test fills: `filters` for what extraction returns, and `live_calls`."""
    box = {"filters": {}, "live_calls": [], "live_adds": []}
    monkeypatch.setattr(run_mod.filters_mod, "extract",
                        lambda m, profile="": query.normalize(box["filters"]))
    monkeypatch.setattr(rank, "pick", lambda rows, f, m, profile="": (
        [{**r, "reason": "fits"} for r in rows][: f["count"]], []))

    def fake_go_live(conn, target):
        box["live_calls"].append(target)
        for sid in box["live_adds"]:
            add(conn, sid, title="UX Designer", city="Kochi, India", source="linkedin")
        return ([("linkedin", s) for s in box["live_adds"]],
                f"searched LinkedIn live just now and added {len(box['live_adds'])}")
    monkeypatch.setattr(run_mod, "go_live", fake_go_live)
    monkeypatch.delenv("JOB_AGENT_LIVE", raising=False)
    return box


def test_short_results_go_live_and_new_jobs_are_shown(conn, offline_search):
    offline_search["filters"] = {"title_keywords": ["ux designer"], "city": "Kochi"}
    offline_search["live_adds"] = ["k1", "k2"]
    r = run_mod.search("UX designer jobs in Kochi", conn=conn, live=True)
    assert offline_search["live_calls"] == [("ux designer", "Kochi, India", False)]
    assert {p["source_id"] for p in r["picks"]} == {"k1", "k2"}
    assert any("live" in n for n in r["notes"])


def test_every_ask_is_saved_even_when_found(conn, offline_search):
    offline_search["filters"] = {"title_keywords": ["growth"], "city": "Bangalore"}
    run_mod.search("growth in bangalore", conn=conn)
    assert conn.execute("SELECT COUNT(*) FROM saved_searches").fetchone()[0] == 1


def test_live_is_off_unless_asked_and_off_under_the_harness(conn, offline_search,
                                                            monkeypatch):
    offline_search["filters"] = {"title_keywords": ["ux designer"], "city": "Kochi"}
    run_mod.search("ux in kochi", conn=conn)
    monkeypatch.setenv("JOB_AGENT_LIVE", "0")
    run_mod.search("ux in kochi", conn=conn, live=True)
    assert offline_search["live_calls"] == []


def test_still_short_offers_other_places_as_a_choice(conn, offline_search):
    for i in range(3):
        add(conn, f"b{i}", title="UX Designer", city="Bangalore, India")
    add(conn, "r", title="UX Designer", city="Remote, India")
    offline_search["filters"] = {"title_keywords": ["ux designer"], "city": "Kochi"}
    r = run_mod.search("UX designer jobs in Kochi", conn=conn, live=True)
    # The Bangalore jobs are counted, never shown as Kochi results.
    assert r["picks"] == [] or all("Kochi" in (p["city"] or "") or p["remote"]
                                   for p in r["picks"])
    choice = [n for n in r["notes"] if "offer these as a choice" in n]
    assert choice and "3 in Bangalore" in choice[0]
    assert "Nothing matched. (" in r["text_agent"] or r["picks"]


def test_experience_years_come_from_the_vault_first(conn):
    conn.execute("INSERT INTO vault (key, value) VALUES ('years_experience', '6')")
    assert run_mod.experience_years(conn) == 6


def test_an_empty_result_keeps_its_notes():
    out = rank.format_results([], ["searched LinkedIn live just now"], numbered=False)
    assert "searched LinkedIn live just now" in out


def test_when_glm_runs_out_the_backup_finishes_the_job(conn, monkeypatch):
    for i in range(40):
        add(conn, str(i))
    used = []

    def fake(system, user, **kw):
        used.append(kw["via"])
        if kw["via"] == "glm":
            raise RuntimeError("429 余额不足")
        return [{"id": i, "role_family": "growth"} for i in range(10)]
    monkeypatch.setattr(labels.llm, "complete_json", fake)
    monkeypatch.setattr(labels.llm, "prompt", lambda n: "")
    monkeypatch.setattr(labels, "provider", lambda: "glm")
    monkeypatch.setattr(labels, "backup", lambda: "openrouter")
    r = labels.run(conn, verbose=False, workers=1)
    assert r["labelled"] == 40 and r["fell_back_to"] == "openrouter"
    assert "openrouter" in used and labels.pending(conn) == []


def test_glm_calls_send_effort_and_temperature(monkeypatch):
    import llm
    sent = {}

    class FakeMessages:
        def create(self, **kw):
            sent.update(kw)
            from types import SimpleNamespace as NS
            return NS(stop_reason="end_turn",
                      content=[NS(type="text", text="ok")])

    class FakeClient:
        messages = FakeMessages()
    monkeypatch.setattr(llm, "client", lambda p=None: FakeClient())
    llm.complete("s", "u", model="glm-5.3-flash", via="glm",
                 effort="low", temperature=0)
    assert sent["output_config"] == {"effort": "low"}
    assert sent["temperature"] == 0 and "thinking" not in sent


def test_glm_never_gets_medium_effort(monkeypatch):
    import llm
    assert llm.effort_level("medium", "glm") == "high"
    assert llm.effort_level("medium", "anthropic") == "medium"
    assert llm.effort_level("low", "glm") == "low"


# --- reading what they said (bugs from the first real test chat) -------------

def test_remote_or_a_city_is_an_or_not_remote_only(conn):
    add(conn, "blr", city="Bangalore, India")
    add(conn, "rem", city="Remote, India")
    add(conn, "pune", city="Pune, India")
    rows, _ = query.search({"city": "Bangalore", "remote": True}, conn)
    assert ids(rows) == {"blr", "rem"}
    assert ids(query.search({"remote": True}, conn)[0]) == {"rem"}


def test_a_typo_still_counts_as_asking():
    assert run_mod.mentioned("Bangalore", "Growth role evenue focused remote or banagalroe")
    assert run_mod.mentioned("Bangalore", "jobs in Bengaluru please")
    assert not run_mod.mentioned("Bangalore", "i need a job")
    assert not run_mod.mentioned("India", "i am looking for something interesting")


def test_saying_yes_to_the_city_counts_as_asking_for_it(conn, offline_search):
    offline_search["filters"] = {"title_keywords": ["growth"], "city": "Bangalore"}
    r = run_mod.search("growth, Bangalore only", conn=conn, said_verbatim="yes",
                       said_recently="growth roles remote or banagalroe yes")
    assert not any("didn't ask" in n for n in r["notes"])


def test_a_named_company_never_triggers_a_live_search():
    assert run_mod.live_target({"title_keywords": ["lead"], "companies": ["CRED"]}) is None
    assert run_mod.live_target({"title_keywords": ["growth"], "city": "Bangalore",
                                "remote": True}) == ("growth", "Bangalore, India", False)
