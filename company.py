"""What a company is, for "what is Konovo?" in the chat.

Sai asked it in the middle of applying and the bot had nothing to say
(2026-10-01). Two sources, in this order, and the answer may only use them:

1. **Their own job post**, already in the jobs table. Most carry an "About us"
   paragraph, it costs nothing, and it is what the company says of itself.
2. **The web**, through Browserbase (Search, then Fetch of the company's own
   site as markdown). Both are included in the $20 plan (1,000 calls each a
   month). LinkedIn and job boards are skipped: LinkedIn walls a script out and
   a job board says nothing a job post did not.

Never from memory: a model that "knows" a small company is how a wrong fact
ends up in a cover letter.
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys

import httpx

API = "https://api.browserbase.com/v1"
#: Not the company's own page: walls, boards and aggregators.
_ELSEWHERE = re.compile(r"linkedin\.|glassdoor\.|indeed\.|naukri\.|ambitionbox\.|greenhouse\.io|"
                        r"lever\.co|ashbyhq\.|smartrecruiters\.|workable\.|keka\.com|crunchbase\.|"
                        r"wikipedia\.|facebook\.|instagram\.|x\.com|twitter\.|youtube\.|exa\.ai", re.I)
_ABOUT = re.compile(r"\b(about (us|the company|the team|{name})|who we are|we are|is (a|an|the) "
                    r"|founded|our mission)\b", re.I)


def from_job_post(company: str, conn: sqlite3.Connection) -> str:
    """A paragraph of their own job posts that names the company and says what
    it is. Only paragraphs that name it: CRED's newest post was about Prefr, a
    lender posting on CRED's board, and its "about" paragraph described Prefr
    (2026-10-01)."""
    name = company.strip()
    try:
        rows = conn.execute(
            "SELECT description FROM jobs WHERE lower(company) = lower(?) AND description IS NOT NULL "
            "ORDER BY posted_at DESC LIMIT 10", (name,)).fetchall()
    except sqlite3.Error:
        return ""
    named = re.compile(rf"\b{re.escape(name)}\b", re.I)
    about = re.compile(_ABOUT.pattern.replace("{name}", re.escape(name)), re.I)
    for (text,) in rows:
        paras = [p.strip() for p in re.split(r"\n\s*\n|(?<=\.)\s{2,}", text or "") if p.strip()]
        # The name in a link ("medium.com/swiggy-bytes") or an equal-opportunity
        # line says nothing about what the company does.
        picked = [p for p in paras
                  if named.search(re.sub(r"https?://\S+", "", p)) and about.search(p)
                  and not re.search(r"equal opportunity|without regard to", p, re.I)][:2]
        if picked:
            return " ".join(picked)[:900]
    return ""


def _web(company: str, timeout: float = 25) -> tuple[str, str]:
    """(text, url) from the company's own site, or ("", "")."""
    key = os.environ.get("BROWSERBASE_API_KEY")
    if not key or "pytest" in sys.modules or os.environ.get("JOB_AGENT_LIVE") == "0":
        return "", ""
    h = {"x-bb-api-key": key, "Content-Type": "application/json"}
    try:
        r = httpx.post(f"{API}/search", headers=h, timeout=timeout,
                       json={"query": f"{company} company", "numResults": 6})
        results = r.json().get("results") or [] if r.status_code == 200 else []
    except Exception as exc:                          # noqa: BLE001
        print(f"[company] search failed: {exc}")
        return "", ""
    own = [x["url"] for x in results if x.get("url") and not _ELSEWHERE.search(x["url"])]
    for url in own[:2]:
        try:
            f = httpx.post(f"{API}/fetch", headers=h, timeout=timeout,
                           json={"url": url, "allowRedirects": True, "format": "markdown"})
            if f.status_code != 200:
                continue
            text = re.sub(r"!\[[^\]]*\]\([^)]*\)|\[([^\]]*)\]\([^)]*\)", r"\1", f.json().get("content") or "")
            # From the first heading on: what is above it is the site's menu.
            head = re.search(r"(?m)^#{1,3} ", text)
            text = re.sub(r"\s+", " ", text[head.start():] if head else text).strip()
            if len(text) > 200:
                return text[:1500], url
        except Exception as exc:                      # noqa: BLE001
            print(f"[company] fetch failed for {url}: {exc}")
    return "", ""


def about(company: str, conn: sqlite3.Connection) -> str:
    """What the chat agent answers from. Says plainly when there is nothing."""
    company = (company or "").strip()
    if not company:
        return "ERROR: which company?"
    post = from_job_post(company, conn)
    web, url = _web(company)
    if not post and not web:
        return (f"Nothing found about {company}: not in their job posts, and no site of "
                f"their own came up. Say you could not find anything on them; do not "
                f"describe them from memory.")
    parts = []
    if post:
        parts.append(f"From {company}'s own job post: {post}")
    if web:
        parts.append(f"From their website ({url}): {web}")
    return ("\n\n".join(parts)
            + "\n\nAnswer in 1-2 lines from this text only, in your own words, and say where "
              "it is from (their job post / their site). Nothing from memory.")
