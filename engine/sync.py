"""The hourly job: poll every board, normalize, upsert, close what vanished.

Concurrent because board polls are not rate limited and are not sleep-bound.
The semaphore is politeness, not throttling — every Greenhouse board shares one
host.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

from . import db
from .adapters import ADAPTERS

BOARDS = Path(__file__).resolve().parent / "boards.json"
CONCURRENCY = 8
TIMEOUT = 25.0
UA = "job-agent-mvp/0.1 (personal job search)"


def load_boards(path: Path = BOARDS) -> list[dict]:
    boards = json.loads(path.read_text())
    return [b for b in boards if b.get("enabled", True)]


async def _poll(client: httpx.AsyncClient, sem: asyncio.Semaphore,
                board: dict) -> tuple[dict, list[dict] | Exception]:
    fn = ADAPTERS.get(board["source"])
    if fn is None:
        return board, ValueError(f"no adapter for {board['source']}")
    meta = {k: board.get(k) for k in ("type", "stage", "industry", "size")}
    async with sem:
        try:
            rows = await fn(client, board["slug"], board["company"], meta)
            return board, rows
        except Exception as exc:                      # noqa: BLE001
            return board, exc


async def sync(boards: list[dict] | None = None, *, verbose: bool = True) -> dict:
    boards = boards if boards is not None else load_boards()
    sem = asyncio.Semaphore(CONCURRENCY)
    limits = httpx.Limits(max_connections=20, max_keepalive_connections=20)

    async with httpx.AsyncClient(timeout=TIMEOUT, limits=limits,
                                 headers={"User-Agent": UA},
                                 follow_redirects=True) as client:
        results = await asyncio.gather(
            *(_poll(client, sem, b) for b in boards)
        )

    conn = db.connect()
    report = {"boards": len(boards), "ok": 0, "failed": 0,
              "rows": 0, "closed": 0, "errors": []}

    for board, rows in results:
        if isinstance(rows, Exception):
            # A failed board closes NOTHING. A timeout must never read as
            # "that company closed every role."
            report["failed"] += 1
            report["errors"].append(f"{board['company']}: {type(rows).__name__}")
            if verbose:
                print(f"  FAIL {board['company']:<22} {type(rows).__name__}")
            continue

        report["ok"] += 1
        report["rows"] += db.upsert(conn, rows)
        if rows:
            closed = db.close_missing(
                conn, board["source"], board["company"],
                [r["source_id"] for r in rows],
            )
            report["closed"] += closed
        if verbose:
            print(f"  ok   {board['company']:<22} {len(rows):>3} jobs")

    report["stats"] = db.stats(conn)
    conn.close()
    return report


async def verify(boards: list[dict] | None = None) -> list[dict]:
    """Hit every board once and report which slugs are live.

    boards.json starts as guesses; this is what turns it into a real list.
    """
    boards = boards if boards is not None else load_boards()
    sem = asyncio.Semaphore(CONCURRENCY)
    async with httpx.AsyncClient(timeout=TIMEOUT,
                                 headers={"User-Agent": UA},
                                 follow_redirects=True) as client:
        results = await asyncio.gather(
            *(_poll(client, sem, b) for b in boards)
        )

    out = []
    for board, rows in results:
        if isinstance(rows, Exception):
            status = getattr(getattr(rows, "response", None), "status_code", None)
            out.append({**board, "live": False,
                        "error": f"{type(rows).__name__}"
                                 + (f" {status}" if status else "")})
        else:
            out.append({**board, "live": True, "jobs": len(rows)})
    return out
