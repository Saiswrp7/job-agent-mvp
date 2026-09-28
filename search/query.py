"""Filter JSON -> SQL. No model, no network.

Two rules carry the design:

- **Null means no clause.** Anything the user did not say drops out of the
  query instead of narrowing it. Under-filling is safe (the ranker cuts);
  over-filling silently deletes good jobs.
- **Skills order, they never exclude.** A JD saying "split testing" instead of
  "A/B test" should rank lower, not vanish. So `must_mention` lands in
  ORDER BY, never WHERE.
"""

from __future__ import annotations

import sqlite3

from engine import db

#: SQL asks for this many times the requested count, because the ranking call
#: cuts hard on criteria SQL cannot express ("B2C", "not CRM in disguise").
OVERFETCH = 4
MAX_ROWS = 40
#: Rows reserved for recency regardless of keyword score, so a job that phrases
#: everything unusually still gets seen.
RECENCY_SLOTS = 5

DEFAULTS = {
    "count": 5,
    "title_keywords": None,
    "city": None,
    "remote": None,
    "company_type": None,
    "industry": None,
    "stage": None,
    "seniority": None,
    "posted_within_days": None,
    "must_mention": None,
    "companies": None,
    # Label columns (engine/labels.py). Lists, because "product or growth" is
    # one ask. Unknown values are dropped in normalize, never passed to SQL.
    "role_family": None,
    "level": None,
    "country": None,
    "owns_pnl": None,
    "soft_criteria": "",
}

#: Kept in step with engine/labels.py — a filter value no job can carry would
#: silently match nothing.
FAMILIES = frozenset({
    "engineering", "data", "product", "design", "growth", "crm_lifecycle",
    "marketing", "sales", "business_development", "customer_success",
    "support", "operations", "supply_chain", "finance", "hr", "legal",
    "content", "research", "healthcare", "education", "admin", "other"})
LEVELS = frozenset({"intern", "junior", "mid", "senior", "lead", "leadership"})

#: A region someone names as one place, stored as the cities jobs carry.
CITY_GROUPS = {
    "Delhi NCR": ["Delhi", "New Delhi", "Gurgaon", "Noida", "Faridabad",
                  "Ghaziabad", "Delhi NCR"],
}


def normalize(filters: dict) -> dict:
    out = dict(DEFAULTS)
    out.update({k: v for k, v in (filters or {}).items() if k in DEFAULTS})
    if not isinstance(out["count"], int) or out["count"] < 1:
        out["count"] = 5
    out["count"] = min(out["count"], 20)
    for key, allowed in (("role_family", FAMILIES), ("level", LEVELS)):
        v = out[key]
        v = [v] if isinstance(v, str) else v if isinstance(v, list) else []
        v = [x.strip().lower() for x in v if isinstance(x, str)]
        out[key] = [x for x in v if x in allowed] or None
    if not isinstance(out["country"], str) or not out["country"].strip():
        out["country"] = None
    out["owns_pnl"] = True if out["owns_pnl"] is True else None
    return out


def _where(f: dict) -> tuple[list[str], list]:
    clauses = ["closed_at IS NULL"]
    params: list = []

    if f["city"]:
        # Remote counts as available in any city the user names. A region is
        # its cities: jobs are stored as Noida or Gurgaon, never "Delhi NCR".
        cities = CITY_GROUPS.get(f["city"], [f["city"]])
        clauses.append(f"(city IN ({', '.join('?' for _ in cities)}) OR remote = 1)")
        params.extend(cities)
    # Remote *and* a city is "Bangalore or remote" — the city clause above
    # already lets remote jobs in. Remote-only is remote with no city. Adding
    # both turned "remote or banagalroe" into remote-only: four of five results
    # were remote jobs in other cities.
    if f["remote"] and not f["city"]:
        clauses.append("remote = 1")
    # Company tags are hand-typed per company in boards.json, so most LinkedIn
    # jobs have none. Untagged is unknown, not a mismatch: it passes, and the
    # ranker reads the description and judges B2C for itself. Strict here would
    # hide nearly all of LinkedIn from "B2C companies".
    for col in ("company_type", "stage"):
        if f[col]:
            clauses.append(f"({col} = ? OR {col} IS NULL)")
            params.append(f[col])
    if f["seniority"]:
        clauses.append("seniority = ?")
        params.append(f["seniority"])
    if f["industry"]:
        clauses.append("industry LIKE ?")
        params.append(f"%{f['industry']}%")
    if f["companies"]:
        marks = ", ".join("?" for _ in f["companies"])
        clauses.append(f"company IN ({marks})")
        params.extend(f["companies"])
    if f["posted_within_days"]:
        clauses.append("posted_at > date('now', ?)")
        params.append(f"-{int(f['posted_within_days'])} days")
    # Role: the title OR the label, never the label alone. Labels are
    # sometimes wrong — a cheap model called "Data Product Manager" `data` — and
    # a wrong label must not hide a job whose title fits. The label's job is
    # the other direction: finding the Product Designer for someone who said
    # "UX", whose title the keyword would miss.
    role: list[str] = []
    if f["title_keywords"]:
        role += ["lower(title) LIKE ?" for _ in f["title_keywords"]]
        params.extend(f"%{k.lower()}%" for k in f["title_keywords"])
    if f["role_family"]:
        role.append(f"role_family IN ({', '.join('?' for _ in f['role_family'])})")
        params.extend(f["role_family"])
        if not f["title_keywords"]:
            role.append("role_family IS NULL")      # not labelled yet
    if role:
        clauses.append(f"({' OR '.join(role)})")
    # Level only filters when they said it. Their experience orders, see _fit.
    if f["level"]:
        clauses.append(f"(level IN ({', '.join('?' for _ in f['level'])}) "
                       f"OR level IS NULL)")
        params.extend(f["level"])
    if f["country"]:
        # "Global" is a remote job open to anyone, which includes them.
        clauses.append("(lower(country) = lower(?) OR country IS NULL "
                       "OR country = 'Global')")
        params.append(f["country"].strip())

    return clauses, params


def _score(f: dict) -> tuple[str, list]:
    """Keyword hits summed, plus the label matches that should sort a job up
    without being allowed to remove one. SQLite booleans are 0/1."""
    parts: list[str] = []
    params: list = []
    for t in f["must_mention"] or []:
        parts.append("(lower(description) LIKE ?)")
        params.append(f"%{t.lower()}%")
    if f["owns_pnl"]:
        # "Where I own the P&L" is a strong ask, but the label is a cheap
        # model's reading. Sorting first and letting the ranker judge costs a
        # little; filtering on a wrong label costs them the job.
        parts.append("2 * (owns_pnl = 1)")
    if f["role_family"] and f["title_keywords"]:
        # Title and label agree: the likeliest real match goes first.
        parts.append(f"(role_family IN ({', '.join('?' for _ in f['role_family'])}))")
        params.extend(f["role_family"])
    if not parts:
        return "0", []
    return "(" + " + ".join(f"COALESCE({p}, 0)" for p in parts) + ")", params


def _fit(years: int | None) -> tuple[str, list]:
    """1 when a job suits someone with `years` of experience, 0 when it plainly
    does not. Orders, never filters: they may want a stretch role, and a job
    with no stated years is not a mismatch.

    This is what stops a 4-year PM's list opening with Director and 10+ year
    roles — the most common miss in the recorded conversations.
    """
    if years is None:
        return "1", []
    return ("(CASE WHEN years_min IS NOT NULL AND years_min > ? THEN 0 "
            "WHEN level = 'leadership' AND ? < 8 THEN 0 "
            "WHEN level = 'intern' AND ? >= 2 THEN 0 ELSE 1 END)",
            [years + 2, years, years])


def build(filters: dict, fit_years: int | None = None) -> tuple[str, list, dict]:
    f = normalize(filters)
    clauses, params = _where(f)
    score_sql, score_params = _score(f)
    fit_sql, fit_params = _fit(fit_years)
    limit = min(f["count"] * OVERFETCH, MAX_ROWS)

    sql = f"""
        SELECT source, source_id, company, title, city, remote, location,
               url, apply_url, posted_at, seniority, years_min, years_max,
               company_type, stage, industry, description,
               role_family, level, owns_pnl, country, work_mode,
               {score_sql} AS hits, {fit_sql} AS fit
        FROM jobs
        WHERE {' AND '.join(clauses)}
        ORDER BY fit DESC, hits DESC, posted_at DESC
        LIMIT ?
    """
    return sql, [*score_params, *fit_params, *params, limit], f


def search(filters: dict, conn: sqlite3.Connection | None = None,
           fit_years: int | None = None) -> tuple[list[dict], dict]:
    """Returns (rows, normalized_filters). Rows are dicts, ready for ranking.

    `fit_years` is the person's experience, from their resume — not a filter
    they stated, so it lives outside `filters` and never shows up as one."""
    close_after = conn is None
    conn = conn or db.connect()
    try:
        sql, params, f = build(filters, fit_years)
        rows = [dict(r) for r in conn.execute(sql, params)]

        # Reserve a few slots for the freshest rows regardless of keyword hits,
        # so odd phrasing does not make a job invisible.
        if f["must_mention"] and len(rows) >= RECENCY_SLOTS:
            top = rows[: max(0, len(rows) - RECENCY_SLOTS)]
            seen = {(r["source"], r["source_id"]) for r in top}
            fresh_sql, fresh_params, _ = build({**f, "must_mention": None},
                                               fit_years)
            fresh = [dict(r) for r in conn.execute(fresh_sql, fresh_params)]
            extra = [r for r in fresh
                     if (r["source"], r["source_id"]) not in seen][:RECENCY_SLOTS]
            rows = top + extra
        return rows, f
    finally:
        if close_after:
            conn.close()


def relax(f: dict, protect: frozenset[str] | set[str] = frozenset()
          ) -> tuple[dict, str] | None:
    """One step of the sparse-result cascade, in fixed order. Returns the
    widened filters and a plain-language note to show the user, or None when
    there is nothing left to relax.

    **`protect` holds the fields the user actually said**, and this function
    will not touch one. Everything here exists to reach `count`, and `count`
    defaults to 5 — a number nobody asked for. Widening a default to hit a
    default is fine; deleting "in Bangalore" to hit it is overriding the person,
    and it is invisible in the answer because four results look like four
    results whichever city they are in.

    Returning None instead is not a failure. Two honest matches and "want me to
    look wider?" beats five where two are somewhere you ruled out.
    """
    def free(key: str) -> bool:
        return key not in protect

    # `must_mention` is deliberately first and is now effectively unreachable:
    # skills live in ORDER BY, never WHERE (see the module docstring), so
    # dropping them cannot return a single extra row. It can only be set by the
    # user asking for it, which means `protect` always covers it. Kept as the
    # first rung so the cascade's order still reads as "cheapest concession
    # first" if skills ever move into the WHERE clause.
    if f.get("must_mention") and free("must_mention"):
        return {**f, "must_mention": None}, "ignored the skill keywords"
    days = f.get("posted_within_days")
    if days and days < 90 and free("posted_within_days"):
        return {**f, "posted_within_days": 90}, "widened the posting window to 90 days"
    if f.get("seniority") and free("seniority"):
        return {**f, "seniority": None}, "dropped the seniority filter"
    if f.get("industry") and free("industry"):
        return {**f, "industry": None}, "looked beyond that industry"
    if f.get("city") and not f.get("remote") and free("city"):
        return {**f, "city": None}, "looked outside that city"
    return None


#: What the user said, as opposed to what `normalize` filled in. Every field in
#: DEFAULTS is None except `count` and `soft_criteria`, so anything non-null
#: after extraction is there because it was asked for. That makes provenance
#: free and deterministic — no extra model call, nothing to hallucinate.
#:
#: `count` is excluded on purpose even when stated: it is the target the
#: cascade is chasing, not a constraint on which jobs are eligible.
def stated(f: dict) -> frozenset[str]:
    return frozenset(
        k for k, v in (f or {}).items()
        if k not in ("count", "soft_criteria") and v not in (None, "", [], {})
    )


def elsewhere(f: dict, conn: sqlite3.Connection) -> tuple[int, list[tuple[str, int]]]:
    """For a city that came up short: (remote matches, [(other city, matches)]).

    Everything else they asked for stays exactly as it was — only the city is
    let go, and only to *count*. These numbers are offered as a choice; they
    are never shown as results, because Bangalore jobs presented as an answer
    to a Kochi search is the thing this whole file is built to avoid.
    """
    base = normalize({**f, "city": None, "remote": None})
    clauses, params = _where(base)
    where = " AND ".join(clauses)
    remote = conn.execute(f"SELECT COUNT(*) FROM jobs WHERE {where} AND remote = 1",
                          params).fetchone()[0]
    mine = CITY_GROUPS.get(f.get("city"), [f.get("city") or ""])
    cities = [(r[0], r[1]) for r in conn.execute(
        f"SELECT city, COUNT(*) FROM jobs WHERE {where} "
        f"AND city IS NOT NULL AND city NOT IN ({', '.join('?' for _ in mine)}) "
        f"AND remote = 0 GROUP BY city ORDER BY COUNT(*) DESC LIMIT 3",
        [*params, *mine])]
    return remote, cities
