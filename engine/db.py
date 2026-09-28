"""Schema, upsert, close_missing.

One table. The unique key is what keeps an hourly cron from filling the
database with 24 copies of every job per day.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import paths

#: Which profile's database. `JOB_AGENT_HOME` moves it, along with every other
#: file this user accumulates — see paths.py for why it is one switch and not
#: an override on this line alone.
DB_PATH = paths.DB

#: Shared by every user: the jobs, and what the fetch needs to keep them
#: fresh. One copy, however many people search it.
JOBS_SCHEMA = """
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

-- LinkedIn jobs we have a link for but no description yet. Collecting links is
-- cheap and fetching descriptions is not, so the two run at different speeds;
-- a job moves to `jobs` only once it has a description. See engine/linkedin.py.
CREATE TABLE IF NOT EXISTS linkedin_queue (
    source_id  TEXT PRIMARY KEY,
    card       TEXT NOT NULL,   -- json: title, company, location, posted_at
    search     TEXT,            -- which saved search found it
    added_at   TEXT DEFAULT CURRENT_TIMESTAMP
);

-- When each saved search last ran to completion. Paging stops at jobs older
-- than this search's own last run — never at jobs another search found.
CREATE TABLE IF NOT EXISTS linkedin_searches (
    search     TEXT PRIMARY KEY,   -- "keywords @ location"
    last_run   TEXT NOT NULL       -- date
);

-- Searches that came from people asking, not from linkedin_searches.json.
-- "UX designer in Kochi" lands here the first time someone asks, and the daily
-- fetch covers it from then on — supply grows toward what people want.
CREATE TABLE IF NOT EXISTS saved_searches (
    keywords     TEXT NOT NULL,
    location     TEXT NOT NULL,
    remote       INTEGER NOT NULL DEFAULT 0,
    times_asked  INTEGER NOT NULL DEFAULT 1,
    first_asked  TEXT DEFAULT CURRENT_TIMESTAMP,
    last_asked   TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (keywords, location, remote)
);
"""

#: Private to one user: who they are, what they said, what they applied to.
USER_SCHEMA = """
CREATE TABLE IF NOT EXISTS vault (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    source     TEXT,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Everything they did, one claim per row. `master.json` is a document and a
-- document cannot answer "did they ever say this?" — which is the question the
-- resume guard has to ask the moment the corpus grows past the parsed PDF.
--
-- `heard_in` is the whole point: the message the claim came from, verbatim. A
-- number is allowed onto a resume because this row exists and says where it
-- came from, not because a model produced it convincingly.
CREATE TABLE IF NOT EXISTS career (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,   -- accomplishment|skill|metric|role|education
    text       TEXT NOT NULL,   -- the claim, in their words
    company    TEXT,            -- which role it belongs to, when known
    source     TEXT NOT NULL,   -- resume|chat|user
    heard_in   TEXT,            -- the raw message it came from
    said_at    TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(kind, text)
);

CREATE INDEX IF NOT EXISTS idx_career ON career(company, kind);

-- What they want and what they avoid, one line per row. Before this table a
-- preference said in chat lived only in the conversation, so "no consulting"
-- on turn 5 was gone once the turn was trimmed. `stated` separates "no
-- consulting" (said outright) from a sigh about consulting firms (inferred):
-- both steer a search, only the first is theirs to be held to.
CREATE TABLE IF NOT EXISTS preferences (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL,             -- want|avoid|constraint
    text         TEXT NOT NULL,
    stated       INTEGER NOT NULL DEFAULT 1, -- 1 said outright, 0 inferred
    heard_in     TEXT,                      -- the raw message it came from
    said_at      TEXT DEFAULT CURRENT_TIMESTAMP,
    retracted_at TEXT,                      -- set when they take it back
    UNIQUE(kind, text)
);

-- The thread of a conversation, in a few lines. Trimmed turns used to be
-- deleted outright; this is what they leave behind, and what the next
-- session starts from. `upto_turn` is how far the summary has read, so each
-- refresh reads only what is new.
CREATE TABLE IF NOT EXISTS summaries (
    session    TEXT PRIMARY KEY,
    summary    TEXT NOT NULL,
    upto_turn  INTEGER NOT NULL DEFAULT 0,
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

SCHEMA = JOBS_SCHEMA + USER_SCHEMA

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
    # Labels, written once per job by engine/labels.py so search can filter on
    # what a job *is* rather than what its title happens to say. NULL means
    # "not labelled yet" and is neutral at query time, never a mismatch.
    ("jobs", "role_family", "TEXT"),     # growth, engineering, sales, ...
    ("jobs", "level", "TEXT"),           # intern .. leadership
    ("jobs", "owns_pnl", "INTEGER"),     # 1 = accountable for a P&L / revenue line
    ("jobs", "country", "TEXT"),
    ("jobs", "work_mode", "TEXT"),       # onsite | hybrid | remote
    ("jobs", "label_hash", "TEXT"),      # content_hash the labels were read from
    # Background runs (apply/worker.py): what the person should hear, and
    # whether they have. Existing rows count as told.
    ("applications", "outcome", "TEXT"),
    ("applications", "reported", "INTEGER DEFAULT 1"),
]

#: Indexes that depend on a migrated column, so they cannot live in SCHEMA —
#: that runs before the ALTERs and would fail on an old database.
POST_MIGRATION_SQL: list[tuple[str, str]] = [
    ("messages", "CREATE INDEX IF NOT EXISTS idx_msg ON messages(session, turn, id)"),
    ("jobs", "CREATE INDEX IF NOT EXISTS idx_labels ON jobs(closed_at, country, role_family)"),
    # `done` used to mean "the apply loop stopped", which said nothing about
    # whether anything was sent. It is retired in favour of statuses named for
    # the outcome. Every existing row becomes `blocked`, which is true of all
    # of them: ALLOW_SUBMIT has never been on, so nothing has ever been
    # submitted. Promoting them to `submitted` would invent history.
    ("applications", "UPDATE applications SET status='blocked' WHERE status='done'"),
]


def _tables(conn: sqlite3.Connection) -> set[str]:
    """Tables in this file only — not in an attached one."""
    return {r[0] for r in conn.execute(
        "SELECT name FROM main.sqlite_master WHERE type = 'table'")}


def _migrate(conn: sqlite3.Connection) -> list[str]:
    """Add any missing column. Returns what it added, for tests.

    Only touches tables that live in this file: a user's own database has no
    `jobs` table, and altering the shared one belongs to its own connect."""
    here = _tables(conn)
    added = []
    for table, column, decl in MIGRATIONS:
        if table not in here:
            continue
        existing = {r[1] for r in conn.execute(f"PRAGMA main.table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            added.append(f"{table}.{column}")
    for table, sql in POST_MIGRATION_SQL:
        if table in here:
            conn.execute(sql)
    conn.commit()
    return added


def connect(path: Path | str = DB_PATH) -> sqlite3.Connection:
    """The database for this profile.

    **Jobs are shared, people are not.** The default profile keeps everything
    in one file, as it always has. Any other profile gets its own file for
    what is private — vault, career, messages, applications — and reads the
    jobs from the shared file, attached. So a new user sees all the jobs the
    fetch has gathered on their first search, and nothing of anyone else's.

    Before this, a second profile was a copy of the whole database: its jobs
    froze on the day it was copied, and a genuinely new profile had none.

    A profile whose file already has its own `jobs` table keeps using it. The
    scenario profiles do, on purpose: a recorded run replays against the jobs
    it was recorded with, not against whatever was fetched this morning.
    """
    if path == DB_PATH:
        # A profile is a directory of files, not just this database. Creating
        # them here means the first command run against a new home works,
        # rather than failing later on whichever file was written first.
        paths.ensure()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    shared = (path == DB_PATH and not paths.IS_DEFAULT
              and "jobs" not in _tables(conn))
    if shared:
        connect(paths.SHARED_DB).close()        # its schema and migrations
        conn.executescript(USER_SCHEMA)
        conn.execute("ATTACH DATABASE ? AS shared", (str(paths.SHARED_DB),))
    else:
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


def stats_by_source(conn: sqlite3.Connection) -> list[dict]:
    """Open jobs and companies per source — where the supply actually comes from."""
    return [dict(r) for r in conn.execute(
        "SELECT source, COUNT(*) AS open, COUNT(DISTINCT company) AS companies "
        "FROM jobs WHERE closed_at IS NULL GROUP BY source ORDER BY open DESC")]
