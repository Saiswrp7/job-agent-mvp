"""Token counts per call, and what they cost. No model, no network.

Run: python -m pytest tests/ -q
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import usage                                       # noqa: E402


def test_anthropic_shaped_counts():
    u = SimpleNamespace(input_tokens=10687, cache_read_input_tokens=13888,
                        output_tokens=2032)
    assert usage.counts(u) == (10687, 13888, 2032)


def test_openai_shaped_counts_split_the_cached_part_out():
    u = {"prompt_tokens": 1000, "completion_tokens": 50,
         "prompt_tokens_details": {"cached_tokens": 400}}
    assert usage.counts(u) == (600, 400, 50)


def test_a_search_message_costs_what_the_sheet_says():
    # The Cost tab's "chat message with a search": $0.0275.
    assert round(usage.dollars("glm-5.3", 10687, 13888, 2032), 4) == 0.0275


def test_an_unknown_model_is_priced_as_the_dearest_not_free():
    assert usage.dollars("glm-9", 1_000_000, 0, 0) == 1.40


def test_tests_never_write_usage(monkeypatch, tmp_path):
    import paths
    monkeypatch.setattr(paths, "DB", tmp_path / "x.db")
    usage.record(SimpleNamespace(usage=SimpleNamespace(
        input_tokens=5, cache_read_input_tokens=0, output_tokens=5)))
    assert not (tmp_path / "x.db").exists()


def test_summary_books_each_task(tmp_path):
    import sqlite3
    db = tmp_path / "u.db"
    c = sqlite3.connect(db)
    c.executescript(usage.SCHEMA)
    c.executemany("INSERT INTO usage (purpose, model, input, cached, output) "
                  "VALUES (?, ?, ?, ?, ?)",
                  [("chat", "glm-5.3", 4677, 0, 55),
                   ("search_jobs", "glm-5.3", 10000, 1000, 1000),
                   ("search_jobs", "glm-5.3", 10000, 1000, 1000)])
    c.commit()
    rows = {r["purpose"]: r for r in usage.summary(db)}
    assert rows["search_jobs"]["calls"] == 2
    assert rows["chat"]["usd"] == usage.dollars("glm-5.3", 4677, 0, 55)
