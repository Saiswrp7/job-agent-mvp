"""Schema, upsert, close_missing.

One table. The unique key is what keeps an hourly cron from filling the
database with 24 copies of every job per day.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "jobs.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    source            TEXT NOT NULL,
    source_id         TEXT NOT NULL,
    company           TEXT NOT NULL,
    title             TEXT NOT NULL,
    department        TEXT,
    employment_type   TEXT,
    location          TEXT,
    city              TEXT,
    remote            INTEGER DEFAULT 0,
    salary            TEXT,
    description       TEXT,
    url               TEXT,
    apply_url         TEXT,
    posted_at         TEXT,
    updated_at        TEXT,
    company_type      TEXT,
    stage             TEXT,
    industry          TEXT,
    size              TEXT,
    seniority         TEXT,
    years_min         INTEGER,
    years_max         INTEGER,
    content_hash      TEXT,
    first_seen        TEXT DEFAULT CURRENT_TIMESTAMP,
    last_seen         TEXT DEFAULT CURRENT_TIMESTAMP,
    closed_at         TEXT,
    PRIMARY KEY (source, source_id)
);

CREATE INDEX IF NOT EXISTS idx_open    ON jobs(closed_at, city, posted_at);
CREATE INDEX IF NOT EXISTS idx_type    ON jobs(company_type, closed_at);
CREATE INDEX IF NOT EXISTS idx_company ON jobs(company);

CREATE TABLE IF NOT EXISTS vault (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    source     TEXT,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS staging (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    fact       TEXT NOT NULL,
    context    TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    promoted   INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session    TEXT,                     -- one process lifetime
    turn       INTEGER,                  -- 1, 2, 3... within that session
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    meta       TEXT,                     -- assistant rows: what ran underneath
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS applications (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source       TEXT,
    source_id    TEXT,
    company      TEXT,
    title        TEXT,
    apply_url    TEXT,
    resume_path  TEXT,
    status       TEXT DEFAULT 'pending',   -- pending|waiting|submitted|failed
    question     TEXT,                     -- set while status='waiting'
    log          TEXT,                     -- json message log, for park/resume
    screenshot   TEXT,
    created_at   TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at   TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS agent_logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id     INTEGER,
    step       INTEGER,
    kind       TEXT,        -- prompt|tool_call|tool_result|error
    payload    TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""

#: Written on every upsert. `first_seen` is deliberately absent — it is how you
#: tell a job posted today from one open for three months, so it must survive.
COLUMNS = [
    "source", "source_id", "company", "title", "department", "employment_type",
    "location", "city", "remote", "salary", "description", "url", "apply_url",
    "posted_at", "updated_at", "company_type", "stage", "industry", "size",
    "seniority", "years_min", "years_max", "content_hash",
]


#: Columns added after a table already shipped. `CREATE TABLE IF NOT EXISTS`
#: silently leaves an existing table alone, so a jobs.db created before these
#: existed would keep the old shape forever and every write would fail. Cheap
#: and idempotent, so it just runs on every connect.
MIGRATIONS: list[tuple[str, str, str]] = [
    ("messages", "session", "TEXT"),
    ("messages", "turn", "INTEGER"),
    ("messages", "meta", "TEXT"),
]

#: Indexes that depend on a migrated column, so they cannot live in SCHEMA —
#: that runs before the ALTERs and would fail on an old database.
POST_MIGRATION_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_msg ON messages(session, turn, id)",
]


def _migrate(conn: sqlite3.Connection) -> list[str]:
    """Add any missing column. Returns what it added, for tests."""
    added = []
    for table, column, decl in MIGRATIONS:
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            added.append(f"{table}.{column}")
    for sql in POST_MIGRATION_SQL:
        conn.execute(sql)
    conn.commit()
    return added


def connect(path: Path | str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def upsert(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    placeholders = ", ".join("?" for _ in COLUMNS)
    updates = ", ".join(
        f"{c}=excluded.{c}" for c in COLUMNS if c not in ("source", "source_id")
    )
    sql = (
        f"INSERT INTO jobs ({', '.join(COLUMNS)}) VALUES ({placeholders}) "
        f"ON CONFLICT(source, source_id) DO UPDATE SET {updates}, "
        f"last_seen = CURRENT_TIMESTAMP, closed_at = NULL"
    )
    conn.executemany(sql, [[r.get(c) for c in COLUMNS] for r in rows])
    conn.commit()
    return len(rows)


def close_missing(conn: sqlite3.Connection, source: str, company: str,
                  seen_ids: list[str]) -> int:
    """Mark jobs that vanished from this company's board as closed.

    ONLY call after a fetch that actually succeeded. Called with the empty list
    from a timeout, it closes every job at that company — the one bug in this
    file worth being careful about, so `sync.py` guards the call site too.
    """
    if not seen_ids:
        # A board that legitimately has zero jobs is indistinguishable here from
        # a failed parse, so refuse. sync.py only reaches this on success.
        return 0
    marks = ", ".join("?" for _ in seen_ids)
    cur = conn.execute(
        f"UPDATE jobs SET closed_at = CURRENT_TIMESTAMP "
        f"WHERE source = ? AND company = ? AND closed_at IS NULL "
        f"AND source_id NOT IN ({marks})",
        [source, company, *seen_ids],
    )
    conn.commit()
    return cur.rowcount


def stats(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        "SELECT COUNT(*) AS total, "
        "SUM(closed_at IS NULL) AS open, "
        "COUNT(DISTINCT company) AS companies FROM jobs"
    ).fetchone()
    return dict(row)
