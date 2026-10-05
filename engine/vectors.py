"""Meaning-vectors for jobs: the second way a search finds a job.

SQL finds a job by a word in its title or a label. "growth marketing" never
found "Manager - Growth Strategy" that way, and the APM search once came back
empty. An embedding is a list of numbers for what a text MEANS; a job whose
numbers sit close to the search's is a match even when the words differ.

How it fits: `query.search` still runs its SQL, then asks `nearest` for the
jobs closest in meaning (inside the same city/level/country filters), and the
two lists are merged by reciprocal rank fusion. SQL's order and the ranker's
reading stay as they were; this only adds the jobs words missed.

Embeddings come from OpenRouter (`openai/text-embedding-3-small`): the GLM key
has no embedding model (error 1211, 2026-10-04). Cut to 512 numbers per job and
stored as float16, about 1 KB a job. A failed call costs a search nothing: it
runs on SQL alone.
"""

from __future__ import annotations

import os
import sqlite3

import numpy as np

import llm  # noqa: F401  (loads .env, where the OpenRouter key lives)

from . import db

MODEL = os.environ.get("JOB_AGENT_EMBED_MODEL", "openai/text-embedding-3-small")
DIMS = 512
BATCH = 100
#: Characters of the description that go into a job's vector: the opening says
#: what the job is; the legal tail only dilutes it.
DESC_CHARS = 500


def configured() -> bool:
    return bool(os.environ.get("OPENROUTER_API_KEY"))


def _client():
    from openai import OpenAI
    return OpenAI(api_key=os.environ["OPENROUTER_API_KEY"],
                  base_url="https://openrouter.ai/api/v1", timeout=60)


def embed(texts: list[str]) -> np.ndarray:
    """(n, DIMS) float32, each row length 1. text-embedding-3 vectors keep their
    meaning when cut short, so a provider that ignores `dimensions` is cut here."""
    r = _client().embeddings.create(model=MODEL, input=[t[:3000] or "-" for t in texts],
                                    dimensions=DIMS)
    m = np.array([d.embedding for d in r.data], dtype=np.float32)[:, :DIMS]
    return m / np.maximum(np.linalg.norm(m, axis=1, keepdims=True), 1e-9)


def text_of(row) -> str:
    return (f"{row['title']} at {row['company']}. {row['role_family'] or ''}. "
            f"{(row['description'] or '')[:DESC_CHARS]}")


_NO_VECTOR = ("NOT EXISTS (SELECT 1 FROM job_vec v WHERE v.source = jobs.source "
              "AND v.source_id = jobs.source_id AND v.model = ?)")


def pending(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    """Visible jobs with no vector in this model's space yet."""
    return conn.execute(
        "SELECT source, source_id, title, company, role_family, description "
        f"FROM jobs WHERE {db.visible()} AND {_NO_VECTOR} LIMIT ?",
        (MODEL, limit)).fetchall()


def run(conn: sqlite3.Connection, limit: int = 2000) -> dict:
    """Embed up to `limit` jobs that have no vector, a batch at a time,
    committing each batch. Stops at the first failed call (a dead key fails
    every batch the same way)."""
    done, stopped = 0, ""
    while done < limit:
        rows = pending(conn, min(BATCH, limit - done))
        if not rows:
            break
        try:
            m = embed([text_of(r) for r in rows])
        except Exception as exc:                       # noqa: BLE001
            stopped = f"{type(exc).__name__}: {str(exc)[:120]}"
            break
        conn.executemany(
            "INSERT OR REPLACE INTO job_vec (source, source_id, model, vec) VALUES (?,?,?,?)",
            [(r["source"], r["source_id"], MODEL, m[i].astype(np.float16).tobytes())
             for i, r in enumerate(rows)])
        conn.commit()
        done += len(rows)
    left = conn.execute(f"SELECT COUNT(*) FROM jobs WHERE {db.visible()} AND {_NO_VECTOR}",
                        (MODEL,)).fetchone()[0]
    return {"embedded": done, "left": left, "stopped": stopped}


_cache: dict = {"key": None, "keys": [], "mat": None}


def _matrix(conn: sqlite3.Connection):
    """(keys, matrix) of every stored vector, rebuilt only when the table changed."""
    n, last = conn.execute("SELECT COUNT(*), MAX(rowid) FROM job_vec WHERE model = ?",
                           (MODEL,)).fetchone()
    key = (n, last, MODEL)
    if _cache["key"] != key:
        rows = conn.execute("SELECT source, source_id, vec FROM job_vec WHERE model = ?",
                            (MODEL,)).fetchall()
        _cache.update(
            key=key, keys=[(r[0], r[1]) for r in rows],
            mat=(np.stack([np.frombuffer(r[2], dtype=np.float16) for r in rows])
                 .astype(np.float32) if rows else np.zeros((0, DIMS), np.float32)))
    return _cache["keys"], _cache["mat"]


def nearest(conn: sqlite3.Connection, text: str, allowed: set[tuple] | None,
            k: int = 20, floor: float = 0.0) -> list[tuple[str, str, float]]:
    """The `k` jobs closest in meaning to `text`, best first, as
    (source, source_id, similarity). Only `allowed` jobs when given (the ones
    that passed the person's filters). Empty on any failure."""
    if not configured() or not text.strip():
        return []
    try:
        keys, mat = _matrix(conn)
        if not len(keys):
            return []
        q = embed([text])[0]
    except Exception:                                  # noqa: BLE001
        return []
    sims = mat @ q
    order = np.argsort(-sims)
    out = []
    for i in order:
        if sims[i] < floor:
            break
        if allowed is None or keys[i] in allowed:
            out.append((keys[i][0], keys[i][1], float(sims[i])))
            if len(out) >= k:
                break
    return out
