"""The first three live applications (2026-09-29) sent nothing, and one was
recorded as sent. Each test here is one of those failures, on a local copy of
the page shape that caused it. No Browserbase, no network, nothing submitted.

- Swiggy (SmartRecruiters): fields inside web components, invisible to the
  reader; submit pressed "Apply With Indeed" and called it "submitted".
- Clickpost (Keka): the link was the job description; the form was one
  "Apply for this job" click away; the agent guessed a sign-in page.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pw = pytest.importorskip("playwright.sync_api")

import llm                                        # noqa: E402
from apply import agent, harness, pagecheck, sites  # noqa: E402
from apply.browser import BaseBrowser              # noqa: E402
from apply.cloud import CloudBrowser               # noqa: E402
from engine import db                              # noqa: E402

# A SmartRecruiters-style form: every field is inside an open shadow root.
SHADOW = """<html><body>
<h2>Easy Apply</h2>
<button type="button" onclick="window.foreign=1">Apply With Indeed</button>
<h3>Personal information</h3>
<x-input label="First name" required></x-input>
<x-input label="Email" required></x-input>
<x-input label="LinkedIn"></x-input>
<h3>Resume *</h3>
<x-drop></x-drop>
<x-button id="next">Next</x-button>
<script>
customElements.define('x-input', class extends HTMLElement {
  connectedCallback() {
    const r = this.attachShadow({mode: 'open'});
    const req = this.hasAttribute('required');
    r.innerHTML = `<label for="i">${this.getAttribute('label')}${req ? '*' : ''}</label>
                   <input id="i" type="text" ${req ? 'required' : ''}>`;
  }
});
customElements.define('x-drop', class extends HTMLElement {
  connectedCallback() {
    this.attachShadow({mode: 'open'}).innerHTML =
      '<label for="f">Choose a file or drop it here</label><input id="f" type="file">';
  }
});
customElements.define('x-button', class extends HTMLElement {
  connectedCallback() {
    this.attachShadow({mode: 'open'}).innerHTML = '<button type="button"><slot></slot></button>';
  }
});
</script></body></html>"""

JD = """<html><body><h1>Associate Product Manager</h1><p>About the role...</p>
<a href="http://jobs.test/apply">Apply for this job</a>
<button onclick="window.foreign=1">Apply with LinkedIn</button></body></html>"""

APPLY = """<html><body><form>
<label for="a">First Name *</label><input id="a" required>
<label for="b">Email *</label><input id="b" type="email" required>
</form></body></html>"""

LOGIN = """<html><body><h1>Sign in to continue</h1>
<label for="e">Email</label><input id="e"><label for="p">Password</label>
<input id="p" type="password"><button>Sign in</button></body></html>"""


def _form(submit_js: str) -> str:
    return f"""<html><body><form onsubmit="return false">
<label for="n">Full name *</label><input id="n" required>
<button type="button" id="s" onclick="{submit_js}">Submit application</button>
</form></body></html>"""


@pytest.fixture(scope="module")
def chromium():
    with pw.sync_playwright() as p:
        try:
            b = p.chromium.launch(headless=True)
        except Exception as exc:                      # noqa: BLE001
            pytest.skip(f"no local chromium: {exc}")
        yield b
        b.close()


@pytest.fixture
def page(chromium):
    ctx = chromium.new_context()
    pages = {"http://jobs.test/jd": JD, "http://jobs.test/apply": APPLY,
             "http://jobs.test/done": "<html><body><p>Your profile page.</p></body></html>"}
    ctx.route("http://jobs.test/**", lambda route: route.fulfill(
        status=200, content_type="text/html", body=pages.get(route.request.url, "<p>404</p>")))
    p = ctx.new_page()
    yield p
    ctx.close()


def _browser(page, html: str | None = None, url: str | None = None) -> CloudBrowser:
    if url:
        page.goto(url)
    else:
        page.set_content(html)
    b = CloudBrowser("about:blank", app_id=1, page=page)
    b._guard_submit = lambda fields: None          # the guard has its own tests
    return b


# --- Swiggy: fields inside web components ---------------------------------------

def test_reads_fields_inside_shadow_roots(page):
    b = _browser(page, SHADOW)
    f = {x["name"]: x for x in b.read_form()}
    assert {"First name", "Email", "LinkedIn"} <= set(f)
    assert f["First name"]["required"] and not f["LinkedIn"]["required"]
    assert any(x["type"] == "file" for x in f.values())


def test_fills_and_reads_back_inside_shadow_roots(page, tmp_path):
    b = _browser(page, SHADOW)
    b.read_form()
    assert b.fill_field("First name", "Asha") == "filled First name"
    cv = tmp_path / "cv.pdf"
    cv.write_bytes(b"%PDF")
    upload = next(n for n in b._where if n.startswith("upload:"))
    assert b.upload_file(upload, str(cv)) == "uploaded cv.pdf"
    vals = b.current_values()
    assert vals["First name"] == "Asha" and vals[upload] == "cv.pdf"


def test_never_presses_apply_with_indeed_and_names_the_next_page(page):
    b = _browser(page, SHADOW)
    b.read_form()
    out = b.submit()
    assert out.startswith("ERROR: NOT SENT") and "'Next'" in out
    assert page.evaluate("window.foreign") is None


# --- Clickpost: the link was the job description ----------------------------------

def test_open_form_presses_the_job_pages_own_apply_button(page):
    b = _browser(page, url="http://jobs.test/jd")
    assert b.page_state()["fields"] == 0
    assert "Apply for this job" in b.open_form()
    assert b.page.url == "http://jobs.test/apply"
    assert {f["name"] for f in b.read_form()} == {"First Name", "Email"}
    assert page.evaluate("window.foreign") is None


def test_open_form_does_nothing_on_a_form(page):
    b = _browser(page, url="http://jobs.test/apply")
    assert b.open_form() == ""


def test_screenshot_says_what_the_page_shows(page, monkeypatch, tmp_path):
    import apply.cloud as cloud
    monkeypatch.setattr(cloud, "SHOTS", tmp_path)
    b = _browser(page, url="http://jobs.test/jd")
    out = b.screenshot()
    assert "Apply for this job" in out and "fields a person could fill: 0" in out
    assert "PASSWORD" not in out
    b = _browser(page, LOGIN)
    assert "PASSWORD" in b.screenshot()


# --- submit says "submitted" only when the page does ---------------------------------

def test_submit_counts_only_a_confirmation(page):
    b = _browser(page, _form("document.body.innerHTML='<h1>Thank you for applying!</h1>'"))
    b.read_form()
    b.fill_field("Full name", "Asha")
    assert b.submit().startswith("submitted — the page confirms it")


def test_a_click_that_does_nothing_is_not_sent(page):
    b = _browser(page, _form(""))
    b.read_form()
    b.fill_field("Full name", "Asha")
    st = b.page_state()
    b._press(pagecheck.submit_button(st["buttons"]))
    out = b._after_submit(st, "Submit application", wait_ms=1000)
    assert out.startswith("ERROR: NOT SENT") and "still there" in out


def test_a_page_change_without_confirmation_is_unconfirmed(page):
    b = _browser(page, _form("location.href='http://jobs.test/done'"))
    b.read_form()
    b.fill_field("Full name", "Asha")
    st = b.page_state()
    b._press(pagecheck.submit_button(st["buttons"]))
    out = b._after_submit(st, "Submit application", wait_ms=1000)
    assert out.startswith("unconfirmed")


def test_button_choice():
    labels = ["Apply With Indeed", "Apply with LinkedIn", "Cookie Settings", "Next"]
    assert pagecheck.submit_button(labels) is None
    assert pagecheck.next_button(labels) == "Next"
    assert pagecheck.submit_button(["Apply now", "Submit application"]) == 1
    assert pagecheck.entry_button(["Apply with LinkedIn", "Apply for this job"]) == 1
    assert pagecheck.entry_button(["I'm interested"]) == 0
    # Breezy and Recruitee, from the form eval
    assert pagecheck.entry_button(["Use My Indeed Resume", "Apply To Position"]) == 1
    assert pagecheck.submit_button(["Privacy Policy", "Send", "Open positions"]) == 1
    assert pagecheck.submit_button(["Send us feedback"]) is None
    # Teamtailor
    assert pagecheck.entry_button(["Share page", "Apply for this job here"]) == 1
    assert pagecheck.entry_button(["Apply Now!"]) == 0
    assert pagecheck.entry_button(["Apply with LinkedIn here"]) is None


def test_confirmation_words_already_on_the_page_do_not_count():
    before = "We will review your application once it is submitted."
    assert not pagecheck.confirmed(before, before, "u", "u")
    assert pagecheck.confirmed(before, "Application submitted! Thanks.", "u", "u")
    assert pagecheck.confirmed("", "", "https://x/apply", "https://x/thank-you")


def test_claims_sent():
    assert pagecheck.claims_sent("The application was submitted — one click.")
    assert not pagecheck.claims_sent("Nothing was submitted.")
    assert not pagecheck.claims_sent("I couldn't submit it; the form wants a sign-in.")


# --- the harness records what the page said, not what the model said -----------------

class _OneSubmit(BaseBrowser):
    def __init__(self, result):
        super().__init__(1)
        self.result = result

    def submit(self):
        return self.result


def _script(monkeypatch, last_words):
    replies = [
        NS(stop_reason="tool_use", content=[NS(type="tool_use", id="t1", name="submit", input={},
                                               model_dump=lambda **k: {"type": "tool_use", "id": "t1",
                                                                        "name": "submit", "input": {}})]),
        NS(stop_reason="end_turn", content=[NS(type="text", text=last_words,
                                               model_dump=lambda **k: {"type": "text", "text": last_words})]),
    ]

    class Msgs:
        def create(self, **kw):
            return replies.pop(0)
    monkeypatch.setattr(llm, "client", lambda p=None: NS(messages=Msgs()))


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    c.execute("INSERT INTO applications (id, source, source_id, company, title) "
              "VALUES (1, 'smartrecruiters', '1', 'Swiggy', 'Growth')")
    c.commit()
    yield c
    c.close()


def test_the_swiggy_run_is_no_longer_recorded_as_sent(monkeypatch, conn):
    _script(monkeypatch, "The application was submitted — it went through as one click.")
    r = harness.run(1, _OneSubmit("ERROR: NOT SENT. No submit button on the page."),
                    "s", first_message="go", conn=conn)
    assert r["status"] == "blocked" and r["message"] == pagecheck.NOT_SENT
    assert conn.execute("SELECT status FROM applications").fetchone()[0] == "blocked"


def test_confirmed_is_submitted_and_unconfirmed_is_kept_apart(monkeypatch, conn):
    _script(monkeypatch, "Sent.")
    assert harness.run(1, _OneSubmit("submitted — the page confirms it (x)"), "s",
                       first_message="go", conn=conn)["status"] == "submitted"
    conn.execute("UPDATE applications SET log = NULL")
    _script(monkeypatch, "It may have gone.")
    assert harness.run(1, _OneSubmit("unconfirmed — pressed 'Submit'"), "s",
                       first_message="go", conn=conn)["status"] == "unconfirmed"


# --- before the model starts: sign-in wall or no form at all --------------------------

def test_a_sign_in_page_stops_before_the_model(page, conn):
    b = _browser(page, LOGIN)
    r = agent._no_form_here(conn, 1, {"company": "Acme", "apply_url": "https://x/1"}, b)
    assert r["status"] == "sign_in" and "https://x/1" in r["message"]


def test_a_page_with_no_form_stops_with_the_link(page, conn):
    b = _browser(page, "<html><body><h1>Careers</h1><p>No openings.</p></body></html>")
    r = agent._no_form_here(conn, 1, {"company": "Acme", "apply_url": "https://x/1"}, b)
    assert r["status"] == "no_form" and "https://x/1" in r["message"]
    assert conn.execute("SELECT status FROM applications").fetchone()[0] == "no_form"


def test_a_job_description_is_opened_not_stopped(page, conn):
    b = _browser(page, url="http://jobs.test/jd")
    assert agent._no_form_here(conn, 1, {"company": "Clickpost"}, b) is None
    assert b.page.url == "http://jobs.test/apply"


def test_a_no_form_run_can_be_retried(conn):
    conn.execute("UPDATE applications SET status='no_form'")
    new = agent.create({"source": "smartrecruiters", "source_id": "1"}, None, conn)
    assert new != 1
    conn.execute("UPDATE applications SET status='unconfirmed' WHERE id=1")
    conn.execute("DELETE FROM applications WHERE id=?", (new,))
    assert agent.create({"source": "smartrecruiters", "source_id": "1"}, None, conn) == 1


# --- only sites the filler passed on ---------------------------------------------------

def test_untested_sites_get_the_link(monkeypatch, conn):
    import chat
    from engine import adapters
    monkeypatch.setattr(chat.apply_agent, "browser_for", lambda *a, **k: None)
    monkeypatch.setattr(chat.apply_agent, "start", lambda *a, **k: {"status": "blocked"})
    monkeypatch.setenv("APPLY_SITES", "lever,keka")   # smartrecruiters left off the list
    row = adapters._row(source="smartrecruiters", source_id="143429", company="Swiggy",
                        title="Growth", location="Bengaluru", description="x",
                        url="https://jobs.smartrecruiters.com/SWIGGY/143429",
                        posted_at="2026-09-29")
    db.upsert(conn, [row])
    state = {"turn": 2, "user_message": "apply with my file", "picks": [], "resumes": {},
             "by_ref": {"j": dict(row)}}
    out = chat.run_tool("start_application", {"ref": "j"}, state, conn)
    assert out.startswith("NOT STARTED") and "SWIGGY/143429" in out
    assert not sites.can_fill("smartrecruiters") and sites.can_fill("keka")
    monkeypatch.delenv("APPLY_SITES")
    assert sites.can_fill("smartrecruiters")          # passed the cloud eval 30 Sep
