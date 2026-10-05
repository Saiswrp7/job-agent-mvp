"""Meaning-search: vectors stored once, merged into SQL's rows, never required.

The embedding call is faked: a text's vector is how much of each topic word it
holds, so "close in meaning" is something a test can state exactly.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import db, vectors      # noqa: E402
from search import query            # noqa: E402

from test_labels_search import add, ids   # noqa: E402,F401

TOPICS = ("growth", "marketing", "account")


def fake_embed(texts):
    m = np.array([[t.lower().count(w) + 0.01 for w in TOPICS] for t in texts], dtype=np.float32)
    return m / np.linalg.norm(m, axis=1, keepdims=True)


@pytest.fixture
def conn(monkeypatch):
    c = db.connect(":memory:")
    monkeypatch.setattr(vectors, "embed", fake_embed)
    monkeypatch.setattr(vectors, "configured", lambda: True)
    monkeypatch.setattr(query, "SEM_FLOOR", 0.5)
    vectors._cache["key"] = None
    yield c
    c.close()


def seeded(conn):
    add(conn, "a", title="Marketing Manager", description="growth marketing funnel")
    add(conn, "b", title="Strategy Lead", description="growth marketing planning")
    add(conn, "c", title="Chartered Accountant", description="account books account audit")
    add(conn, "d", title="Strategy Lead", city="Mumbai, India", description="growth marketing planning")
    vectors.run(conn)


def test_every_visible_job_gets_one_vector_once(conn):
    seeded(conn)
    assert conn.execute("SELECT COUNT(*) FROM job_vec").fetchone()[0] == 4
    assert vectors.run(conn)["embedded"] == 0            # nothing left to do


def test_a_job_whose_title_lacks_the_word_is_found_by_meaning(conn):
    seeded(conn)
    f = {"title_keywords": ["marketing"], "city": "Bangalore", "count": 5}
    sql_only, _ = query.search(f, conn)
    assert ids(sql_only) == {"a"}                        # SQL needs the word in the title
    rows, _ = query.search({**f, "semantic": "growth marketing"}, conn)
    assert {"a", "b"} <= ids(rows)
    assert "c" not in ids(rows)                          # an accountant is not close
    assert "d" not in ids(rows)                          # Mumbai: their city filter still holds
    assert [r for r in rows if r["source_id"] == "b"][0]["by_meaning"] is True


def test_no_vectors_or_a_failed_call_leaves_sql_alone(conn, monkeypatch):
    add(conn, "a", title="Marketing Manager")
    f = {"title_keywords": ["marketing"], "semantic": "growth marketing"}
    assert ids(query.search(f, conn)[0]) == {"a"}        # nothing embedded yet
    seeded(conn)

    def boom(texts):
        raise RuntimeError("provider down")
    monkeypatch.setattr(vectors, "embed", boom)
    vectors._cache["key"] = None
    assert ids(query.search({**f, "city": "Bangalore"}, conn)[0]) == {"a"}


def test_a_changed_model_is_a_new_space(conn, monkeypatch):
    seeded(conn)
    monkeypatch.setattr(vectors, "MODEL", "other/model")
    assert vectors.run(conn)["embedded"] == 4            # re-embedded, not mixed


def test_the_meaning_text_is_the_work_never_the_place():
    from search import run
    f = {"title_keywords": ["apm", "associate product manager"], "role_family": ["product"],
         "city": "Bangalore", "level": ["junior"]}
    t = run.semantic_text(f)
    assert t == "Associate Product Manager. product."
    assert "Bangalore" not in t and "junior" not in t
    assert run.semantic_text({"city": "Bangalore"}) == ""        # no work named: no meaning-search


def test_meaning_adds_at_most_five_and_never_pushes_out_the_top(conn, monkeypatch):
    for i in range(12):
        add(conn, f"g{i}", title=f"Growth Strategist {i}", description="growth marketing planning")
    add(conn, "sql1", title="Marketing Manager", description="x")
    vectors.run(conn)
    f = {"title_keywords": ["marketing manager"], "city": "Bangalore", "count": 5,
         "semantic": "growth marketing"}
    rows, _ = query.search(f, conn)
    assert rows[0]["source_id"] == "sql1"                  # SQL's rows keep their place
    assert sum(1 for r in rows if r.get("by_meaning")) == query.SEM_MAX
