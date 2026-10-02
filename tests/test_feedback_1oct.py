"""Sai's full Telegram test, 2026-10-01 night, and what each point became.

- City and country: "India" was saved over "Bengaluru" and a real Swiggy
  application went out with City: India.
- Years of experience asked although the resume's first line says "4+ years".
- The approval card cut every answer at 120 characters ("incomplete").
- Tailored for one job: build and show it first; a batch still builds unseen.
- No repeated jobs: shown and applied jobs stay out of the next search.
- "What is Konovo?": from the company's own job post (and site), never memory.
No network: in-memory databases only.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import chat                                      # noqa: E402
import company                                   # noqa: E402
import vault                                     # noqa: E402
from apply import confirm, harness               # noqa: E402
from resume import parse                         # noqa: E402
from search import query                         # noqa: E402


def _vault(rows):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE vault (key TEXT PRIMARY KEY, value TEXT, source TEXT, updated_at TEXT)")
    c.executemany("INSERT INTO vault (key, value, source) VALUES (?, ?, ?)", rows)
    return c


# --- city and country ----------------------------------------------------------

def test_country_is_its_own_fact_and_comes_from_the_phone():
    assert vault.match_key("Country/Region") == "country"
    assert vault.match_key("Country") == "country"
    assert vault.match_key("City") == "location"
    c = _vault([("phone", "+91-9876543210", "resume"), ("location", "Bengaluru", "resume")])
    got = vault.slice_for(["Country/Region", "City"], c)
    assert got == {"Country/Region": "India", "City": "Bengaluru"}


def test_a_country_saved_as_location_is_never_typed_into_a_city_box():
    c = _vault([("location", "India", "user")])
    assert "City" not in vault.slice_for(["City"], c)          # asked, not "India"


def test_a_country_answer_never_overwrites_the_city():
    c = _vault([("location", "Bengaluru", "resume")])
    history = [{"role": "assistant", "content": [{"type": "tool_use", "id": "q1", "name": "ask_user",
                                                  "input": {"question": "Where are you based?"}}]},
               {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "q1",
                                             "content": "1. india 2. LinkedIn"}]}]
    saved = harness.remember_answer(c, history, {"name": "Current location", "value": "India"})
    assert saved == {"key": "country", "value": "India"}
    assert vault.get("location", c) == "Bengaluru"


# --- years from the resume -----------------------------------------------------

def test_years_of_experience_come_from_the_resume_summary():
    master = {"name": "Bugata Sai Swaroop",
              "summary": "Growth Product Manager with 4+ years scaling revenue and retention.",
              "contact": {"location": "Bengaluru"}, "experience": []}
    rows = parse.vault_rows(master)
    assert rows["years_experience"] == "4"
    assert rows["location"] == "Bengaluru"
    assert "first_name" not in rows                      # three words: asked once


# --- the approval card shows answers whole ---------------------------------------

def test_long_answers_are_shown_in_full():
    why = ("I have spent my career in growth marketing and CRM, most recently owning CRM revenue "
           "and lifecycle monetization at Lenskart, and this role is a direct continuation.")
    q = confirm.question({"title": "Manager", "company": "Swiggy"}, {"Why us": why, "Name": "Sai"})
    assert why in q
    huge = {f"Q{i}": "x" * 900 for i in range(8)}             # past Telegram's limit
    assert len(confirm.question({"title": "T", "company": "C"}, huge)) < 4096


# --- tailored: one job is shown first, a batch is not -----------------------------

def test_one_job_versus_a_batch():
    assert chat._batch_message("Apply 5 and 1")
    assert chat._batch_message("apply to all of them")
    assert not chat._batch_message("2 can you apply for me that")
    state = {"user_message": "Tailored", "resume_asked_turn": 4, "asked_refs": {4: {"a"}}}
    assert not chat._is_batch(state, "a")
    state["asked_refs"] = {4: {"a", "b"}}
    assert chat._is_batch(state, "a")


# --- no repeated jobs -------------------------------------------------------------

def test_shown_and_applied_jobs_are_left_out_of_the_query():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE applications (id INTEGER PRIMARY KEY, source TEXT, source_id TEXT)")
    c.execute("INSERT INTO applications (source, source_id) VALUES ('lever', 'cred-1')")
    chat._mark_seen(c, [{"source": "smartrecruiters", "source_id": "swiggy-9"}])
    seen = chat._seen_jobs(c)
    assert set(seen) == {"smartrecruiters:swiggy-9", "lever:cred-1"}
    sql, params, f = query.build({"title_keywords": ["growth"], "exclude": seen})
    assert "NOT IN" in sql and "lever:cred-1" in params
    assert f["exclude"] == seen                              # survives relax steps
    assert chat._SEE_AGAIN.search("show me those again")
    assert not chat._SEE_AGAIN.search("growth roles in Bangalore")


# --- what a company is ---------------------------------------------------------------

def test_company_comes_from_a_paragraph_that_names_it():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE jobs (company TEXT, description TEXT, posted_at TEXT)")
    c.execute("INSERT INTO jobs VALUES ('CRED', ?, '2026-10-01')",
              ("Prefr is a digital-first lending platform.\n\nWe are hiring a Finance Manager.",))
    c.execute("INSERT INTO jobs VALUES ('CRED', ?, '2026-09-20')",
              ("CRED is an exclusive community for India's most trustworthy individuals.\n\n"
               "We are an equal opportunity employer.",))
    got = company.from_job_post("CRED", c)
    assert got.startswith("CRED is an exclusive community") and "Prefr" not in got
    assert "equal opportunity" not in got
    assert "Nothing found" in company.about("Unknownco", c)         # never from memory


# --- second round (Loop run, 2026-10-01 night) -------------------------------------

def test_a_date_answer_is_kept_whatever_the_format():
    from apply.harness import _same_date
    assert _same_date("1995-03-14", "Date of birth 14 march 1995") == "1995-03-14"
    assert _same_date("14/03/1995", "dob: 14th March, 1995") == "1995-03-14"
    assert _same_date("1995-03-14", "born March 14, 1995") == "1995-03-14"
    assert _same_date("1995-03-15", "Date of birth 14 march 1995") is None
    c = _vault([])
    history = [{"role": "assistant", "content": [{"type": "tool_use", "id": "q", "name": "ask_user",
                                                  "input": {"question": "Date of birth?"}}]},
               {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "q",
                                             "content": "Date of birth 14 march 1995"}]}]
    saved = harness.remember_answer(c, history, {"name": "Date of Birth", "value": "1995-03-14"})
    assert saved == {"key": "date_of_birth", "value": "1995-03-14"}


def test_one_key_per_job_whatever_the_ref_spelling():
    job = {"source": "keka", "source_id": "163387", "company": "Loop"}
    assert chat._key_of(job, "loop-founders-office") == chat._key_of(job, "loop-founder-s-office-growth")
    assert chat._key_of({}, " [x-1] ") == "x-1"
