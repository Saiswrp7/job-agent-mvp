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
    "soft_criteria": "",
}


def normalize(filters: dict) -> dict:
    out = dict(DEFAULTS)
    out.update({k: v for k, v in (filters or {}).items() if k in DEFAULTS})
    if not isinstance(out["count"], int) or out["count"] < 1:
        out["count"] = 5
    out["count"] = min(out["count"], 20)
    return out


def _where(f: dict) -> tuple[list[str], list]:
    clauses = ["closed_at IS NULL"]
    params: list = []

    if f["city"]:
        # Remote counts as available in any city the user names.
        clauses.append("(city = ? OR remote = 1)")
        params.append(f["city"])
    if f["remote"]:
        clauses.append("remote = 1")
    for col in ("company_type", "stage", "seniority"):
        if f[col]:
            clauses.append(f"{col} = ?")
            params.append(f[col])
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
    if f["title_keywords"]:
        ors = " OR ".join("lower(title) LIKE ?" for _ in f["title_keywords"])
        clauses.append(f"({ors})")
        params.extend(f"%{k.lower()}%" for k in f["title_keywords"])

    return clauses, params


def _score(f: dict) -> tuple[str, list]:
    """Keyword hits summed. SQLite booleans are 0/1, so addition works."""
    if not f["must_mention"]:
        return "0", []
    terms = f["must_mention"]
    expr = " + ".join("(lower(description) LIKE ?)" for _ in terms)
    return f"({expr})", [f"%{t.lower()}%" for t in terms]


def build(filters: dict) -> tuple[str, list, dict]:
    f = normalize(filters)
    clauses, params = _where(f)
    score_sql, score_params = _score(f)
    limit = min(f["count"] * OVERFETCH, MAX_ROWS)

    sql = f"""
        SELECT source, source_id, company, title, city, remote, location,
               url, apply_url, posted_at, seniority, years_min, years_max,
               company_type, stage, industry, description,
               {score_sql} AS hits
        FROM jobs
        WHERE {' AND '.join(clauses)}
        ORDER BY hits DESC, posted_at DESC
        LIMIT ?
    """
    return sql, [*score_params, *params, limit], f


def search(filters: dict, conn: sqlite3.Connection | None = None) -> tuple[list[dict], dict]:
    """Returns (rows, normalized_filters). Rows are dicts, ready for ranking."""
    close_after = conn is None
    conn = conn or db.connect()
    try:
        sql, params, f = build(filters)
        rows = [dict(r) for r in conn.execute(sql, params)]

        # Reserve a few slots for the freshest rows regardless of keyword hits,
        # so odd phrasing does not make a job invisible.
        if f["must_mention"] and len(rows) >= RECENCY_SLOTS:
            top = rows[: max(0, len(rows) - RECENCY_SLOTS)]
            seen = {(r["source"], r["source_id"]) for r in top}
            fresh_sql, fresh_params, _ = build({**f, "must_mention": None})
            fresh = [dict(r) for r in conn.execute(fresh_sql, fresh_params)]
            extra = [r for r in fresh
                     if (r["source"], r["source_id"]) not in seen][:RECENCY_SLOTS]
            rows = top + extra
        return rows, f
    finally:
        if close_after:
            conn.close()


def relax(f: dict) -> tuple[dict, str] | None:
    """One step of the sparse-result cascade, in fixed order. Returns the
    widened filters and a plain-language note to show the user, or None when
    there is nothing left to relax."""
    if f.get("must_mention"):
        return {**f, "must_mention": None}, "ignored the skill keywords"
    days = f.get("posted_within_days")
    if days and days < 90:
        return {**f, "posted_within_days": 90}, "widened the posting window to 90 days"
    if f.get("seniority"):
        return {**f, "seniority": None}, "dropped the seniority filter"
    if f.get("industry"):
        return {**f, "industry": None}, "looked beyond that industry"
    if f.get("city") and not f.get("remote"):
        return {**f, "city": None}, "looked outside that city"
    return None
