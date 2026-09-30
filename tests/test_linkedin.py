"""LinkedIn guest fetch. No network: a fake LinkedIn answers every request,
and the database is in memory.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import adapters, db, linkedin      # noqa: E402
from search import query                       # noqa: E402


def card_html(jid, title="Growth Manager", company="Zupee",
              location="Bengaluru, Karnataka, India", date="2026-09-20"):
    return f"""<li>
      <div class="base-card job-search-card" data-entity-urn="urn:li:jobPosting:{jid}">
        <h3 class="base-search-card__title">
          {title}
        </h3>
        <h4 class="base-search-card__subtitle">
          <a class="hidden-nested-link" href="https://in.linkedin.com/company/x">
            {company}
          </a>
        </h4>
        <span class="job-search-card__location">
          {location}
        </span>
        <time class="job-search-card__listdate" datetime="{date}">1 day ago</time>
      </div></li>"""


def detail_html(desc="Own the funnel. 3-5 years.", closed=False):
    return f"""
      <div class="show-more-less-html__markup relative">
        <p>{desc}</p><ul><li>SQL</li></ul>
      </div>
      {'<figcaption>No longer accepting applications</figcaption>' if closed else ''}
      <ul class="description__job-criteria-list">
        <li><h3 class="description__job-criteria-subheader">
            Seniority level
          </h3>
          <span class="description__job-criteria-text description__job-criteria-text--criteria">
            Mid-Senior level
          </span></li>
        <li><h3 class="description__job-criteria-subheader">
            Industries
          </h3>
          <span class="description__job-criteria-text description__job-criteria-text--criteria">
            Financial Services
          </span></li>
      </ul>"""


class FakeLinkedIn:
    """pages: {start: [card html, ...]}. details: {id: html}. Records calls."""

    def __init__(self, pages=None, details=None, block_after=None):
        self.pages = pages or {}
        self.details = details or {}
        self.block_after = block_after
        self.calls = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(str(request.url))
        if self.block_after is not None and len(self.calls) > self.block_after:
            return httpx.Response(429)
        url = urlparse(str(request.url))
        if url.path.endswith("/search"):
            start = int(parse_qs(url.query)["start"][0])
            return httpx.Response(200, text="".join(self.pages.get(start, [])))
        jid = url.path.rsplit("/", 1)[1]
        return httpx.Response(200, text=self.details.get(jid, detail_html()))

    def client(self):
        return httpx.AsyncClient(transport=httpx.MockTransport(self))


SEARCH = [{"keywords": "growth manager", "location": "India",
           "remote": False, "days": 30}]


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


def collect(conn, fake, searches=SEARCH):
    async def go():
        async with fake.client() as client:
            return await linkedin.collect(client, conn, searches,
                                          pause=0, verbose=False)
    return asyncio.run(go())


def fill(conn, fake, cap=300):
    async def go():
        async with fake.client() as client:
            return await linkedin.fill(client, conn, cap=cap,
                                       pause=0, verbose=False)
    return asyncio.run(go())


def queued(conn):
    return {r[0] for r in conn.execute("SELECT source_id FROM linkedin_queue")}


# --- reading the pages -----------------------------------------------------

def test_card_fields_are_read():
    [c] = linkedin.parse_cards(card_html("42", title="APM &amp; Growth",
                                         company="PW (PhysicsWallah)"))
    assert c == {"id": "42", "title": "APM & Growth",
                 "company": "PW (PhysicsWallah)",
                 "location": "Bengaluru, Karnataka, India",
                 "posted_at": "2026-09-20"}


def test_detail_gives_description_and_labels():
    d = linkedin.parse_detail(detail_html())
    assert "Own the funnel" in d["description"] and "SQL" in d["description"]
    assert "<" not in d["description"]
    assert d["seniority"] == "Mid-Senior level"
    assert d["industry"] == "Financial Services"
    assert d["closed"] is False
    assert linkedin.parse_detail(detail_html(closed=True))["closed"] is True


def test_searches_are_every_role_in_every_place():
    s = linkedin.load_searches()
    spec = json.loads(linkedin.SEARCHES.read_text())
    assert len(s) == len(spec["roles"]) * len(spec["places"])
    remote = [x for x in s if x["remote"]][0]
    p = linkedin.search_params(remote, 10)
    assert p["f_WT"] == "2" and p["sortBy"] == "DD" and p["start"] == 10


# --- step 1: collect links -------------------------------------------------

def test_new_links_are_queued_not_stored_as_jobs(conn):
    fake = FakeLinkedIn(pages={0: [card_html("1"), card_html("2", title="PM")]})
    r = collect(conn, fake)
    assert r["queued"] == 2 and queued(conn) == {"1", "2"}
    assert db.stats(conn)["total"] == 0          # no description, not a job yet


def test_a_repeat_run_stops_at_jobs_older_than_its_last_run(conn):
    pages = {0: [card_html("1", date="2999-01-01")],
             10: [card_html("2", date="2020-01-02")],
             20: [card_html("3", date="2020-01-01")]}
    collect(conn, FakeLinkedIn(pages=pages))          # first run: every page
    assert queued(conn) == {"1", "2", "3"}
    # Second run: page 0 has something newer than the last run, page 10 is all
    # older, so stop there. Two requests, not four.
    again = FakeLinkedIn(pages=pages)
    collect(conn, again)
    assert len(again.calls) == 2


def test_overlap_with_another_search_does_not_stop_paging(conn):
    """The bug this replaced: APM's first pages were all jobs the PM search had
    already found, so it stopped — and never reached the Growth Manager roles
    300 results deep that the PM search had been cut off before."""
    collect(conn, FakeLinkedIn(pages={0: [card_html("1")]}))
    apm = [{**SEARCH[0], "keywords": "associate product manager"}]
    pages = {0: [card_html("1")], 10: [card_html("1")], 20: [card_html("99")]}
    collect(conn, FakeLinkedIn(pages=pages), searches=apm)
    assert "99" in queued(conn)


def test_a_blocked_search_gets_no_date_and_pages_fully_next_time(conn):
    pages = {0: [card_html("1")], 10: [card_html("2")]}
    with pytest.raises(linkedin.Blocked):
        collect(conn, FakeLinkedIn(pages=pages, block_after=1))
    assert conn.execute("SELECT COUNT(*) FROM linkedin_searches").fetchone()[0] == 0


def test_a_job_a_board_already_has_is_skipped(conn):
    db.upsert(conn, [adapters._row(
        source="lever", source_id="L1", company="Paytm", title="Growth Manager",
        location="Noida", description="x", url="https://jobs.lever.co/paytm/1")])
    fake = FakeLinkedIn(pages={0: [card_html("9", company="Paytm"),
                                   card_html("10", company="Groww")]})
    r = collect(conn, fake)
    assert r["duplicates"] == 1 and queued(conn) == {"10"}


# --- step 2: fill in descriptions ------------------------------------------

def test_fill_moves_queued_jobs_into_the_table(conn):
    collect(conn, FakeLinkedIn(pages={0: [card_html("1")]}))
    r = fill(conn, FakeLinkedIn())
    assert r["added"] == 1 and queued(conn) == set()
    job = dict(conn.execute("SELECT * FROM jobs").fetchone())
    assert job["source"] == "linkedin" and job["city"] == "Bangalore"
    assert job["url"] == "https://www.linkedin.com/jobs/view/1"
    assert job["years_min"] == 3 and job["industry"] == "Financial Services"


def test_a_closed_job_is_dropped_not_stored(conn):
    collect(conn, FakeLinkedIn(pages={0: [card_html("1")]}))
    r = fill(conn, FakeLinkedIn(details={"1": detail_html(closed=True)}))
    assert r["added"] == 0 and r["already_closed"] == 1
    assert db.stats(conn)["total"] == 0 and queued(conn) == set()


def test_fill_stops_at_the_cap_and_leaves_the_rest_queued(conn):
    collect(conn, FakeLinkedIn(pages={0: [card_html(str(i)) for i in range(5)]}))
    fill(conn, FakeLinkedIn(), cap=2)
    assert len(queued(conn)) == 3 and db.stats(conn)["total"] == 2


def test_a_known_company_keeps_its_tags(conn):
    collect(conn, FakeLinkedIn(pages={0: [card_html("1", company="Groww")]}))
    fill(conn, FakeLinkedIn())
    row = conn.execute("SELECT company_type, stage FROM jobs").fetchone()
    assert tuple(row) == ("b2c", "late")


def test_remote_search_marks_the_job_remote(conn):
    remote = [{**SEARCH[0], "remote": True}]
    collect(conn, FakeLinkedIn(pages={0: [card_html("1", location="India")]}),
            searches=remote)
    fill(conn, FakeLinkedIn())
    assert conn.execute("SELECT remote FROM jobs").fetchone()[0] == 1


# --- being blocked, and closing --------------------------------------------

def test_a_block_keeps_what_was_collected(conn):
    pages = {0: [card_html("1")], 10: [card_html("2")], 20: [card_html("3")]}
    with pytest.raises(linkedin.Blocked):
        collect(conn, FakeLinkedIn(pages=pages, block_after=2))
    assert queued(conn) == {"1", "2"}


def test_closing_is_by_age_never_by_absence(conn):
    for jid, posted in (("old", "2020-01-01"), ("new", "2999-01-01")):
        db.upsert(conn, [adapters._row(
            source="linkedin", source_id=jid, company="X", title="PM",
            location="India", description="x", url="u", posted_at=posted)])
    assert linkedin.close_old(conn) == 1
    open_ids = {r[0] for r in conn.execute(
        "SELECT source_id FROM jobs WHERE closed_at IS NULL")}
    assert open_ids == {"new"}


def test_age_closing_never_touches_board_jobs(conn):
    db.upsert(conn, [adapters._row(
        source="lever", source_id="1", company="X", title="PM",
        location="India", description="x", url="u", posted_at="2020-01-01")])
    assert linkedin.close_old(conn) == 0


# --- search sees them ------------------------------------------------------

def test_untagged_company_passes_a_b2c_filter_tagged_b2b_does_not(conn):
    # Untagged is unknown, not a mismatch. It was LinkedIn's case; now it is
    # every board the dataset added, none of which carry tags. (LinkedIn rows
    # themselves are no longer searched.)
    recent = (__import__("datetime").date.today()
              - __import__("datetime").timedelta(days=3)).isoformat()
    for sid, ctype in (("li", None), ("b2b", "b2b"), ("b2c", "b2c")):
        row = adapters._row(source="greenhouse", source_id=sid, company=sid,
                            title="Growth Manager", location="Bangalore",
                            description="x", url="u", posted_at=recent,
                            meta={"type": ctype})
        db.upsert(conn, [row])
    rows, _ = query.search({"company_type": "b2c"}, conn)
    assert {r["source_id"] for r in rows} == {"li", "b2c"}


# --- a dropped connection is retried once; a refusal is not -----------------

def test_dropped_connection_is_retried_once_then_stops():
    import asyncio

    import httpx

    from engine import linkedin

    def client_for(outcomes):
        calls = []

        def handler(request):
            calls.append(request.url)
            out = outcomes[min(len(calls), len(outcomes)) - 1]
            if isinstance(out, Exception):
                raise out
            return httpx.Response(out, text="ok")

        return httpx.AsyncClient(transport=httpx.MockTransport(handler)), calls

    async def run(outcomes):
        client, calls = client_for(outcomes)
        async with client:
            try:
                return await linkedin._get(client, "https://x/y", pause=0), calls
            except linkedin.Blocked as exc:
                return f"blocked: {exc}", calls

    drop = httpx.ReadError("reset")
    asyncio_sleep = asyncio.sleep

    async def no_wait(_s):
        await asyncio_sleep(0)

    import engine.adapters as adapters
    adapters.asyncio.sleep, saved = no_wait, adapters.asyncio.sleep
    try:
        assert asyncio.run(run([drop, 200]))[0] == "ok"            # one drop: fine
        out, calls = asyncio.run(run([drop, drop, 200]))
        assert out.startswith("blocked") and len(calls) == 2       # two in a row: stop
        out, calls = asyncio.run(run([429]))
        assert out == "blocked: http 429" and len(calls) == 1      # refusal: no retry
    finally:
        adapters.asyncio.sleep = saved
