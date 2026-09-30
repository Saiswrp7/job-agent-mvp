"""The company list: which employer boards the engine re-reads.

One table (`boards`), three ways in:

- **engine/boards.json**: the hand-typed and discovered boards, with their
  tags (type, stage, industry). `sync_json` copies it in; `cli.py discover
  --add` still writes there, so a discovered board reaches the engine on the
  next start.
- **the free ats-scrapers dataset** (MIT): every employer it lists with a job
  in India, matched to its careers-site address. `pull` writes the list to
  engine/dataset_boards.csv (the evidence, and what tests read); `seed` adds
  it. Rerun weekly: new employers are added, nothing is removed.
- the engine itself retires a board whose site answers 404 read after read.

Staffing firms are left out: their apply goes to a recruiter, not the employer.
"""

from __future__ import annotations

import csv
import json
import re
import sqlite3
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .adapters import kind_of
from .ats_bridge import ALLOWED as BRIDGE_ATS

BOARDS_JSON = Path(__file__).resolve().parent / "boards.json"
DATASET_BOARDS = Path(__file__).resolve().parent / "dataset_boards.csv"
DATASET = "https://storage.stapply.ai/jobhive/v1"

#: Read by our own adapters (engine/adapters.py), the rest by the library.
NATIVE_ATS = frozenset({"greenhouse", "lever", "ashby", "workable", "smartrecruiters"})
DATASET_ATS = NATIVE_ATS | BRIDGE_ATS

#: Recruiters, staffing firms and hiring marketplaces seen posting India jobs
#: under their own board. Names as the dataset spells them, lowercased.
STAFFING = frozenset({"webleetechnologies", "squircleitconsultingservicespvtltd",
                      "weekdayai"})

#: Oracle sites are named by a tenant code ("Hcbt", "Fa Ewjt Saasfaprod1"),
#: which is what a person would be shown. The employer, where the site's own
#: job texts name it (read 2026-09-29; a code stays a code when they do not).
NAMES = {
    "hcbt.fa.em2.oraclecloud.com": "Kotak Mahindra Bank",
    "fa-ewjt-saasfaprod1.fa.ocs.oraclecloud.com": "EXL",
    "iaceiz.fa.ocs.oraclecloud.com": "Digitide Solutions",
    "ejwl.fa.us2.oraclecloud.com": "Marriott",
    "ekjy.fa.em2.oraclecloud.com": "Essar",
    "efet.fa.us2.oraclecloud.com": "Hilton",
    "fa-etvl-saasfaprod1.fa.ocs.oraclecloud.com": "Zensar Technologies",
    "fa-espx-saasfaprod1.fa.ocs.oraclecloud.com": "Cummins",
    "eeih.fa.us2.oraclecloud.com": "The Hackett Group",
    "emit.fa.ca3.oraclecloud.com": "WSP",
    "egup.fa.us2.oraclecloud.com": "Vertiv",
    "hdpc.fa.us2.oraclecloud.com": "Goldman Sachs",
    "edbz.fa.us2.oraclecloud.com": "Texas Instruments",
    "fa-etqo-saasfaprod1.fa.ocs.oraclecloud.com": "Hexaware",
    "fa-euxc-saasfaprod1.fa.ocs.oraclecloud.com": "Citco",
    "iadugs.fa.ocs.oraclecloud.com": "Legrand",
    "emcm.fa.us2.oraclecloud.com": "WM",
    "fa-evax-saasfaprod1.fa.ocs.oraclecloud.com": "IHG Hotels",
}
#: Sites that list placeholder jobs ("External Short Description English 1").
SKIP_HOSTS = frozenset({"eubt.fa.us6.oraclecloud.com"})

_GENERIC = re.compile(r"^(career site|careers?( inc)?|jobs|candidate experience site)$", re.I)
_TIDY = [re.compile(p, re.I) for p in (
    r"^careers? at\s+", r"\s*[:|\-–]\s*search jobs.*$", r"\s+job opportunities$",
    r"\s*\([^)]*careers? site\)$", r"\s+careers?( site)?$")]


def clean_name(name: str | None) -> str:
    """'Careers at EY: search jobs' -> 'EY'. Leaves a real name alone."""
    out = (name or "").strip()
    for p in _TIDY:
        out = p.sub("", out).strip()
    return out


def _host(url: str) -> str:
    return urlparse(url or "").netloc.lower()


def pick_name(ats: str, dataset_name: str, list_name: str, slug: str) -> str:
    """The name a person is shown for this board's jobs."""
    host = _host(slug)
    if host in NAMES:
        return NAMES[host]
    mine, theirs = clean_name(dataset_name), clean_name(list_name)
    code = ((ats == "oracle" and mine.lower().replace(" ", "-") in (host.split(".")[0], host))
            or bool(re.fullmatch(r"[\w-]+(\.[\w-]+)+", mine)))      # careers.statestreet.com
    if code or not mine or _GENERIC.match(mine):
        if theirs and not _GENERIC.match(theirs) \
                and theirs.lower().replace(" ", "-") != host.split(".")[0]:
            return theirs
    return mine or theirs or dataset_name
STAFFING_RE = re.compile(r"staffing|placements?\b|manpower|\brecruit(ment|ers|ing)\b"
                         r"|\bhr (services|solutions)\b", re.I)


def is_staffing(company: str, slug: str = "") -> bool:
    from .discover import NOT_EMPLOYERS        # discover imports sync, sync imports us
    low = (company or "").lower().replace(" ", "")
    return (low in STAFFING or (slug or "").lower() in NOT_EMPLOYERS
            or bool(STAFFING_RE.search(company or "")))


# --- boards.json ---------------------------------------------------------------

def sync_json(conn: sqlite3.Connection, path: Path = BOARDS_JSON) -> dict:
    """Copy boards.json into the table, and tie the jobs already stored to
    their board (rows from before the engine have no `board`)."""
    boards = json.loads(path.read_text())
    added = 0
    for b in boards:
        tags = {k: b[k] for k in ("type", "stage", "industry", "size", "country")
                if b.get(k)}
        cur = conn.execute(
            "INSERT INTO boards (source, slug, company, tags, found_by, enabled) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(source, slug) DO UPDATE SET "
            "company = excluded.company, tags = excluded.tags, "
            "enabled = excluded.enabled",
            (b["source"], b["slug"], b["company"], json.dumps(tags) if tags else None,
             b.get("found_by") or "boards.json", 1 if b.get("enabled", True) else 0))
        added += cur.rowcount
        conn.execute("UPDATE jobs SET board = ? WHERE source = ? AND company = ? "
                     "AND board IS NULL", (b["slug"], b["source"], b["company"]))
    backfill_kinds(conn)
    conn.commit()
    return {"boards": len(boards), "written": added}


def backfill_kinds(conn: sqlite3.Connection) -> int:
    """`apply_kind` for rows stored before it existed. Once; a no-op after."""
    rows = conn.execute("SELECT source, source_id, apply_url, url FROM jobs "
                        "WHERE apply_kind IS NULL").fetchall()
    conn.executemany("UPDATE jobs SET apply_kind = ? WHERE source = ? AND source_id = ?",
                     [(kind_of(r["source"], r["apply_url"] or r["url"]),
                       r["source"], r["source_id"]) for r in rows])
    return len(rows)


# --- the dataset -----------------------------------------------------------------

def _site_key(ats: str, url: str) -> tuple[str, str]:
    """What identifies one careers site within an ATS, from any URL on it."""
    p = urlparse(url or "")
    host = p.netloc.lower()
    segs = [s for s in p.path.split("/") if s and not re.fullmatch(r"[a-z]{2}-[A-Za-z]{2}", s)]
    if ats == "oracle":
        site = (parse_qs(p.query).get("site_number") or [""])[0]
        m = re.search(r"/sites/([^/?#]+)", p.path)
        return host, (site or (m.group(1) if m else "")).lower()
    if ats == "workday":
        return host, (segs[0].lower() if segs else "")
    if ats in NATIVE_ATS:
        # jobs.smartrecruiters.com and careers.smartrecruiters.com are one site.
        return ats, (segs[0].lower() if segs else "")
    return host, ""


def slug_for(ats: str, row: dict) -> str | None:
    """What the reader for `ats` needs, from a dataset company row (the rules
    of ats-scrapers' own pipeline, for the ATSs we read)."""
    slug = (row.get("slug") or "").strip()
    url = (row.get("url") or "").strip()
    name = (row.get("name") or "").strip()
    if ats in ("workday", "keka", "phenom"):
        return url or None
    if ats == "oracle":
        if not url.startswith("http"):
            return url or None
        # The API is at the host root and takes the site separately (and the
        # site number keeps its case: CX_1001).
        p = urlparse(url)
        m = re.search(r"/sites/([^/?#]+)", p.path)
        site = ((parse_qs(p.query).get("site_number") or [""])[0]
                or (m.group(1) if m else ""))
        base = f"{p.scheme}://{p.netloc}"
        return f"{base}?site_number={site}" if site else base
    if ats == "successfactors":
        return url.rstrip("/") or slug or None
    if ats == "icims":
        host = (urlparse(url).hostname or "").lower()
        m = re.fullmatch(r"careers-([a-z0-9-]+)\.icims\.com", host)
        if m:
            return m.group(1)
        if host.endswith(".icims.com"):
            return url.split("?", 1)[0].rstrip("/")
        return slug or name or None
    if ats in ("cornerstone", "dayforce", "ukg", "paycom"):
        return slug or url or None
    if ats in ("greenhouse", "ashby", "rippling"):
        return slug.lower() or _site_key(ats, url)[1] or None
    if ats == "lever":
        m = re.match(r"https?://jobs\.lever\.co/([^/?#]+)", url, re.I)
        return (m.group(1) if m else slug) or None
    if ats == "recruitee":
        m = re.match(r"https?://([a-z0-9][a-z0-9-]+)\.recruitee\.com", url, re.I)
        return (slug.lower() if slug else (m.group(1).lower() if m else None))
    return slug or name or url or None


INDIA_SQL = (r"(country_iso = 'IN' OR regexp_matches(coalesce(location, ''), "
             r"'(?i)\bindia\b|bengaluru|bangalore|mumbai|\bdelhi\b|gurgaon|gurugram|"
             r"noida|hyderabad|\bpune\b|chennai|kolkata|ahmedabad|jaipur|kochi'))")


def pull(out: Path = DATASET_BOARDS, *, dataset: str = DATASET) -> dict:
    """Every dataset employer with an India job, matched to its careers site.

    Reads the public parquet with DuckDB (the location column of 5M rows,
    about two minutes) and the 80k-row company list. Writes one CSV line per
    board. Needs the network; `seed` does not.
    """
    import duckdb
    con = duckdb.connect()
    con.execute("SET enable_progress_bar = false; INSTALL httpfs; LOAD httpfs;")
    allowed = ", ".join(f"'{a}'" for a in sorted(DATASET_ATS))
    india = con.execute(
        f"SELECT ats_type, company, any_value(url) AS job_url, count(*) AS n "
        f"FROM read_parquet('{dataset}/all.parquet') "
        f"WHERE ats_type IN ({allowed}) AND {INDIA_SQL} GROUP BY 1, 2").fetchall()
    companies = con.execute(
        f"SELECT ats, name, slug, url FROM read_csv_auto('{dataset}/companies.csv', "
        f"all_varchar = true) WHERE ats IN ({allowed})").fetchall()
    return match(india, companies, out)


def match(india: list[tuple], companies: list[tuple], out: Path) -> dict:
    """Pair each India employer with a row of the company list: by careers
    site, then by host, then by exact name. Unmatched ones are counted, not
    guessed."""
    by_site: dict[tuple, list[dict]] = {}
    by_host: dict[tuple, list[dict]] = {}
    by_name: dict[tuple, list[dict]] = {}
    for ats, name, slug, url in companies:
        row = {"ats": ats, "name": name, "slug": slug, "url": url}
        host, site = _site_key(ats, url or "")
        by_site.setdefault((ats, host, site), []).append(row)
        by_host.setdefault((ats, host), []).append(row)
        by_name.setdefault((ats, (name or "").strip().lower()), []).append(row)

    written, unmatched, staffing = [], 0, 0
    seen: set[tuple[str, str]] = set()
    for ats, company, job_url, n in sorted(india, key=lambda r: -r[3]):
        host, site = _site_key(ats, job_url or "")
        hit = None
        for cands in (by_site.get((ats, host, site)), by_host.get((ats, host)),
                      by_name.get((ats, (company or "").strip().lower()))):
            if cands and len(cands) == 1:
                hit = cands[0]
                break
        if hit is None:
            unmatched += 1
            continue
        slug = slug_for(ats, hit)
        if not slug or (ats, slug) in seen or _host(slug) in SKIP_HOSTS:
            continue
        name = pick_name(ats, company, hit["name"], slug)
        if is_staffing(name, slug) or is_staffing(company, slug):
            staffing += 1
            continue
        seen.add((ats, slug))
        written.append({"source": ats, "slug": slug, "company": name.strip(),
                        "india_jobs": n})
    with out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["source", "slug", "company", "india_jobs"])
        w.writeheader()
        w.writerows(written)
    return {"employers": len(india), "boards": len(written),
            "unmatched": unmatched, "staffing": staffing}


def seed(conn: sqlite3.Connection, path: Path = DATASET_BOARDS) -> dict:
    """Add the dataset's India boards. Existing boards keep everything but
    their name, which follows the CSV (so a name fixed in NAMES reaches the
    board, and its jobs on their next read)."""
    added = renamed = 0
    with path.open() as f:
        for r in csv.DictReader(f):
            if (r["source"] not in DATASET_ATS or is_staffing(r["company"], r["slug"])
                    or _host(r["slug"]) in SKIP_HOSTS):
                continue
            added += conn.execute(
                "INSERT OR IGNORE INTO boards (source, slug, company, found_by) "
                "VALUES (?, ?, ?, 'dataset')",
                (r["source"], r["slug"], r["company"])).rowcount
            renamed += conn.execute(
                "UPDATE boards SET company = ? WHERE source = ? AND slug = ? "
                "AND found_by = 'dataset' AND company != ?",
                (r["company"], r["source"], r["slug"], r["company"])).rowcount
    conn.commit()
    return {"added": added, "renamed": renamed}


def counts(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT source, COUNT(*) AS boards, SUM(retired_at IS NOT NULL) AS retired, "
        "SUM(last_ok IS NOT NULL) AS read_ok, SUM(fail_streak > 0) AS failing "
        "FROM boards GROUP BY source ORDER BY boards DESC")]
