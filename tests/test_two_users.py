"""Two users, one jobs table, nothing else shared.

Each user runs in its own process, because `paths` resolves the profile once
at import — the same way the real CLI and web server start. The shared jobs
file is a temporary one, never the real jobs.db.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SNIPPET = r"""
import json, sys
sys.path.insert(0, {root!r})
from engine import adapters, db
conn = db.connect()
action = {action!r}
if action == "add_job":
    db.upsert(conn, [adapters._row(source="lever", source_id="new", company="Zupee",
        title="Growth Manager", location="Bangalore", description="x", url="u")])
if action == "remember":
    conn.execute("INSERT OR REPLACE INTO vault (key, value) VALUES ('full_name', 'Asha')")
    conn.commit()
print(json.dumps({{
    "jobs": [r[0] for r in conn.execute("SELECT source_id FROM jobs ORDER BY 1")],
    "vault": [r[0] for r in conn.execute("SELECT value FROM vault")],
}}))
"""


def as_user(home: Path | None, shared: Path, action: str = "") -> dict:
    env = {**os.environ, "JOB_AGENT_SHARED": str(shared)}
    env.pop("JOB_AGENT_HOME", None)
    if home is not None:
        env["JOB_AGENT_HOME"] = str(home)
    out = subprocess.run([sys.executable, "-c",
                          SNIPPET.format(root=str(ROOT), action=action)],
                         env=env, capture_output=True, text=True, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


def seed_shared(path: Path) -> None:
    sys.path.insert(0, str(ROOT))
    from engine import adapters, db
    c = db.connect(path)
    db.upsert(c, [adapters._row(source="lever", source_id="seed", company="Paytm",
                                title="PM", location="Noida", description="x",
                                url="u")])
    c.close()


def test_a_new_user_sees_every_job_and_nobody_elses_data(tmp_path):
    shared = tmp_path / "shared.db"
    seed_shared(shared)
    asha, ravi = tmp_path / "asha", tmp_path / "ravi"

    assert as_user(asha, shared, "remember") == {"jobs": ["seed"], "vault": ["Asha"]}
    # Ravi is brand new: all the jobs, none of Asha.
    assert as_user(ravi, shared) == {"jobs": ["seed"], "vault": []}


def test_a_job_fetched_by_anyone_is_there_for_everyone(tmp_path):
    shared = tmp_path / "shared.db"
    seed_shared(shared)
    as_user(tmp_path / "asha", shared, "add_job")
    assert as_user(tmp_path / "ravi", shared)["jobs"] == ["new", "seed"]


def test_a_user_file_holds_no_jobs_table(tmp_path):
    import sqlite3
    shared = tmp_path / "shared.db"
    seed_shared(shared)
    as_user(tmp_path / "asha", shared, "remember")
    own = sqlite3.connect(tmp_path / "asha" / "jobs.db")
    tables = {r[0] for r in own.execute("SELECT name FROM sqlite_master")}
    assert "jobs" not in tables and "vault" in tables


def test_a_profile_with_its_own_frozen_jobs_keeps_them(tmp_path):
    """The scenario profiles: a recorded run replays against its own jobs."""
    sys.path.insert(0, str(ROOT))
    from engine import adapters, db
    shared = tmp_path / "shared.db"
    seed_shared(shared)
    frozen = tmp_path / "scen"
    frozen.mkdir()
    c = db.connect(frozen / "jobs.db")
    db.upsert(c, [adapters._row(source="lever", source_id="frozen", company="X",
                                title="PM", location="Pune", description="x", url="u")])
    c.close()
    assert as_user(frozen, shared)["jobs"] == ["frozen"]
