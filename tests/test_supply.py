"""Growing supply: the Workable and SmartRecruiters boards, board discovery,
free rule labels, and the usable-supply count.

No network (every platform is a fake transport), no real table (an in-memory
or tmp database per test).
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import adapters, db, discover, rules, supply, sync   # noqa: E402


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


def client(handler, calls=None):
    def wrap(request):
        if calls is not None:
            calls.append(request)
        return handler(request)
    return httpx.AsyncClient(transport=httpx.MockTransport(wrap))


async def call(fn, handler, slug="acme", calls=None, meta=None):
    async with client(handler, calls) as c:
        return await fn(c, slug, "Acme", meta or {})


# --- Workable ----------------------------------------------------------------

WORKABLE = {"name": "Acme", "jobs": [{
    "title": "Product Manager", "shortcode": "AB12", "employment_type": "Full-time",
    "telecommuting": "False", "department": "Product",
    "url": "https://apply.workable.com/j/AB12",
    "application_url": "https://apply.workable.com/j/AB12/apply",
    "published_on": "2026-09-20", "country": "India", "city": "Bengaluru",
    "state": "Karnataka", "description": "<p>Own the roadmap.</p><ul><li>3-5 years</li></ul>"}]}


def test_workable_gives_full_text_and_the_employer_apply_link():
    rows = asyncio.run(call(adapters.workable,
                            lambda r: httpx.Response(200, json=WORKABLE)))
    [r] = rows
    assert r["source"] == "workable" and r["source_id"] == "AB12"
    assert r["apply_url"].endswith("/AB12/apply") and "adzuna" not in r["apply_url"]
    assert r["city"] == "Bangalore" and r["remote"] == 0
    assert "Own the roadmap." in r["description"] and "<p>" not in r["description"]
    assert r["years_min"] == 3


def test_workable_remote_jobs_are_marked_remote():
    remote = {"jobs": [{**WORKABLE["jobs"][0], "telecommuting": "True"}]}
    [r] = asyncio.run(call(adapters.workable, lambda _: httpx.Response(200, json=remote)))
    assert r["remote"] == 1


# --- SmartRecruiters -----------------------------------------------------------

def sr_detail(pid, released="2026-09-25T05:00:00.000Z"):
    return {"id": pid, "name": "Data Analyst ", "releasedDate": released,
            "location": {"city": "Mumbai", "region": "MH", "country": "in",
                         "remote": False},
            "postingUrl": f"https://jobs.smartrecruiters.com/Acme/{pid}",
            "applyUrl": f"https://jobs.smartrecruiters.com/Acme/{pid}?oga=true",
            "typeOfEmployment": {"label": "Full-time"},
            "function": {"label": "Analytics"},
            "jobAd": {"sections": {
                "jobDescription": {"title": "Job Description", "text": "<p>SQL all day.</p>"},
                "qualifications": {"title": "Qualifications", "text": "<p>2+ years</p>"}}}}


def sr_handler(n, released="2026-09-25T05:00:00.000Z"):
    """A board with n postings, listed 100 a page, each with its own detail."""
    def handle(request):
        parts = request.url.path.rstrip("/").split("/")
        if parts[-1] == "postings":
            off = int(request.url.params.get("offset", 0))
            content = [{"id": str(i), "releasedDate": released}
                       for i in range(off, min(off + 100, n))]
            return httpx.Response(200, json={"totalFound": n, "content": content})
        return httpx.Response(200, json=sr_detail(parts[-1], released))
    return handle


def test_smartrecruiters_pages_the_list_and_reads_every_new_posting(monkeypatch):
    monkeypatch.setattr(adapters, "SR_PAUSE", 0)
    calls = []
    rows = asyncio.run(call(adapters.smartrecruiters, sr_handler(150), calls=calls))
    assert len(rows) == 150
    assert calls[0].url.params["country"] == "in"          # Indian postings only
    r = rows[0]
    assert r["source"] == "smartrecruiters" and r["city"] == "Mumbai"
    assert "SQL all day." in r["description"] and "Qualifications" in r["description"]
    assert r["apply_url"].startswith("https://jobs.smartrecruiters.com/")
    assert len(calls) == 2 + 150                           # 2 list pages + details


def test_smartrecruiters_does_not_refetch_what_it_already_holds(monkeypatch):
    monkeypatch.setattr(adapters, "SR_PAUSE", 0)
    known = {"0": "2026-09-25T05:00:00.000Z", "1": "2026-09-01T00:00:00.000Z"}
    calls = []
    rows = asyncio.run(call(adapters.smartrecruiters, sr_handler(3), calls=calls,
                            meta={"known": known}))
    assert [r.get("unchanged", False) for r in rows] == [True, False, False]
    assert len(calls) == 1 + 2        # "1" was re-released, so it is read again


def test_sync_keeps_an_unchanged_posting_open_and_intact(conn, monkeypatch):
    """An `unchanged` row must count as seen, or close_missing closes it, and
    must not be upserted, or its description is wiped."""
    monkeypatch.setattr(adapters, "SR_PAUSE", 0)

    class Keep:                        # sync closes its connection at the end
        def __init__(self, c): self.c = c
        def __getattr__(self, k): return getattr(self.c, k)
        def close(self): pass
    monkeypatch.setattr(db, "connect", lambda *a, **k: Keep(conn))
    board = [{"company": "Acme", "source": "smartrecruiters", "slug": "acme"}]
    calls = []

    def run_once():
        return asyncio.run(sync.sync(board, verbose=False, transport=httpx.MockTransport(
            lambda r: calls.append(r) or sr_handler(2)(r))))

    run_once()
    before = conn.execute("SELECT description FROM jobs WHERE source_id='0'").fetchone()[0]
    calls.clear()
    run_once()
    rows = conn.execute("SELECT description, closed_at FROM jobs").fetchall()
    assert len(rows) == 2 and all(r[1] is None for r in rows)
    assert all(r[0] == before for r in rows) and before
    assert len(calls) == 1                     # the list only; no detail refetched


# --- discovery -------------------------------------------------------------------

def test_slug_guesses_drop_legal_words_and_try_the_core_name():
    assert discover.slugs("Zensar Technologies") == \
        ["zensartechnologies", "zensar", "zensar-technologies"]
    assert discover.slugs("Trigent Software Private Limited")[:2] == \
        ["trigentsoftware", "trigent"]
    assert discover.slugs("slice") == ["slice"]
    assert discover.slugs("BSR & Co") == ["bsr"]


def test_a_named_board_must_share_a_word_with_our_name():
    assert discover.same_company("Bosch Group", "Bosch Group")
    assert discover.same_company("Paytm Payments Services", None)   # no name given
    assert not discover.same_company("Beyond Border Consultants", "Beyond Meat")


def platform_fake(live: dict[str, dict], refuse: int = 0):
    """Greenhouse-shaped fake: a slug in `live` answers, others 404; the
    first `refuse` requests answer 429."""
    seen = {"n": 0}

    def handle(request):
        seen["n"] += 1
        if seen["n"] <= refuse:
            return httpx.Response(429)
        slug = request.url.path.split("/")[3]
        if slug in live:
            return httpx.Response(200, json=live[slug])
        return httpx.Response(404)
    return httpx.MockTransport(handle)


def test_probe_records_live_boards_with_their_indian_roles(tmp_path):
    out = tmp_path / "d.jsonl"
    live = {"acme": {"jobs": [{"location": {"name": "Bengaluru, India"}},
                              {"location": {"name": "Remote"}},
                              {"location": {"name": "New York"}}]}}
    r = asyncio.run(discover.run(["Acme", "Nobody"], out, platforms=["greenhouse"],
                                 pause=0, transport=platform_fake(live)))
    assert r["greenhouse"]["live"] == 1
    [hit] = discover.live(out)
    assert (hit["slug"], hit["jobs"], hit["india"], hit["remote"]) == ("acme", 3, 1, 1)
    # A second run asks nothing it already has an answer for.
    again = asyncio.run(discover.run(["Acme", "Nobody"], out, platforms=["greenhouse"],
                                     pause=0, transport=platform_fake(live)))
    assert again["greenhouse"]["probed"] == 0


def test_probe_stops_a_platform_that_keeps_refusing(tmp_path):
    names = [f"Company{i}" for i in range(20)]
    r = asyncio.run(discover.run(names, tmp_path / "d.jsonl", platforms=["greenhouse"],
                                 pause=0, transport=platform_fake({}, refuse=100)))
    assert r["greenhouse"]["stopped"] and r["greenhouse"]["probed"] == 0


# --- rule labels -----------------------------------------------------------------

def add(conn, **kw):
    base = dict(source="adzuna", source_id="1", company="Acme", title="Product Manager",
                location="Bangalore, Karnataka", description="x", url="u",
                posted_at="2026-09-25", department="IT Jobs")
    db.upsert(conn, [adapters._row(**{k: v for k, v in {**base, **kw}.items()
                                      if k != "department"})
                     | {"department": kw.get("department", base["department"])}])


def labels(conn, sid="1"):
    return dict(conn.execute("SELECT role_family, level, country, work_mode, remote "
                             "FROM jobs WHERE source_id = ?", (sid,)).fetchone())


def test_rules_fill_what_the_title_and_place_make_certain(conn):
    add(conn, title="Senior Product Manager")
    rules.run(conn)
    assert labels(conn) == {"role_family": "product", "level": "senior",
                            "country": "India", "work_mode": None, "remote": 0}


def test_rules_never_touch_a_row_the_llm_labelled(conn):
    add(conn, title="Senior Product Manager")
    conn.execute("UPDATE jobs SET role_family='growth', level=NULL, "
                 "label_hash=content_hash")
    rules.run(conn)
    got = labels(conn)
    assert got["role_family"] == "growth" and got["level"] is None


def test_rules_keep_a_value_already_there(conn):
    add(conn, title="Data Analyst")
    conn.execute("UPDATE jobs SET role_family='product'")
    rules.run(conn)
    assert labels(conn)["role_family"] == "product"


@pytest.mark.parametrize("title,category,expected", [
    ("Sales & Marketing Manager", None, None),            # two families: none
    ("Senior Growth Manager", "Sales Jobs", None),        # growth vs CRM: never guessed
    ("Retention Marketing Lead", None, None),
    ("Product Marketing Manager", None, "marketing"),
    ("Product Designer", None, "design"),
    ("Backend Developers", None, "engineering"),
    ("Relationship Manager", "Sales Jobs", "sales"),      # category, title silent
    ("Relationship Manager", "IT Jobs", None),            # IT names a dozen families
])
def test_one_family_or_none(title, category, expected):
    assert rules.family(title, category) == expected


def test_country_only_when_exactly_one_is_named():
    assert rules.country("Bengaluru, India", "lever") == "India"
    assert rules.country("New York, NY", "greenhouse") == "United States"
    assert rules.country("Bangalore; New York", "greenhouse") is None
    assert rules.country("Remote", "ashby") is None
    assert rules.country(None, "adzuna") == "India"


def test_a_rule_label_never_hides_a_job_whose_title_fits(conn):
    """The search rule for labels: title OR label. A rule-labelled job must
    still come back for a title search that names it."""
    from search import query
    add(conn, title="Growth Marketing Analyst")      # rules give no family
    add(conn, source_id="2", title="Product Marketing Manager")
    rules.run(conn)
    rows, _ = query.search({"title_keywords": ["product"], "role_family": ["product"]},
                           conn=conn)
    assert {r["source_id"] for r in rows} == {"2"}
    rows, _ = query.search({"role_family": ["growth"]}, conn=conn)
    assert {r["source_id"] for r in rows} == {"1"}   # unlabelled still passes


# --- the usable-supply count ----------------------------------------------------------

def test_supply_counts_only_jobs_the_product_can_fully_handle(conn):
    long = "Own the roadmap. " * 60
    recent = __import__("datetime").date.today().isoformat()
    rows = [
        dict(source="lever", source_id="1", apply_url="https://jobs.lever.co/a/1/apply"),
        dict(source="adzuna", source_id="2", apply_url="https://www.adzuna.in/land/2"),
        dict(source="linkedin", source_id="3", apply_url="https://www.linkedin.com/jobs/view/3"),
        dict(source="greenhouse", source_id="4", apply_url="https://boards.greenhouse.io/4",
             description="short"),
        dict(source="greenhouse", source_id="5", apply_url="https://boards.greenhouse.io/5",
             posted_at="2025-01-01"),
    ]
    for r in rows:
        base = dict(company="Acme", title="Product Manager", location="Bengaluru",
                    description=long, url="u", posted_at=recent)
        db.upsert(conn, [adapters._row(**{**base, **r})])
    assert supply.count(conn, "product manager", "Bangalore") == 1
