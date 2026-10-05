"""Fixes from Sai's comments in the Telegram eval sheet (2026-09-30).

Run: python -m pytest tests/test_eval_sheet_fixes.py -q

No model, no network, no real profile: every check here is code.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import chat                                        # noqa: E402
import telegram_bot as tg                          # noqa: E402
import vault                                       # noqa: E402
from apply import harness                          # noqa: E402
from engine import db                              # noqa: E402
from resume import structure, verify               # noqa: E402


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


# --- row 29: no em dashes in anything sent -----------------------------------

class _Http:
    def __init__(self):
        self.posts = []

    def post(self, url, json=None, **kw):
        self.posts.append(json)

        class R:
            @staticmethod
            def json():
                return {"ok": True, "result": {}}
        return R()


def test_every_telegram_message_loses_its_em_dashes():
    http = _Http()
    tg.Bot("t", client=http).send(7, "Update — Traya: paused — sign-in")
    assert "—" not in http.posts[0]["text"]


def test_application_updates_are_written_without_em_dashes():
    line = chat.news_lines([{"title": "PM", "company": "Co", "status": "blocked",
                             "outcome": "All filled."}])
    assert "—" not in line and line.startswith("Update: PM at Co. Form filled")


# --- rows 2, 22, 23: short greeting, resume asked with how to send it ---------
# Written fresh by the model each time (Sai: the greeting can't be fixed), so
# the prompt carries what it must cover, never a verbatim text to copy.

def test_greeting_rule_is_two_short_messages_and_resume_steps_come_when_needed():
    """Sai's Telegram eval, 2026-10-01: "Hey I'm Jojo, how can I help?", then a
    second message with the three things; the resume is asked when needed."""
    prompt = (Path(__file__).resolve().parent.parent / "prompts" / "chat.md").read_text()
    rule = prompt[prompt.index("**Greeting**"):prompt.index("**Their resume: ask only when you need it**")]
    # Sai, 2026-10-01 night: the same greeting every time read as static, so
    # the rule says what to cover and that it is written fresh each time.
    assert "[[next]]" in rule and "find jobs" in rule and "apply for them" in rule
    assert "own words" in rule and "Hey, I'm Jojo" not in rule
    assert "→ File" not in rule
    later = prompt[prompt.index("**Their resume: ask only when you need it**"):]
    assert "Attach your resume" in later[:400] and "→ File" not in later[:400]
    assert not hasattr(tg, "WELCOME")


# --- row 24: no links in a job list unless asked -----------------------------

LIST = ("1. **Growth Manager, Swiggy, Bangalore** · 4d ago\n"
        "   Owns CAC budget.\n"
        "   https://jobs.smartrecruiters.com/SWIGGY/1\n\n"
        "Want me to apply to one?")


def test_a_job_list_goes_out_without_links():
    out = chat.hide_links(LIST, "growth roles in bangalore")
    assert "https://" not in out and "Swiggy" in out and "apply" in out


@pytest.mark.parametrize("said", ["send me the links", "link for 1 please",
                                  "apply to 1"])
def test_links_stay_when_they_ask(said):
    assert "https://" in chat.hide_links(LIST, said)


# --- row 27: what they tell a form is kept for the next form ------------------

def _asked(answer: str) -> list:
    return [{"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "ask_user",
                 "input": {"question": "Notice period?"}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": answer}]}]


def test_an_answer_they_gave_a_form_is_saved(conn):
    h = _asked("30 days, and I'm in Bengaluru")
    harness.remember_answer(conn, h, {"name": "Notice Period", "value": "30 days"})
    assert vault.get("notice_period", conn) == "30 days"


def test_a_value_they_never_said_is_not_saved(conn):
    h = _asked("30 days")
    harness.remember_answer(conn, h, {"name": "Expected CTC", "value": "40 LPA"})
    assert vault.get("expected_ctc", conn) is None


def test_a_field_with_no_vault_key_is_not_saved(conn):
    h = _asked("Yes")
    assert harness.remember_answer(conn, h, {"name": "Why us?", "value": "Yes"}) is None


# --- rows 31-33: tailored first, and what changed is said --------------------

def test_the_resume_question_is_short_and_offers_tailored_first():
    q = chat.resume_question(None, updates=False)
    assert q == "Which resume: tailored or yours?" and "best odds" not in q


def test_a_tailor_reports_what_landed_and_why():
    master = {"summary": "Growth person.", "experience": [
        {"company": "Acme", "title": "PM", "dates": "2020",
         "bullets": ["Ran experiments on checkout", "Wrote docs", "Cut churn 5%"]}]}
    patch = {"experience": [{"index": 0, "keep": [2, 0],
                             "rewrites": {"0": "Ran A/B tests on checkout"},
                             "why": {"0": "the JD asks for A/B testing"}}]}
    changes: list[str] = []
    verify.apply_patch(master, patch, set(), changes=changes)
    text = "\n".join(changes)
    assert "moved" in text and "left out 1" in text
    assert "because the JD asks for A/B testing" in text


def test_a_reverted_rewrite_is_not_reported_as_a_change():
    master = {"experience": [{"company": "Acme", "title": "PM", "dates": "2020",
                              "bullets": ["Grew revenue"]}]}
    patch = {"experience": [{"index": 0, "keep": [0],
                             "rewrites": {"0": "Grew revenue 80%"}}]}
    changes: list[str] = []
    verify.apply_patch(master, patch, set(), changes=changes)
    assert changes == []


# --- row 32: nothing changed, nothing rebuilt ---------------------------------

def test_an_unchanged_record_is_not_rebuilt(conn, monkeypatch, tmp_path):
    from resume import generate
    pdf = tmp_path / "r.pdf"
    pdf.write_bytes(b"%PDF")
    calls = []

    def fake_build(conn=None, layout=None):
        calls.append(1)
        return {"master": {"summary": "S", "experience": []}, "notes": [],
                "pdf": pdf}

    monkeypatch.setattr(generate, "build", fake_build)
    monkeypatch.setattr(chat, "_record_print", lambda c, l: "same")
    state = {"channel": "telegram", "picks": [], "resumes": {}}
    first = chat.run_tool("build_resume", {}, state, conn)
    again = chat.run_tool("build_resume", {}, state, conn)
    assert len(calls) == 1 and "NOT rebuilt" in again and "NOT sent" in first


# --- row 30: text first, the PDF only when they're happy ----------------------

def test_a_build_sends_no_file_until_asked(conn, monkeypatch, tmp_path):
    from resume import generate
    pdf = tmp_path / "r.pdf"
    pdf.write_bytes(b"%PDF")
    monkeypatch.setattr(generate, "build", lambda conn=None, layout=None: {
        "master": {"summary": "S", "experience": []}, "notes": [], "pdf": pdf})
    monkeypatch.setattr(chat, "_record_print", lambda c, l: "p1")
    state = {"channel": "telegram", "picks": [], "resumes": {}}
    chat.run_tool("build_resume", {}, state, conn)
    assert not state.get("outbox")


# --- rows 34-35: structure is theirs to change --------------------------------

def test_order_heading_and_headline_are_saved(conn):
    s, problems = structure.update(conn, order=["skills"],
                                   rename={"experience": "Work Experience"},
                                   hide=["projects"],
                                   headline="Growth Manager | CRM")
    assert not problems
    assert s["order"][0] == "skills" and "projects" not in s["order"]
    m = structure.apply({"name": "A"}, conn)
    assert m["_titles"]["experience"] == "Work Experience"
    assert m["headline"] == "Growth Manager | CRM"


def test_a_heading_an_ats_may_not_read_is_refused_with_options(conn):
    s, problems = structure.update(conn, rename={"summary": "My Journey"})
    assert "summary" not in (s.get("titles") or {})
    assert problems and "Professional Summary" in problems[0]


def test_render_follows_their_order_and_prints_the_headline():
    from resume import render
    m = {"name": "Asha", "summary": "S", "skills": ["SQL"],
         "experience": [{"company": "Acme", "title": "PM", "dates": "2020",
                         "bullets": ["x"]}],
         "_order": ["skills", "experience", "summary"],
         "_titles": {"experience": "Work Experience"}, "headline": "PM | Growth"}
    tex = render.to_tex(m, "charter")
    assert tex.index("Skills") < tex.index("Work Experience") < tex.index("Summary}")
    assert "PM | Growth" in tex


# --- Sai's expected outcomes, Telegram eval 2026-10-01 -------------------------

def test_a_resume_chosen_for_a_job_is_not_asked_again(tmp_path, monkeypatch):
    """'Did my Swiggy application go through?' got 'which resume?' 4 times."""
    from engine import db as _db
    c = _db.connect(":memory:")
    assert chat._job_resume(c, "swiggy-growth") is None
    chat._job_resume(c, "swiggy-growth", "existing")
    assert chat._job_resume(c, "swiggy-growth") == "existing"
    assert chat._job_resume(c, "other-job") is None


@pytest.mark.parametrize("said", ["base one", "use my current resume", "mine", "the one i sent"])
def test_plain_ways_of_saying_their_own_resume(said):
    assert chat.said_resume({"user_message": said}, "tailored") == {"existing"}


def test_an_update_is_its_own_telegram_message():
    from tests.test_telegram import FakeBot, _worker
    bot = FakeBot()
    w, _ = _worker(Path("/tmp"), bot, replies={"hi": "Update: PM at Co. Not sent.\n[[next]]\nNo worries 👍"})
    w.handle({"chat": {"id": 7}, "text": "hi"})
    assert [t for _, t in bot.sent] == ["Update: PM at Co. Not sent.", "No worries 👍"]


def test_a_refused_submit_is_not_sent():
    from apply import pagecheck
    before = "Apply\nSubmit Application"
    after = "We couldn't submit your application\nYour application submission was flagged as possible spam."
    assert pagecheck.refused(before, after).startswith("We couldn't submit")


def test_a_place_and_sub_headings_are_not_bullets():
    from resume.parse import tidy
    m = {"experience": [{"company": "Paytm", "bullets": [
        "Bengaluru", "User Growth & Retention",
        "Owned end-to-end user growth strategy across acquisition and retention.",
        "Revenue & Monetization", "Cut costs ~25% with Pareto targeting."]}]}
    e = tidy(m)["experience"][0]
    assert e["location"] == "Bengaluru"
    assert e["bullets"] == ["Owned end-to-end user growth strategy across acquisition and retention.",
                            "Cut costs ~25% with Pareto targeting."]


def test_an_empty_ranker_answer_is_retried_then_shown_unranked(monkeypatch):
    from search import rank
    calls = []
    monkeypatch.setattr(rank.llm, "complete_json", lambda *a, **k: calls.append(1) or {"picks": [], "dropped": []})
    monkeypatch.setattr(rank.llm, "prompt", lambda name: "{profile}{soft_criteria}")
    rows = [{"source": "keka", "source_id": str(i), "title": f"APM {i}", "company": "C", "city": "Blr",
             "description": "", "posted_at": "2026-09-30"} for i in range(3)]
    monkeypatch.setattr(rank, "_render", lambda rows: "rows")
    chosen, cut = rank.pick(rows, {"count": 5}, "apm roles")
    assert len(calls) == 2 and [r["title"] for r in chosen] == ["APM 0", "APM 1", "APM 2"]


def test_an_unusable_ranker_answer_shows_only_jobs_whose_title_matches(monkeypatch):
    """One ranker call that named no job in the list: the first five rows went
    out as results, general marketing jobs for a programmatic-ads search
    (2026-10-01). Only a title match is safe to show unchecked."""
    from search import rank
    calls = []
    monkeypatch.setattr(rank.llm, "complete_json", lambda *a, **k: calls.append(1) or {
        "picks": [{"source": "keka", "source_id": "not-in-the-list", "reason": "fits"}], "dropped": []})
    monkeypatch.setattr(rank.llm, "prompt", lambda name: "{profile}{soft_criteria}")
    monkeypatch.setattr(rank, "_render", lambda rows: "rows")
    titles = ["Social Media Manager", "Digital Marketing Specialist", "Programmatic Trader", "Ad Ops Lead"]
    rows = [{"source": "keka", "source_id": str(i), "title": t, "company": "C", "role_family": "marketing"}
            for i, t in enumerate(titles)]
    f = {"count": 5, "title_keywords": ["programmatic", "ad ops", "advertising"], "role_family": ["marketing"]}
    chosen, cut = rank.pick(rows, f, "programmatic roles")
    assert len(calls) == 1 and cut == []
    assert [r["title"] for r in chosen] == ["Programmatic Trader", "Ad Ops Lead"]
    chosen, _ = rank.pick(rows[:2], f, "programmatic roles")
    assert chosen == []                                   # "nothing matched", not the label's guesses


def test_with_no_title_words_the_label_or_the_filters_decide(monkeypatch):
    from search import rank
    monkeypatch.setattr(rank.llm, "complete_json", lambda *a, **k: {"picks": [], "dropped": []})
    monkeypatch.setattr(rank.llm, "prompt", lambda name: "{profile}{soft_criteria}")
    monkeypatch.setattr(rank, "_render", lambda rows: "rows")
    rows = [{"source": "keka", "source_id": "1", "title": "Brand Lead", "company": "C", "role_family": "marketing"},
            {"source": "keka", "source_id": "2", "title": "Line Cook", "company": "C", "role_family": None}]
    chosen, _ = rank.pick(rows, {"count": 5, "role_family": ["marketing"]}, "marketing roles")
    assert [r["title"] for r in chosen] == ["Brand Lead"]   # an unlabelled row waits for the ranker
    chosen, _ = rank.pick(rows, {"count": 5, "companies": ["C"]}, "jobs at C")
    assert len(chosen) == 2                                 # the SQL held everything they said
    rows[1]["role_family"] = "sales,marketing"
    chosen, _ = rank.pick(rows, {"count": 5, "role_family": ["marketing"]}, "marketing roles")
    assert len(chosen) == 1                                 # only the main label counts
    rows[1]["role_family"] = "marketing,sales"
    chosen, _ = rank.pick(rows, {"count": 5, "role_family": ["marketing"]}, "marketing roles")
    assert len(chosen) == 2


@pytest.mark.parametrize("said,default,want", [
    ("just apply to the 2nd one too, stop asking me", "tailored", {"tailored"}),
    ("don't ask, apply", None, {"tailored"}),
    ("your call", "existing", {"existing"})])
def test_stop_asking_takes_the_default(said, default, want):
    assert chat.said_resume({"user_message": said}, default) == want


def test_an_update_is_reworded_but_never_claims_sent(monkeypatch):
    from engine import db as _db
    c = _db.connect(":memory:")
    c.execute("INSERT INTO applications (id, source, source_id, company, title, status) "
              "VALUES (1,'ashby','1','metaforms','PM','blocked')")
    row = {"id": 1, "title": "PM", "company": "metaforms", "status": "blocked",
           "outcome": "Not sent, the site refused it."}
    monkeypatch.setattr(chat.llm, "complete", lambda *a, **k: "metaforms didn't go through, the site blocked it. Want another one?")
    assert chat.phrase_update([row], c).startswith("metaforms didn't go through")
    monkeypatch.setattr(chat.llm, "complete", lambda *a, **k: "Your metaforms application went through! 🚀")
    assert "went through" not in chat.phrase_update([row], c)        # false claim replaced
    monkeypatch.setattr(chat.llm, "complete", lambda *a, **k: 1 / 0)
    assert chat.phrase_update([row], c) == chat.news_lines([row])     # model down: template


def test_a_waiting_update_carries_every_question(monkeypatch):
    """Loop, 2026-10-01: the update stopped at '1.' and the questions were lost."""
    row = {"id": 1, "title": "Director, Growth & Strategy", "company": "Loop", "status": "waiting",
           "outcome": "I need 4 missing details:\n1. Current salary\n2. Expected salary\n3. Notice period\n4. Gender"}
    line = chat.news_lines([row])
    assert all(q in line for q in ("1. Current salary", "2. Expected salary", "3. Notice period", "4. Gender"))
    monkeypatch.setattr(chat.llm, "complete", lambda *a, **k: "Loop's paused, need a few answers 👀")
    out = chat.phrase_update([row])
    assert out.startswith("Loop's paused") and "4. Gender" in out


# --- 2026-10-05: "show me more jobs" turned growth into marketing/PM --------

def test_nearby_work_they_did_not_name_is_held_not_shown():
    from search.run import hold_unasked
    f = {"title_keywords": ["growth"], "role_family": ["growth", "marketing", "product"]}
    said = "Yes hunt for more Bangalore growth roles Show me more jobs .."
    picks = [{"title": "Manager - Performance Marketing", "role_family": "marketing"},
             {"title": "Product Manager II", "role_family": "product"},
             {"title": "City Growth Manager", "role_family": "growth,operations"}]
    shown, held = hold_unasked(picks, f, said)
    assert [p["title"] for p in shown] == ["City Growth Manager"]
    assert len(held) == 2


def test_a_kind_of_work_they_named_is_shown_without_its_word_in_the_title():
    from search.run import hold_unasked
    f = {"title_keywords": ["growth"], "role_family": ["growth", "marketing"]}
    shown, held = hold_unasked(
        [{"title": "Manager - Performance Marketing", "role_family": "marketing"}],
        f, "growth marketing roles in Bangalore")
    assert len(shown) == 1 and not held


def test_title_words_not_in_their_words_hold_nothing():
    from search.run import hold_unasked
    f = {"title_keywords": ["product manager"], "role_family": ["product"]}
    picks = [{"title": "Product Owner", "role_family": "product"}]
    assert hold_unasked(picks, f, "PM jobs") == (picks, [])
