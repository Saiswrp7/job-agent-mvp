"""Adzuna: every role in India, from one free API.

LinkedIn logged out covers every employer but gives one description per ~2 s
request and stops us with a 429 long before a day's jobs are in. Adzuna returns
50 jobs a call, newest first, for any role, and the free tier is 2,500 calls a
month (about 25 a minute, 250 a day). That is the only free way to a table of
"every job in India from the last 30 days".

**What it does not give: the full description.** Adzuna returns a snippet of a
few hundred characters ("we currently only provide a snippet", their docs) and
a redirect link. Enough to search and rank on; thin for tailoring. So Adzuna
comes last and never wins a duplicate: a job a board or LinkedIn also has is
skipped, and an Adzuna row is closed once a fuller copy of it arrives.

Keys: `ADZUNA_APP_ID` and `ADZUNA_APP_KEY` (free, developer.adzuna.com). With
none set this step is skipped and says so; nothing else changes.

Their terms ask that results are credited "Jobs by Adzuna". `get_job` in
chat.py does that for these rows.

Rules copied from the LinkedIn step, for the same reasons:
- one request at a time, paced; anything but a 200 stops the run and closes
  nothing
- jobs close by age (30 days), never because a page stopped showing them

Paging is oldest first, from where the last run got to. India gets about
4,000 new Adzuna jobs a day (97k in 30 days, measured 2026-09-28), which is
the whole free tier's worth of calls, so a wasted page is a real cost.
Newest first, every job that lands mid-run pushes the rest down a slot and the
next page repeats it; about half of each page was repeats. Oldest first, new
jobs land at the end and pages stay put. The window is "since the newest job we
have from this query", in whole days, and a short page search skips the part
of that first day we already read.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import httpx

from .adapters import CITY_ALIASES, _row, get_once_more, norm_city

HERE = Path(__file__).resolve().parent
SEARCHES = HERE / "adzuna_searches.json"
API = "https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"
TIMEOUT = 25.0
#: About 25 calls a minute is the free limit; 2.6 s keeps under it.
PAUSE = 2.6
OPEN_DAYS = 30
#: How far back a query starts when we have nothing from it yet. Oldest first
#: from 30 days would spend the budget on jobs about to close by age.
FIRST_RUN_DAYS = 3
#: Adzuna serves 100 pages of a query and no more: page 256 of a 25,000-job
#: query came back identical to page 100 (checked 2026-09-28). So a query
#: must stay under 5,000 results per window, which is why the config splits
#: India by category, and why a walk never asks past this page.
MAX_PAGE = 100


class Blocked(RuntimeError):
    """Anything but a 200. The run stops and keeps what it had."""


class OutOfCalls(Exception):
    """This run's call budget is spent. Not an error: the next run resumes."""


def keys() -> tuple[str, str] | None:
    # `.env` is read when `llm` is imported, and `sync --no-labels` never
    # imports it, so the keys would sit in `.env` unseen.
    import llm  # noqa: F401
    app_id, app_key = os.environ.get("ADZUNA_APP_ID"), os.environ.get("ADZUNA_APP_KEY")
    return (app_id, app_key) if app_id and app_key else None


def load_config(path: Path = SEARCHES) -> dict:
    return json.loads(path.read_text())


def _salary(j: dict) -> str | None:
    """Only a salary the employer gave. Adzuna fills in an estimate when there
    is none (`salary_is_predicted`), and a guess shown as the pay is a number
    the person may quote in an interview."""
    if str(j.get("salary_is_predicted")) == "1":
        return None
    lo, hi = j.get("salary_min"), j.get("salary_max")
    if not lo and not hi:
        return None
    if lo and hi and lo != hi:
        return f"₹{lo:,.0f}–{hi:,.0f}"
    return f"₹{(lo or hi):,.0f}"


def city_of(location: dict) -> str | None:
    """The city from `location.area`, most specific first.

    `area` runs country, state, city, locality: ["India", "Karnataka",
    "Bangalore", "Kenchanahalli"]. The display name is only the last two, so
    "Noida, Ghaziabad" and "Kenchanahalli, Bangalore" read the wrong way round
    from it. A known city anywhere in the list wins; else the third entry;
    ["India"] alone is no city at all (2,292 rows said city = "India")."""
    area = [a for a in (location.get("area") or []) if isinstance(a, str)]
    for part in reversed(area[1:]):
        if part.strip().lower() in CITY_ALIASES:
            return CITY_ALIASES[part.strip().lower()]
    if len(area) >= 3:
        return area[2]
    if area:
        return None
    return norm_city(location.get("display_name"))[0]


def city_in_text(*texts: str | None) -> str | None:
    """The one city a title or snippet names ("Sales Executive - Pune"), or
    None when it names none or several. Only for rows Adzuna placed in
    "India" and nowhere more precise."""
    found = set()
    for text in texts:
        low = (text or "").lower()
        for alias, canonical in CITY_ALIASES.items():
            if re.search(rf"\b{re.escape(alias)}\b", low):
                found.add(canonical)
    return found.pop() if len(found) == 1 else None


def backfill_city(conn: sqlite3.Connection) -> dict:
    """Rows stored before `location.area` was read have city = "India". Only
    the display name was kept, and for these it is "India" too, so the city
    comes from the title or snippet when they name exactly one; otherwise it
    is emptied, since "India" is not a city. No API call."""
    rows = conn.execute(
        "SELECT source_id, title, description FROM jobs "
        "WHERE source = 'adzuna' AND city = 'India'").fetchall()
    found = 0
    for sid, title, desc in rows:
        city = city_in_text(title, desc)
        found += city is not None
        conn.execute("UPDATE jobs SET city = ? WHERE source = 'adzuna' "
                     "AND source_id = ? AND city = 'India'", (city, sid))
    conn.commit()
    return {"rows": len(rows), "city_found": found}


def to_row(j: dict) -> dict:
    location = j.get("location") or {}
    created = (j.get("created") or "")[:19] or None
    row = _row(
        source="adzuna", source_id=j["id"],
        company=(j.get("company") or {}).get("display_name") or "",
        title=j.get("title") or "", location=location.get("display_name"),
        description=(j.get("description") or "").strip(),
        url=j.get("redirect_url"), posted_at=created, updated_at=created,
        department=(j.get("category") or {}).get("label"),
        employment_type=j.get("contract_time"), salary=_salary(j),
    )
    row["city"] = city_of(location) or city_in_text(row["title"])
    return row


def _known(conn: sqlite3.Connection) -> tuple[set[str], set[tuple[str, str]]]:
    """Our own ids, and (company, title) pairs any fuller source already has."""
    ids = {r[0] for r in conn.execute(
        "SELECT source_id FROM jobs WHERE source = 'adzuna'")}
    pairs = {(r[0].lower(), r[1].lower()) for r in conn.execute(
        "SELECT company, title FROM jobs "
        "WHERE source != 'adzuna' AND closed_at IS NULL")}
    return ids, pairs


def reached(conn: sqlite3.Connection, query: dict) -> str | None:
    """Where this query's last walk got to: the newest job we hold from it.

    A category query is read from the category label every row stores, so
    each query resumes from its own point, never from another query's."""
    sql = "SELECT MAX(posted_at) FROM jobs WHERE source = 'adzuna'"
    params: list = []
    if query.get("category"):
        sql += " AND department = ?"
        params.append(query.get("label") or "")
    row = conn.execute(sql, params).fetchone()
    return row[0] if row and row[0] else None


def ran_recently(conn: sqlite3.Connection, hours: int | None) -> bool:
    """Did an Adzuna run add anything in the last `hours`? A run that added
    nothing leaves no mark, so the next one tries again: a few calls, not a
    day's worth."""
    if not hours:
        return False
    row = conn.execute(
        "SELECT MAX(first_seen) > datetime('now', ?) FROM jobs "
        "WHERE source = 'adzuna'", (f"-{int(hours)} hours",)).fetchone()
    return bool(row and row[0])


def window_days(since: str | None, cap: int, now: datetime | None = None) -> int:
    """Whole days back to reach `since`, plus one: the API takes only whole
    days, so the window always starts a little before where we stopped."""
    if not since:
        return min(FIRST_RUN_DAYS, cap)
    now = now or datetime.now(UTC)
    then = datetime.fromisoformat(since[:19]).replace(tzinfo=UTC)
    return max(1, min(cap, math.ceil((now - then).total_seconds() / 86400) + 1))


def _newer(jobs: list[dict], since: str | None) -> bool:
    """Does this page reach past `since`? True for an empty or last page."""
    if not since or not jobs:
        return True
    return (jobs[-1].get("created") or "")[:19] > since[:19]

async def _page(client: httpx.AsyncClient, cfg: dict, query: dict, page: int,
                creds: tuple[str, str], pause: float, days: int) -> dict:
    await asyncio.sleep(pause)
    params = {"app_id": creds[0], "app_key": creds[1],
              "results_per_page": cfg.get("results_per_page", 50),
              "max_days_old": days, "sort_by": "date",
              "sort_direction": "up", "content-type": "application/json",
              **{k: v for k, v in query.items() if v and k != "label"}}
    url = API.format(country=cfg.get("country", "in"), page=page)
    try:
        r = await get_once_more(client, url, params=params)
    except (httpx.HTTPError, asyncio.TimeoutError) as exc:
        raise Blocked(type(exc).__name__) from exc
    if r.status_code != 200:
        raise Blocked(f"http {r.status_code}")
    return r.json()


def close_old(conn: sqlite3.Connection, days: int = OPEN_DAYS) -> int:
    cur = conn.execute(
        "UPDATE jobs SET closed_at = CURRENT_TIMESTAMP "
        "WHERE source = 'adzuna' AND closed_at IS NULL "
        "AND posted_at < date('now', ?)", (f"-{int(days)} days",))
    conn.commit()
    return cur.rowcount


def close_superseded(conn: sqlite3.Connection) -> int:
    """An Adzuna row whose job has since arrived from a board or LinkedIn: that
    copy has the full description and often the real apply link, so it wins."""
    cur = conn.execute(
        "UPDATE jobs SET closed_at = CURRENT_TIMESTAMP "
        "WHERE source = 'adzuna' AND closed_at IS NULL AND EXISTS ("
        "  SELECT 1 FROM jobs o WHERE o.source != 'adzuna' "
        "  AND o.closed_at IS NULL "
        "  AND lower(o.company) = lower(jobs.company) "
        "  AND lower(o.title) = lower(jobs.title))")
    conn.commit()
    return cur.rowcount


async def run(conn: sqlite3.Connection, cfg: dict | None = None, *,
              pause: float = PAUSE, verbose: bool = True, calls: int | None = None,
              transport: httpx.AsyncBaseTransport | None = None) -> dict:
    """Walk each query oldest first from where it last got to, until caught
    up or out of this run's calls, then close by age and by duplicate.

    `calls` caps this run below `pages_per_run` (the free tier is monthly, and
    one run should not be able to spend it)."""
    creds = keys()
    if creds is None:
        return {"skipped": "no ADZUNA_APP_ID / ADZUNA_APP_KEY set"}
    cfg = cfg or load_config()
    if calls is None and ran_recently(conn, cfg.get("every_hours")):
        # Sync is hourly; this budget is monthly. A caught-up run still costs
        # a call or two per category, so hourly would spend the month in days.
        return {"skipped": f"ran in the last {cfg['every_hours']} hours "
                           f"(--adzuna-calls N runs it anyway)"}
    budget = int(cfg.get("pages_per_run", 100))
    if calls is not None:
        budget = min(budget, int(calls))
    per_page = int(cfg.get("results_per_page", 50))
    cap = int(cfg.get("max_days_old", OPEN_DAYS))
    ids, pairs = _known(conn)
    report = {"calls": 0, "added": 0, "duplicates": 0, "repeats": 0,
              "skipped_pages": 0, "narrowed": 0, "stuck": [], "blocked": None}

    def keep(jobs: list[dict]) -> int:
        rows = []
        for j in jobs:
            row = to_row(j)
            pair = (row["company"].lower(), row["title"].lower())
            if pair in pairs:
                report["duplicates"] += 1
            elif row["source_id"] in ids:
                report["repeats"] += 1
            elif row["company"]:
                rows.append(row)
                ids.add(row["source_id"])
        if rows:
            from . import db
            db.upsert(conn, rows)
            report["added"] += len(rows)
        return len(rows)

    async with httpx.AsyncClient(timeout=TIMEOUT, transport=transport) as client:

        seen_pages: dict[int, list[dict]] = {}

        async def fetch(query, page, days):
            if page in seen_pages:              # read during the page search
                return seen_pages.pop(page), None
            if report["calls"] >= budget:       # a hard cap, even mid-search
                raise OutOfCalls
            data = await _page(client, cfg, query, page, creds, pause, days)
            report["calls"] += 1
            return data.get("results") or [], data.get("count")

        async def locate(query, since, days):
            """(first page to walk, its jobs, last page), or None when the
            place we stopped lies past the 100th page of this window.

            The window opens up to a day before where we stopped. Rather
            than read that day again, find the first page that reaches past
            it: a few calls instead of dozens."""
            seen_pages.clear()
            jobs, total = await fetch(query, 1, days)
            # No count means no page search: walk until a short page.
            last = min(math.ceil(int(total) / per_page), MAX_PAGE) if total else None
            if _newer(jobs, since) or not last or last == 1:
                return 1, jobs, last
            keep(jobs)
            tail, _ = await fetch(query, last, days)
            if not _newer(tail, since):
                if last < MAX_PAGE:          # all of it is older: nothing new
                    return last, tail, last
                return None
            seen_pages[last] = tail
            lo, hi = 2, last
            while lo < hi and report["calls"] < budget:
                mid = (lo + hi) // 2
                probe, _ = await fetch(query, mid, days)
                seen_pages[mid] = probe
                if _newer(probe, since):
                    hi = mid
                else:
                    lo = mid + 1
            # Probed pages the walk will not reach are kept now; the rest
            # are kept when the walk gets to them, once.
            for p in [p for p in seen_pages if p < lo]:
                keep(seen_pages.pop(p))
            report["skipped_pages"] += lo - 2
            first, _ = await fetch(query, lo, days)
            return lo, first, last

        try:
            for query in cfg.get("queries") or [{}]:
                if report["calls"] >= budget:
                    break
                since = reached(conn, query)
                days = window_days(since, cap)
                name = query.get("label") or query.get("what") or "all roles"
                found = await locate(query, since, days)
                while found is None and days > 1:
                    # More than 100 pages sit between the window's start and
                    # where we stopped: unreachable. A day narrower gives up
                    # jobs this query can never serve anyway.
                    days -= 1
                    report["narrowed"] += 1
                    found = await locate(query, since, days)
                if found is None:
                    report["stuck"].append(name)
                    continue
                page, jobs, last = found
                while True:
                    added = keep(jobs)
                    if verbose and jobs:
                        print(f"  ok   adzuna {name} p{page}/{last or '?'} "
                              f"({days}d) +{added}")
                    if (last and page >= last) or (jobs and len(jobs) < per_page) \
                            or report["calls"] >= budget:
                        break
                    page += 1
                    jobs, _ = await fetch(query, page, days)
                    if not jobs:
                        break
        except OutOfCalls:
            conn.commit()
        except Blocked as exc:
            report["blocked"] = str(exc)
            conn.commit()

    report["closed"] = close_old(conn)
    report["superseded"] = close_superseded(conn)
    report["city_backfill"] = backfill_city(conn)
    return report
