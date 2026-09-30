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

-- The company list the engine re-reads (engine/loop.py). One row per employer
-- board; boards.json and the free dataset seed it (engine/registry.py).
-- `first_ok` is when the board was first read whole: a job with no date that
-- appears after it is new since we started watching, one that was already
-- there is of unknown age.
CREATE TABLE IF NOT EXISTS boards (
    source       TEXT NOT NULL,           -- the ATS: greenhouse, workday, oracle...
    slug         TEXT NOT NULL,           -- what its reader needs: slug or careers URL
    company      TEXT NOT NULL,           -- the name its jobs are stored under
    tags         TEXT,                    -- json from boards.json: type, stage, industry, size, country
    found_by     TEXT,                    -- boards.json | dataset | discover
    enabled      INTEGER NOT NULL DEFAULT 1,
    next_due     TEXT,                    -- NULL = read now
    first_ok     TEXT,
    last_ok      TEXT,
    last_fail    TEXT,
    fail_streak  INTEGER NOT NULL DEFAULT 0,
    gone_streak  INTEGER NOT NULL DEFAULT 0,   -- 404s in a row
    last_error   TEXT,
    open_jobs    INTEGER,                 -- postings in the feed at the last whole read
    retired_at   TEXT,
    added_at     TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (source, slug)
);

-- Every read the engine made: the evidence behind "latest" and "removed".
CREATE TABLE IF NOT EXISTS board_reads (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    slug        TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    seconds     REAL,
    ok          INTEGER NOT NULL,
    complete    INTEGER,          -- the whole list was read, so absence means gone
    postings    INTEGER,          -- in the feed, any age, any country
    stored      INTEGER,          -- rows written: in India, posted in the last 30 days
    new         INTEGER,
    missed      INTEGER,          -- first misses: re-read soon to confirm
    closed      INTEGER,          -- second misses: gone
    error       TEXT
);

CREATE INDEX IF NOT EXISTS idx_reads ON board_reads(source, slug, id);

-- Postings the engine saw but did not keep: posted over 30 days ago, or of
-- unknown age. Remembered so a later read does not mistake them for new ones.
CREATE TABLE IF NOT EXISTS seen_ids (
    source      TEXT NOT NULL,
    source_id   TEXT NOT NULL,
    board       TEXT,
    first_seen  TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (source, source_id)
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
    "seniority", "years_min", "years_max", "content_hash", "board", "apply_kind",
]

#: Sources that show a slice of the market, never an employer's whole list. A
#: job from one cannot be checked for still being open, so search leaves them
#: out; their company names feed discovery (engine/discover.py) instead.
AGGREGATORS = ("adzuna", "linkedin")
#: The window. Jobs posted longer ago are not kept.
DAYS = 30
#: How long a job stays findable after its board last listed it. Long enough to
#: survive a night of the Mac asleep, short enough that a board we can no
#: longer read drops out within two days instead of showing jobs nobody checked.
SEEN_HOURS = 48

#: What search and supply may show. One definition, so the two never disagree.
VISIBLE = (
    "closed_at IS NULL "
    f"AND source NOT IN ({', '.join(repr(s) for s in AGGREGATORS)}) "
    f"AND last_seen >= datetime('now', '-{SEEN_HOURS} hours') "
    f"AND posted_at >= date('now', '-{DAYS} days')"
)


def visible() -> str:
    """VISIBLE, except over a recording. The scenario profiles carry the jobs
    their conversations were recorded with (scenario-runs/drive.py sets
    JOB_AGENT_FROZEN_JOBS=1); "seen in the last 48 hours" would empty them
    the day after recording, so a replay shows what was open when recorded."""
    import os
    if os.environ.get("JOB_AGENT_FROZEN_JOBS") == "1":
        return "closed_at IS NULL"
    return VISIBLE


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
    # The engine (engine/loop.py). `board` is the slug the job was read from:
    # two boards on one ATS can share a company name ("Career Site" on Oracle),
    # so closing by company would close the other board's jobs.
    ("jobs", "board", "TEXT"),
    ("jobs", "apply_kind", "TEXT"),      # form | account | link
    ("jobs", "miss_count", "INTEGER DEFAULT 0"),
    # Background runs (apply/worker.py): what the person should hear, and
    # whether they have. Existing rows count as told.
    ("applications", "outcome", "TEXT"),
    ("applications", "reported", "INTEGER DEFAULT 1"),
    # Yes before submit (apply/confirm.py): the answers the person was shown,
    # and when they said submit to exactly those.
    ("applications", "confirm_values", "TEXT"),
    ("applications", "confirmed_at", "TEXT"),
]

#: Indexes that depend on a migrated column, so they cannot live in SCHEMA —
#: that runs before the ALTERs and would fail on an old database.
POST_MIGRATION_SQL: list[tuple[str, str]] = [
    ("messages", "CREATE INDEX IF NOT EXISTS idx_msg ON messages(session, turn, id)"),
    ("jobs", "CREATE INDEX IF NOT EXISTS idx_labels ON jobs(closed_at, country, role_family)"),
    ("jobs", "CREATE INDEX IF NOT EXISTS idx_board ON jobs(source, board, closed_at)"),
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
    # A re-read that carries no date or no text must not wipe the ones stored:
    # Workday and Oracle lists have no description (it costs a request per
    # job, paid once), and an undated posting keeps the date we gave it.
    keep = {"posted_at": "COALESCE(excluded.posted_at, jobs.posted_at)",
            "description": "CASE WHEN length(excluded.description) > 0 "
                           "THEN excluded.description ELSE jobs.description END"}
    updates = ", ".join(
        f"{c}={keep.get(c, f'excluded.{c}')}"
        for c in COLUMNS if c not in ("source", "source_id")
    )
    sql = (
        f"INSERT INTO jobs ({', '.join(COLUMNS)}) VALUES ({placeholders}) "
        f"ON CONFLICT(source, source_id) DO UPDATE SET {updates}, "
        f"last_seen = CURRENT_TIMESTAMP, closed_at = NULL, miss_count = 0"
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


def reconcile(conn: sqlite3.Connection, source: str, board: str,
              seen_ids, *, complete: bool = True) -> dict:
    """After one read of one board: what is still listed, what went missing.

    The only honest test of "still open" is the employer's own list. A job on
    it was seen now. A job missing from a **complete** read gets a miss, and
    is closed on the second miss in a row: Workday and Oracle lists shift while
    being paged, and one read can skip a job that is still there. The caller
    re-reads the board soon after a first miss, so a gone job closes within
    minutes of the next read, not a day later.

    An incomplete read (a capped Workday query, a board that suddenly lost most
    of its list) proves nothing about absence: it only refreshes what it saw.
    A failed read never reaches here, and closes nothing.
    """
    seen = {str(s) for s in seen_ids}
    rows = conn.execute(
        "SELECT source_id, COALESCE(miss_count, 0) AS misses, closed_at FROM jobs "
        "WHERE source = ? AND board = ?", (source, board)).fetchall()
    # Listed means open, whatever we thought before: a job closed on two
    # misses that is back on the list was never gone.
    here = [r["source_id"] for r in rows if r["source_id"] in seen]
    conn.executemany(
        "UPDATE jobs SET last_seen = CURRENT_TIMESTAMP, miss_count = 0, "
        "closed_at = NULL WHERE source = ? AND source_id = ?",
        [(source, s) for s in here])
    out = {"missed": 0, "closed": 0}
    if complete:
        gone = [r for r in rows if r["source_id"] not in seen and r["closed_at"] is None]
        first = [r["source_id"] for r in gone if r["misses"] < 1]
        second = [r["source_id"] for r in gone if r["misses"] >= 1]
        conn.executemany(
            "UPDATE jobs SET miss_count = COALESCE(miss_count, 0) + 1 "
            "WHERE source = ? AND source_id = ?", [(source, s) for s in first])
        conn.executemany(
            "UPDATE jobs SET closed_at = CURRENT_TIMESTAMP "
            "WHERE source = ? AND source_id = ?", [(source, s) for s in second])
        out = {"missed": len(first), "closed": len(second)}
    conn.commit()
    return out


def close_board(conn: sqlite3.Connection, source: str, board: str) -> int:
    """Every open job of a board that no longer exists (404 read after read)."""
    cur = conn.execute(
        "UPDATE jobs SET closed_at = CURRENT_TIMESTAMP "
        "WHERE source = ? AND board = ? AND closed_at IS NULL", (source, board))
    conn.commit()
    return cur.rowcount


#: How long a closed job stays in the table, so a closing can be checked by
#: opening its link, before it is deleted.
KEEP_CLOSED_DAYS = 3
#: A board job nobody has seen for this long is deleted: its board is failing
#: or gone, and search stopped showing it after SEEN_HOURS anyway.
UNSEEN_DAYS = 7


def prune(conn: sqlite3.Connection) -> dict:
    """The nightly clear-out. The table holds the last 30 days and nothing else.

    Aggregator rows keep their own age rule (adzuna.close_old, linkedin
    .close_old); they are deleted here only once closed. Ids of jobs deleted
    for age go to `seen_ids` first, or an undated one would come back on the
    next read looking brand new. Applications keep their own copy of company,
    title and link, so deleting a job never breaks a person's history.
    """
    aged = (f"source NOT IN ({', '.join(repr(s) for s in AGGREGATORS)}) AND ("
            f"posted_at < date('now', '-{DAYS} days') "
            f"OR last_seen < datetime('now', '-{UNSEEN_DAYS} days'))")
    conn.execute(
        "INSERT OR IGNORE INTO seen_ids (source, source_id, board, first_seen) "
        f"SELECT source, source_id, board, first_seen FROM jobs WHERE {aged}")
    old = conn.execute(f"DELETE FROM jobs WHERE {aged}").rowcount
    closed = conn.execute(
        "DELETE FROM jobs WHERE closed_at IS NOT NULL "
        f"AND closed_at < datetime('now', '-{KEEP_CLOSED_DAYS} days')").rowcount
    # A posting unseen for four months will not be mistaken for a new one.
    forgotten = conn.execute(
        "DELETE FROM seen_ids WHERE first_seen < datetime('now', '-120 days')").rowcount
    conn.commit()
    return {"aged_out": old, "closed_deleted": closed, "forgotten_ids": forgotten}


def wal(conn: sqlite3.Connection) -> str:
    """One writer (the engine), many readers (every bot worker). In WAL mode a
    reader never waits for a write. It is a property of the file, set once;
    in-memory test databases answer 'memory' and are left alone."""
    try:
        return conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
    except sqlite3.OperationalError as exc:       # busy: another process holds it
        return f"unchanged ({exc})"


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
