"""The engine: re-read every employer board, keep India's last 30 days, drop
what the employer took down.

    boards table ──due──▶ read the board's whole list ──failed──▶ change nothing
                                     │ ok
                                     ▼
            new job in India, posted ≤ 30 days ago → store it (full text once)
            still listed                          → seen now
            missing from a complete read          → re-read in 15 min;
                                                    missing again → closed

Search shows a job only while it is open, was listed in the last 48 hours and
was posted in the last 30 days (db.VISIBLE). A nightly prune deletes the rest.

Nothing here runs a model, and nothing here runs while a user waits:
`python cli.py engine` is its own always-on process, like the bot.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

import httpx

from . import db
from .adapters import ADAPTERS, in_scope
from .ats_bridge import ALLOWED as BRIDGE_ATS, Feed

UA = "job-agent-mvp/0.1 (personal job search)"
TIMEOUT = 25.0
#: Boards read at once, and requests at once to any one host.
BOARDS_AT_ONCE = 12
PER_HOST = 2
#: Our adapters' shared API hosts take a few more at once (one request per board).
SHARED_HOST_LIMIT = {"boards-api.greenhouse.io": 4, "api.lever.co": 4,
                     "api.ashbyhq.com": 4, "workday": 3}
#: Every tenant has its own host, but Workday limits a caller across all of
#: them: twelve tenants at once got 403/429 until the library gave up (the
#: first real sweep, 9 of 214 reads). One gate for the whole ATS.
ONE_GATE = frozenset({"workday"})

#: How often a board is read again after a good read.
EVERY = timedelta(hours=2)
#: A big list costs dozens of pages; read it less often.
HEAVY_POSTINGS = 400
HEAVY_EVERY = timedelta(hours=6)
#: After a first miss: re-read soon, so a gone job closes fast and a job
#: skipped by a shifting list is found again fast.
CONFIRM = timedelta(minutes=15)
#: After failures in a row: 30 min, 2 h, 6 h, then daily.
BACKOFF = [timedelta(minutes=30), timedelta(hours=2), timedelta(hours=6),
           timedelta(hours=24)]
#: 404s in a row before a board is taken as gone.
GONE_AFTER = 3
#: A "complete" read that lost most of a big list at once is not trusted to
#: prove absence: a site change looks exactly like that.
DROP_GUARD = (20, 0.3)


def _now() -> datetime:
    return datetime.now(UTC)


def _sql_time(d: datetime) -> str:
    """The format SQLite's CURRENT_TIMESTAMP uses, so comparisons are plain."""
    return d.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


def host_of(board: dict) -> str:
    src = board["source"]
    fixed = {"greenhouse": "boards-api.greenhouse.io", "lever": "api.lever.co",
             "ashby": "api.ashbyhq.com", "workable": "apply.workable.com",
             "smartrecruiters": "api.smartrecruiters.com"}
    if src in fixed:
        return fixed[src]
    if src in ONE_GATE:
        return src
    slug = board["slug"]
    return urlparse(slug).netloc.lower() if slug.startswith("http") else f"{src}:{slug}"


def _tags(board: dict) -> dict:
    raw = board.get("tags")
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}


# --- one read ----------------------------------------------------------------------

async def _native(conn: sqlite3.Connection, client: httpx.AsyncClient,
                  board: dict) -> Feed:
    """Our own five adapters. Each returns the board's whole list in one go
    (SmartRecruiters pages to the end, and raises on any failure)."""
    tags = _tags(board)
    known = None
    if board["source"] == "smartrecruiters":
        known = {r[0]: r[1] for r in conn.execute(
            "SELECT source_id, posted_at FROM jobs WHERE source = 'smartrecruiters' "
            "AND board = ? AND closed_at IS NULL AND length(description) > 0",
            (board["slug"],))}
    meta = {k: tags.get(k) for k in ("type", "stage", "industry", "size", "country")}
    meta["known"] = known
    rows = await ADAPTERS[board["source"]](client, board["slug"], board["company"], meta)
    ids = [str(r["source_id"]) for r in rows]
    keep = []
    for r in rows:
        if r.get("unchanged") or r.get("old"):   # listed; not re-read, see adapters
            continue
        if not in_scope(r.get("location")):
            continue
        r["board"] = board["slug"]
        keep.append(r)
    return Feed(rows=keep, ids=ids, complete=True)


async def _read(conn, client, board) -> Feed:
    if board["source"] in ADAPTERS:
        return await _native(conn, client, board)
    if board["source"] in BRIDGE_ATS:
        from . import ats_bridge
        return await ats_bridge.fetch(board, _tags(board))
    raise ValueError(f"no reader for {board['source']}")


async def store(conn: sqlite3.Connection, board: dict, feed: Feed) -> dict:
    """Which listed jobs to keep, and their full text for the new ones.

    - posted more than 30 days ago: not kept (remembered, so it is never
      mistaken for new later)
    - already stored: refreshed when the list carries the full text; else
      the reconcile step marks it seen
    - no date: new since the board's first whole read → dated today; seen
      in that first read → age unknown, not kept
    - new and in the window: its description is read (once) and it is stored
    """
    now = _now()
    cutoff = (now - timedelta(days=db.DAYS)).date().isoformat()
    src, slug = board["source"], board["slug"]
    stored = {r[0] for r in conn.execute(
        "SELECT source_id FROM jobs WHERE source = ? AND board = ?", (src, slug))}
    remembered = {r[0] for r in conn.execute(
        "SELECT source_id FROM seen_ids WHERE source = ? AND board = ?", (src, slug))}

    refresh, new, remember = [], [], []
    for r in feed.rows:
        sid = str(r["source_id"])
        posted = r.get("posted_at")
        if posted and posted[:10] < cutoff:
            if sid not in stored:
                remember.append(sid)
            continue
        if sid in stored:
            if r.get("description"):
                refresh.append(r)
            continue
        if sid in remembered:
            continue
        if not posted:
            if not board.get("first_ok"):
                remember.append(sid)
                continue
            r["posted_at"] = now.date().isoformat()
        new.append(r)

    if feed.describe and any(not r.get("description") for r in new):
        need = [r for r in new if not r.get("description")]
        dated = {r["source_id"]: r["posted_at"] for r in need}
        got = await feed.describe(need)
        for g in got:
            g["posted_at"] = g.get("posted_at") or dated.get(g["source_id"])
        new = [r for r in new if r.get("description")] + got
    new = [r for r in new if r.get("description")
           and (r.get("posted_at") or "")[:10] >= cutoff]

    db.upsert(conn, refresh + new)
    conn.executemany("INSERT OR IGNORE INTO seen_ids (source, source_id, board) "
                     "VALUES (?, ?, ?)", [(src, s, slug) for s in remember])
    conn.commit()
    return {"stored": len(refresh) + len(new), "new": len(new)}


def _gone(exc: BaseException) -> bool:
    """The site says this board does not exist (not: slow down, not: broken)."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 404
    try:
        from ats_scrapers.exceptions import CompanyNotFoundError
        return isinstance(exc, CompanyNotFoundError)
    except ImportError:
        return False


async def read_board(conn: sqlite3.Connection, client: httpx.AsyncClient,
                     board: dict) -> dict:
    """Read one board, store, reconcile, schedule the next read. Never raises:
    a failure is recorded and closes nothing."""
    started = _now()
    t0 = time.monotonic()
    src, slug = board["source"], board["slug"]
    out = {"source": src, "slug": slug, "company": board["company"], "ok": 0,
           "complete": 0, "postings": None, "stored": 0, "new": 0, "missed": 0,
           "closed": 0, "error": None}
    try:
        feed = await _read(conn, client, board)
    except Exception as exc:                          # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
        gone = _gone(exc)
        streak = (board.get("fail_streak") or 0)
        gone_streak = (board.get("gone_streak") or 0) + 1 if gone else 0
        wait = BACKOFF[min(streak, len(BACKOFF) - 1)]
        conn.execute(
            "UPDATE boards SET last_fail = ?, fail_streak = fail_streak + 1, "
            "gone_streak = ?, last_error = ?, next_due = ? WHERE source = ? AND slug = ?",
            (_sql_time(started), gone_streak, out["error"],
             _sql_time(started + wait), src, slug))
        if gone and gone_streak >= GONE_AFTER:
            conn.execute("UPDATE boards SET retired_at = ? WHERE source = ? AND slug = ?",
                         (_sql_time(started), src, slug))
            out["closed"] = db.close_board(conn, src, slug)
            out["error"] += " (board retired)"
        _log(conn, out, started, time.monotonic() - t0)
        return out

    complete = feed.complete
    before = board.get("open_jobs") or 0
    if complete and before >= DROP_GUARD[0] and len(feed.ids) < DROP_GUARD[1] * before:
        complete = False
        feed.notes.append(f"list fell from {before} to {len(feed.ids)}: not trusted")
    try:
        kept = await store(conn, board, feed)
    except Exception as exc:                          # noqa: BLE001
        # The list was read but its details were not: seen jobs stay seen,
        # nothing is closed on a half-finished store.
        kept = {"stored": 0, "new": 0}
        complete = False
        feed.notes.append(f"store failed: {type(exc).__name__}: {str(exc)[:120]}")
    rec = db.reconcile(conn, src, slug, feed.ids, complete=complete)
    out.update(ok=1, complete=int(complete), postings=len(feed.ids), **kept, **rec)
    if feed.notes:
        out["error"] = "; ".join(feed.notes)
    every = HEAVY_EVERY if len(feed.ids) > HEAVY_POSTINGS else EVERY
    nxt = started + (CONFIRM if rec["missed"] else every)
    conn.execute(
        "UPDATE boards SET last_ok = ?, first_ok = CASE WHEN ? THEN COALESCE(first_ok, ?) "
        "ELSE first_ok END, fail_streak = 0, gone_streak = 0, last_error = ?, "
        "open_jobs = CASE WHEN ? THEN ? ELSE open_jobs END, next_due = ? "
        "WHERE source = ? AND slug = ?",
        (_sql_time(started), complete, _sql_time(_now()), out["error"],
         complete, len(feed.ids), _sql_time(nxt), src, slug))
    _log(conn, out, started, time.monotonic() - t0)
    return out


def _log(conn, out: dict, started: datetime, seconds: float) -> None:
    conn.execute(
        "INSERT INTO board_reads (source, slug, started_at, seconds, ok, complete, "
        "postings, stored, new, missed, closed, error) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (out["source"], out["slug"], _sql_time(started), round(seconds, 2), out["ok"],
         out["complete"], out["postings"], out["stored"], out["new"], out["missed"],
         out["closed"], out["error"]))
    conn.commit()


# --- many reads ------------------------------------------------------------------

def due(conn: sqlite3.Connection, limit: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM boards WHERE enabled = 1 AND retired_at IS NULL "
        "AND (next_due IS NULL OR next_due <= ?) "
        "ORDER BY next_due IS NOT NULL, next_due LIMIT ?",
        (_sql_time(_now()), limit))]


async def read_all(conn: sqlite3.Connection, client: httpx.AsyncClient,
                   boards: list[dict], *, verbose: bool = False) -> list[dict]:
    """Read these boards, a few at once, politely per host."""
    gate = asyncio.Semaphore(BOARDS_AT_ONCE)
    hosts: dict[str, asyncio.Semaphore] = {}

    async def one(b: dict) -> dict:
        h = host_of(b)
        hosts.setdefault(h, asyncio.Semaphore(SHARED_HOST_LIMIT.get(h, PER_HOST)))
        # Host first: a board queued behind its own host must not hold one of
        # the slots boards on other hosts could use.
        async with hosts[h], gate:
            r = await read_board(conn, client, b)
        if verbose:
            mark = "ok  " if r["ok"] else "FAIL"
            extra = (f"{r['postings']:>5} listed {r['new']:>4} new {r['closed']:>3} closed"
                     if r["ok"] else r["error"])
            print(f"  {mark} {b['source']:<15} {b['company'][:30]:<30} {extra}", flush=True)
        return r

    return list(await asyncio.gather(*(one(b) for b in boards)))


def client(transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=TIMEOUT, follow_redirects=True, transport=transport,
        headers={"User-Agent": UA},
        limits=httpx.Limits(max_connections=40, max_keepalive_connections=20))


#: How often the nightly work runs (prune + free labels).
MAINTENANCE_EVERY = timedelta(hours=20)


def drop_out_of_scope(conn: sqlite3.Connection) -> int:
    """Board jobs outside India (and not remote-open) are not kept. New reads
    never store them; this clears the ones stored before the engine."""
    rows = conn.execute(
        "SELECT source, source_id, location FROM jobs WHERE source NOT IN "
        f"({', '.join(repr(s) for s in db.AGGREGATORS)})").fetchall()
    out = [(r["source"], r["source_id"]) for r in rows if not in_scope(r["location"])]
    conn.executemany("DELETE FROM jobs WHERE source = ? AND source_id = ?", out)
    conn.commit()
    return len(out)


def maintain(conn: sqlite3.Connection) -> dict:
    from . import rules
    out = {"prune": {**db.prune(conn), "out_of_scope": drop_out_of_scope(conn)}}
    out["rules"] = rules.run(conn)
    return out


#: New arrivals labelled per round, at most. A new board can bring thousands
#: at once; the rest wait for the next round instead of one long stall.
LABEL_PER_ROUND = 500
#: Jobs embedded after each round of reads.
EMBED_PER_ROUND = 1000


def label_new(conn: sqlite3.Connection, since: str, verbose: bool = True) -> str:
    """AI labels for the jobs that arrived since `since`, so a search finds
    them by the kind of work and not only by a word in the title. Returns
    where the next round starts: unchanged while some are left or a call
    failed, so no arrival is skipped. Until 2026-10-02 only `cli.py sync` and
    `cli.py label` labelled, never this loop, and 60% of open jobs had no role
    label. One model call per 10 jobs (labels.BATCH). ENGINE_LABELS=0 turns
    it off."""
    if os.environ.get("ENGINE_LABELS") == "0":
        return since
    from . import labels
    started = _sql_time(_now())
    r = labels.run(conn, since=since, limit=LABEL_PER_ROUND, verbose=False)
    if verbose and (r["labelled"] or r["stopped"]):
        print(f"  labels: {r['labelled']} new jobs labelled"
              + (f", stopped ({r['stopped']})" if r["stopped"] else ""), flush=True)
    if r["stopped"] or r["failed_batches"] or r["pending"] >= LABEL_PER_ROUND:
        return since
    return started


def embed_new(conn: sqlite3.Connection, verbose: bool = True) -> None:
    """Meaning-vectors for the jobs that arrived (engine/vectors.py), so a
    search can find them by what they are and not only by a word in the title.
    A failed call costs nothing: the jobs are picked up next round.
    ENGINE_EMBED=0 turns it off."""
    if os.environ.get("ENGINE_EMBED") == "0":
        return
    from . import vectors
    if not vectors.configured():
        return
    r = vectors.run(conn, limit=EMBED_PER_ROUND)
    if verbose and (r["embedded"] or r["stopped"]):
        print(f"  vectors: {r['embedded']} new jobs embedded"
              + (f", stopped ({r['stopped']})" if r["stopped"] else ""), flush=True)


async def run(*, once: bool = False, batch: int = 5000, verbose: bool = True,
              conn: sqlite3.Connection | None = None,
              transport: httpx.AsyncBaseTransport | None = None) -> dict:
    """Read due boards until stopped. `once`: until nothing is due, then stop.

    Every due board goes to `read_all` at once and its gates pace them
    (BOARDS_AT_ONCE, PER_HOST): a free slot takes the next board. Picking a
    small batch and waiting for it made every batch as slow as its slowest
    board (the first real sweep: 47 boards in four minutes)."""
    from . import registry
    conn = conn or db.connect()
    if verbose:
        print(f"journal: {db.wal(conn)}")
    registry.sync_json(conn)
    last_maintained = None
    label_from = _sql_time(_now())       # what arrives from here on gets labels
    totals = {"reads": 0, "ok": 0, "failed": 0, "new": 0, "closed": 0}
    async with client(transport) as c:
        while True:
            boards = due(conn, batch)
            if boards:
                for r in await read_all(conn, c, boards, verbose=verbose):
                    totals["reads"] += 1
                    totals["ok" if r["ok"] else "failed"] += 1
                    totals["new"] += r["new"]
                    totals["closed"] += r["closed"]
                label_from = label_new(conn, label_from, verbose)
                embed_new(conn, verbose)
            if last_maintained is None or _now() - last_maintained > MAINTENANCE_EVERY:
                m = maintain(conn)
                last_maintained = _now()
                if verbose:
                    print(f"  maintenance: {m['prune']}")
            if not boards:
                if once:
                    break
                await asyncio.sleep(30)
    return totals


def status(conn: sqlite3.Connection) -> dict:
    """What `cli.py engine status` prints."""
    now = _sql_time(_now())
    b = dict(conn.execute(
        "SELECT COUNT(*) AS boards, SUM(retired_at IS NOT NULL) AS retired, "
        "SUM(retired_at IS NULL AND enabled = 1 AND (next_due IS NULL OR next_due <= ?)) AS due_now, "
        "SUM(retired_at IS NULL AND last_ok IS NULL) AS never_read, "
        "SUM(fail_streak > 0) AS failing FROM boards", (now,)).fetchone())
    reads = dict(conn.execute(
        "SELECT COUNT(*) AS reads_24h, SUM(ok) AS ok_24h, SUM(new) AS new_24h, "
        "SUM(closed) AS closed_24h FROM board_reads "
        "WHERE started_at >= datetime('now', '-24 hours')").fetchone())
    visible = conn.execute(f"SELECT COUNT(*) FROM jobs WHERE {db.VISIBLE}").fetchone()[0]
    kinds = dict(conn.execute(
        f"SELECT apply_kind, COUNT(*) FROM jobs WHERE {db.VISIBLE} GROUP BY 1").fetchall())
    failing = [dict(r) for r in conn.execute(
        "SELECT source, company, fail_streak, last_error FROM boards "
        "WHERE fail_streak > 0 AND retired_at IS NULL ORDER BY fail_streak DESC LIMIT 15")]
    return {**b, **reads, "visible": visible, "by_kind": kinds, "failing_boards": failing}
