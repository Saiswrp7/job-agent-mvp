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
#: SmartRecruiters boards at once. Each one is a request per new posting to
#: the same host, so eight at once would be eight streams at their API.
SR_CONCURRENCY = 2
TIMEOUT = 25.0
UA = "job-agent-mvp/0.1 (personal job search)"


def load_boards(path: Path = BOARDS) -> list[dict]:
    boards = json.loads(path.read_text())
    return [b for b in boards if b.get("enabled", True)]


def known_postings(conn, boards: list[dict]) -> dict[str, dict[str, str]]:
    """Per SmartRecruiters company: posting id -> release date we stored.

    Only that adapter pays a request per posting, so only it needs to know
    what it already has. Read before the fetch, in one short query."""
    names = [b["company"] for b in boards if b["source"] == "smartrecruiters"]
    out: dict[str, dict[str, str]] = {n: {} for n in names}
    if not names:
        return out
    marks = ", ".join("?" for _ in names)
    for r in conn.execute(
            f"SELECT company, source_id, posted_at FROM jobs "
            f"WHERE source = 'smartrecruiters' AND closed_at IS NULL "
            f"AND length(description) > 0 AND company IN ({marks})", names):
        out[r[0]][r[1]] = r[2]
    return out


async def _poll(client: httpx.AsyncClient, sem: asyncio.Semaphore,
                board: dict, known: dict | None = None
                ) -> tuple[dict, list[dict] | Exception]:
    fn = ADAPTERS.get(board["source"])
    if fn is None:
        return board, ValueError(f"no adapter for {board['source']}")
    meta = {k: board.get(k) for k in ("type", "stage", "industry", "size",
                                      "country")}
    meta["known"] = (known or {}).get(board["company"])
    # SmartRecruiters boards queue on their own gate, not the shared one: a
    # board waiting its turn at their host must not hold a slot the other
    # hosts' boards could use.
    gate = _sr_gate() if board["source"] == "smartrecruiters" else sem
    async with gate:
        try:
            rows = await fn(client, board["slug"], board["company"], meta)
            return board, rows
        except Exception as exc:                      # noqa: BLE001
            return board, exc


_SR_GATES: dict[int, asyncio.Semaphore] = {}


def _sr_gate() -> asyncio.Semaphore:
    """One gate per event loop: each `asyncio.run` is a new loop, and a
    semaphore made in one cannot be awaited in another."""
    loop = id(asyncio.get_running_loop())
    if loop not in _SR_GATES:
        _SR_GATES.clear()
        _SR_GATES[loop] = asyncio.Semaphore(SR_CONCURRENCY)
    return _SR_GATES[loop]


async def sync(boards: list[dict] | None = None, *, verbose: bool = True,
               with_linkedin: bool = False,
               linkedin_cap: int | None = None,
               with_adzuna: bool = False,
               adzuna_calls: int | None = None,
               with_labels: bool = False,
               transport: httpx.AsyncBaseTransport | None = None) -> dict:
    """Boards first, then LinkedIn, then Adzuna. Boards go first so the
    LinkedIn step can skip jobs a board already has — the board copy carries
    the real apply link. Adzuna goes last for the same reason one step down:
    its copy is a snippet, so it only adds jobs nobody fuller has.
    """
    boards = boards if boards is not None else load_boards()
    sem = asyncio.Semaphore(CONCURRENCY)
    limits = httpx.Limits(max_connections=20, max_keepalive_connections=20)
    conn = db.connect()
    known = known_postings(conn, boards)

    async with httpx.AsyncClient(timeout=TIMEOUT, limits=limits,
                                 headers={"User-Agent": UA},
                                 follow_redirects=True,
                                 transport=transport) as client:
        results = await asyncio.gather(
            *(_poll(client, sem, b, known) for b in boards)
        )

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
        # `unchanged` rows are postings we already hold in full: they count as
        # seen (so they stay open) but are not rewritten with an empty body.
        report["rows"] += db.upsert(conn, [r for r in rows
                                           if not r.get("unchanged")])
        if rows:
            closed = db.close_missing(
                conn, board["source"], board["company"],
                [r["source_id"] for r in rows],
            )
            report["closed"] += closed
        if verbose:
            print(f"  ok   {board['company']:<22} {len(rows):>3} jobs")

    if with_linkedin:
        from . import linkedin
        if verbose:
            print("\n  LinkedIn")
        kw = {"verbose": verbose}
        if linkedin_cap is not None:
            kw["cap"] = linkedin_cap
        report["linkedin"] = await linkedin.run(conn, **kw)

    if with_adzuna:
        from . import adzuna
        if verbose:
            print("\n  Adzuna")
        report["adzuna"] = await adzuna.run(conn, verbose=verbose,
                                            calls=adzuna_calls)

    # Free labels from title, location and category: no model, milliseconds.
    # Before the LLM step, which replaces them on every row it reads.
    from . import rules
    report["rules"] = rules.run(conn)

    if with_labels:
        # Last, so everything fetched above is labelled in the same run. It
        # never blocks the fetch: a dead provider leaves jobs unlabelled, and
        # unlabelled jobs still match every search.
        from . import labels
        try:
            report["labels"] = labels.run(conn, verbose=verbose)
        except Exception as exc:                      # noqa: BLE001
            report["labels"] = {"labelled": 0, "stopped": str(exc)[:120]}

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
