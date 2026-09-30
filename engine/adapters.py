"""One fetch function per ATS. Each returns normalized job dicts.

Both endpoints return every job *with* its full description in a single
request. That is the whole performance story — without `content=true` on
Greenhouse you would need one request per job.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import re
from datetime import UTC, datetime, timedelta

import httpx

from .db import DAYS as WINDOW_DAYS

# Indian metro spellings that boards use interchangeably. Normalized on write so
# queries stay pure SQL.
CITY_ALIASES = {
    "bengaluru": "Bangalore", "bangalore": "Bangalore", "blr": "Bangalore",
    "gurugram": "Gurgaon", "gurgaon": "Gurgaon",
    "mumbai": "Mumbai", "bombay": "Mumbai",
    "new delhi": "Delhi", "delhi": "Delhi", "noida": "Noida",
    "hyderabad": "Hyderabad", "pune": "Pune", "chennai": "Chennai",
    "kolkata": "Kolkata", "ahmedabad": "Ahmedabad", "jaipur": "Jaipur",
}

COUNTRY_ONLY = {"india", "in", "bharat"}

REMOTE_RE = re.compile(r"\bremote\b|\bwork from home\b|\banywhere\b", re.I)
SENIORITY_RE = [
    (re.compile(r"\b(vp|vice president|head of|director)\b", re.I), "head"),
    (re.compile(r"\b(principal|staff|lead)\b", re.I), "lead"),
    (re.compile(r"\b(sr\.?|senior)\b", re.I), "senior"),
    (re.compile(r"\b(associate|junior|jr\.?|intern|trainee|graduate)\b", re.I), "junior"),
]
YEARS_RE = re.compile(
    r"(\d{1,2})\s*(?:\+|-|–|to)?\s*(\d{1,2})?\s*(?:\+)?\s*years?", re.I
)
TAG_RE = re.compile(r"<[^>]+>")


#: The most one request may take, start to finish. httpx's timeout is per
#: step, so a stalled connection once sat for 15 minutes under a 20 s setting.
REQUEST_LIMIT = 30.0


async def get_once_more(client: httpx.AsyncClient, url: str, **kw) -> httpx.Response:
    """GET, and on a dropped connection try exactly once more.

    A dropped connection is not the site saying "slow down" — that answer comes
    as a status code (429, 999), and the callers still stop on it at once. On
    2026-09-25, 3 of 15 LinkedIn fetches from this Mac died with ReadError and
    all 3 worked on an immediate retry; before this, each one ended the whole
    run (LinkedIn added 0 jobs twice, Adzuna stopped after 6-7 calls twice).
    A second drop in a row still stops the run.
    """
    try:
        return await asyncio.wait_for(client.get(url, **kw), REQUEST_LIMIT)
    except (httpx.TransportError, asyncio.TimeoutError):
        await asyncio.sleep(2)
        return await asyncio.wait_for(client.get(url, **kw), REQUEST_LIMIT)


def strip_html(raw: str | None) -> str:
    if not raw:
        return ""
    text = html.unescape(raw)
    text = re.sub(r"<(br|/p|/div|/li)[^>]*>", "\n", text, flags=re.I)
    text = TAG_RE.sub(" ", text)
    text = html.unescape(text)
    return re.sub(r"[ \t]+", " ", text).strip()


def norm_city(location: str | None) -> tuple[str | None, int]:
    """(city, remote_flag). Both derived once, at write time."""
    if not location:
        return None, 0
    remote = 1 if REMOTE_RE.search(location) else 0
    low = location.lower()
    for alias, canonical in CITY_ALIASES.items():
        if re.search(rf"\b{re.escape(alias)}\b", low):
            return canonical, remote
    head = re.split(r"[,/|]", location)[0].strip()
    # "India" alone says where the country is, not the city. Stored as a city
    # it matched no search and was offered as "another city" to try.
    if head.lower() in COUNTRY_ONLY:
        return None, remote
    return (head or None), remote


INDIA_RE = re.compile(
    r"\bindia\b|bengaluru|bangalore|mumbai|\bdelhi\b|gurgaon|gurugram|noida|"
    r"hyderabad|\bpune\b|chennai|kolkata|ahmedabad|jaipur|kochi|chandigarh|"
    r"coimbatore|indore|thiruvananthapuram|trivandrum|mysore|mysuru|vadodara|"
    r"nagpur|lucknow|bhubaneswar|visakhapatnam|\bthane\b|navi mumbai|\bblr\b", re.I)
#: Remote that says who may take it, when it says anyone.
OPEN_REMOTE_RE = re.compile(r"anywhere|global|worldwide|\bapac\b|\basia\b", re.I)


def in_scope(location: str | None, country_iso: str | None = None) -> bool:
    """India first: a job in India, or remote and open to someone here.

    "Remote" alone counts (it names nobody it excludes); "Remote - US" does
    not. A board's feed lists every country; only these rows are kept.
    """
    if (country_iso or "").upper() == "IN":
        return True
    text = location or ""
    if INDIA_RE.search(text):
        return True
    if REMOTE_RE.search(text):
        rest = REMOTE_RE.sub("", text).strip(" -,/|()")
        return not rest or bool(OPEN_REMOTE_RE.search(text))
    return False


#: What the apply button leads to, by ATS. `form`: an application form anyone
#: can fill. `account`: sign up or sign in first (Workday, Oracle, SAP
#: SuccessFactors...). The click test (research/engine) is what these rest on.
FORM_ATS = frozenset({"greenhouse", "lever", "ashby", "workable", "smartrecruiters",
                      "keka", "recruitee", "teamtailor", "breezy"})
ACCOUNT_ATS = frozenset({"workday", "oracle", "successfactors", "phenom", "icims",
                         "amazon", "taleo", "cornerstone", "ukg", "dayforce",
                         "paycom", "jobvite"})
#: An apply link on one of these is not the employer's own form.
JOB_BOARD_RE = re.compile(
    r"naukri\.|linkedin\.|indeed\.|foundit\.|monster\.|instahyre\.|iimjobs\.|"
    r"hirist\.|glassdoor\.|adzuna\.|shine\.com|timesjobs\.", re.I)


def kind_of(source: str, apply_url: str | None) -> str:
    """form | account | link. `link` is never applied to by the agent."""
    url = apply_url or ""
    if not url.startswith("http") or JOB_BOARD_RE.search(url):
        return "link"
    if source in FORM_ATS:
        return "form"
    if source in ACCOUNT_ATS:
        return "account"
    return "link"


def seniority_of(title: str) -> str | None:
    for pattern, label in SENIORITY_RE:
        if pattern.search(title):
            return label
    return None


def years_of(text: str) -> tuple[int | None, int | None]:
    """Explicit year ranges only. Most postings have none — that's fine, a
    missing value is neutral at query time, never a mismatch."""
    window = text[:4000]
    for m in YEARS_RE.finditer(window):
        lo = int(m.group(1))
        hi = int(m.group(2)) if m.group(2) else None
        if lo > 20 or (hi and hi > 30):
            continue
        return lo, hi
    return None, None


def content_hash(company: str, title: str, location: str, body: str) -> str:
    """Hash the normalized tuple, never raw HTML — cosmetic source noise
    (tracking pixels, reordered keys) would otherwise look like a real change."""
    blob = f"{company}|{title}|{location}|{body.strip()}"
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _row(*, source, source_id, company, title, location, description,
         url, apply_url=None, posted_at=None, updated_at=None,
         department=None, employment_type=None, salary=None, meta=None,
         board=None, apply_kind=None) -> dict:
    city, remote = norm_city(location)
    description = description or ""
    ymin, ymax = years_of(description)
    meta = meta or {}
    return {
        "source": source,
        "source_id": str(source_id),
        "company": company,
        "title": title,
        "department": department,
        "employment_type": employment_type,
        "location": location,
        "city": city,
        "remote": remote,
        "salary": salary,
        "description": description,
        "url": url,
        "apply_url": apply_url or url,
        "posted_at": posted_at,
        "updated_at": updated_at,
        "company_type": meta.get("type"),
        "stage": meta.get("stage"),
        "industry": meta.get("industry"),
        "size": meta.get("size"),
        "seniority": seniority_of(title),
        "years_min": ymin,
        "years_max": ymax,
        "content_hash": content_hash(company, title, location or "", description),
        "board": board,
        "apply_kind": apply_kind or kind_of(source, apply_url or url),
    }


def _ts(ms: int | None) -> str | None:
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, UTC).isoformat(timespec="seconds")


# --------------------------------------------------------------------------


async def greenhouse(client: httpx.AsyncClient, slug: str, company: str,
                     meta: dict) -> list[dict]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    r = await client.get(url)
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        body = strip_html(j.get("content"))
        offices = j.get("offices") or []
        location = (j.get("location") or {}).get("name") or (
            offices[0].get("name") if offices else None)
        depts = j.get("departments") or []
        out.append(_row(
            source="greenhouse", source_id=j["id"], company=company,
            title=j.get("title", ""), location=location, description=body,
            url=j.get("absolute_url"), updated_at=j.get("updated_at"),
            posted_at=j.get("first_published") or j.get("updated_at"),
            department=depts[0]["name"] if depts else None, meta=meta,
        ))
    return out


async def lever(client: httpx.AsyncClient, slug: str, company: str,
                meta: dict) -> list[dict]:
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    r = await client.get(url)
    r.raise_for_status()
    out = []
    for j in r.json():
        cat = j.get("categories") or {}
        body = strip_html(j.get("descriptionPlain") or j.get("description"))
        lists = "\n".join(
            f"{b.get('text','')}\n{strip_html(b.get('content'))}"
            for b in (j.get("lists") or [])
        )
        out.append(_row(
            source="lever", source_id=j["id"], company=company,
            title=j.get("text", ""), location=cat.get("location"),
            description=f"{body}\n{lists}".strip(),
            url=j.get("hostedUrl"), apply_url=j.get("applyUrl"),
            posted_at=_ts(j.get("createdAt")), updated_at=_ts(j.get("createdAt")),
            department=cat.get("team"), employment_type=cat.get("commitment"),
            salary=(j.get("salaryRange") or {}).get("currency"), meta=meta,
        ))
    return out


async def ashby(client: httpx.AsyncClient, slug: str, company: str,
                meta: dict) -> list[dict]:
    url = (f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
           f"?includeCompensation=true")
    r = await client.get(url)
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        out.append(_row(
            source="ashby", source_id=j.get("id"), company=company,
            title=j.get("title", ""), location=j.get("location"),
            description=strip_html(j.get("descriptionHtml")
                                   or j.get("descriptionPlain")),
            url=j.get("jobUrl"), apply_url=j.get("applyUrl") or j.get("jobUrl"),
            posted_at=j.get("publishedAt"), updated_at=j.get("updatedAt"),
            department=j.get("department"), employment_type=j.get("employmentType"),
            salary=(j.get("compensation") or {}).get("summary"), meta=meta,
        ))
    return out


def _place(city: str | None, region: str | None, country: str | None,
           remote: bool) -> str | None:
    """One location string from the parts a board gives separately, so
    `norm_city` and the country rules read it like any other board's."""
    parts = [p for p in (city, region, country) if p]
    if remote:
        parts.append("Remote")
    return ", ".join(dict.fromkeys(parts)) or None


async def workable(client: httpx.AsyncClient, slug: str, company: str,
                   meta: dict) -> list[dict]:
    """Workable's public widget feed: the endpoint their own embeddable
    careers widget reads, documented in their help centre as needing no
    key. `details=true` puts every description in the one response."""
    url = f"https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true"
    r = await client.get(url)
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        remote = str(j.get("telecommuting")).lower() == "true"
        out.append(_row(
            source="workable", source_id=j["shortcode"], company=company,
            title=j.get("title", ""),
            location=_place(j.get("city"), j.get("state"), j.get("country"), remote),
            description=strip_html(j.get("description")),
            url=j.get("url") or j.get("shortlink"),
            apply_url=j.get("application_url"),
            posted_at=j.get("published_on") or j.get("created_at"),
            updated_at=j.get("published_on") or j.get("created_at"),
            department=j.get("department") or None,
            employment_type=j.get("employment_type") or None, meta=meta,
        ))
    return out


SR_API = "https://api.smartrecruiters.com/v1/companies/{slug}/postings"
#: Between detail requests. SmartRecruiters' list gives no description, so a
#: board costs one request per new posting, and they all hit one host.
SR_PAUSE = 0.3


async def smartrecruiters(client: httpx.AsyncClient, slug: str, company: str,
                          meta: dict) -> list[dict]:
    """SmartRecruiters' public Posting API (no key for public postings).

    The list is filtered to one country (`country` in boards.json, India by
    default): a global employer like Bosch has thousands of postings and
    these users can take only the Indian ones.

    The list has no description, so each posting needs its own request. A
    posting already stored with the same release date comes back marked
    `unchanged` instead: sync keeps it open without rewriting it, and the
    hourly run only pays for what is new.
    """
    country = (meta.get("country") or "in").lower()
    known = meta.get("known") or {}
    listed, offset = [], 0
    while True:
        r = await client.get(SR_API.format(slug=slug),
                             params={"limit": 100, "offset": offset,
                                     "country": country})
        r.raise_for_status()
        page = r.json()
        listed += page.get("content") or []
        offset += 100
        if offset >= int(page.get("totalFound") or 0) or not page.get("content"):
            break

    # Postings released before the window are listed (they are still open, so
    # they count as seen) but never read in full: nobody here is shown them.
    cutoff = (datetime.now(UTC) - timedelta(days=WINDOW_DAYS)).strftime("%Y-%m-%d")
    out = []
    for p in listed:
        pid, released = str(p["id"]), p.get("releasedDate")
        if known.get(pid) and known[pid] == released:
            out.append({"source": "smartrecruiters", "source_id": pid,
                        "unchanged": True})
            continue
        if released and released[:10] < cutoff:
            out.append({"source": "smartrecruiters", "source_id": pid,
                        "old": True})
            continue
        await asyncio.sleep(SR_PAUSE)
        d = await client.get(f"{SR_API.format(slug=slug)}/{pid}")
        d.raise_for_status()
        out.append(sr_row(d.json(), company, meta))
    return out


def sr_row(d: dict, company: str, meta: dict) -> dict:
    loc = d.get("location") or {}
    sections = (d.get("jobAd") or {}).get("sections") or {}
    body = "\n\n".join(
        f"{s.get('title') or ''}\n{strip_html(s.get('text'))}".strip()
        for key in ("jobDescription", "qualifications", "additionalInformation")
        if (s := sections.get(key)) and s.get("text"))
    return _row(
        source="smartrecruiters", source_id=d["id"], company=company,
        title=(d.get("name") or "").strip(),
        location=_place(loc.get("city"), loc.get("region"),
                        "India" if loc.get("country") == "in" else loc.get("country"),
                        bool(loc.get("remote"))),
        description=body, url=d.get("postingUrl"), apply_url=d.get("applyUrl"),
        posted_at=d.get("releasedDate"), updated_at=d.get("releasedDate"),
        department=(d.get("department") or {}).get("label")
        or (d.get("function") or {}).get("label"),
        employment_type=(d.get("typeOfEmployment") or {}).get("label"), meta=meta,
    )


ADAPTERS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby,
            "workable": workable, "smartrecruiters": smartrecruiters}
