"""What every model call cost, per person.

GLM's reply carries token counts, never a price: new input, cached input and
output (thinking is billed as output). So every call's counts are saved in the
profile's own database, next to what the call was for, and the price table
below turns them into money. The table is ours, not theirs: once a month,
compare `python cli.py cost --all` with the bigmodel.cn billing page and fix a
price here if the two disagree.

Before this, only the evals counted tokens, so the cost per user was an
estimate built on guessed usage (Cost tab, 2026-09-28).

Recording can never break a call: any failure here is swallowed.
"""

from __future__ import annotations

import contextlib
import contextvars
import os
import sqlite3
import sys
import threading
from pathlib import Path

#: US$ per million tokens: (new input, cached input, output). Z.ai's published
#: list, 2026-09-28. The account bills in yuan on bigmodel.cn; check there.
PRICES = {
    "glm-5.3": (1.40, 0.26, 4.40),
    "glm-5.3-flash": (0.15, 0.03, 0.50),
}
#: A model not in the table is priced as the dearest one, and flagged, so an
#: unknown never reads as free.
FALLBACK = "glm-5.3"
RUPEES = float(os.environ.get("RUPEES_PER_USD") or 88)

SCHEMA = """
CREATE TABLE IF NOT EXISTS usage (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    at         TEXT DEFAULT CURRENT_TIMESTAMP,
    purpose    TEXT,       -- chat | search_jobs | tailor_resume | apply | parse | ...
    model      TEXT,
    input      INTEGER,    -- new input tokens
    cached     INTEGER,    -- input read from the cache
    output     INTEGER     -- output, thinking included
);
"""

_purpose: contextvars.ContextVar[str] = contextvars.ContextVar("purpose", default="other")
_local = threading.local()


@contextlib.contextmanager
def purpose(name: str):
    """Everything called inside is booked to `name`: a search's filter and
    ranking calls count as the search, not as "other"."""
    token = _purpose.set(name)
    try:
        yield
    finally:
        _purpose.reset(token)


def counts(u) -> tuple[int, int, int]:
    """(new input, cached, output) from an SDK usage object or a plain dict
    (OpenAI-shaped: prompt_tokens includes the cached part)."""
    if u is None:
        return 0, 0, 0
    get = (u.get if isinstance(u, dict) else lambda k, d=0: getattr(u, k, d))
    cached = get("cache_read_input_tokens", 0) or 0
    if get("input_tokens", None) is not None:
        return int(get("input_tokens") or 0), int(cached), int(get("output_tokens") or 0)
    details = get("prompt_tokens_details", None) or {}
    cached = (details.get("cached_tokens") if isinstance(details, dict) else 0) or 0
    prompt = int(get("prompt_tokens", 0) or 0)
    return prompt - int(cached), int(cached), int(get("completion_tokens", 0) or 0)


def dollars(model: str, new: int, cached: int, out: int) -> float:
    p_in, p_cache, p_out = PRICES.get(model) or PRICES[FALLBACK]
    return (new * p_in + cached * p_cache + out * p_out) / 1_000_000


def _conn() -> sqlite3.Connection:
    import paths
    c = getattr(_local, "conn", None)
    if c is None or getattr(_local, "path", None) != paths.DB:
        paths.ensure()
        c = sqlite3.connect(paths.DB, timeout=10)
        c.executescript(SCHEMA)
        _local.conn, _local.path = c, paths.DB
    return c


def record(resp, model: str | None = None, purpose: str | None = None) -> None:
    """Save one call's tokens to this profile. `resp` is the SDK response, or
    anything with a `usage`; a dict of usage also works."""
    if "pytest" in sys.modules or os.environ.get("JOB_AGENT_USAGE") == "0":
        return                  # tests never write to a real profile
    try:
        u = resp.get("usage") if isinstance(resp, dict) else getattr(resp, "usage", None)
        new, cached, out = counts(u)
        if not (new or cached or out):
            return
        model = model or getattr(resp, "model", None) or "unknown"
        c = _conn()
        c.execute("INSERT INTO usage (purpose, model, input, cached, output) "
                  "VALUES (?, ?, ?, ?, ?)",
                  (purpose or _purpose.get(), model, new, cached, out))
        c.commit()
    except Exception:                                  # noqa: BLE001
        pass


def summary(db: Path | str, days: int | None = None) -> list[dict]:
    """Per purpose and model: calls, tokens and money, newest `days` only."""
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    if "usage" not in {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}:
        return []
    where, params = "", []
    if days:
        where, params = "WHERE at >= datetime('now', ?)", [f"-{days} days"]
    rows = c.execute(
        f"SELECT purpose, model, COUNT(*) calls, SUM(input) input, "
        f"SUM(cached) cached, SUM(output) output FROM usage {where} "
        f"GROUP BY purpose, model ORDER BY purpose", params).fetchall()
    c.close()
    out = []
    for r in rows:
        d = dict(r)
        d["usd"] = dollars(d["model"], d["input"], d["cached"], d["output"])
        d["priced_as"] = d["model"] if d["model"] in PRICES else FALLBACK
        out.append(d)
    return out
