"""Label every job once, when it arrives. Search filters on the labels after.

Why here and not at search time: a label is paid for once per job and read by
every search from every user. Reading the same 40 descriptions on every search
is paid again each time. Push work toward ingest.

What a label is: a fact about the job — what the person does all day, how
senior, where, whether they own a P&L. Never a preference. Preferences come
from the person, in the search.

Rules this file keeps:

- **Unknown stays NULL.** A wrong label hides a job; NULL passes every filter.
  So anything the model returns that is not on the list is dropped, not forced.
- **Labels never overwrite a fact the fetch already had.** `years_min` from
  the regex wins; the model only fills it when empty.
- **No B2C/B2B label.** The model guessed it from company names (two
  consulting firms came back "b2c"), and `company_type` is a real filter, so a
  wrong guess hides a job. Only the hand-typed board list sets it; the ranker
  judges the rest by reading the description.
- **A job is relabelled when its text changes,** via `label_hash`, and never
  otherwise.
- **A dead provider stops the run** after a few failed batches instead of
  failing every batch in the table one by one.
"""

from __future__ import annotations

import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed

import llm

from . import db

FAMILIES = {
    "engineering", "data", "product", "design", "growth", "crm_lifecycle",
    "marketing", "sales", "business_development", "customer_success",
    "support", "operations", "supply_chain", "finance", "hr", "legal",
    "content", "research", "healthcare", "education", "admin", "other",
}
LEVELS = {"intern", "junior", "mid", "senior", "lead", "leadership"}
WORK_MODES = {"onsite", "hybrid", "remote"}
#: Kinds of work kept per job. A job that truly does more than this is the
#: model listing everything, which would put the job in every search.
MAX_FAMILIES = 4

#: Jobs per model call. Ten keeps the reply short enough to come back whole
#: from a cheap model, and makes the instructions a tenth of the bill.
BATCH = 10
#: Requirements sit near the bottom of a JD, but the role, level and location
#: are nearly always in the first screen. Labels do not need the benefits list.
DESC_CHARS = 2500
WORKERS = 6
#: Consecutive failed batches before assuming the provider is down.
GIVE_UP_AFTER = 3


def provider() -> str:
    """The same provider as everything else, so labels and chat never drift
    apart. `LABEL_PROVIDER` overrides."""
    return os.environ.get("LABEL_PROVIDER") or llm.provider()


def backup() -> str | None:
    """OpenRouter, when set and not already the provider. Used only when the
    main provider stops mid-run (out of credit), so labelling carries on."""
    p = provider()
    if p != "openrouter" and os.environ.get(llm.PROVIDERS["openrouter"][0]):
        return "openrouter"
    return None


def model(via: str | None = None) -> str:
    via = via or provider()
    if os.environ.get("LABEL_MODEL") and via == provider():
        return os.environ["LABEL_MODEL"]
    return llm.PROVIDERS[via][3]


# --- reading and writing ---------------------------------------------------

def pending(conn: sqlite3.Connection, limit: int | None = None,
            only: list[tuple[str, str]] | None = None,
            since: str | None = None) -> list[dict]:
    """Open jobs with no labels, or labels read from older text. Newest first,
    so a capped run spends itself on what people are most likely to see.
    `since` keeps to jobs first seen at or after it that a search can show:
    the engine loop labels its new arrivals with it."""
    sql = ("SELECT source, source_id, title, company, location, description, "
           "content_hash FROM jobs WHERE closed_at IS NULL "
           "AND (label_hash IS NULL OR label_hash != content_hash)")
    params: list = []
    if since:
        sql += f" AND first_seen >= ? AND {db.visible()}"
        params.append(since)
    if only:
        sql += " AND (" + " OR ".join(
            "(source = ? AND source_id = ?)" for _ in only) + ")"
        for s, sid in only:
            params += [s, str(sid)]
    sql += " ORDER BY posted_at DESC"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return [dict(r) for r in conn.execute(sql, params)]


def _render(rows: list[dict]) -> str:
    return "\n\n".join(
        f"--- id: {i}\n{r['title']} at {r['company']} · {r.get('location') or '?'}\n\n"
        f"{(r.get('description') or '')[:DESC_CHARS]}"
        for i, r in enumerate(rows))


def clean(raw: dict) -> dict:
    """Whatever came back, reduced to values the columns accept."""
    def pick(key: str, allowed: set[str]) -> str | None:
        v = raw.get(key)
        v = v.strip().lower() if isinstance(v, str) else None
        return v if v in allowed else None

    years = raw.get("years_min")
    if isinstance(years, str) and years.strip().isdigit():
        years = int(years.strip())
    elif isinstance(years, float) and years.is_integer():
        years = int(years)
    if isinstance(years, bool) or not isinstance(years, int) or not 0 <= years <= 30:
        years = None
    pnl = raw.get("owns_pnl")
    if isinstance(pnl, str):
        pnl = {"true": True, "false": False}.get(pnl.strip().lower())
    country = raw.get("country")
    country = (country.strip()[:40] or None) if isinstance(country, str) else None
    return {
        "role_family": families(raw.get("role_family")),
        "level": pick("level", LEVELS),
        "years_min": years,
        "owns_pnl": 1 if pnl is True else 0 if pnl is False else None,
        "country": country,
        "work_mode": pick("work_mode", WORK_MODES),
    }


def families(raw) -> str | None:
    """The kinds of work, as stored: "growth,marketing", main one first, or
    None. A list or a bare string; unknown names dropped, never forced;
    `other` only when nothing else fits."""
    items = [raw] if isinstance(raw, str) else raw if isinstance(raw, list) else []
    kept: list[str] = []
    for v in items:
        v = v.strip().lower() if isinstance(v, str) else None
        if v in FAMILIES and v not in kept:
            kept.append(v)
    if len(kept) > 1 and "other" in kept:
        kept.remove("other")
    return ",".join(kept[:MAX_FAMILIES]) or None


def ask(rows: list[dict], via: str | None = None) -> list[dict | None]:
    """One model call for up to BATCH jobs. Returns cleaned labels in the same
    order; a job the reply skipped gets None and stays unlabelled."""
    via = via or provider()
    import usage
    with usage.purpose("label"):       # pool threads start with no purpose
        raw = llm.complete_json(llm.prompt("label"), _render(rows), model=model(via),
                                via=via, max_tokens=200 * len(rows) + 200,
                                temperature=0, effort="low", cache=False)
    if not isinstance(raw, list):
        raise ValueError(f"labels: expected a list, got {type(raw).__name__}")
    by_id: dict[int, dict] = {}
    for item in raw:
        if isinstance(item, dict):
            try:
                by_id[int(item.get("id"))] = item
            except (TypeError, ValueError):
                continue
    return [clean(by_id[i]) if i in by_id else None for i in range(len(rows))]


def write(conn: sqlite3.Connection, row: dict, labels: dict) -> None:
    conn.execute(
        """UPDATE jobs SET
             role_family = ?, level = ?, owns_pnl = ?, country = ?, work_mode = ?,
             years_min    = COALESCE(years_min, ?),
             remote       = CASE WHEN ? = 'remote' THEN 1 ELSE remote END,
             label_hash   = content_hash
           WHERE source = ? AND source_id = ?""",
        (labels["role_family"], labels["level"], labels["owns_pnl"],
         labels["country"], labels["work_mode"],
         labels["years_min"], labels["work_mode"],
         row["source"], str(row["source_id"])))


def run(conn: sqlite3.Connection, *, limit: int | None = None,
        only: list[tuple[str, str]] | None = None,
        workers: int = WORKERS, verbose: bool = True,
        since: str | None = None) -> dict:
    """Label what needs it. `only` restricts to specific jobs — the live search
    uses it to label the handful it just fetched, and nothing else.

    If the main provider stops (the usual cause is running out of credit), the
    rest goes to the backup rather than waiting for tomorrow."""
    report = _run(conn, limit, only, workers, verbose, provider(), since)
    spare = backup()
    if report["stopped"] and spare:
        if verbose:
            print(f"  {provider()} stopped ({report['stopped']}); "
                  f"continuing on {spare}")
        more = _run(conn, limit, only, workers, verbose, spare, since)
        report = {**more,
                  "labelled": report["labelled"] + more["labelled"],
                  "skipped": report["skipped"] + more["skipped"],
                  "failed_batches": report["failed_batches"] + more["failed_batches"],
                  "fell_back_to": spare}
    return report


def _run(conn, limit, only, workers, verbose, via: str,
         since: str | None = None) -> dict:
    rows = pending(conn, limit, only, since)
    batches = [rows[i:i + BATCH] for i in range(0, len(rows), BATCH)]
    report = {"pending": len(rows), "labelled": 0, "skipped": 0,
              "failed_batches": 0, "stopped": None}
    if not batches:
        return report

    failures_in_a_row = 0
    # The model calls run in threads; every database write stays on this one,
    # because a sqlite connection belongs to the thread that made it.
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(ask, b, via): b for b in batches}
        for done, fut in enumerate(as_completed(futures), 1):
            batch = futures[fut]
            try:
                labels = fut.result()
            except Exception as exc:                  # noqa: BLE001
                report["failed_batches"] += 1
                failures_in_a_row += 1
                if failures_in_a_row >= GIVE_UP_AFTER:
                    report["stopped"] = f"{type(exc).__name__}: {str(exc)[:120]}"
                    for f in futures:
                        f.cancel()
                    break
                continue
            failures_in_a_row = 0
            for row, lab in zip(batch, labels):
                if lab is None:
                    report["skipped"] += 1
                else:
                    write(conn, row, lab)
                    report["labelled"] += 1
            conn.commit()
            if verbose and done % 20 == 0:
                print(f"  ...  {report['labelled']}/{len(rows)} labelled")
    return report


def coverage(conn: sqlite3.Connection) -> tuple[int, int]:
    """(labelled, open) — for `doctor` and `stats`."""
    row = conn.execute(
        "SELECT SUM(label_hash IS NOT NULL AND label_hash = content_hash), "
        "COUNT(*) FROM jobs WHERE closed_at IS NULL").fetchone()
    return (row[0] or 0), (row[1] or 0)
