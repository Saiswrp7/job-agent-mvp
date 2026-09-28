"""How many jobs can the product fully handle, per realistic search.

"Fully handle" means the whole path works: a full description to tailor from
and an apply link on the employer's own site. An Adzuna row fails both (a
500-character snippet and an adzuna.in redirect). A LinkedIn row fails the
second (logged out, LinkedIn hides where Apply leads). So neither counts, and
a table can grow by thousands of rows while this number stays flat. That is
the reason to measure this and not `stats`.

Pure SQL over the jobs table. No model, no network.
"""

from __future__ import annotations

import sqlite3

#: The bar a search has to clear before beta users arrive.
GATE = 15
DAYS = 14

#: An employer link, on a job with a real description. 700 characters is well
#: above any Adzuna snippet (they stop at 500) and below any real JD.
USABLE = ("closed_at IS NULL AND source NOT IN ('adzuna', 'linkedin') "
          "AND apply_url IS NOT NULL AND apply_url NOT LIKE '%adzuna.%' "
          "AND apply_url NOT LIKE '%linkedin.com%' "
          "AND length(description) >= 700 "
          "AND posted_at >= date('now', ?)")

#: Title OR label, the same way search matches a role: a wrong label must not
#: hide a job, and a label finds the one whose title the keyword misses.
ROLES = {
    "product manager": (["product manager", "product owner", "product lead",
                         "head of product"], ["product"]),
    "growth / marketing": (["growth", "marketing"], ["growth", "marketing"]),
    "software engineer": (["engineer", "developer", "sde"], ["engineering"]),
    "data analyst": (["analyst", "analytics"], ["data"]),
    "UX/product designer": (["designer", "ux", "ui/ux"], ["design"]),
    "sales / business development": (
        ["sales", "business development", "account executive"],
        ["sales", "business_development"]),
}

INDIA = ("(country = 'India' OR location LIKE '%India%' "
         "OR city IN ('Bangalore','Mumbai','Delhi','Gurgaon','Noida','Hyderabad',"
         "'Pune','Chennai','Kolkata','Ahmedabad','Jaipur'))")
#: Remote here means remote and open to someone in India. A US-only remote
#: role is not supply for these users.
REMOTE_INDIA = f"(remote = 1 AND ({INDIA} OR country = 'Global'))"

PLACES = {
    "Bangalore": "city = 'Bangalore'",
    "Mumbai": "city = 'Mumbai'",
    "Delhi NCR": "city IN ('Delhi','Gurgaon','Noida','Ghaziabad','Faridabad')",
    "Hyderabad": "city = 'Hyderabad'",
    "Pune": "city = 'Pune'",
    "remote India": REMOTE_INDIA,
    "remote": REMOTE_INDIA,
}

SEARCHES = [
    ("product manager", ["Bangalore", "Mumbai", "Delhi NCR", "remote India"]),
    ("growth / marketing", ["Bangalore", "Mumbai", "remote"]),
    ("software engineer", ["Bangalore", "Hyderabad", "Pune"]),
    ("data analyst", ["Bangalore", "Mumbai"]),
    ("UX/product designer", ["Bangalore"]),
    ("sales / business development", ["Mumbai", "Delhi NCR"]),
]


def _role_sql(role: str) -> tuple[str, list]:
    words, families = ROLES[role]
    parts = ["lower(title) LIKE ?" for _ in words]
    parts.append(f"role_family IN ({', '.join('?' for _ in families)})")
    return "(" + " OR ".join(parts) + ")", [f"%{w}%" for w in words] + families


def count(conn: sqlite3.Connection, role: str, place: str,
          days: int = DAYS) -> int:
    role_sql, params = _role_sql(role)
    sql = (f"SELECT COUNT(*) FROM jobs WHERE {USABLE} AND {role_sql} "
           f"AND {PLACES[place]}")
    return conn.execute(sql, [f"-{int(days)} days", *params]).fetchone()[0]


def report(conn: sqlite3.Connection, days: int = DAYS) -> list[tuple[str, str, int]]:
    return [(role, place, count(conn, role, place, days))
            for role, places in SEARCHES for place in places]
