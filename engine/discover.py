"""Find company boards from the companies already in the jobs table.

Adzuna and LinkedIn name ~2,400 employers hiring in India but give a snippet
or no apply link. The same employer's own Greenhouse, Lever, Ashby, Workable
or SmartRecruiters board gives every job in full with the real apply link. So
the cheapest supply there is: guess each company's board slug and ask.

How it stays polite and honest:

- One request at a time per platform, with a pause. Platforms run side by side
  because they are different hosts.
- A 404 is the normal "no board here". Anything else (429, 5xx) five times in
  a row stops that platform for this run: we are being told to slow down.
- A board only counts when it answers *with jobs*. "India or remote" is
  recorded too, because a US-only board adds rows nobody here can apply to.
- Every answer is appended to a JSONL file as it comes, so a stopped run picks
  up where it was and the evidence behind boards.json is on disk.

This is a one-off tool, run by hand. It never writes boards.json itself:
`python cli.py discover --add` does, after the probe, from the file.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from pathlib import Path

import httpx

from .sync import UA

PAUSE = 0.25
TIMEOUT = 20.0
#: Refusals in a row before a platform is left alone for this run.
STOP_AFTER = 5

INDIA_RE = re.compile(
    r"\bindia\b|bangalore|bengaluru|mumbai|\bdelhi\b|gurgaon|gurugram|noida|"
    r"hyderabad|\bpune\b|chennai|kolkata|ahmedabad|jaipur|kochi", re.I)
REMOTE_RE = re.compile(r"remote|anywhere|work from home", re.I)

#: Words a legal name carries that a board slug almost never does.
LEGAL = re.compile(
    r"\b(private|pvt|limited|ltd|llp|llc|inc|incorporated|corp|corporation|"
    r"co|plc|gmbh|pte|s\.?a|b\.?v|ag)\b\.?", re.I)
#: Words that are sometimes in the slug and sometimes not ("zensar" vs
#: "zensartechnologies"), so both are tried.
FILLER = re.compile(
    r"\b(india|in india|technologies|technology|tech|solutions|services|"
    r"software|systems|global|group|labs|consulting|international|infotech|"
    r"infosystems|the)\b", re.I)
#: Boards that answer with jobs but are not the employer: aggregators and
#: recruiters posting for unnamed clients. Their "apply" is not the company's.
NOT_EMPLOYERS = {"jobgether", "careerbloc", "applicantz", "hralways",
                 "stepsworthsconsultancyservices", "expertrons",
                 "allegisglobalsolutions"}

#: Names that are a category, not an employer: no board to find.
NOT_A_COMPANY = re.compile(r"^(confidential|financial services|hiring|"
                           r"company|client|jobs?|recruit\w*)$", re.I)


def slugs(name: str) -> list[str]:
    """Up to three guesses for a company's board slug, likeliest first."""
    base = re.sub(r"\(.*?\)", " ", name).replace("&", " ")
    base = LEGAL.sub(" ", base)
    words = re.findall(r"[a-z0-9]+", base.lower())
    core = re.findall(r"[a-z0-9]+", FILLER.sub(" ", " ".join(words)))
    out = ["".join(words), "".join(core), "-".join(words)]
    if len(core) > 1:
        out.append(core[0])        # "Zeta Suite" -> "zeta"; tried last
    seen: list[str] = []
    for s in out:
        if len(s) >= 3 and s not in seen:
            seen.append(s)
    return seen[:3]


def companies(conn: sqlite3.Connection, skip: set[str]) -> list[str]:
    """Employers named by Adzuna and LinkedIn, most jobs first, so a run cut
    short has already asked about the ones that matter most."""
    rows = conn.execute(
        "SELECT company, COUNT(*) n FROM jobs WHERE source IN ('adzuna','linkedin') "
        "AND company != '' GROUP BY lower(company) ORDER BY n DESC").fetchall()
    return [r[0].strip() for r in rows
            if r[0].strip().lower() not in skip
            and not NOT_A_COMPANY.match(r[0].strip())]


# --- one light probe per platform -------------------------------------------
# Each returns None for "no board", or what the board holds. Anything but 200
# or 404 raises, and the caller counts it as a refusal.

class Refused(RuntimeError):
    pass


def _check(r: httpx.Response) -> bool:
    if r.status_code == 404:
        return False
    if r.status_code != 200:
        raise Refused(f"http {r.status_code}")
    return True


def _tally(places: list[str], remote_flags: list[bool]) -> dict:
    return {"jobs": len(places),
            "india": sum(bool(INDIA_RE.search(p or "")) for p in places),
            "remote": sum(f or bool(REMOTE_RE.search(p or ""))
                          for p, f in zip(places, remote_flags))}


async def greenhouse(client, slug):
    r = await client.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    if not _check(r):
        return None
    jobs = r.json().get("jobs") or []
    places = [(j.get("location") or {}).get("name") or "" for j in jobs]
    out = _tally(places, [False] * len(jobs))
    out["name"] = (jobs[0].get("company_name") if jobs else None)
    return out


async def lever(client, slug):
    r = await client.get(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    if not _check(r):
        return None
    jobs = r.json() if isinstance(r.json(), list) else []
    # Lever's `country` is a code; "IN" alone would not match the pattern.
    places = [" ".join(filter(None, [(j.get("categories") or {}).get("location"),
                                     "India" if j.get("country") == "IN" else ""]))
              for j in jobs]
    return {**_tally(places, [j.get("workplaceType") == "remote" for j in jobs]),
            "name": None}


async def ashby(client, slug):
    r = await client.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    if not _check(r):
        return None
    jobs = r.json().get("jobs") or []
    places = []
    for j in jobs:
        addr = ((j.get("address") or {}).get("postalAddress") or {})
        extra = " ".join(s.get("location", "") for s in j.get("secondaryLocations") or [])
        places.append(" ".join(filter(None, [j.get("location"), extra,
                                             addr.get("addressCountry")])))
    return {**_tally(places, [bool(j.get("isRemote")) for j in jobs]), "name": None}


async def workable(client, slug):
    r = await client.get(f"https://apply.workable.com/api/v1/widget/accounts/{slug}")
    if not _check(r):
        return None
    d = r.json()
    jobs = d.get("jobs") or []
    places = [" ".join(filter(None, [j.get("city"), j.get("country")])) for j in jobs]
    remote = [str(j.get("telecommuting")).lower() == "true" for j in jobs]
    return {**_tally(places, remote), "name": d.get("name")}


async def smartrecruiters(client, slug):
    """Answers 200 with zero postings for any name at all, so "live" here is
    "has postings", and it asks for Indian ones directly."""
    base = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
    r = await client.get(base, params={"limit": 1, "country": "in"})
    if not _check(r):
        return None
    d = r.json()
    india = int(d.get("totalFound") or 0)
    if not india:
        return None
    name = ((d.get("content") or [{}])[0].get("company") or {}).get("name")
    return {"jobs": india, "india": india, "remote": 0, "name": name}


PROBES = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby,
          "workable": workable, "smartrecruiters": smartrecruiters}


# --- the run -----------------------------------------------------------------

def done(path: Path) -> set[tuple[str, str]]:
    """(platform, slug) pairs already answered, from an earlier run."""
    if not path.exists():
        return set()
    out = set()
    for line in path.read_text().splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("status") in ("live", "none"):
            out.add((d["platform"], d["slug"]))
    return out


async def _platform(platform: str, targets: list[tuple[str, str]], path: Path,
                    pause: float, transport=None) -> dict:
    fn = PROBES[platform]
    stats = {"probed": 0, "live": 0, "stopped": None}
    refusals = 0
    async with httpx.AsyncClient(timeout=TIMEOUT, headers={"User-Agent": UA},
                                 follow_redirects=True, transport=transport) as client:
        for company, slug in targets:
            await asyncio.sleep(pause)
            try:
                found = await fn(client, slug)
                refusals = 0
            except (Refused, httpx.HTTPError, ValueError) as exc:
                refusals += 1
                if refusals >= STOP_AFTER:
                    stats["stopped"] = f"{type(exc).__name__}: {exc}"[:100]
                    break
                continue
            stats["probed"] += 1
            rec = {"platform": platform, "company": company, "slug": slug,
                   "status": "live" if found and found["jobs"] else "none"}
            if found:
                rec.update(found)
                stats["live"] += rec["status"] == "live"
            with path.open("a") as f:
                f.write(json.dumps(rec) + "\n")
    return stats


async def run(names: list[str], path: Path, *, platforms: list[str] | None = None,
              pause: float = PAUSE, transport=None) -> dict:
    """Probe every slug guess for every name on every platform. Resumable."""
    platforms = platforms or list(PROBES)
    skip = done(path)
    tasks = {}
    for p in platforms:
        # One slug is one board: two companies that guess the same slug are
        # asked about once.
        targets, asked = [], set()
        for n in names:
            for s in slugs(n):
                if (p, s) not in skip and s not in asked:
                    asked.add(s)
                    targets.append((n, s))
        tasks[p] = _platform(p, targets, path, pause, transport)
    results = await asyncio.gather(*tasks.values())
    return dict(zip(tasks, results))


def live(path: Path) -> list[dict]:
    """Every board that answered with jobs, best first."""
    if not path.exists():
        return []
    rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    rows = [r for r in rows if r.get("status") == "live"]
    return sorted(rows, key=lambda r: (-(r.get("india") or 0), -r["jobs"]))


OUT = Path(__file__).resolve().parent / "discovered.jsonl"
STOPWORDS = {"the", "and", "of", "india", "private", "limited", "pvt", "ltd",
             "inc", "llc", "llp", "group", "technologies", "technology",
             "solutions", "services", "software", "global", "co", "company"}


def same_company(ours: str, theirs: str | None) -> bool:
    """A guessed slug can land on a different firm with the same word
    ("beyond" was Beyond Meat, not Beyond Border Consultants). When the board
    names itself, every distinctive word of the shorter name must be in the
    longer, or the two must match once spaces go ("AccorHotel", "Accor
    Hotel"). When it does not name itself (Lever, Ashby), the Indian roles it
    holds are the evidence, and the added list is read by a person."""
    if not theirs:
        return True
    words = lambda s: {w for w in re.findall(r"[a-z0-9]+", s.lower())   # noqa: E731
                       if w not in STOPWORDS and len(w) > 1}
    a, b = words(ours), words(theirs)
    short, long_ = sorted((a, b), key=len)
    if short and short <= long_:
        return True
    squash = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())              # noqa: E731
    x, y = squash(ours), squash(theirs)
    return bool(x and y) and (x.startswith(y) or y.startswith(x))
