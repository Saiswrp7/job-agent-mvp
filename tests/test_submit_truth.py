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
from apply import agent, cloud, harness, pagecheck, sites  # noqa: E402
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
    b._guard_submit = lambda fields, partial=False: None   # the guard has its own tests
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


def test_no_approval_is_asked_before_the_last_page(page):
    """Swiggy, 2026-09-30: approval was asked on page 1, and after the yes the
    run stopped at Next. Now submit on a Next page asks nothing and points to
    next_page."""
    b = _browser(page, SHADOW)
    asked = []
    b._guard_submit = lambda fields: asked.append(fields)
    b.read_form()
    out = b.submit()
    assert asked == [] and "next_page" in out and "Stop" not in out


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

def test_submit_counts_only_a_confirmation(page, monkeypatch, tmp_path):
    monkeypatch.setattr(cloud, "SHOTS", tmp_path)
    b = _browser(page, _form("document.body.innerHTML='<h1>Thank you for applying!</h1>'"))
    b.read_form()
    b.fill_field("Full name", "Asha")
    out = b.submit()
    # Quoted in the site's own words, and pictured the moment it confirmed.
    assert out.startswith('submitted — The page confirms it: "Thank you for applying!"')
    assert (tmp_path / "app_1_sent.png").exists()


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


# --- the chat sees finished applications too ---------------------------------------

def test_a_finished_unsent_application_is_visible_as_not_sent(conn):
    """Sai, 2026-09-30: Swiggy ended blocked, the status list showed only open
    ones and came back "(none)", and the bot said his submit "already went in"."""
    import chat
    conn.execute("UPDATE applications SET status = 'blocked'")
    out = chat.run_tool("application_status", {}, {"picks": []}, conn)
    assert "Swiggy" in out and "NOT SENT" in out and "(none)" not in out
    assert "NOT SENT" in chat.turn_state(conn, [])
    conn.execute("UPDATE applications SET status = 'submitted'")
    assert "SENT, the site confirmed it" in chat.applications(conn)


def test_a_fresh_start_does_not_carry_the_old_run(monkeypatch, conn):
    """Swiggy retry, 2026-09-30: the row kept its finished log, the new run
    loaded it, and the model repeated the old "Stop and say so"."""
    import json
    old = [{"role": "user", "content": "Fill this application."},
           {"role": "assistant", "content": "Nothing was sent. Stop."}]
    conn.execute("UPDATE applications SET status='blocked', log=?", (json.dumps(old),))
    _script(monkeypatch, "Sent.")
    harness.run(1, _OneSubmit("submitted — the page confirms it (x)"), "s",
                first_message="Fill this application, fresh.", conn=conn)
    log = json.loads(conn.execute("SELECT log FROM applications").fetchone()[0])
    assert log[0]["content"] == "Fill this application, fresh."
    assert "Stop." not in json.dumps(log)


# --- Swiggy page 2: question boxes labelled only "*" (2026-10-01) ------------------

def _sr_page2(with_definition: bool) -> str:
    qs = [("aaaa1111-0000-0000-0000-000000000001", "How many years experience do you have?", "text"),
          ("bbbb2222-0000-0000-0000-000000000002", "What is your current fixed salary?", "number"),
          ("cccc3333-0000-0000-0000-000000000003", "What is your current notice period?", "text"),
          ("dddd4444-0000-0000-0000-000000000004", "Are you willing to relocate?", "text")]
    import json
    definition = json.dumps({"id": "", "questions": [{"id": q, "label": t} for q, t, _ in qs]})
    boxes = "".join(
        f'<div data-test="question-container"><p class="q">{t}</p>'
        f'<spl-input id="question_{q}" data-type="{ty}"></spl-input></div>' for q, t, ty in qs)
    attr = f" definition='{definition}'" if with_definition else ""
    return f"""<html><body><h2>Preliminary questions</h2>
<sr-screening-questions-form{attr}>{boxes}</sr-screening-questions-form>
<label><input type="checkbox" required> I have read the privacy notice</label>
<button>Back</button><button>Submit</button>
<script>
customElements.define('spl-input', class extends HTMLElement {{
  connectedCallback() {{
    const r = this.attachShadow({{mode: 'open'}});
    r.innerHTML = '<label for="i">*</label><input id="' + this.id + '" type="' + this.dataset.type + '"><span>0/200</span>';
  }}
}});
</script></body></html>"""


@pytest.mark.parametrize("with_definition", [True, False])
def test_star_labelled_question_boxes_are_read_by_their_question(page, with_definition):
    b = _browser(page, _sr_page2(with_definition))
    names = [f["name"] for f in b.read_form()]
    for q in ("How many years experience do you have?", "What is your current fixed salary?",
              "What is your current notice period?", "Are you willing to relocate?"):
        assert q in names, names
    assert any(f["type"] == "checkbox" for f in b.read_form())
    assert b.fill_field("What is your current fixed salary?", "1200000").startswith("filled")


def test_a_box_with_no_label_anywhere_is_still_listed(page):
    b = _browser(page, '<html><body><input id="question_zzzz9999-1"><button>Submit</button></body></html>')
    assert [f["name"] for f in b.read_form()] == ["question_zzzz9999-1"]


def test_the_approval_lists_boxes_left_blank():
    from apply import confirm
    fields = [{"name": "a", "label": "Expected salary"}, {"name": "b", "label": "Notice period"}]
    q = confirm.question({"title": "PM", "company": "Co"},
                         confirm.shown({"a": "18 LPA"}, fields),
                         empty=confirm.blank({"a": "18 LPA", "b": ""}, fields))
    assert "- Expected salary: 18 LPA" in q and "Left blank: Notice period." in q


@pytest.mark.parametrize("said,typed", [("12 LPA", "1200000"), ("12.5 lakh", "1250000"),
                                        ("1.2 Cr", "12000000"), ("1,200,000", "1200000"),
                                        ("0", "0"), ("5 years", "5")])
def test_a_number_box_gets_rupees_not_the_lakh_figure(said, typed):
    """A rupee box given "12 LPA" must hold 1200000, never 12."""
    assert cloud._number(said) == typed



# --- research fixes, 2026-10-01 ------------------------------------------------------

def test_relocate_is_matched_before_location():
    import vault
    assert vault.match_key("Are you willing to relocate if you are in a different location?") == "relocate"
    assert vault.match_key("Current location") == "location"


@pytest.mark.parametrize("label,text,risky", [
    ("Continue", "Your details", True), ("Proceed", "", True),
    ("Continue", "Step 1 of 3 Your details", False), ("Next", "", False),
    ("Save and continue", "", False)])
def test_a_bare_continue_without_steps_may_send(label, text, risky):
    assert pagecheck.may_send(label, text) is risky


def _continue_page(then: str) -> str:
    return ("<html><body><label>Full name <input name='n' required></label>"
            "<button type='button' onclick=\"" + then + "\">Continue</button></body></html>")


def test_next_page_never_presses_a_continue_that_may_send(page):
    b = _browser(page, _continue_page("document.body.innerHTML='<h1>Thank you for applying!</h1>'"))
    b.read_form()
    b.fill_field("Full name", "Asha")
    out = b.next_page()
    assert out.startswith("ERROR: NOT PRESSED") and "submit" in out
    assert "Thank you" not in page.inner_text("body")


def test_submit_presses_a_risky_continue_only_after_the_approval(page, monkeypatch, tmp_path):
    monkeypatch.setattr(cloud, "SHOTS", tmp_path)
    b = _browser(page, _continue_page("document.body.innerHTML='<h1>Thank you for applying!</h1>'"))
    asked = []
    b._guard_submit = lambda fields, partial=False: asked.append(partial)
    b.read_form()
    b.fill_field("Full name", "Asha")
    out = b.submit()
    assert asked == [True] and out.startswith("submitted")


def test_a_risky_continue_that_opens_another_page_says_nothing_was_sent(page):
    nxt = ("document.body.innerHTML=`<label>Notice period <input name=np></label>"
           "<button>Submit</button>`")
    b = _browser(page, _continue_page(nxt))
    b.read_form()
    b.fill_field("Full name", "Asha")
    out = b.submit()
    assert out.startswith("Nothing was sent: it opened another page") and "Notice period" in out


def test_partial_approval_lets_the_first_page_through():
    from apply import confirm
    from apply.browser import Park
    c = db.connect(":memory:")
    c.execute("INSERT INTO applications (id, source, source_id, company, title) VALUES (1,'x','1','Co','PM')")
    a = confirm.Approval(c, 1, {"title": "PM", "company": "Co"})
    fields = [{"name": "a", "label": "Name"}, {"name": "b", "label": "Notice"}]
    with pytest.raises(Park):
        a.check({"a": "Asha", "b": "30 days"}, fields)
    confirm.approve(c, 1)
    a.check({"a": "Asha"}, fields, partial=True)          # page 1 again: agrees
    with pytest.raises(Park):
        a.check({"a": "Asha"}, fields)                    # but not as the final send
    with pytest.raises(Park):
        a.check({"a": "Ravi"}, fields, partial=True)      # changed: ask again


def test_the_wait_runs_on_while_browserbase_solves_a_captcha(page):
    b = _browser(page, "<html><body><p>Form</p></body></html>")
    page.evaluate("""() => { console.log('browserbase-solving-started');
        setTimeout(() => { console.log('browserbase-solving-finished');
                           document.body.innerHTML = '<h1>Thank you for applying!</h1>'; }, 2500); }""")
    st = b.page_state()
    st["text"] = "Form"
    assert b._after_submit(st, "Submit", wait_ms=1000).startswith("submitted")


def test_a_dropdown_that_opens_on_click_shows_its_choices(page):
    html = """<html><body><label for=d>Degree</label>
<input id=d role=combobox aria-autocomplete=list>
<ul id=menu style="display:none"><li role=option>Bachelors</li><li role=option>Masters</li></ul>
<script>
const d = document.getElementById('d'), m = document.getElementById('menu');
d.addEventListener('click', () => { m.style.display = 'block'; });
d.addEventListener('input', () => { m.style.display = 'none'; });
d.addEventListener('blur', () => setTimeout(() => {
  if (!['Bachelors', 'Masters'].includes(d.value)) d.value = ''; }, 50));
m.querySelectorAll('li').forEach(li => li.addEventListener('click', () => { d.value = li.innerText; m.style.display = 'none'; }));
</script></body></html>"""
    b = _browser(page, html)
    b.read_form()
    out = b.fill_field("Degree", "B.Tech")
    assert "Bachelors" in out and "Masters" in out, out


# --- eval fixes, 2026-10-01 -----------------------------------------------------------

def test_a_dash_never_decides_a_dropdown_match():
    assert cloud._match(["Immediate", "1–2 weeks", "3–4 weeks"], "3-4 weeks") == "3–4 weeks"


def test_a_dropdown_that_filters_to_nothing_is_opened_and_the_match_picked(page):
    html = """<html><body><label for=d>Notice period</label>
<input id=d role=combobox aria-autocomplete=list>
<ul id=menu style="display:none"><li role=option>Immediate</li><li role=option>3–4 weeks</li></ul>
<script>
const d = document.getElementById('d'), m = document.getElementById('menu');
d.addEventListener('click', () => { m.style.display = 'block'; });
d.addEventListener('input', () => { m.style.display = 'none'; });
d.addEventListener('blur', () => setTimeout(() => {
  if (!['Immediate', '3–4 weeks'].includes(d.value)) d.value = ''; }, 50));
m.querySelectorAll('li').forEach(li => li.addEventListener('mousedown', e => {
  e.preventDefault(); d.value = li.innerText; m.style.display = 'none'; }));
</script></body></html>"""
    b = _browser(page, html)
    b.read_form()
    assert b.fill_field("Notice period", "3-4 weeks").startswith("filled"), b.current_values()
    assert page.evaluate("document.getElementById('d').value") == "3–4 weeks"


@pytest.mark.parametrize("url,cap", [
    ("https://coditude.keka.com/careers/applyjob/1", ("#imgCaptcha", "#captcha")),
    ("https://jobs.lever.co/x/1/apply", None), ("https://notkeka.com/x", None)])
def test_keka_sessions_tell_browserbase_where_the_captcha_is(url, cap):
    assert cloud.custom_captcha(url) == cap


def test_the_check_before_submit_reads_the_page_as_it_is_now(page):
    """Lever's signature Name/Date appeared after the agent's last read."""
    b = _browser(page, "<html><body><label>Full name <input name=n required></label>"
                       "<button>Submit</button></body></html>")
    b.read_form()
    b.fill_field("Full name", "Asha")
    page.evaluate("""() => document.body.insertAdjacentHTML('afterbegin',
        '<label>Signature date <input name=sd required></label>')""")
    seen = []
    b._guard_submit = lambda fields, partial=False: seen.append([f["name"] for f in fields])
    b.submit()
    assert "Signature date" in seen[0]
