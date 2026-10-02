"""Which resume goes out: your file, updated or tailored, chosen on every apply.

In-memory database, a temporary master.json, and every slow thing stubbed —
no browser, no model, no PDF. Nothing here reads the real profile.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import chat      # noqa: E402
import vault     # noqa: E402
from engine import db   # noqa: E402

JOBS = {f"job{i}": {"title": f"PM {i}", "company": f"Co{i}", "source": "fixture",
                    "source_id": str(i), "apply_url": f"https://example.com/{i}"}
        for i in (1, 2, 3)}


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A person with an uploaded resume on file, and a record of what got sent."""
    conn = db.connect(":memory:")
    master = tmp_path / "master.json"
    master.write_text(json.dumps({"name": "X", "experience": []}))
    monkeypatch.setattr(chat.paths, "MASTER", master)
    vault.put("master_resume_path", str(tmp_path / "upload.pdf"),
              source="user", conn=conn)

    sent = []
    monkeypatch.setattr(chat.apply_agent, "browser_for",
                        lambda job, app_id, fixture=None: None)
    monkeypatch.setattr(
        chat.apply_agent, "start",
        lambda app_id, job, browser, path, c: sent.append(path) or
        {"status": "blocked", "message": "filled, submit is off"})

    from resume import generate, tailor
    built = []

    def fake_generate(conn=None, **kw):
        built.append("updated")
        return {"pdf": rendered(tmp_path / "updated.pdf")}

    def fake_tailor(job, conn=None, wants="", **kw):
        built.append("tailored")
        return {"pdf": rendered(tmp_path / f"tailored_{job['company']}.pdf")}

    monkeypatch.setattr(generate, "build", fake_generate)
    # Something told in chat since the upload, so "updated" is a real third
    # choice. The tests about two choices set it to 0.
    monkeypatch.setattr(generate, "additions", lambda master=None, conn=None: 1)
    monkeypatch.setattr(tailor, "build", fake_tailor)

    def say(message, turn=2, **extra):
        return {"turn": turn, "user_message": message, "picks": [],
                "resumes": {}, "by_ref": dict(JOBS),
                # A fixture keeps the run inline, so each call holds its result.
                "fixture": "greenhouse_sample", **extra}

    yield conn, say, sent, built, tmp_path
    conn.close()


def rendered(pdf: Path) -> Path:
    """What `render.render` leaves behind: the stored PDF, and beside it the
    copy named for the recruiter, which is the one that gets uploaded."""
    pdf.write_text("stored")
    copy = pdf.parent / "upload" / pdf.stem / "Sai_Swaroop_Resume.pdf"
    copy.parent.mkdir(parents=True, exist_ok=True)
    copy.write_text("upload")
    return pdf


def upload(pdf: Path, company="Co2") -> str:
    """The copy that goes out: the recruiter-named copy, then named for the
    company it is going to."""
    copy = pdf.parent / "upload" / pdf.stem / "Sai_Swaroop_Resume.pdf"
    return str(copy.parent / "upload" / copy.stem / "to" / f"X_{company}.pdf")


def apply(conn, state, ref="job1", resume=None):
    args = {"ref": ref, **({"resume": resume} if resume else {})}
    return chat.run_tool("start_application", args, state, conn)


def started(conn):
    return conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0]


# --- the question --------------------------------------------------------

def test_an_apply_with_no_choice_asks_and_sends_nothing(world):
    conn, say, sent, built, _ = world
    state = say("apply to 1")
    out = apply(conn, state)
    assert out.startswith("ERROR: not started")
    assert "yours" in out and "updated" in out and "tailored" in out
    assert started(conn) == 0 and sent == [] and built == []
    assert state["resume_asked_turn"] == 2


def test_the_model_cannot_answer_for_them(world):
    """The recorded failure: 'don't ask me anything' and the model picked."""
    conn, say, sent, _, _ = world
    state = say("just apply to everything, don't ask me anything")
    out = apply(conn, state, resume="tailored")
    assert out.startswith("ERROR: not started") and sent == []


def test_naming_it_in_the_apply_message_is_the_answer(world):
    conn, say, sent, built, tmp_path = world
    out = apply(conn, say("apply to 2 with my updated resume"), ref="job2",
                resume="updated")
    assert "Resume: updated" in out
    # The recruiter-named copy is uploaded, not the stored per-job file.
    assert built == ["updated"] and sent == [upload(tmp_path / "updated.pdf")]
    assert vault.get("resume_mode", conn) == "updated"    # the next default


def test_the_resume_can_be_left_out_when_they_named_one(world):
    conn, say, sent, built, _ = world
    apply(conn, say("apply with the one i uploaded"))
    assert built == [] and sent[0].endswith("upload.pdf")


def test_a_choice_they_did_not_make_is_refused(world):
    conn, say, sent, _, _ = world
    out = apply(conn, say("use my updated resume"), resume="tailored")
    assert "they said updated, not tailored" in out and sent == []


def test_a_negated_name_is_not_a_choice(world):
    conn, say, sent, built, _ = world
    assert chat.said_resume(say("not tailored, the one I uploaded"), None) == {"existing"}
    assert chat.said_resume(say("don't use updated, tailor it"), None) == {"tailored"}
    assert chat.said_resume(say("no, updated"), None) == {"updated"}
    assert chat.said_resume(say("send my file"), None) == {"existing"}


# --- the default ---------------------------------------------------------

def test_yes_after_the_question_takes_the_default(world):
    conn, say, sent, built, _ = world
    vault.put("resume_mode", "tailored", source="user", conn=conn)
    out = apply(conn, say("yes", turn=5, resume_asked_turn=4))
    assert "Resume: tailored" in out and built == ["tailored"]


def test_yes_with_no_question_behind_it_is_not_an_answer(world):
    conn, say, sent, _, _ = world
    vault.put("resume_mode", "tailored", source="user", conn=conn)
    out = apply(conn, say("yes", turn=5, resume_asked_turn=2))
    assert out.startswith("ERROR") and sent == []


def test_same_resume_as_before_needs_no_question(world):
    conn, say, sent, _, _ = world
    vault.put("resume_mode", "updated", source="user", conn=conn)
    assert "Resume: updated" in apply(conn, say("apply to 3, same resume as before"),
                                      ref="job3")


def test_the_question_offers_last_time_in_their_words(world):
    """A vault saved before the rename still says `existing`; they read 'your file'."""
    conn, say, _, _, _ = world
    vault.put("resume_mode", "existing", source="user", conn=conn)
    out = apply(conn, say("apply to 1"))
    assert "(last time: yours)" in out
    assert "Offer `yours` as the default" in chat.resume_status(conn)


def test_a_default_saved_as_master_reads_as_updated(world):
    """`master` was the old name for the updated resume."""
    conn, say, _, _, _ = world
    vault.put("resume_mode", "master", source="user", conn=conn)
    assert "(last time: updated)" in apply(conn, say("apply to 1"))


# --- nothing new since the upload: two choices, not three ----------------

def test_nothing_new_means_the_question_has_two_choices(world, monkeypatch):
    conn, say, _, _, _ = world
    from resume import generate
    monkeypatch.setattr(generate, "additions", lambda master=None, conn=None: 0)
    out = apply(conn, say("apply to 1"))
    assert "yours" in out and "tailored" in out and "updated" not in out
    assert "updated" not in chat.resume_status(conn).split("Offer")[0]


def test_updated_with_nothing_new_sends_their_file(world, monkeypatch):
    """A re-typeset copy of the same words is not what they asked for."""
    conn, say, sent, built, tmp_path = world
    from resume import generate
    monkeypatch.setattr(generate, "additions", lambda master=None, conn=None: 0)
    out = apply(conn, say("updated one please"), resume="updated")
    assert "Resume: yours (nothing new since your upload" in out
    assert built == [] and sent == [str(tmp_path / "upload.pdf")]


# --- batches, and resumes they have already seen -------------------------

def test_one_answer_covers_the_whole_batch(world):
    conn, say, sent, built, _ = world
    state = say("tailored")
    outs = [apply(conn, state, ref=r, resume="tailored") for r in JOBS]
    assert all("Resume: tailored" in o for o in outs)
    assert built == ["tailored"] * 3 and started(conn) == 3


def test_a_batch_with_no_answer_asks_the_same_one_question(world):
    conn, say, _, _, _ = world
    state = say("apply to all of them")
    outs = {apply(conn, state, ref=r) for r in JOBS}
    assert len(outs) == 1 and started(conn) == 0


def test_an_approved_tailored_resume_is_sent_as_they_saw_it(world):
    conn, say, sent, built, tmp_path = world
    shown = rendered(tmp_path / "edited.pdf")
    state = say("looks good, apply", turn=4,
                resumes={"job1": str(shown)}, tailored_turn={"job1": 3})
    out = apply(conn, state)
    assert "Resume: tailored" in out
    assert built == [] and sent == [upload(shown, "Co1")]    # not tailored again
    assert state["resumes"]["job1"] == str(shown)     # they still see their copy


def test_a_tailored_resume_built_this_turn_is_not_an_approval(world):
    conn, say, sent, _, tmp_path = world
    shown = rendered(tmp_path / "fresh.pdf")
    state = say("apply to 1", turn=4,
                resumes={"job1": str(shown)}, tailored_turn={"job1": 4})
    assert apply(conn, state, resume="tailored").startswith("ERROR")
    assert sent == []


def test_the_path_is_on_the_row_for_every_mode(world):
    """A parked run reloads its resume from the row; `existing` used to leave
    it empty, so a resumed run had nothing to upload."""
    conn, say, _, _, _ = world
    apply(conn, say("the original one"))
    path = conn.execute("SELECT resume_path FROM applications").fetchone()[0]
    assert path and path.endswith("upload.pdf")


def test_no_resume_on_file_means_no_question(world, monkeypatch, tmp_path):
    conn, say, sent, _, _ = world
    monkeypatch.setattr(chat.paths, "MASTER", tmp_path / "nothing.json")
    conn.execute("DELETE FROM vault WHERE key='master_resume_path'")
    out = apply(conn, say("apply to 1"))
    assert "Resume: none on file" in out and sent == [None]


# --- the default is a default, not a decision ----------------------------

def test_saving_a_default_does_not_send_anything(world):
    conn, say, sent, _, _ = world
    state = say("use my updated one from now on")
    out = chat.run_tool("set_resume_preference", {"choice": "updated"}, state, conn)
    assert "still ask" in out and vault.get("resume_mode", conn) == "updated"


def test_the_reply_that_asks_is_remembered_as_the_question():
    assert chat._asks_which_resume(chat.resume_question("updated"))
    assert chat._asks_which_resume(chat.resume_question(None, updates=False))
    assert not chat._asks_which_resume("Applying to PM at CRED with your updated resume.")


# --- tailoring starts from everything on record --------------------------

def test_tailoring_can_select_a_fact_told_in_chat(monkeypatch):
    """From master.json alone, a fact they said in chat could be permitted
    through a rewrite but never chosen — no bullet carried it."""
    from resume import generate, tailor as t
    seen = {}
    with_chat = {"name": "X", "summary": "s", "experience": [
        {"company": "Lenskart", "title": "GM",
         "bullets": ["Owned CRM.", "Grew activation 18%"]}]}
    monkeypatch.setattr(generate, "from_career", lambda master=None, conn=None: (with_chat, []))
    monkeypatch.setattr(t, "load_master",
                        lambda *a, **k: pytest.fail("tailor read master.json alone"))

    def fake(system, user, **kw):
        seen["user"] = user
        return {"experience": [{"index": 0, "keep": [1]}]}
    monkeypatch.setattr(t.llm, "complete_json", fake)
    import career
    monkeypatch.setattr(career, "supported", lambda conn=None: set())

    out = t.tailor({"title": "PM", "company": "Paytm", "description": "d"})
    assert "Grew activation 18%" in seen["user"]
    assert out["master"]["experience"][0]["bullets"] == ["Grew activation 18%"]


# --- the updated resume fits the page ------------------------------------

def test_updated_cuts_the_weakest_line_until_it_fits(tmp_path):
    """Oldest role first, a line with no number before one with a number, and
    no role ever loses its last line."""
    from resume import generate
    master = {"name": "X", "experience": [
        {"company": "New", "bullets": ["Grew revenue 20%", "Ran standups"]},
        {"company": "Old", "bullets": ["Cut churn 5%", "Wrote docs", "Owned roadmap"]},
    ]}
    renders = []

    def render_fn(m, name):
        renders.append(sum(len(j["bullets"]) for j in m["experience"]))
        return tmp_path / name

    # Two pages while more than three lines are left.
    count = lambda pdf: 2 if renders[-1] > 3 else 1      # noqa: E731
    out, _, cut = generate.fit(master, 1, render_fn, "x.pdf", count=count)
    assert cut == ["Owned roadmap", "Wrote docs"]
    assert out["experience"][1]["bullets"] == ["Cut churn 5%"]
    assert master["experience"][1]["bullets"] == ["Cut churn 5%", "Wrote docs",
                                                  "Owned roadmap"]   # record untouched


def test_every_resume_is_one_page():
    """Sai's eval sheet row 39: always one page, at any length of career."""
    from resume import generate
    assert generate.page_limit(None) == 1
    assert generate.page_limit(5) == 1
    assert generate.page_limit(14) == 1


# --- the file name the company sees -------------------------------------

def test_every_resume_goes_out_as_firstname_company(world):
    conn, say, sent, _, tmp_path = world
    (tmp_path / "upload.pdf").write_bytes(b"%PDF their own")
    apply(conn, say("send the original"), ref="job3")
    assert Path(sent[0]).name == "X_Co3.pdf"
    assert Path(sent[0]).read_bytes() == b"%PDF their own"      # bytes untouched


def test_a_set_first_name_wins_over_the_order_they_wrote(world):
    conn, say, sent, _, tmp_path = world
    (tmp_path / "upload.pdf").write_bytes(b"%PDF")
    vault.put("first_name", "sai", source="user", conn=conn)
    apply(conn, say("send the original"), ref="job1")
    assert Path(sent[0]).name == "Sai_Co1.pdf"


def test_company_names_become_one_token():
    from resume import render
    assert render.company_part("Paytm First Games") == "PaytmFirstGames"
    assert render.company_part("CRED") == "CRED"
    assert render.company_part("") == ""
