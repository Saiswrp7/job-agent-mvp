"""The four resume bugs the 2026-09-28 launch evals found.

Run: python -m pytest tests/ -q

1. Grouped skills lost everything but the group names.
2. A summary naming an older employer first failed the order check.
3. A role with no bullets was left off the page, then the gate blocked it.
4. A bullet opening with a verb ("Collaborated") read as an invented name.

Fixtures only; PDFs go under pytest's tmp_path.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import career                                  # noqa: E402
from resume import generate, render, verify    # noqa: E402

needs_tex = pytest.mark.skipif(
    not (shutil.which("tectonic") and shutil.which("pdftotext")),
    reason="needs tectonic and poppler")


def person(**over) -> dict:
    m = {
        "name": "Asha Rao",
        "contact": {"email": "asha@example.com"},
        "summary": "Growth manager, formerly at Paytm, now at Lenskart.",
        "experience": [
            {"company": "Lenskart", "title": "Growth Manager",
             "dates": "Jan 2023 – Present", "bullets": ["Grew repeat orders 18%"]},
            {"company": "Paytm", "title": "Associate",
             "dates": "Jun 2020 – Dec 2022", "bullets": ["Ran 40 A/B tests"]},
        ],
        "education": [],
        "skills": {"Growth": ["SEO", "CRM"], "Tools": ["SQL", "Amplitude"]},
    }
    m.update(over)
    return m


# --- 1. grouped skills ------------------------------------------------------

def test_grouped_skills_keep_their_skills(monkeypatch):
    monkeypatch.setattr(career, "all", lambda conn=None: [])
    out, _ = generate.from_career(person())
    assert out["skills"] == {"Growth": ["SEO", "CRM"], "Tools": ["SQL", "Amplitude"]}


def test_a_skill_said_in_chat_joins_under_other(monkeypatch):
    said = [{"source": "chat", "kind": "skill", "text": "Figma", "company": None},
            {"source": "chat", "kind": "skill", "text": "SQL", "company": None}]
    monkeypatch.setattr(career, "all", lambda conn=None: said)
    out, _ = generate.from_career(person())
    assert out["skills"]["Other"] == ["Figma"]          # SQL was already there
    assert generate.additions(person()) == 1


def test_a_flat_skill_list_still_works(monkeypatch):
    monkeypatch.setattr(career, "all", lambda conn=None: [])
    out, _ = generate.from_career(person(skills=["SEO", "SQL"]))
    assert out["skills"] == ["SEO", "SQL"]


# --- 2. order check ---------------------------------------------------------

def test_summary_naming_an_older_employer_first_is_not_columns(monkeypatch):
    text = ("ASHA RAO asha@example.com Growth manager, formerly at Paytm, now at Lenskart. "
            "Lenskart Jan 2023 – Present Growth Manager Grew repeat orders 18% "
            "Paytm Jun 2020 – Dec 2022 Associate Ran 40 A/B tests")
    monkeypatch.setattr(render, "extract_text", lambda pdf: text)
    assert render.gate(Path("x.pdf"), person()) == []


def test_real_columns_still_fail(monkeypatch):
    text = ("ASHA RAO asha@example.com Paytm Jun 2020 – Dec 2022 Associate Ran 40 A/B tests "
            "Lenskart Jan 2023 – Present Growth Manager Grew repeat orders 18%")
    monkeypatch.setattr(render, "extract_text", lambda pdf: text)
    assert any("out of order" in p for p in render.gate(Path("x.pdf"), person()))


# --- 3. a role with no bullets ----------------------------------------------

def test_a_role_with_no_bullets_still_prints_its_line():
    m = person()
    m["experience"][1]["bullets"] = []
    tex = render.to_tex(m)
    assert "Paytm" in tex and "Jun 2020" in tex


@needs_tex
def test_a_role_with_no_bullets_passes_the_gate(tmp_path):
    m = person()
    m["experience"][1]["bullets"] = []
    pdf = render.compile_pdf(render.to_tex(m), tmp_path / "r.pdf")
    assert render.gate(pdf, m) == []


# --- 4. verbs are not names -------------------------------------------------

@pytest.mark.parametrize("rewrite", [
    "Collaborated with sales to ship the pricing page",
    "Leveraged cohort data to cut churn",
    "Wrote the onboarding flow. Used cohort data to cut churn",
])
def test_an_opening_verb_is_not_an_invented_name(rewrite):
    original = "worked with sales on the pricing page and cohort data to cut churn"
    assert verify.check_bullet(original, rewrite) == []


def test_a_tool_opening_the_sentence_is_still_a_name():
    problems = verify.check_bullet("built dashboards for the team",
                                   "Amplitude dashboards built for the team")
    assert any("Amplitude" in p for p in problems)


def test_a_name_mid_sentence_is_still_a_name():
    problems = verify.check_bullet("worked with sales", "Collaborated with Salesforce")
    assert any("Salesforce" in p for p in problems)
