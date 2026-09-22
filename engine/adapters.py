"""One fetch function per ATS. Each returns normalized job dicts.

Both endpoints return every job *with* its full description in a single
request. That is the whole performance story — without `content=true` on
Greenhouse you would need one request per job.
"""

from __future__ import annotations

import hashlib
import html
import re
from datetime import UTC, datetime

import httpx

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
    return (head or None), remote


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
         department=None, employment_type=None, salary=None, meta=None) -> dict:
    city, remote = norm_city(location)
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


ADAPTERS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby}
