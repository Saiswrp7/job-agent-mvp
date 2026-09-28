"""Resume Phase 1: merged letters, margins, visible links, the upload name.

Run: python -m pytest tests/ -q

Fixtures only. Every PDF is written under pytest's tmp_path; `render.OUT_DIR`
is pointed there too, so nothing lands in the person's artifacts.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from resume import parse, render              # noqa: E402

needs_tex = pytest.mark.skipif(
    not (shutil.which("tectonic") and shutil.which("pdftotext")),
    reason="needs tectonic and poppler")

#: Every word here has an fi/fl/ffi pair the old template merged into one glyph.
LIGATURE_WORDS = ("AppsFlyer", "Financial", "office", "efficient")


def person(**contact) -> dict:
    return {
        "name": "Asha Rao",
        "contact": {"email": "asha@example.com", "phone": "+91-9000000000",
                    "location": "Bengaluru", **contact},
        "summary": "Growth product manager.",
        "experience": [
            {"company": "AppsFlyer", "title": "Financial Analyst",
             "dates": "Jan 2023 – Present",
             "bullets": ["Cut office costs 12% with an efficient vendor review"]},
            {"company": "Office Depot", "title": "Associate",
             "dates": "Jan 2021 – Dec 2022",
             "bullets": ["Ran weekly finance reports for 40 stores"]},
        ],
        "education": [{"school": "IIT Madras", "degree": "B.Tech",
                       "dates": "2020"}],
        "skills": {"Tools": ["AppsFlyer", "SQL"]},
    }


# --- merged letters ---------------------------------------------------------

@needs_tex
def test_appsflyer_survives_extraction_letter_for_letter(tmp_path):
    """"AppsFlyer" came out of the PDF as "Appsﬂyer" (U+FB02), and a recruiter
    searching AppsFlyer never found the resume."""
    m = person()
    pdf = render.compile_pdf(render.to_tex(m), tmp_path / "r.pdf")
    text = render.extract_text(pdf)
    for word in LIGATURE_WORDS:
        assert word in text, word
    assert not any("ﬀ" <= ch <= "ﬆ" for ch in text)
    assert render.gate(pdf, m) == []


@needs_tex
def test_dates_still_get_their_dash(tmp_path):
    """Switching ligatures off must not switch off -- → –, or every date breaks."""
    pdf = render.compile_pdf(render.to_tex(person()), tmp_path / "r.pdf")
    assert "Jan 2023 – Present" in render.extract_text(pdf)


def test_gate_fails_on_merged_letters_and_icon_glyphs(monkeypatch):
    monkeypatch.setattr(render, "extract_text",
                        lambda pdf: "Asha Rao asha@example.com Appsﬂyer  x�")
    problems = " ".join(render.gate(Path("x.pdf"), {"name": "Asha Rao"}))
    assert "U+FB02" in problems and "U+E001" in problems and "U+FFFD" in problems


def test_gate_fails_on_a_token_that_only_matches_after_normalising(monkeypatch):
    """The company is "there" to anything that normalises, and missing to a
    literal search. The literal search is what the recruiter runs."""
    m = {"name": "Asha Rao", "experience": [{"company": "Profile Co"}]}
    monkeypatch.setattr(render, "extract_text", lambda pdf: "ASHA RAO Proﬁle Co")
    problems = render.gate(Path("x.pdf"), m)
    assert any("Profile Co" in p and "normalising" in p for p in problems)


def test_capitalised_name_is_not_a_leak(monkeypatch):
    """The name prints in capitals; that is layout, not a lost token."""
    monkeypatch.setattr(render, "extract_text", lambda pdf: "ASHA RAO")
    assert render.gate(Path("x.pdf"), {"name": "Asha Rao"}) == []


# --- margins ----------------------------------------------------------------

def test_margins_are_half_an_inch():
    """The default layout (Charter); every layout sets its own, none below 0.5."""
    tex = render.to_tex(person())
    assert "margin=0.5in" in tex and "0.45in]" not in tex


# --- links on the contact line ----------------------------------------------

def test_every_link_is_printed_as_an_address_not_a_word():
    m = person(website="https://asha.dev/", linkedin="https://www.linkedin.com/in/asha-rao",
               github="https://github.com/asha_rao", x="https://x.com/asharao")
    line = render._contact(m)
    assert "{LinkedIn}" not in line
    for shown in ("asha.dev", "linkedin.com/in/asha-rao",
                  r"github.com/asha\_rao", "x.com/asharao"):
        assert shown in line, shown
    # Order is fixed: portfolio, LinkedIn, GitHub, X.
    assert line.index("asha.dev") < line.index("linkedin") < line.index("github") < line.index("x.com")


def test_missing_links_and_anchor_text_are_skipped():
    m = person(linkedin="LinkedIn", github=None)
    assert render.links(m) == []
    assert r"\href" not in render._contact(m)


def test_older_key_spellings_still_render():
    m = person(portfolio="https://asha.dev", twitter="https://twitter.com/asharao")
    assert render.links(m) == ["https://asha.dev", "https://twitter.com/asharao"]


@needs_tex
def test_links_come_out_of_the_pdf_as_text(tmp_path):
    m = person(website="https://asha.dev", github="https://github.com/asha_rao")
    pdf = render.compile_pdf(render.to_tex(m), tmp_path / "r.pdf")
    text = render.extract_text(pdf)
    assert "asha.dev" in text and "github.com/asha_rao" in text
    assert render.gate(pdf, m) == []


def test_gate_fails_when_a_link_is_only_behind_a_word(monkeypatch):
    m = {"name": "Asha Rao", "contact": {"linkedin": "https://linkedin.com/in/asha"}}
    monkeypatch.setattr(render, "extract_text", lambda pdf: "ASHA RAO LinkedIn")
    assert any("not visible" in p for p in render.gate(Path("x.pdf"), m))


# --- the name the employer sees ---------------------------------------------

def test_upload_name_is_their_name_and_nothing_else():
    assert render.upload_name({"name": "BUGATA SAI SWAROOP"}) == "Bugata_Sai_Swaroop_Resume.pdf"
    assert render.upload_name({"name": "Asha  Rao"}) == "Asha_Rao_Resume.pdf"
    assert render.upload_name({}) == "Resume.pdf"


@needs_tex
def test_render_leaves_an_upload_copy_beside_the_stored_pdf(tmp_path, monkeypatch):
    monkeypatch.setattr(render, "OUT_DIR", tmp_path)
    stored = render.render(person(), "Asha_Rao_Paytm_Growth.pdf")
    assert stored.name == "Asha_Rao_Paytm_Growth.pdf"    # storage name unchanged
    up = render.upload_path(stored)
    assert up.name == "Asha_Rao_Resume.pdf"
    assert up.read_bytes() == stored.read_bytes()


@needs_tex
def test_a_failed_rebuild_leaves_no_old_copy_to_send(tmp_path, monkeypatch):
    monkeypatch.setattr(render, "OUT_DIR", tmp_path)
    stored = render.render(person(), "job.pdf")
    monkeypatch.setattr(render, "gate", lambda pdf, m: ["broken"])
    with pytest.raises(RuntimeError):
        render.render(person(), "job.pdf")
    with pytest.raises(FileNotFoundError):
        render.upload_path(stored)


# --- parsing links out of an uploaded resume --------------------------------

def test_profiles_typed_as_text_are_found():
    text = ("Bengaluru | github.com/asha_rao | www.linkedin.com/in/asha-rao | "
            "x.com/asharao | github.com/someone/repo | inbox.com/x")
    assert parse.text_links(text) == [
        "https://github.com/asha_rao", "https://linkedin.com/in/asha-rao",
        "https://x.com/asharao"]


def test_a_repo_link_is_not_a_github_profile():
    """The repo may not even be theirs; only github.com/<name> is a profile."""
    m = parse._fix_links({"contact": {}}, ["https://github.com/someone/repo"])
    assert m["contact"]["github"] is None
    assert m["contact"]["website"] is None     # a repo is not a portfolio either


def test_each_link_lands_in_its_own_key():
    links = ["mailto:asha@example.com", "https://asha.dev",
             "https://www.linkedin.com/in/asha-rao", "https://github.com/asha_rao",
             "https://twitter.com/asharao"]
    c = parse._fix_links({"contact": {"linkedin": "LinkedIn"}}, links)["contact"]
    assert c == {"linkedin": "https://www.linkedin.com/in/asha-rao",
                 "github": "https://github.com/asha_rao",
                 "x": "https://twitter.com/asharao",
                 "website": "https://asha.dev"}


def test_model_spelling_twitter_becomes_x():
    c = parse._fix_links({"contact": {"twitter": "https://x.com/asharao"}}, [])["contact"]
    assert c["x"] == "https://x.com/asharao" and "twitter" not in c


def test_vault_keeps_github_and_x_only_as_urls():
    rows = parse.vault_rows({"name": "A B", "contact": {
        "github": "https://github.com/ab", "x": "@ab"}})
    assert rows["github"] == "https://github.com/ab"
    assert "x" not in rows
