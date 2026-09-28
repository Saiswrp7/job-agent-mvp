"""LinkedIn's logged-out job search. The widest free supply there is.

Greenhouse, Lever and Ashby only cover companies that happen to use them — 19
of the 137 we probed. LinkedIn covers nearly every employer in India, and its
public job pages need no login and no key. One search for "product manager,
India" returned 496 jobs from 295 companies when this was built.

Two steps, because they cost very different amounts:

1. **Collect links.** Each saved search returns cards of 10 — id, title,
   company, city, date. Cheap. New ids go into `linkedin_queue`.
2. **Fill in details.** One request per job for the description, which the
   ranker cannot work without. Slow, so it is capped per run and drains the
   queue over several runs. A job only reaches `jobs` once it has a
   description, so search never sees a half-filled row.

Rules this file keeps:

- **Stop the moment LinkedIn pushes back.** Anything but a 200 ends the run.
  What was collected stays; nothing is closed because of it.
- **One request at a time, with a pause.** This is an unofficial endpoint.
  Being slow is what keeps it working.
- **Closing is by age, not absence.** A search is not a complete list of a
  company's jobs, so "not in today's results" means nothing. A LinkedIn job
  closes when it is older than `OPEN_DAYS`.

Known gap: logged out, LinkedIn hides where "Apply" leads, so `apply_url` is the
LinkedIn page, not the company's form.
"""

from __future__ import annotations

import asyncio
import html
import json
import random
import re
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import httpx

from .adapters import _row, get_once_more, strip_html

SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
DETAIL_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{id}"
SEARCHES = Path(__file__).resolve().parent / "linkedin_searches.json"
BOARDS = Path(__file__).resolve().parent / "boards.json"

#: LinkedIn returns nothing past start=990 for one search. "product manager"
#: in Bangalore, 30 days, hit that ceiling — narrower searches reach the rest.
PAGE_SIZE = 10
MAX_START = 990
#: Seconds between requests, plus up to half again at random.
PAUSE = 1.0
#: Descriptions fetched per run. The first run leaves most of the queue for the
#: next ones; after that, a run only sees what was posted since the last.
DETAIL_CAP = 300
OPEN_DAYS = 30
TIMEOUT = 20.0
#: A browser's user agent. LinkedIn serves these pages to logged-out browsers;
#: an unknown agent is more likely to be turned away.
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


class Blocked(Exception):
    """LinkedIn answered with something other than a page. Stop the run."""


# --- parsing ---------------------------------------------------------------

_CARD_SPLIT = re.compile(r"<li>")
_ID = re.compile(r'data-entity-urn="urn:li:jobPosting:(\d+)"')
_TITLE = re.compile(r'base-search-card__title">\s*(.*?)\s*</h3>', re.S)
_COMPANY = re.compile(r'base-search-card__subtitle">\s*(?:<a[^>]*>)?\s*(.*?)\s*<', re.S)
_LOCATION = re.compile(r'job-search-card__location">\s*(.*?)\s*<', re.S)
_DATE = re.compile(r'<time[^>]*datetime="([\d-]+)"')
_DESC = re.compile(r'show-more-less-html__markup[^>]*>(.*?)</div>', re.S)
_CRITERIA = re.compile(
    r'description__job-criteria-subheader">\s*(.*?)\s*</h3>\s*'
    r'<span[^>]*description__job-criteria-text[^>]*>\s*(.*?)\s*</span>', re.S)


def _text(m: re.Match | None) -> str | None:
    if not m:
        return None
    return html.unescape(re.sub(r"\s+", " ", m.group(1))).strip() or None


def parse_cards(page: str) -> list[dict]:
    """One search page -> the cards on it. A card missing its id is skipped."""
    cards = []
    for chunk in _CARD_SPLIT.split(page):
        jid = _ID.search(chunk)
        if not jid:
            continue
        cards.append({
            "id": jid.group(1),
            "title": _text(_TITLE.search(chunk)) or "",
            "company": _text(_COMPANY.search(chunk)) or "",
            "location": _text(_LOCATION.search(chunk)),
            "posted_at": (_DATE.search(chunk) or [None, None])[1],
        })
    return cards


def parse_detail(page: str) -> dict:
    """One job page -> description and LinkedIn's own labels.

    `closed` is LinkedIn saying so on the page. Such a job is dropped rather
    than stored, since it can never be applied to.
    """
    body = _DESC.search(page)
    criteria = {k.strip(): html.unescape(v).strip()
                for k, v in _CRITERIA.findall(page)}
    return {
        "description": strip_html(body.group(1)) if body else "",
        "seniority": criteria.get("Seniority level"),
        "employment_type": criteria.get("Employment type"),
        "function": criteria.get("Job function"),
        "industry": criteria.get("Industries"),
        "closed": "No longer accepting applications" in page,
    }


def job_url(jid: str) -> str:
    return f"https://www.linkedin.com/jobs/view/{jid}"


# --- the saved searches ----------------------------------------------------

#: A search nobody has asked for in this long stops being fetched daily.
ASKED_WITHIN_DAYS = 60


def load_searches(path: Path = SEARCHES,
                  conn: sqlite3.Connection | None = None) -> list[dict]:
    """Every role in every place from the JSON, plus every search a person has
    asked for recently. The JSON is the floor; what people ask is how supply
    grows past it."""
    spec = json.loads(path.read_text())
    days = int(spec.get("posted_within_days", OPEN_DAYS))
    out = [{"keywords": role, "location": place["location"],
            "remote": bool(place.get("remote")), "days": days}
           for role in spec["roles"] for place in spec["places"]]
    if conn is not None:
        have = {search_key(x) for x in out}
        for r in conn.execute(
                "SELECT keywords, location, remote FROM saved_searches "
                "WHERE last_asked > datetime('now', ?) ORDER BY times_asked DESC",
                (f"-{ASKED_WITHIN_DAYS} days",)):
            s = {"keywords": r[0], "location": r[1], "remote": bool(r[2]),
                 "days": days}
            if search_key(s) not in have:
                out.append(s)
    return out


def remember(conn: sqlite3.Connection, keywords: str, location: str,
             remote: bool) -> None:
    """Record that someone asked for this, so the daily fetch covers it."""
    conn.execute(
        "INSERT INTO saved_searches (keywords, location, remote) VALUES (?, ?, ?) "
        "ON CONFLICT(keywords, location, remote) DO UPDATE SET "
        "times_asked = times_asked + 1, last_asked = CURRENT_TIMESTAMP",
        (keywords.strip().lower(), location.strip(), int(bool(remote))))
    conn.commit()


def search_params(s: dict, start: int, newest_first: bool = True) -> dict:
    p = {"keywords": s["keywords"], "location": s["location"],
         "f_TPR": f"r{s['days'] * 86400}",   # posted within, in seconds
         "start": start}
    if newest_first:
        p["sortBy"] = "DD"                    # newest first — see collect()
    if s["remote"]:
        p["f_WT"] = "2"
    return p


# --- company tags ----------------------------------------------------------

def _tags_by_company(path: Path = BOARDS) -> dict[str, dict]:
    """The hand-typed B2C/stage/size tags, for companies we already know.

    A LinkedIn job at Paytm should be as filterable as a Lever one. Everyone
    else gets none, and a missing tag is neutral at query time.
    """
    try:
        boards = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return {b["company"].lower(): b for b in boards}


def to_row(card: dict, detail: dict, tags: dict[str, dict]) -> dict:
    known = tags.get(card["company"].lower(), {})
    row = _row(
        source="linkedin", source_id=card["id"], company=card["company"],
        title=card["title"], location=card.get("location"),
        description=detail["description"], url=job_url(card["id"]),
        posted_at=card.get("posted_at"), updated_at=card.get("posted_at"),
        department=detail.get("function"),
        employment_type=detail.get("employment_type"),
        meta={"type": known.get("type"), "stage": known.get("stage"),
              "size": known.get("size"),
              "industry": known.get("industry") or detail.get("industry")},
    )
    if card.get("remote"):
        row["remote"] = 1
    return row


# --- network ---------------------------------------------------------------

async def _get(client: httpx.AsyncClient, url: str, params: dict | None = None,
               pause: float = PAUSE) -> str:
    await asyncio.sleep(pause * (1 + random.random() / 2))
    try:
        r = await get_once_more(client, url, params=params)
    except (httpx.HTTPError, asyncio.TimeoutError) as exc:
        raise Blocked(type(exc).__name__) from exc
    if r.status_code != 200:
        # 429 is "slow down", 999 is LinkedIn's own refusal, a redirect is the
        # login wall. All three mean the same thing here: stop.
        raise Blocked(f"http {r.status_code}")
    return r.text


def _known(conn: sqlite3.Connection) -> tuple[set[str], set[tuple[str, str]]]:
    """Ids we have in any form, and (company, title) pairs a board already has.

    The second set is the duplicate check: Paytm's Lever board and Paytm's
    LinkedIn posts are the same jobs, and the board copy has the real apply
    link, so it wins.
    """
    ids = {r[0] for r in conn.execute(
        "SELECT source_id FROM jobs WHERE source = 'linkedin' "
        "UNION SELECT source_id FROM linkedin_queue")}
    # Adzuna is left out: its copy is a snippet, so LinkedIn's full description
    # should still come in, and the Adzuna row is closed as superseded.
    pairs = {(r[0].lower(), r[1].lower()) for r in conn.execute(
        "SELECT company, title FROM jobs "
        "WHERE source NOT IN ('linkedin', 'adzuna') AND closed_at IS NULL")}
    return ids, pairs


def search_key(s: dict) -> str:
    return f"{s['keywords']} @ {s['location']}" + (" (remote)" if s["remote"] else "")


def _all_older(cards: list[dict], since: str | None) -> bool:
    """True when every card was posted before this search's last run.

    A day of slack, because `posted_at` is a date and the last run was part
    way through one. A card with no date counts as new, so it never stops us.
    """
    if not since:
        return False
    cutoff = (date.fromisoformat(since) - timedelta(days=1)).isoformat()
    return all(c.get("posted_at") and c["posted_at"] < cutoff for c in cards)


async def collect(client: httpx.AsyncClient, conn: sqlite3.Connection,
                  searches: list[dict], *, pause: float = PAUSE,
                  verbose: bool = True) -> dict:
    """Step 1: page through every search, queue ids not seen before.

    Results come newest first, so a search can stop at the first page that is
    entirely older than *its own* last run — everything after it is older still.

    It must be its own last run, not "nothing new on this page". Searches
    overlap: the first pages of "associate product manager" are all jobs the
    "product manager" search already found, but that search hit LinkedIn's
    ~1000-result ceiling, and 300 results deep the APM search had Growth
    Manager roles nobody had collected. Stopping on overlap lost them.
    """
    seen, on_boards = _known(conn)
    last = dict(conn.execute("SELECT search, last_run FROM linkedin_searches"))
    report = {"searches": 0, "pages": 0, "queued": 0, "duplicates": 0}

    for s in searches:
        key = search_key(s)
        added_here = 0
        for start in range(0, MAX_START + 1, PAGE_SIZE):
            page = await _get(client, SEARCH_URL, search_params(s, start), pause)
            report["pages"] += 1
            cards = parse_cards(page)
            if not cards:
                break
            for c in cards:
                if c["id"] in seen:
                    continue
                seen.add(c["id"])
                if (c["company"].lower(), c["title"].lower()) in on_boards:
                    report["duplicates"] += 1
                    continue
                c["remote"] = s["remote"]
                conn.execute(
                    "INSERT OR IGNORE INTO linkedin_queue (source_id, card, search) "
                    "VALUES (?, ?, ?)", (c["id"], json.dumps(c), key))
                added_here += 1
            conn.commit()
            if _all_older(cards, last.get(key)):
                break
        # Only a search that finished gets a date. One cut short by a block
        # raises out of here first, and pages from the top again next run.
        conn.execute("INSERT OR REPLACE INTO linkedin_searches (search, last_run) "
                     "VALUES (?, date('now'))", (key,))
        conn.commit()
        report["searches"] += 1
        report["queued"] += added_here
        if verbose:
            print(f"  ok   {key[:56]:<56} +{added_here}")
    return report


async def fill(client: httpx.AsyncClient, conn: sqlite3.Connection, *,
               cap: int = DETAIL_CAP, pause: float = PAUSE,
               verbose: bool = True) -> dict:
    """Step 2: fetch descriptions for queued jobs, newest first, up to `cap`."""
    from . import db

    tags = _tags_by_company()
    queued = conn.execute(
        "SELECT source_id, card FROM linkedin_queue "
        "ORDER BY json_extract(card, '$.posted_at') DESC LIMIT ?", (cap,)
    ).fetchall()
    report = {"fetched": 0, "added": 0, "already_closed": 0}

    for i, (jid, card_json) in enumerate(queued, 1):
        card = json.loads(card_json)
        detail = parse_detail(await _get(client, DETAIL_URL.format(id=jid),
                                         pause=pause))
        report["fetched"] += 1
        if detail["closed"] or not detail["description"]:
            report["already_closed"] += 1
        else:
            db.upsert(conn, [to_row(card, detail, tags)])
            report["added"] += 1
        conn.execute("DELETE FROM linkedin_queue WHERE source_id = ?", (jid,))
        conn.commit()
        if verbose and i % 25 == 0:
            print(f"  ...  {i}/{len(queued)} descriptions")
    return report


def close_old(conn: sqlite3.Connection, days: int = OPEN_DAYS) -> int:
    """Close LinkedIn jobs past their age, and drop queued ones that are too.

    Age, not absence — see the module docstring.
    """
    cutoff = f"-{int(days)} days"
    conn.execute("DELETE FROM linkedin_queue "
                 "WHERE json_extract(card, '$.posted_at') < date('now', ?)",
                 (cutoff,))
    cur = conn.execute(
        "UPDATE jobs SET closed_at = CURRENT_TIMESTAMP "
        "WHERE source = 'linkedin' AND closed_at IS NULL "
        "AND posted_at < date('now', ?)", (cutoff,))
    conn.commit()
    return cur.rowcount


async def run(conn: sqlite3.Connection, searches: list[dict] | None = None, *,
              cap: int = DETAIL_CAP, pause: float = PAUSE,
              verbose: bool = True) -> dict:
    """Collect, then fill, then close by age. A block stops the network steps
    and keeps everything gathered before it."""
    searches = searches if searches is not None else load_searches(conn=conn)
    report: dict = {"blocked": None}
    async with httpx.AsyncClient(timeout=TIMEOUT, headers={"User-Agent": UA},
                                 follow_redirects=False) as client:
        try:
            report.update(await collect(client, conn, searches,
                                        pause=pause, verbose=verbose))
            report.update(await fill(client, conn, cap=cap, pause=pause,
                                     verbose=verbose))
        except Blocked as exc:
            report["blocked"] = str(exc)
            conn.commit()
    report["closed"] = close_old(conn)
    report["waiting"] = conn.execute(
        "SELECT COUNT(*) FROM linkedin_queue").fetchone()[0]
    return report


# --- live: while someone waits ---------------------------------------------

#: A live search is someone waiting. Two pages and eight descriptions is about
#: fifteen seconds; the daily fetch does the thorough version tomorrow.
LIVE_PAGES = 2
LIVE_MAX_NEW = 8
LIVE_PAUSE = 0.4


def _get_sync(client: httpx.Client, url: str, params: dict | None,
              pause: float) -> str:
    import time
    time.sleep(pause * (1 + random.random() / 2))
    try:
        r = client.get(url, params=params)
    except httpx.HTTPError as exc:
        raise Blocked(type(exc).__name__) from exc
    if r.status_code != 200:
        raise Blocked(f"http {r.status_code}")
    return r.text


def live(conn: sqlite3.Connection, keywords: str, location: str,
         remote: bool = False, *, max_new: int = LIVE_MAX_NEW,
         pause: float = LIVE_PAUSE,
         client: httpx.Client | None = None) -> list[tuple[str, str]]:
    """Search LinkedIn now, store what is new, return the (source, id) added.

    The one place this module goes online while someone waits — only when the
    table came up short. Sorted by relevance, not date: here the best match
    matters more than the newest. A link already sitting in the queue is
    fetched now instead of waiting for the daily run.
    """
    from . import db

    s = {"keywords": keywords, "location": location, "remote": remote,
         "days": OPEN_DAYS}
    own = client is None
    client = client or httpx.Client(timeout=TIMEOUT, headers={"User-Agent": UA})
    try:
        cards: list[dict] = []
        for page in range(LIVE_PAGES):
            got = parse_cards(_get_sync(client, SEARCH_URL,
                                        search_params(s, page * PAGE_SIZE,
                                                      newest_first=False), pause))
            cards += got
            if len(got) < PAGE_SIZE:
                break
        stored = {r[0] for r in conn.execute(
            "SELECT source_id FROM jobs WHERE source = 'linkedin'")}
        _, on_boards = _known(conn)
        fresh = [c for c in cards if c["id"] not in stored
                 and (c["company"].lower(), c["title"].lower()) not in on_boards]
        tags = _tags_by_company()
        added: list[tuple[str, str]] = []
        for c in fresh[:max_new]:
            c["remote"] = remote
            detail = parse_detail(_get_sync(client, DETAIL_URL.format(id=c["id"]),
                                            None, pause))
            conn.execute("DELETE FROM linkedin_queue WHERE source_id = ?", (c["id"],))
            if not detail["closed"] and detail["description"]:
                db.upsert(conn, [to_row(c, detail, tags)])
                added.append(("linkedin", c["id"]))
        conn.commit()
        return added
    finally:
        if own:
            client.close()


async def probe() -> tuple[bool, str]:
    """One search page, for `doctor`."""
    s = {"keywords": "product manager", "location": "India",
         "remote": False, "days": 7}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT,
                                     headers={"User-Agent": UA}) as client:
            n = len(parse_cards(await _get(client, SEARCH_URL,
                                           search_params(s, 0), pause=0)))
    except Blocked as exc:
        return False, f"blocked ({exc})"
    return (n > 0), f"{n} cards on one page"
