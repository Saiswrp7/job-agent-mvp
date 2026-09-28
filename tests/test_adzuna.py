"""Adzuna: every role, newest first, and never over a fuller copy.

A fake transport stands in for the API: no network, no keys, no real table.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import adzuna, db     # noqa: E402

CFG = {"country": "in", "max_days_old": 30, "results_per_page": 2,
       "pages_per_run": 5, "queries": [{"what": ""}]}


def job(i, created="2026-09-24T09:00:00Z", company="Acme", title=None, **kw):
    return {"id": str(i), "title": title or f"Role {i}", "created": created,
            "company": {"display_name": company},
            "location": {"display_name": "Bengaluru, Karnataka"},
            "description": "A short snippet.", "redirect_url": f"https://adz/{i}",
            "category": {"label": "IT Jobs"}, **kw}


def transport(pages, calls):
    def handle(request):
        calls.append(request)
        page = int(request.url.path.rsplit("/", 1)[-1])
        if isinstance(pages, int):
            return httpx.Response(pages)
        return httpx.Response(200, json={"results": pages.get(page, [])})
    return httpx.MockTransport(handle)


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("ADZUNA_APP_ID", "id")
    monkeypatch.setenv("ADZUNA_APP_KEY", "key")
    c = db.connect(tmp_path / "t.db")
    yield c
    c.close()


def run(conn, pages, calls=None, cfg=CFG):
    calls = [] if calls is None else calls
    return asyncio.run(adzuna.run(conn, dict(cfg), pause=0, verbose=False,
                                  transport=transport(pages, calls)))


def open_rows(conn, source="adzuna"):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM jobs WHERE source=? AND closed_at IS NULL", (source,))]


def test_pages_oldest_first_until_a_short_page(conn):
    """Oldest first: a job that lands mid-run goes to the end instead of
    pushing every page down a slot and repeating half of the next one."""
    calls = []
    r = run(conn, {1: [job(1), job(2)], 2: [job(3)]}, calls)
    assert r["added"] == 3 and r["calls"] == 2
    q = calls[0].url.params
    assert q["sort_by"] == "date" and q["sort_direction"] == "up"
    assert q["max_days_old"] == str(adzuna.FIRST_RUN_DAYS)   # nothing here yet
    assert q["app_id"] == "id" and "what" not in q         # every role


def test_no_keys_skips_and_calls_nothing(conn, monkeypatch):
    monkeypatch.delenv("ADZUNA_APP_KEY")
    calls = []
    assert "skipped" in run(conn, {1: [job(1)]}, calls) and calls == []


def test_the_page_budget_is_a_hard_stop(conn):
    full = {p: [job(p * 10), job(p * 10 + 1)] for p in range(1, 20)}
    assert run(conn, full, cfg={**CFG, "pages_per_run": 3})["calls"] == 3


def test_a_refusal_stops_and_closes_nothing(conn):
    run(conn, {1: [job(1)]})
    r = run(conn, 429)
    assert r["blocked"] == "http 429" and len(open_rows(conn)) == 1


def test_a_fuller_copy_wins(conn):
    """A board or LinkedIn copy has the full description; Adzuna's is a snippet."""
    db.upsert(conn, [adzuna.to_row(job(9, company="Paytm", title="PM"))
                     | {"source": "linkedin", "source_id": "li9"}])
    r = run(conn, {1: [job(1, company="Paytm", title="PM")]})
    assert r["duplicates"] == 1 and open_rows(conn) == []


def test_an_adzuna_row_is_closed_when_a_fuller_copy_arrives(conn):
    run(conn, {1: [job(1, company="CRED", title="Growth Manager")]})
    db.upsert(conn, [adzuna.to_row(job(2, company="CRED", title="Growth Manager"))
                     | {"source": "lever", "source_id": "lv2"}])
    assert adzuna.close_superseded(conn) == 1 and open_rows(conn) == []


def test_the_window_opens_just_before_where_we_stopped():
    from datetime import datetime, UTC
    now = datetime(2026, 9, 28, 11, 0, tzinfo=UTC)
    assert adzuna.window_days("2026-09-25T03:26:24", 30, now) == 5
    assert adzuna.window_days("2026-09-28T10:00:00", 30, now) == 2
    assert adzuna.window_days("2026-01-01T00:00:00", 30, now) == 30   # capped
    assert adzuna.window_days(None, 30, now) == adzuna.FIRST_RUN_DAYS


def paged(n_pages, newer_from, per=2):
    """n pages of `per` jobs, oldest first; pages before `newer_from` are all
    older than the row already stored, the rest newer."""
    pages = {}
    for p in range(1, n_pages + 1):
        day = "2026-09-20" if p < newer_from else "2026-09-26"
        pages[p] = [job(p * 100 + i, created=f"{day}T{p:02d}:00:0{i}Z")
                    for i in range(per)]
    return pages


def test_the_page_search_skips_the_day_already_read(conn):
    """The window starts up to a day early. Reading that day again cost dozens
    of pages; a page search finds where new jobs start in a few calls."""
    db.upsert(conn, [adzuna.to_row(job(1, created="2026-09-25T00:00:00Z"))])
    pages = paged(20, newer_from=15)
    calls = []
    r = asyncio.run(adzuna.run(conn, {**CFG, "pages_per_run": 50}, pause=0,
                               verbose=False, transport=counted(pages, calls, 40)))
    fetched = sorted(int(c.url.path.rsplit("/", 1)[-1]) for c in calls)
    assert set(range(15, 21)) <= set(fetched)          # every new page read
    assert len(calls) < 12 and len(set(fetched)) == len(fetched)   # each once
    assert r["added"] >= 12


def test_a_run_never_spends_more_than_its_calls(conn):
    calls = []
    r = asyncio.run(adzuna.run(conn, {**CFG, "pages_per_run": 50}, pause=0,
                               verbose=False, calls=2,
                               transport=counted(paged(10, 1), calls, 20)))
    assert r["calls"] == 2 and len(calls) == 2


def counted(pages, calls, count):
    def handle(request):
        calls.append(request)
        page = int(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(200, json={"results": pages.get(page, []),
                                         "count": count})
    return httpx.MockTransport(handle)


def test_city_comes_from_the_area_list_not_the_display_name():
    assert adzuna.city_of({"area": ["India", "Uttar Pradesh", "Ghaziabad", "Noida"],
                           "display_name": "Noida, Ghaziabad"}) == "Noida"
    assert adzuna.city_of({"area": ["India", "Karnataka", "Bangalore",
                                    "Kenchanahalli"]}) == "Bangalore"
    assert adzuna.city_of({"area": ["India"], "display_name": "India"}) is None
    row = adzuna.to_row(job(1, location={"area": ["India"], "display_name": "India"},
                            title="Sales Executive - Pune"))
    assert row["city"] == "Pune"


def test_backfill_sets_a_named_city_and_empties_india(conn):
    rows = [adzuna.to_row(job(1, title="Sales Executive - Pune")),
            adzuna.to_row(job(2, title="Accountant")),
            adzuna.to_row(job(3, title="Ops lead, Mumbai or Pune"))]
    for r in rows:
        r["city"] = "India"
    db.upsert(conn, rows)
    assert adzuna.backfill_city(conn) == {"rows": 3, "city_found": 1}
    got = dict(conn.execute("SELECT source_id, city FROM jobs").fetchall())
    assert got == {"1": "Pune", "2": None, "3": None}     # two cities: unknown


def test_a_predicted_salary_is_not_shown_as_the_pay():
    assert adzuna.to_row(job(1, salary_min=900000, salary_max=1200000,
                             salary_is_predicted="1"))["salary"] is None
    assert adzuna.to_row(job(1, salary_min=900000, salary_max=1200000,
                             salary_is_predicted="0"))["salary"] == "₹900,000–1,200,000"


def test_old_jobs_close_by_age(conn):
    run(conn, {1: [job(1, created="2026-01-01T00:00:00Z")]})
    assert open_rows(conn) == []


def test_a_walk_never_asks_past_page_100_and_narrows_an_unreachable_window(conn):
    """Adzuna serves 100 pages of a query; page 256 is page 100 again. When
    where we stopped lies past that, a day narrower is the only way forward."""
    db.upsert(conn, [adzuna.to_row(job(1, created="2026-09-25T00:00:00Z"))])
    calls = []

    def handle(request):
        calls.append(request)
        days = int(request.url.params["max_days_old"])
        page = int(request.url.path.rsplit("/", 1)[-1])
        if days >= 3:       # huge window: 100+ pages all older than the stop
            old = [job(page * 10 + i, created="2026-09-24T00:00:00Z") for i in range(2)]
            return httpx.Response(200, json={"results": old, "count": 100000})
        new = [job(9000 + page, created="2026-09-27T00:00:00Z")]
        return httpx.Response(200, json={"results": new, "count": 1})

    r = asyncio.run(adzuna.run(conn, {**CFG, "pages_per_run": 50}, pause=0,
                               verbose=False, transport=httpx.MockTransport(handle)))
    pages = [int(c.url.path.rsplit("/", 1)[-1]) for c in calls]
    assert max(pages) <= adzuna.MAX_PAGE
    assert r["narrowed"] >= 1 and r["stuck"] == []
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE source_id = '9001'").fetchone()[0]


def test_the_call_cap_holds_even_inside_the_page_search(conn):
    db.upsert(conn, [adzuna.to_row(job(1, created="2026-09-25T00:00:00Z"))])
    calls = []
    r = asyncio.run(adzuna.run(conn, {**CFG, "pages_per_run": 50}, pause=0,
                               verbose=False, calls=3,
                               transport=counted(paged(20, newer_from=15), calls, 40)))
    assert len(calls) == 3 and r["calls"] == 3 and r["blocked"] is None


def test_each_category_resumes_from_its_own_point(conn):
    db.upsert(conn, [adzuna.to_row(job(1, created="2026-09-25T00:00:00Z",
                                       category={"label": "IT Jobs"})),
                     adzuna.to_row(job(2, created="2026-09-20T00:00:00Z",
                                       category={"label": "Sales Jobs"}))])
    it = {"category": "it-jobs", "label": "IT Jobs"}
    sales = {"category": "sales-jobs", "label": "Sales Jobs"}
    assert adzuna.reached(conn, it) == "2026-09-25T00:00:00"
    assert adzuna.reached(conn, sales) == "2026-09-20T00:00:00"


def test_hourly_sync_runs_adzuna_at_most_once_a_day(conn):
    cfg = {**CFG, "every_hours": 20}
    run(conn, {1: [job(1)]}, cfg=cfg)                     # adds a row now
    calls = []
    assert "skipped" in run(conn, {1: [job(2)]}, calls, cfg=cfg) and calls == []
    r = asyncio.run(adzuna.run(conn, dict(cfg), pause=0, verbose=False, calls=1,
                               transport=transport({1: [job(3)]}, calls)))
    assert r["calls"] == 1                                # an explicit cap runs it
