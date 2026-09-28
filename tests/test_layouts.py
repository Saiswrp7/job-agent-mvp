"""Resume layouts: seven looks, one template, one gate.

Fixtures only. Every PDF goes under tmp_path, and `render.OUT_DIR` is pointed
there, so nothing lands in anyone's artifacts. Compile tests skip without
tectonic and poppler.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import chat                                    # noqa: E402
import vault                                   # noqa: E402
from engine import db                          # noqa: E402
from resume import generate, layouts, render   # noqa: E402

needs_tex = pytest.mark.skipif(
    not (shutil.which("tectonic") and shutil.which("pdftotext")),
    reason="needs tectonic and poppler")


def person() -> dict:
    """Everything a layout has to get right in one resume: merged-letter
    words, a rupee amount, hyphens in links, quotes, and the optional
    sections Indian Placement shows."""
    return {
        "name": "Asha Rao",
        "contact": {"email": "asha@example.com", "phone": "+91-9000000000",
                    "location": "Bengaluru",
                    "website": "https://asha-rao.dev",
                    "linkedin": "https://www.linkedin.com/in/asha-rao"},
        "summary": 'Grew revenue from ₹20 Cr to ₹35 Cr; built a "Growth Brain".',
        "experience": [
            {"company": "AppsFlyer", "title": "Financial Analyst",
             "dates": "Jan 2023 – Present",
             "bullets": ["Cut office costs 12% (₹1.2 Cr) with an efficient review",
                         "Ran A/B Testing across 40 stores"]},
            {"company": "Office Depot", "title": "Associate",
             "dates": "Jan 2021 – Dec 2022",
             "bullets": ["Ran weekly finance reports for 40 stores"]},
        ],
        "education": [{"school": "IIT Madras", "degree": "B.Tech",
                       "dates": "2020", "cgpa": "7.99"},
                      {"school": "DPS Delhi", "degree": "Class XII",
                       "dates": "2016"}],
        "positions": ["Head, Placement Committee (2019)"],
        "skills": {"Tools": ["AppsFlyer", "SQL"], "Growth": ["A/B Testing"]},
    }


# --- every layout, through the one gate ---------------------------------

@needs_tex
@pytest.mark.parametrize("key", list(layouts.LAYOUTS))
def test_every_layout_compiles_and_passes_the_gate(key, tmp_path):
    m = person()
    pdf = render.compile_pdf(render.to_tex(m, key), tmp_path / f"{key}.pdf")
    assert render.gate(pdf, m) == []
    text = render.extract_text(pdf)
    assert "₹1.2 Cr" in text                         # the rupee sign survives
    assert "7.99" in text and " 8 " not in text      # scores never rounded
    assert "Test-" not in text                        # no hyphenated keywords


@needs_tex
def test_indian_placement_opens_with_education_and_shows_positions(tmp_path):
    m = person()
    pdf = render.compile_pdf(render.to_tex(m, "indian_placement"), tmp_path / "p.pdf")
    text = render.extract_text(pdf)
    assert text.index("EDUCATION") < text.index("WORK EXPERIENCE")
    assert "POSITIONS OF RESPONSIBILITY" in text and "Placement Committee" in text
    assert "SUMMARY" not in text


def test_body_size_is_set_exactly_not_by_the_class_option():
    """article ignores 10.5pt; the old template was silently 10pt."""
    tex = render.to_tex(person(), "charter")
    assert r"\fontsize{10.5pt}" in tex and "10.5pt," not in tex.split("\n")[14]


def test_a_heading_cannot_end_a_page_alone():
    tex = render.to_tex(person(), "modern_accent")
    assert tex.count(r"\needspace") == tex.count(r"\section*")


def test_skills_rows_leave_no_trailing_break():
    """A trailing \\\\ after the last row pushed an empty line onto page 2."""
    assert not render._skills(person()).rstrip().endswith(r"\\[4pt]")


def test_education_rows_end_their_paragraph():
    assert render._education(person()).count(r"\par") == 2


def test_the_gate_catches_a_glyph_the_font_dropped(monkeypatch):
    """XCharter printed "₹200 Cr" as "200 Cr" and nothing noticed."""
    m = person()
    monkeypatch.setattr(render, "extract_text", lambda pdf: (
        "ASHA RAO asha@example.com asha-rao.dev linkedin.com/in/asha-rao "
        "AppsFlyer Jan 2023 – Present Office Depot Jan 2021 – Dec 2022 20 Cr"))
    assert any("₹" in p for p in render.gate(Path("x.pdf"), m))


# --- choosing one ---------------------------------------------------------

def test_names_and_descriptions_resolve_to_a_layout():
    assert layouts.resolve("Harvard") == "harvard"
    assert layouts.resolve("something modern") == "modern_accent"
    assert layouts.resolve("the IIM one") == "indian_placement"
    assert layouts.resolve("Charter Professional") == "charter"
    assert layouts.resolve("tech_compact") == "tech_compact"
    assert layouts.resolve("purple with a photo") is None


def test_suggestions_follow_role_and_years():
    assert layouts.suggest("Growth Product Manager", 5) == ["charter", "modern_accent"]
    assert layouts.suggest("Software Engineer", 3)[0] == "tech_compact"
    assert layouts.suggest("Consultant", 4)[0] == "harvard"
    assert layouts.suggest("VP Growth", 14)[0] == "executive"
    assert layouts.suggest("", 1)[0] == "indian_placement"


def test_a_layout_that_hurts_them_gets_one_warning():
    assert "two pages" in layouts.fit_warning("executive", 3)
    assert layouts.fit_warning("executive", 14) is None
    assert layouts.fit_warning("charter", 3) is None


def test_an_unknown_saved_layout_falls_back_instead_of_failing():
    assert layouts.get("deleted_layout")["key"] == layouts.DEFAULT


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


def test_the_layout_they_name_becomes_their_default(conn, monkeypatch):
    monkeypatch.setattr(chat, "_role_years", lambda c: ("PM", 3))
    key, line = chat._pick_layout({"layout": "make it look like Harvard"}, conn)
    assert key == "harvard" and vault.get("resume_layout", conn) == "harvard"
    assert chat._pick_layout({}, conn)[0] == "harvard"      # used next time
    assert "Tell them once" not in line


def test_a_two_pager_at_three_years_is_built_with_one_warning(conn, monkeypatch):
    monkeypatch.setattr(chat, "_role_years", lambda c: ("PM", 3))
    key, line = chat._pick_layout({"layout": "executive"}, conn)
    assert key == "executive" and "Tell them once" in line


def test_a_look_that_matches_nothing_is_asked_about_not_guessed(conn):
    key, line = chat._pick_layout({"layout": "purple with a photo"}, conn)
    assert key is None and line.startswith("ERROR") and "harvard" in line


def test_apply_builds_in_their_saved_layout(conn, monkeypatch, tmp_path):
    """The resume that goes out on "apply" looks like the one they chose."""
    master = tmp_path / "master.json"
    master.write_text('{"name": "X", "experience": []}')
    monkeypatch.setattr(chat.paths, "MASTER", master)
    vault.put("master_resume_path", str(tmp_path / "up.pdf"), source="user", conn=conn)
    vault.put("resume_layout", "modern_accent", source="user", conn=conn)
    seen = {}

    def fake(conn=None, layout=None, **kw):
        seen["layout"] = layout
        pdf = tmp_path / "u.pdf"
        pdf.write_text("x")
        (tmp_path / "upload" / "u").mkdir(parents=True, exist_ok=True)
        (tmp_path / "upload" / "u" / "X_Resume.pdf").write_text("x")
        return {"pdf": pdf}
    monkeypatch.setattr(generate, "build", fake)
    monkeypatch.setattr(generate, "additions", lambda master=None, conn=None: 1)
    monkeypatch.setattr(chat.apply_agent, "browser_for", lambda *a, **k: None)
    monkeypatch.setattr(chat.apply_agent, "start",
                        lambda *a, **k: {"status": "blocked", "message": "off"})
    state = {"turn": 2, "user_message": "apply with my updated resume",
             "picks": [], "resumes": {}, "fixture": "greenhouse_sample",
             "by_ref": {"j": {"title": "PM", "company": "Co", "source": "f",
                              "source_id": "1", "apply_url": "https://e.com"}}}
    chat.run_tool("start_application", {"ref": "j", "resume": "updated"}, state, conn)
    assert seen["layout"] == "modern_accent"


@needs_tex
def test_previews_are_their_own_resume_as_an_image(monkeypatch, tmp_path):
    if not shutil.which("pdftoppm"):
        pytest.skip("needs pdftoppm")
    monkeypatch.setattr(render, "OUT_DIR", tmp_path)
    monkeypatch.setattr(generate, "from_career",
                        lambda master=None, conn=None: (person(), []))
    shown = generate.preview_layouts(["harvard", "modern_accent"])
    assert [p["layout"] for p in shown] == ["harvard", "modern_accent"]
    assert all(Path(p["png"]).stat().st_size > 1000 for p in shown)
