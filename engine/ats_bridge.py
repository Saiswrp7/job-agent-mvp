"""Every ATS our own adapters do not cover, read through `ats-scrapers` (MIT).

The library knows the public job APIs behind 60+ careers-site products
(Workday, Oracle, SAP SuccessFactors, Keka...). We use it to read lists. What
counts as India, as fresh and as gone is decided in engine/loop.py, not here.

Three rules on top of it:

- **Only ATSs in ALLOWED.** For a few sites the library gets past bot
  protection: Darwinbox and Uber pin a TLS-impersonation client; Avature,
  Tesla, Meta, TikTok and ByteDance drive a stealth browser. fetch.md rules
  that out, so they are not here. `httpcloak` is not installed either, so a
  403 anywhere else is a failed read, never an escalation.
- **A description that costs a request per job is paid once.** For PER_JOB
  ATSs the list is read without descriptions, and only the jobs we will keep
  (new, in India, posted in the last 30 days) are read in full.
- **Workday is read for India only**, through the site's own location
  filter, and says whether the read was whole: past Workday's 2,000-result cap
  a query cannot prove that a missing job is gone.

The pinned version matters: IndiaWorkday uses two of the library's private
methods. `python cli.py doctor` and the tests catch a version that moved them.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs, urlparse

from .adapters import _row, in_scope

#: ATSs read through the library. Keep in step with registry.DATASET_ATS.
ALLOWED = frozenset({
    "workday", "oracle", "successfactors", "phenom", "keka", "icims",
    "rippling", "recruitee", "teamtailor", "breezy", "bamboohr", "pinpoint",
    "recruiterbox", "cornerstone", "gem", "jobvite", "paycom", "dayforce", "ukg",
})
#: Lists without descriptions: one request per job, paid only for kept jobs.
PER_JOB = frozenset({"workday", "oracle", "icims", "rippling", "breezy",
                     "bamboohr", "gem", "jobvite", "paycom", "ukg"})


@dataclass
class Feed:
    """One read of one board, before anything is stored."""
    rows: list[dict]                 # in India or open remote, as job rows
    ids: list[str]                   # every posting the list gave, any place
    complete: bool = True            # absence from this list means gone
    describe: Callable[[list[dict]], Awaitable[list[dict]]] | None = None
    notes: list[str] = field(default_factory=list)


# --- Workday, India only ----------------------------------------------------

_POSTED_RE = re.compile(r"(\d+)\+?\s*days?\s*ago", re.I)


def _days_ago(text: str | None) -> int | None:
    """'Posted Today' 0, 'Posted Yesterday' 1, 'Posted 3 Days Ago' 3,
    'Posted 30+ Days Ago' 31 (past the window, whatever the true age)."""
    t = (text or "").lower()
    if "today" in t:
        return 0
    if "yesterday" in t:
        return 1
    m = _POSTED_RE.search(t)
    if not m:
        return None
    n = int(m.group(1))
    return n + 1 if "+" in t else n


def _india_values(facets: list[dict]) -> tuple[str, list[str]] | None:
    """The site's own filter for India: its country facet if it has one, else
    every office location that names India or an Indian city. None when the
    site has no location facet at all (then it is read whole and filtered by
    the text of each job's location)."""
    flat: list[dict] = []

    def walk(items):
        for f in items or []:
            if not isinstance(f, dict):
                continue
            flat.append(f)
            walk([v for v in f.get("values") or [] if "facetParameter" in v])
    walk(facets)
    by_param = {f.get("facetParameter"): f for f in flat}
    for param in ("locationCountry", "locations", "Location_Country"):
        f = by_param.get(param)
        if not f:
            continue
        ids = [v["id"] for v in f.get("values") or []
               if isinstance(v, dict) and v.get("id")
               and in_scope(v.get("descriptor"))]
        return param, ids
    return None


def _india_workday():
    """Built on first use so importing this module never needs the library."""
    import ats_scrapers.scrapers.workday as wd
    from ats_scrapers.scrapers.workday import QUERY_TOTAL_CAP, WorkdayScraper

    # The library pages one site ten requests at a time. Two is plenty for a
    # list we re-read every few hours, and Workday limits callers across
    # tenants (engine/loop.py ONE_GATE).
    wd.MAX_CONCURRENCY = 2

    class IndiaWorkday(WorkdayScraper):
        """Workday, filtered to India at the source, honest about completeness."""

        total: int | None = None           # what the filtered query says exists
        filtered: str | None = None        # the facet used, or None (read whole)

        def _parse_job(self, item, base_url, company):
            job = super()._parse_job(item, base_url, company)
            raw = dict(job.raw or {})
            if item.get("postedOn"):
                raw["postedOn"] = item["postedOn"]
            return job.model_copy(update={"raw": raw})

        async def _exhaust_query(self, client, api, sem, *, applied_facets,
                                 absorb, depth):
            if depth or applied_facets:
                return await super()._exhaust_query(
                    client, api, sem, applied_facets=applied_facets,
                    absorb=absorb, depth=depth)
            first = await self._request(client, api, sem, applied_facets={},
                                        offset=0)
            india = _india_values(first.get("facets") or [])
            if india is None:
                self.total = int(first.get("total") or 0)
                return await super()._exhaust_query(
                    client, api, sem, applied_facets={}, absorb=absorb, depth=0)
            param, ids = india
            self.filtered = param
            if not ids:                    # no office in India: nothing listed here
                self.total = 0
                return None
            facets = {param: ids}
            probe = await self._request(client, api, sem, applied_facets=facets,
                                        offset=0)
            self.total = int(probe.get("total") or 0)
            return await super()._exhaust_query(
                client, api, sem, applied_facets=facets, absorb=absorb, depth=0)

        def complete_for(self, n: int) -> bool:
            return (self.total is not None and self.total < QUERY_TOTAL_CAP
                    and n >= self.total)

        async def details(self, jobs):
            """Full text, the real location and the absolute posting date for
            these jobs only. Best effort: a posting whose detail fails keeps
            its list row and is simply not stored this time."""
            import httpx
            from ats_scrapers.scrapers.workday import URL_PATTERN
            m = URL_PATTERN.match(self.company_slug.rstrip("/"))
            prefix = (f"https://{m['company']}.{m['instance']}.myworkdayjobs.com"
                      f"/wday/cxs/{m['company']}/{m['site']}")
            jobs = list(jobs)
            async with httpx.AsyncClient(timeout=self.timeout,
                                         follow_redirects=True) as client:
                await self._enrich_details(client, asyncio.Semaphore(2), prefix, jobs)
            return jobs

    return IndiaWorkday


# --- reading --------------------------------------------------------------------

def make(board: dict):
    """The library's reader for this board."""
    from ats_scrapers.scrapers.base import ScraperRegistry
    ats = board["source"]
    if ats not in ALLOWED:
        raise ValueError(f"{ats} is not read through the library")
    import inspect
    cls = _india_workday() if ats == "workday" else ScraperRegistry.get(ats)
    kw: dict[str, Any] = {"include_descriptions": ats not in PER_JOB}
    takes = inspect.signature(cls.__init__).parameters
    if "company_name" in takes:
        kw["company_name"] = board["company"]
    if "max_fetch_seconds" in takes:
        kw["max_fetch_seconds"] = 300.0
    return cls(board["slug"], **kw)


def _iso(d: datetime | str | None) -> str | None:
    if d is None:
        return None
    if isinstance(d, str):
        return d
    if d.tzinfo is None:
        d = d.replace(tzinfo=UTC)
    return d.astimezone(UTC).isoformat(timespec="seconds")


def posted_of(job) -> str | None:
    """The date a posting went up, from whatever the ATS gives."""
    if job.posted_at:
        return _iso(job.posted_at)
    raw = job.raw or {}
    if raw.get("startDate"):                     # Workday detail: 'YYYY-MM-DD'
        return str(raw["startDate"])[:10]
    days = _days_ago(raw.get("postedOn"))        # Workday list: 'Posted 3 Days Ago'
    if days is not None:
        return (datetime.now(UTC) - timedelta(days=days)).date().isoformat()
    return None


def apply_url_of(ats: str, url: str, apply_url: str | None) -> str:
    """Where the apply button goes. The feed gives it for most ATSs; for
    Workday it is the job page + /apply, and Oracle sometimes lists only a
    search page with the job id after '#', which is turned into the job page."""
    if apply_url:
        return apply_url
    if ats == "workday" and "/job/" in url:
        return url.rstrip("/") + "/apply"
    if ats == "oracle" and "#" in url:
        base, _, jid = url.partition("#")
        p = urlparse(base)
        site = (parse_qs(p.query).get("site_number") or [""])[0]
        if site and jid:
            return (f"{p.scheme}://{p.netloc}/hcmUI/CandidateExperience/en/sites/"
                    f"{site}/job/{jid}")
    return url


def to_row(job, board: dict, tags: dict | None = None) -> dict:
    ats = board["source"]
    url = str(job.url)
    return _row(
        source=ats, source_id=url, company=board["company"],
        title=job.title or "", location=job.location,
        description=job.description or "", url=url,
        apply_url=apply_url_of(ats, url, str(job.apply_url) if job.apply_url else None),
        posted_at=posted_of(job), updated_at=_iso(job.fetched_at),
        department=job.department,
        employment_type=job.employment_type, salary=job.salary_summary,
        meta=tags or {}, board=board["slug"],
    )


async def fetch(board: dict, tags: dict | None = None) -> Feed:
    """Read one board's whole list. Raises on any failure: the caller closes
    nothing for a board whose read failed."""
    scraper = make(board)
    jobs = await scraper.afetch()
    ids = [str(j.url) for j in jobs]
    # A site already filtered to India lists only India, including jobs whose
    # location reads "2 Locations" until the detail is read.
    trusted = bool(getattr(scraper, "filtered", None))

    def here(j) -> bool:
        return trusted or in_scope(j.location, j.country_iso)

    kept = {str(j.url): j for j in jobs if here(j)}
    complete = (scraper.complete_for(len(jobs))
                if hasattr(scraper, "complete_for") else True)
    notes = []
    if getattr(scraper, "filtered", None):
        notes.append(f"filtered by {scraper.filtered}")
    if hasattr(scraper, "total") and not complete:
        notes.append(f"incomplete: {len(jobs)} of {scraper.total}")

    async def describe(rows: list[dict]) -> list[dict]:
        ats = board["source"]
        want = [kept[r["source_id"]] for r in rows if r["source_id"] in kept]
        if ats not in PER_JOB or not want:
            return rows
        if hasattr(scraper, "details"):
            filled = await scraper.details(want)
        else:
            filled = await asyncio.to_thread(scraper.enrich_descriptions, want)
        # The detail can move a job out of India ("2 Locations" resolved).
        return [to_row(j, board, tags) for j in filled if here(j)]

    return Feed(rows=[to_row(j, board, tags) for j in kept.values()],
                ids=ids, complete=complete, describe=describe, notes=notes)
