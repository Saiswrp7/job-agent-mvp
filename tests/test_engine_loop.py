"""The engine (engine/loop.py): India first, the last 30 days, and a job
closed only when its employer's own list stops showing it.

No network (every board is a fake transport or a fake feed), no real table
(an in-memory database per test).
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import adapters, ats_bridge, db, loop, registry   # noqa: E402
from engine.ats_bridge import Feed                             # noqa: E402


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


def ago(days: int) -> str:
    return (date.today() - timedelta(days=days)).isoformat()


BOARD = {"source": "greenhouse", "slug": "acme", "company": "Acme"}


def gh_job(jid, location="Bengaluru, India", posted=None, title="Product Manager"):
    return {"id": jid, "title": title, "location": {"name": location},
            "content": "<p>Own the roadmap.</p>" * 40,
            "absolute_url": f"https://job-boards.greenhouse.io/acme/jobs/{jid}",
            "first_published": posted or ago(3), "updated_at": posted or ago(3),
            "departments": [{"name": "Product"}]}


def gh(jobs=None, status=200):
    """A Greenhouse board answering with these jobs (or this status)."""
    def handle(request):
        if status != 200:
            return httpx.Response(status)
        return httpx.Response(200, json={"jobs": jobs() if callable(jobs) else jobs})
    return httpx.MockTransport(handle)


def add_board(conn, **kw):
    b = {**BOARD, **kw}
    conn.execute("INSERT INTO boards (source, slug, company) VALUES (?, ?, ?)",
                 (b["source"], b["slug"], b["company"]))
    conn.commit()
    return b


def board_row(conn, b=BOARD):
    return dict(conn.execute("SELECT * FROM boards WHERE source = ? AND slug = ?",
                             (b["source"], b["slug"])).fetchone())


def read(conn, transport, b=None):
    b = b or board_row(conn)

    async def go():
        async with loop.client(transport) as c:
            return await loop.read_board(conn, c, b)
    return asyncio.run(go())


def open_ids(conn):
    return {r[0] for r in conn.execute(
        "SELECT source_id FROM jobs WHERE closed_at IS NULL")}


# --- reading and closing ----------------------------------------------------------

def test_a_job_closes_on_the_second_complete_read_that_misses_it(conn):
    add_board(conn)
    listed = [gh_job(1), gh_job(2)]
    read(conn, gh(lambda: listed))
    assert open_ids(conn) == {"1", "2"}

    listed = [gh_job(1)]
    r = read(conn, gh(lambda: listed))
    assert r["missed"] == 1 and r["closed"] == 0 and open_ids(conn) == {"1", "2"}
    # A first miss brings the next read forward, to confirm it soon.
    due = board_row(conn)["next_due"]
    assert due < loop._sql_time(loop._now() + timedelta(minutes=20))

    r = read(conn, gh(lambda: listed))
    assert r["closed"] == 1 and open_ids(conn) == {"1"}


def test_a_job_skipped_once_by_a_shifting_list_is_never_closed(conn):
    add_board(conn)
    read(conn, gh([gh_job(1), gh_job(2)]))
    read(conn, gh([gh_job(1)]))                 # a miss
    read(conn, gh([gh_job(1), gh_job(2)]))      # back: the miss is forgotten
    read(conn, gh([gh_job(1)]))                 # a first miss again, not a second
    assert open_ids(conn) == {"1", "2"}


def test_a_failed_read_closes_nothing_and_backs_off(conn):
    add_board(conn)
    read(conn, gh([gh_job(1), gh_job(2)]))
    for _ in range(3):
        r = read(conn, gh(status=503))
        assert not r["ok"] and r["closed"] == 0
    assert open_ids(conn) == {"1", "2"}
    b = board_row(conn)
    assert b["fail_streak"] == 3 and b["retired_at"] is None


def test_a_board_that_404s_three_times_is_retired_and_its_jobs_closed(conn):
    add_board(conn)
    read(conn, gh([gh_job(1)]))
    read(conn, gh(status=404))
    read(conn, gh(status=404))
    assert open_ids(conn) == {"1"}              # two 404s: still only a failure
    r = read(conn, gh(status=404))
    assert "retired" in r["error"] and open_ids(conn) == set()
    assert board_row(conn)["retired_at"] is not None


def test_a_list_that_suddenly_loses_most_jobs_proves_nothing(conn):
    add_board(conn)
    read(conn, gh([gh_job(i) for i in range(30)]))
    r = read(conn, gh([gh_job(0)]))
    assert r["complete"] == 0 and r["missed"] == 0
    assert len(open_ids(conn)) == 30


def test_a_closed_job_that_is_listed_again_reopens(conn):
    add_board(conn)
    read(conn, gh([gh_job(1), gh_job(2)]))
    read(conn, gh([gh_job(1)]))
    read(conn, gh([gh_job(1)]))
    assert open_ids(conn) == {"1"}
    read(conn, gh([gh_job(1), gh_job(2)]))
    assert open_ids(conn) == {"1", "2"}


def test_one_board_never_closes_another_boards_jobs(conn):
    """Two boards on one ATS can share a company name ("Career Site" on
    Oracle): closing is per board, never per company."""
    a = add_board(conn, slug="a", company="Career Site")
    b = add_board(conn, slug="b", company="Career Site")
    read(conn, gh([gh_job(1)]), board_row(conn, a))
    read(conn, gh([gh_job(2)]), board_row(conn, b))
    for _ in range(2):
        read(conn, gh([gh_job(1)]), board_row(conn, a))
    assert open_ids(conn) == {"1", "2"}


# --- India, and the last 30 days ------------------------------------------------------

def test_only_india_and_the_last_30_days_are_stored(conn):
    add_board(conn)
    read(conn, gh([gh_job(1), gh_job(2, location="New York, NY"),
                   gh_job(3, posted=ago(45)), gh_job(4, location="Remote"),
                   gh_job(5, location="Remote - US")]))
    assert open_ids(conn) == {"1", "4"}
    # The old one is remembered, not stored, so it is never taken for new.
    assert conn.execute("SELECT COUNT(*) FROM seen_ids").fetchone()[0] == 1


@pytest.mark.parametrize("where,ok", [
    ("Bengaluru, Karnataka, India", True), ("1401-G-India: Hyderabad", True),
    ("Remote", True), ("Remote - Anywhere", True), ("Remote, APAC", True),
    ("Remote - US", False), ("London, UK", False), (None, False),
])
def test_in_scope(where, ok):
    assert adapters.in_scope(where) is ok


def test_prune_keeps_30_days_and_remembers_what_it_drops(conn):
    rows = [adapters._row(source="lever", source_id=s, company="Acme", title="PM",
                          location="Pune", description="x", url="u",
                          posted_at=p, board="acme")
            for s, p in (("new", ago(2)), ("old", ago(40)))]
    db.upsert(conn, rows)
    out = db.prune(conn)
    assert out["aged_out"] == 1
    assert {r[0] for r in conn.execute("SELECT source_id FROM jobs")} == {"new"}
    assert conn.execute("SELECT source_id FROM seen_ids").fetchone()[0] == "old"


def test_prune_deletes_closed_jobs_after_a_few_days_not_at_once(conn):
    db.upsert(conn, [adapters._row(source="lever", source_id="1", company="A",
                                   title="PM", location="Pune", description="x",
                                   url="u", posted_at=ago(2), board="a")])
    conn.execute("UPDATE jobs SET closed_at = datetime('now', '-1 days')")
    assert db.prune(conn)["closed_deleted"] == 0
    conn.execute("UPDATE jobs SET closed_at = datetime('now', '-4 days')")
    assert db.prune(conn)["closed_deleted"] == 1


def fake_feed(monkeypatch, rows, described=None):
    """The bridge, faked: `rows` listed, `described` what a detail read gives."""
    asked = []

    async def describe(want):
        asked.extend(r["source_id"] for r in want)
        return [{**r, "description": "Full text. " * 50} for r in want] \
            if described is None else described

    async def fake_read(conn, client, board):
        return Feed(rows=[dict(r) for r in rows], ids=[r["source_id"] for r in rows],
                    describe=describe)
    monkeypatch.setattr(loop, "_read", fake_read)
    return asked


def wd_row(sid, posted, description=""):
    return adapters._row(source="workday", source_id=sid, company="Genpact",
                         title="Analyst", location="Mumbai, India",
                         description=description, url=f"https://g/job/{sid}",
                         apply_url=f"https://g/job/{sid}/apply", posted_at=posted,
                         board="g")


def test_descriptions_are_read_only_for_new_jobs_in_the_window(conn, monkeypatch):
    b = add_board(conn, source="workday", slug="g", company="Genpact")
    asked = fake_feed(monkeypatch, [wd_row("a", ago(1)), wd_row("b", ago(40))])
    read(conn, None, board_row(conn, b))
    assert asked == ["a"]                       # b is too old: never paid for
    asked.clear()
    read(conn, None, board_row(conn, b))
    assert asked == []                          # a is stored: a re-read is the list only
    row = conn.execute("SELECT description, apply_kind FROM jobs").fetchone()
    assert row["description"].startswith("Full text") and row["apply_kind"] == "account"


def test_an_undated_job_is_new_only_after_the_first_whole_read(conn, monkeypatch):
    b = add_board(conn, source="workday", slug="g", company="Genpact")
    fake_feed(monkeypatch, [wd_row("old", None)])
    read(conn, None, board_row(conn, b))
    assert open_ids(conn) == set()              # listed on day one: age unknown
    fake_feed(monkeypatch, [wd_row("old", None), wd_row("fresh", None)])
    read(conn, None, board_row(conn, b))
    assert open_ids(conn) == {"fresh"}          # appeared later: new, dated today
    # Dated in UTC, like every time the engine stores (not the Mac's local day).
    from datetime import UTC, datetime
    assert conn.execute("SELECT posted_at FROM jobs").fetchone()[0] == \
        datetime.now(UTC).date().isoformat()


def test_a_re_read_without_text_never_wipes_the_stored_text(conn):
    db.upsert(conn, [wd_row("a", ago(1), description="The real JD.")])
    db.upsert(conn, [wd_row("a", None, description="")])
    row = conn.execute("SELECT description, posted_at FROM jobs").fetchone()
    assert row["description"] == "The real JD." and row["posted_at"] == ago(1)


# --- what search may show ---------------------------------------------------------------

def test_search_shows_only_open_recent_board_jobs_seen_lately(conn):
    from search import query
    rows = [adapters._row(source=s, source_id=i, company="A", title="Product Manager",
                          location="Pune, India", description="x", url="https://x",
                          posted_at=p)
            for s, i, p in (("lever", "ok", ago(2)), ("adzuna", "agg", ago(2)),
                            ("lever", "stale", ago(2)), ("lever", "old", ago(35)))]
    db.upsert(conn, rows)
    conn.execute("UPDATE jobs SET last_seen = datetime('now', '-3 days') "
                 "WHERE source_id = 'stale'")
    found, _ = query.search({"title_keywords": ["product"]}, conn)
    assert {r["source_id"] for r in found} == {"ok"}


def test_a_replay_over_recorded_jobs_shows_what_was_open(conn, monkeypatch):
    from search import query
    db.upsert(conn, [adapters._row(source="lever", source_id="rec", company="A",
                                   title="PM", location="Pune", description="x",
                                   url="u", posted_at="2026-01-01")])
    assert query.search({}, conn)[0] == []
    monkeypatch.setenv("JOB_AGENT_FROZEN_JOBS", "1")
    assert [r["source_id"] for r in query.search({}, conn)[0]] == ["rec"]


def test_forms_we_can_fill_come_before_sign_up_sites(conn):
    from search import query
    for sid, src in (("acct", "workday"), ("form", "lever")):
        db.upsert(conn, [adapters._row(source=src, source_id=sid, company="A",
                                       title="PM", location="Pune", description="x",
                                       url=f"https://x/{sid}", posted_at=ago(1))])
    assert [r["source_id"] for r in query.search({}, conn)[0]] == ["form", "acct"]


# --- the bridge and the registry ----------------------------------------------------------

@pytest.mark.parametrize("text,days", [
    ("Posted Today", 0), ("Posted Yesterday", 1), ("Posted 3 Days Ago", 3),
    ("Posted 30+ Days Ago", 31), (None, None), ("sometime", None)])
def test_workday_relative_dates(text, days):
    assert ats_bridge._days_ago(text) == days


def test_workday_india_filter_uses_the_sites_own_locations():
    facets = [{"facetParameter": "jobFamilyGroup", "values": [{"id": "x", "descriptor": "Ops"}]},
              {"facetParameter": "locationMainGroup", "values": [
                  {"facetParameter": "locations", "values": [
                      {"id": "hyd", "descriptor": "1401-G-India: Uppal, Hyderabad"},
                      {"id": "pol", "descriptor": "6003-GP: Lublin, Poland"},
                      {"id": "blr", "descriptor": "GIPL: Tech Park, Bangalore"}]}]}]
    assert ats_bridge._india_values(facets) == ("locations", ["hyd", "blr"])
    country = [{"facetParameter": "locationCountry", "values": [
        {"id": "in", "descriptor": "India"}, {"id": "us", "descriptor": "United States"}]}]
    assert ats_bridge._india_values(country) == ("locationCountry", ["in"])
    assert ats_bridge._india_values([]) is None


def test_apply_links_are_the_employers_own():
    wd = "https://g.wd1.myworkdayjobs.com/Site/job/Mumbai/Analyst_R1"
    assert ats_bridge.apply_url_of("workday", wd, None) == wd + "/apply"
    orc = "https://ebwg.fa.us2.oraclecloud.com/?keyword=&mode=jobs&lang=en&site_number=CX#19877"
    assert ats_bridge.apply_url_of("oracle", orc, None) == \
        "https://ebwg.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX/job/19877"
    assert adapters.kind_of("workday", wd) == "account"
    assert adapters.kind_of("greenhouse", "https://job-boards.greenhouse.io/a/jobs/1") == "form"
    assert adapters.kind_of("greenhouse", "https://www.naukri.com/job/1") == "link"
    assert adapters.kind_of("lever", "mailto:jobs@acme.com") == "link"


def test_the_bridge_reads_only_what_it_is_allowed_to():
    assert "darwinbox" not in ats_bridge.ALLOWED and "uber" not in ats_bridge.ALLOWED
    with pytest.raises(ValueError):
        ats_bridge.make({"source": "darwinbox", "slug": "x", "company": "X"})


@pytest.mark.parametrize("ats,row,slug", [
    ("workday", {"url": "https://g.wd108.myworkdayjobs.com/External_Careers"},
     "https://g.wd108.myworkdayjobs.com/External_Careers"),
    ("oracle", {"url": "https://ebwg.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001"},
     "https://ebwg.fa.us2.oraclecloud.com?site_number=CX_1001"),
    ("greenhouse", {"slug": "Acme", "url": ""}, "acme"),
    ("lever", {"url": "https://jobs.lever.co/zupee"}, "zupee"),
    ("icims", {"url": "https://careers-abc.icims.com/jobs"}, "abc"),
    ("keka", {"url": "https://kp.keka.com/careers"}, "https://kp.keka.com/careers"),
])
def test_dataset_rows_become_what_each_reader_needs(ats, row, slug):
    assert registry.slug_for(ats, row) == slug


def test_registry_matches_by_careers_site_then_name_and_leaves_out_staffing(tmp_path):
    companies = [
        ("workday", "Genpact", "genpact/External_Careers",
         "https://genpact.wd108.myworkdayjobs.com/External_Careers"),
        ("oracle", "ACI Worldwide", "ebwg",
         "https://ebwg.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX"),
        ("smartrecruiters", "WebleeTechnologies", "WebleeTechnologies",
         "https://careers.smartrecruiters.com/WebleeTechnologies"),
        ("greenhouse", "Zeta", "zeta", "https://job-boards.greenhouse.io/zeta"),
    ]
    india = [
        ("workday", "Genpact",
         "https://genpact.wd108.myworkdayjobs.com/External_Careers/job/Mumbai/A_R1", 1500),
        ("oracle", "Ebwg",                         # the dataset's name is the tenant code
         "https://ebwg.fa.us2.oraclecloud.com/?keyword=&mode=jobs&lang=en&site_number=CX#1", 90),
        ("smartrecruiters", "WebleeTechnologies",
         "https://jobs.smartrecruiters.com/WebleeTechnologies/1", 3500),
        ("greenhouse", "Zeta", "https://www.zeta.tech/careers/1", 12),  # own domain: by name
        ("workday", "Nobody", "https://nobody.wd1.myworkdayjobs.com/x/job/1", 5),
    ]
    out = tmp_path / "boards.csv"
    r = registry.match(india, companies, out)
    assert r == {"employers": 5, "boards": 3, "unmatched": 1, "staffing": 1}
    rows = out.read_text()
    assert "ACI Worldwide" in rows and "Weblee" not in rows

    c = db.connect(":memory:")
    assert registry.seed(c, out) == {"added": 3, "renamed": 0}
    assert registry.seed(c, out)["added"] == 0           # weekly rerun adds only new ones
    c.close()


# --- the agent's tools on the new table ----------------------------------------------------

@pytest.fixture
def tools(conn, monkeypatch):
    import chat
    monkeypatch.setattr(chat.apply_agent, "browser_for", lambda *a, **k: None)
    monkeypatch.setattr(chat.apply_agent, "start",
                        lambda *a, **k: {"status": "blocked", "message": "off"})

    def job(sid, source="lever", **kw):
        row = adapters._row(source=source, source_id=sid, company="Acme",
                            title="Product Manager", location="Pune",
                            description="x", url=f"https://jobs.lever.co/acme/{sid}",
                            posted_at=ago(1), **kw)
        db.upsert(conn, [row])
        return dict(row)

    def run(tool, j, message="apply with my file"):
        state = {"turn": 2, "user_message": message, "picks": [], "resumes": {},
                 "by_ref": {"j": j}}
        return chat.run_tool(tool, {"ref": "j"}, state, conn)
    return job, run


def test_a_job_closed_since_the_search_is_not_described_tailored_or_applied(conn, tools):
    job, run = tools
    j = job("1")
    conn.execute("UPDATE jobs SET closed_at = CURRENT_TIMESTAMP")
    conn.commit()
    for tool in ("get_job", "tailor_resume", "start_application"):
        assert run(tool, j).startswith("GONE:"), tool
    assert conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 0


def test_a_job_pruned_since_the_search_is_gone_too(conn, tools):
    job, run = tools
    j = job("1")
    conn.execute("DELETE FROM jobs")
    assert run("get_job", j).startswith("GONE:")


def test_sign_up_sites_and_links_are_never_started(conn, tools):
    job, run = tools
    acct = job("w", source="workday")
    out = run("start_application", acct)
    assert out.startswith("NOT STARTED") and "account" in out and "/w" in out
    link = job("n", apply_url="https://www.naukri.com/job/9")
    assert run("start_application", link).startswith("NOT STARTED")
    assert conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 0


def test_application_status_survives_its_job_being_deleted(conn, tools):
    import chat
    from apply import agent as apply_agent
    job, _ = tools
    j = job("1")
    app_id = apply_agent.create(j, None, conn)
    conn.execute("UPDATE applications SET status = 'waiting', question = 'notice?' "
                 "WHERE id = ?", (app_id,))            # a parked run
    conn.execute("DELETE FROM jobs")
    conn.commit()
    out = chat.run_tool("application_status", {"app_id": app_id},
                        {"turn": 1, "picks": [], "resumes": {}}, conn)
    assert "Acme" in out or "Product Manager" in out


def test_the_link_guard_knows_the_new_careers_sites():
    import chat
    jobs = [{"company": "Zeta", "title": "PM",
             "apply_url": "https://careers.zeta.tech/job/1"}]
    # Outside a numbered item, a job link nobody returned is removed: on a
    # known ATS host, or on the employer's own careers host.
    text = ("See https://genpact.wd108.myworkdayjobs.com/x/job/y, "
            "https://careers.zeta.tech/job/555 and https://help.example.com/faq\n"
            "1. PM at Zeta https://careers.zeta.tech/job/999\n")
    fixed = chat.fix_links(text, jobs)
    assert "myworkdayjobs" not in fixed and "job/555" not in fixed
    assert "help.example.com/faq" in fixed
    # Inside the item, the link becomes the named job's own.
    assert "job/999" not in fixed and "https://careers.zeta.tech/job/1" in fixed
