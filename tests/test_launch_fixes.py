"""The chat and search fixes from Sai's comments on the 2026-09-28 launch evals.

Run: python -m pytest tests/ -q

No model, no network: the search guard, link check, file sending, empty-result
rule and slop check are all code, which is the point of each one.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import chat                                        # noqa: E402
import telegram_bot as tg                          # noqa: E402
from engine import db                              # noqa: E402
from resume import slop, verify                    # noqa: E402
from search import query                           # noqa: E402
from search import run as srun                     # noqa: E402


def f(**kw) -> dict:
    return query.normalize(dict(kw))


# --- seniority only when they said a level -----------------------------------

def test_manager_is_not_a_level():
    x = f(title_keywords=["growth manager"], seniority="senior", level=["senior"])
    srun.guard(x, "growth manager jobs")
    assert x["seniority"] is None and x["level"] is None


@pytest.mark.parametrize("said", ["senior product manager roles in Bangalore",
                                  "I'm a fresher, any APM roles?",
                                  "product roles for someone with 2 years experience"])
def test_a_level_they_said_stays(said):
    x = f(seniority="junior", level=["junior"])
    srun.guard(x, said)
    assert x["level"] == ["junior"]


def test_fresher_counts_as_asking_for_junior():
    assert srun.mentioned("junior", "fresher APM or product analyst roles")
    assert not srun.added_without_asking(f(seniority="junior"), "fresher APM roles")


# --- city only when they named one -------------------------------------------

def test_a_city_they_did_not_name_is_removed_and_said():
    x = f(title_keywords=["growth"], city="Bangalore")
    notes = srun.guard(x, "Growth Roles which posted today")
    assert x["city"] is None and "did not name a city" in notes[0]


def test_near_me_keeps_the_city():
    x = f(city="Bangalore")
    srun.guard(x, "growth jobs near me")
    assert x["city"] == "Bangalore"


def test_yes_to_the_question_names_the_city():
    x = f(city="Bangalore")
    srun.guard(x, "growth roles posted today yes Want me to look only in Bangalore?")
    assert x["city"] == "Bangalore"


def test_delhi_ncr_is_read_from_the_sentence_when_the_model_drops_it():
    x = f(title_keywords=["growth"])
    srun.guard(x, "growth jobs in Delhi NCR")
    assert x["city"] == "Delhi NCR"


def test_delhi_ncr_searches_every_ncr_city():
    clauses, params = query._where(f(city="Delhi NCR"))
    assert {"Delhi", "Gurgaon", "Noida"} <= set(params)


# --- an empty search ends the searching --------------------------------------

@pytest.fixture
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


def test_after_an_empty_search_the_agent_must_ask(monkeypatch, conn, tmp_path):
    monkeypatch.setattr(chat, "LAST", tmp_path / "last.json")
    monkeypatch.setattr(chat.reconcile, "read_profile", lambda conn: "")
    empty = {"picks": [], "dropped": [], "filters": {}, "notes": [],
             "funnel": {}, "text_agent": "Nothing matched."}
    monkeypatch.setattr(srun, "search", lambda *a, **k: empty)
    state = {"picks": [], "searched": []}
    first = chat.run_tool("search_jobs", {"query": "growth roles posted today"},
                          state, conn)
    second = chat.run_tool("search_jobs", {"query": "growth roles in Bengaluru"},
                           state, conn)
    assert "ask one short question" in first
    assert second.startswith("ERROR: not searched")


def test_today_is_in_every_turn(conn):
    from datetime import datetime
    assert datetime.now().strftime("%B %Y") in chat.turn_state(conn, [])


# --- links are the tools', not the model's -----------------------------------

JOBS = [{"company": "Pocket FM", "title": "Senior Manager - Founder's Office",
         "apply_url": "https://www.adzuna.in/land/ad/111"},
        {"company": "TrueFoundry", "title": "Founder's Office - FDE Team",
         "url": "https://www.linkedin.com/jobs/view/4471446760"}]


def test_a_link_under_the_wrong_job_is_replaced():
    reply = ("1. **Senior Manager - Founder's Office, Pocket FM, Bangalore**\n"
             "   https://www.linkedin.com/jobs/view/4471446760\n\n"
             "2. **Founder's Office - FDE Team, TrueFoundry, Bangalore**\n"
             "   https://www.linkedin.com/jobs/view/4471446760\n")
    out = chat.fix_links(reply, JOBS)
    item1, item2 = out.split("2. ")
    assert "adzuna.in/land/ad/111" in item1 and "4471446760" not in item1
    assert "4471446760" in item2


def test_a_job_link_no_tool_returned_is_removed_and_others_stay():
    reply = ("Also see https://www.linkedin.com/jobs/view/999 and "
             "https://help.example.com/faq")
    out = chat.fix_links(reply, JOBS)
    assert "jobs/view/999" not in out and "help.example.com/faq" in out


# --- files, not paths --------------------------------------------------------

def test_on_telegram_a_file_is_queued_not_written(tmp_path):
    png = tmp_path / "charter.png"
    png.write_bytes(b"png")
    state = {"channel": "telegram"}
    note = chat.attach(state, png)
    assert str(png) not in note and state["outbox"] == [str(png)]
    assert chat.attach({}, png) == str(png)          # terminal and web keep paths


def test_phone_replies_have_no_paths_or_em_dashes():
    text = ("Here it is: ![Your resume](/Users/x/artifacts/charter.png)\n"
            "Saved at /Users/x/artifacts/r.pdf — ready.")
    out = chat.for_phone(text)
    assert "/Users" not in out and "—" not in out


class Bot:
    def __init__(self):
        self.sent, self.docs, self.photos = [], [], []

    def send(self, chat_id, text):
        self.sent.append(text)

    def send_files(self, chat_id, paths):
        tg.Bot.send_files(self, chat_id, paths)

    def send_document(self, chat_id, path, caption=""):
        self.docs.append(Path(path).name)

    def send_photo(self, chat_id, path, caption):
        self.photos.append(Path(path).name)

    def typing(self, chat_id):
        pass


def test_the_worker_sends_the_files_after_the_reply(tmp_path):
    pdf, png = tmp_path / "Asha_Rao_Resume.pdf", tmp_path / "charter.png"
    pdf.write_bytes(b"%PDF"), png.write_bytes(b"png")
    bot = Bot()
    w = tg.Worker(7, bot, None, lambda text: ("Attached.", [str(pdf), str(png)]),
                  None, lambda c: [], None, tmp_path)
    w.handle({"text": "can I see my resume?"})
    assert bot.sent == ["Attached."]
    assert bot.docs == ["Asha_Rao_Resume.pdf"] and bot.photos == ["charter.png"]


# --- plain words ---------------------------------------------------------------

def test_a_rewrite_that_adds_slop_keeps_the_original():
    master = {"experience": [{"company": "Acme", "title": "PM", "dates": "2023",
                              "bullets": ["ran pricing tests with sales"]}]}
    patch = {"experience": [{"index": 0, "keep": [0],
                             "rewrites": {"0": "Leveraged pricing tests with sales"}}]}
    out, notes = verify.apply_patch(master, patch)
    assert out["experience"][0]["bullets"] == ["ran pricing tests with sales"]
    assert "AI-written" in notes[0]


def test_an_em_dash_is_tidied_not_reverted():
    master = {"experience": [{"company": "Acme", "title": "PM", "dates": "2023",
                              "bullets": ["ran pricing tests with sales"]}]}
    patch = {"experience": [{"index": 0, "keep": [0], "rewrites": {
        "0": "Ran pricing tests — with sales"}}]}
    out, notes = verify.apply_patch(master, patch)
    assert out["experience"][0]["bullets"] == ["Ran pricing tests, with sales"]
    assert notes == []


def test_their_own_words_are_never_slop():
    assert slop.added("Helped the team ship", "Helped the team ship faster") == []


# --- a resume already on file goes as a file; a refused send is said out loud ---

class _Resp:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


class _Http:
    """Stands in for httpx.Client. Answers each upload from a queue."""

    def __init__(self, answers):
        self.answers, self.posts = list(answers), []

    def post(self, url, json=None, data=None, files=None):
        self.posts.append((url.rsplit("/", 1)[-1], data or json, files))
        return _Resp(self.answers.pop(0) if self.answers else {"ok": True, "result": {}})


def test_a_pdf_goes_out_as_a_document(tmp_path):
    pdf = tmp_path / "Asha_Rao_Resume.pdf"
    pdf.write_bytes(b"%PDF")
    http = _Http([{"ok": True, "result": {}}])
    bot = tg.Bot("t", client=http)
    bot.send_document(7, pdf)
    method, data, files = http.posts[0]
    assert method == "sendDocument" and data["chat_id"] == 7
    assert files["document"][0] == "Asha_Rao_Resume.pdf"
    assert len(http.posts) == 1                       # no apology after a good send


def test_a_refused_send_tells_them_instead_of_silence(tmp_path):
    pdf = tmp_path / "Asha_Rao_Resume.pdf"
    pdf.write_bytes(b"%PDF")
    http = _Http([{"ok": False, "description": "Bad Request: chat not found"}])
    bot = tg.Bot("t", client=http)
    bot.send_document(7, pdf)
    methods = [m for m, _, _ in http.posts]
    assert methods == ["sendDocument", "sendMessage"]
    assert tg.Bot.NO_FILE in str(http.posts[-1])


def test_a_file_that_vanished_before_sending_is_reported(tmp_path):
    http = _Http([])
    bot = tg.Bot("t", client=http)
    bot.send_files(7, [str(tmp_path / "gone.pdf")])
    assert [m for m, _, _ in http.posts] == ["sendMessage"]


def test_send_resume_queues_the_newest_built_pdf(tmp_path, monkeypatch, conn):
    import os
    import time
    import paths
    resumes = tmp_path / "resumes"
    resumes.mkdir()
    old, new = resumes / "Asha_Old.pdf", resumes / "Asha_New.pdf"
    old.write_bytes(b"%PDF"), new.write_bytes(b"%PDF")
    t = time.time()
    os.utime(old, (t - 100, t - 100)), os.utime(new, (t, t))
    monkeypatch.setattr(paths, "RESUMES", resumes)
    state = {"channel": "telegram", "picks": [], "resumes": {}}
    out = chat.run_tool("send_resume", {"which": "latest"}, state, conn)
    assert "Asha_New.pdf" in out and state["outbox"] == [str(new)]


def test_send_resume_with_nothing_on_file_is_an_error(tmp_path, monkeypatch, conn):
    import paths
    monkeypatch.setattr(paths, "RESUMES", tmp_path / "none")
    state = {"channel": "telegram", "picks": [], "resumes": {}}
    out = chat.run_tool("send_resume", {"which": "latest"}, state, conn)
    assert out.startswith("ERROR") and not state.get("outbox")


def test_send_resume_file_is_their_upload(tmp_path, monkeypatch, conn):
    import vault
    up = tmp_path / "BUGATA_SAI_SWAROOP.pdf"
    up.write_bytes(b"%PDF")
    vault.put("master_resume_path", str(up), source="resume", conn=conn)
    state = {"channel": "telegram", "picks": [], "resumes": {}}
    out = chat.run_tool("send_resume", {"which": "file"}, state, conn)
    assert state["outbox"] == [str(up)] and "BUGATA_SAI_SWAROOP.pdf" in out


def test_a_failed_build_cannot_be_reported_as_attached():
    state = {"channel": "telegram", "outbox": [],
             "build_failed": "ATS extraction gate failed"}
    out = chat.no_false_attach("Built it, attached below.", state)
    assert out.startswith("No file this time") and "ATS" in out
    # A reply that makes no claim is left alone, and so is one with a file queued.
    assert chat.no_false_attach("It failed the ATS check, sorry.", state) \
        == "It failed the ATS check, sorry."
    state["outbox"] = ["/x/a.pdf"]
    assert chat.no_false_attach("Attached.", state) == "Attached."
