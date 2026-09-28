"""The cloud browser, against a local copy of a Lever form. No Browserbase, no
network: the page is set in a headless Chromium on this machine, and the class
is handed that page instead of opening a session.

The form copies what the real Lever form showed on 2026-09-24: ✱ for required,
the question in `.application-label` two levels above a Yes/No radio, a hidden
resume input, and a location box that throws the text away on blur unless a
suggestion was clicked.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pw = pytest.importorskip("playwright.sync_api")

from apply import agent, cloud                  # noqa: E402
from apply.browser import SubmitRefused         # noqa: E402
from apply.cloud import CloudBrowser, _match    # noqa: E402

FORM = """<html><body><form>
<ul>
 <li class="application-question"><label>
   <div class="application-label">Resume/CV<span class="required">✱</span></div>
   <div class="application-field"><input type="file" name="resume" style="display:none"></div>
 </label></li>
 <li class="application-question"><label>
   <div class="application-label">Full name<span class="required">✱</span></div>
   <div class="application-field"><input type="text" name="name" required></div>
 </label></li>
 <li class="application-question"><label>
   <div class="application-label">Current location<span class="required">✱</span></div>
   <div class="application-field"><input type="text" name="location" id="location-input" autocomplete="off">
   <div class="dropdown-results"></div></div>
 </label></li>
 <li class="application-question"><label>
   <div class="application-label">Notice period</div>
   <div class="application-field"><select name="notice">
     <option value="">Select...</option><option value="0">Immediately</option>
     <option value="30">30 days</option><option value="90">90 days</option></select></div>
 </label></li>
 <li class="application-question custom-question">
   <div class="application-label">Are you authorized to work in India?<span>✱</span></div>
   <div class="application-field required-field"><ul>
     <li><label><input type="radio" name="cards[a][field0]" value="Yes" required>Yes</label></li>
     <li><label><input type="radio" name="cards[a][field0]" value="No" required>No</label></li>
   </ul></div>
 </li>
 <li class="application-question custom-question">
   <div class="application-label">Why this company?</div>
   <div class="application-field"><textarea name="cards[b][field0]"></textarea></div>
 </li>
</ul>
<button type="button" id="go">Submit application</button>
</form>
<script>
  const loc = document.getElementById('location-input');
  const box = loc.parentElement.querySelector('.dropdown-results');
  let chosen = false;
  loc.addEventListener('input', () => {
    chosen = false;
    box.innerHTML = '';
    setTimeout(() => {           // looked up "online", like Lever's
      for (const c of ['Bengaluru, Karnataka, India', 'Bengaluru Rural, Karnataka, India']) {
        const d = document.createElement('div'); d.textContent = c;
        d.onmousedown = () => { loc.value = c; chosen = true; box.innerHTML = ''; };
        box.appendChild(d);
      }
    }, 400);
  });
  loc.addEventListener('blur', () => setTimeout(() => {
    if (!chosen) loc.value = '';
    box.innerHTML = '';
  }, 50));
</script></body></html>"""


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
def browser(chromium):
    page = chromium.new_page()
    page.set_content(FORM)
    yield CloudBrowser("about:blank", app_id=1, page=page)
    page.close()


def _by_name(fields):
    return {f["name"]: f for f in fields}


def test_reads_lever_labels_required_and_groups(browser):
    f = _by_name(browser.read_form())
    assert set(f) == {"upload: Resume/CV", "Full name", "Current location",
                      "Notice period", "Are you authorized to work in India?",
                      "Why this company?"}
    assert f["Full name"]["required"] and f["Current location"]["required"]
    assert f["upload: Resume/CV"]["required"]         # ✱, though the input says nothing
    assert not f["Notice period"]["required"]
    radio = f["Are you authorized to work in India?"]
    assert radio["type"] == "radio" and radio["options"] == ["Yes", "No"]
    assert radio["required"]
    assert f["Notice period"]["options"] == ["Immediately", "30 days", "90 days"]
    assert f["Why this company?"]["type"] == "textarea"


def test_fills_text_select_radio_textarea(browser):
    browser.read_form()
    assert browser.fill_field("Full name", "Asha Rao") == "filled Full name"
    assert "filled Notice period" in browser.fill_field("Notice period", "30 days")
    assert "chose 'Yes'" in browser.fill_field("Are you authorized to work in India?", "yes")
    assert browser.fill_field("Why this company?", "The product.") == "filled Why this company?"
    vals = browser.current_values()
    assert vals["Full name"] == "Asha Rao"
    assert vals["Notice period"] == "30 days"
    assert vals["Are you authorized to work in India?"] == "Yes"


def test_wrong_choice_and_unknown_field_are_errors(browser):
    browser.read_form()
    out = browser.fill_field("Are you authorized to work in India?", "Maybe")
    assert out.startswith("ERROR") and "Yes" in out
    assert browser.fill_field("Salary", "10").startswith("ERROR: no field named")


def test_location_picks_a_suggestion_so_it_survives_blur(browser):
    browser.read_form()
    out = browser.fill_field("Current location", "Bengaluru")
    assert "picked 'Bengaluru, Karnataka, India'" in out
    assert browser.current_values()["Current location"] == "Bengaluru, Karnataka, India"


def test_location_never_takes_a_suggestion_that_does_not_match(browser):
    """Live bug: "Bengaluru" became "Luru, Chainpur, Gumla, Jharkhand" when the
    first suggestion was taken on no match."""
    browser.read_form()
    out = browser.fill_field("Current location", "Bangalore")
    assert out.startswith("ERROR") and "Bengaluru, Karnataka, India" in out
    assert not browser.current_values().get("Current location")
    assert "picked" in browser.fill_field("Current location", "Bengaluru, Karnataka, India")


def test_upload_reaches_a_hidden_file_input(browser, tmp_path):
    cv = tmp_path / "Asha_Acme.pdf"
    cv.write_bytes(b"%PDF-1.4 test")
    browser.read_form()
    assert browser.upload_file("upload: Resume/CV", str(cv)) == "uploaded Asha_Acme.pdf"
    assert browser.current_values()["upload: Resume/CV"] == "Asha_Acme.pdf"
    assert browser.upload_file("upload: Resume/CV", str(tmp_path / "nope.pdf")) \
        .startswith("ERROR: file not found")


def test_submit_guard_reads_the_page(browser, tmp_path, monkeypatch):
    monkeypatch.delenv("ALLOW_SUBMIT", raising=False)
    browser.read_form()
    with pytest.raises(SubmitRefused, match="required fields still empty"):
        browser.submit()
    cv = tmp_path / "cv.pdf"
    cv.write_bytes(b"%PDF")
    browser.upload_file("upload: Resume/CV", str(cv))
    browser.fill_field("Full name", "Asha Rao")
    browser.fill_field("Current location", "Bengaluru")
    browser.fill_field("Are you authorized to work in India?", "No")
    with pytest.raises(SubmitRefused, match="ALLOW_SUBMIT"):
        browser.submit()


def test_match():
    opts = ["Immediately", "30 days", "90 days"]
    assert _match(opts, "30 DAYS") == "30 days"
    assert _match(opts, "Immed") == "Immediately"
    assert _match(opts, "90") == "90 days"
    assert _match(opts, "never") is None


# --- wiring ---------------------------------------------------------------

def test_browser_for_picks_cloud_only_when_asked(monkeypatch):
    made = []

    class Stub:
        def __init__(self, url, app_id):
            made.append((url, app_id))

    monkeypatch.setattr(cloud, "CloudBrowser", Stub)
    monkeypatch.setenv(agent.BROWSER_ENV, "cloud")
    b = agent.browser_for({"apply_url": "https://jobs.lever.co/x/1/apply"}, 5)
    assert isinstance(b, Stub) and made == [("https://jobs.lever.co/x/1/apply", 5)]


def test_only_cloud_sessions_are_closed_after_a_run():
    closed = []

    class Cloudy:
        close_after_run = True
        def close(self):
            closed.append("cloud")

    class Local:
        def close(self):
            closed.append("jev")

    agent._close(Cloudy())
    agent._close(Local())
    assert closed == ["cloud"]


def test_session_waits_when_every_browser_is_busy(monkeypatch):
    monkeypatch.setattr(cloud, "_keys", lambda: ("k", "p"))
    monkeypatch.setattr(cloud.time, "sleep", lambda s: None)
    codes = iter([429, 429, 201])
    sent = []

    class R:
        def __init__(self, code):
            self.status_code = code
        def raise_for_status(self):
            return self
        def json(self):
            return {"id": "s1", "connectUrl": "wss://x"}

    def post(url, json=None, **kw):
        sent.append(json)
        return R(next(codes))

    monkeypatch.setattr(cloud.httpx, "post", post)
    s = cloud.Session(app_id=9)
    assert s.id == "s1" and len(sent) == 3
    assert sent[0]["timeout"] == cloud.SESSION_SECONDS
    assert s.replay_url.endswith("/sessions/s1")


def test_session_gives_up_with_a_plain_message(monkeypatch):
    monkeypatch.setattr(cloud, "_keys", lambda: ("k", "p"))
    monkeypatch.setattr(cloud.time, "sleep", lambda s: None)

    class R:
        status_code = 429

    monkeypatch.setattr(cloud.httpx, "post", lambda *a, **k: R())
    with pytest.raises(RuntimeError, match="busy"):
        cloud.Session(tries=2)


# --- Greenhouse dropdowns (react-select) ------------------------------------

GREENHOUSE = """<html><body><form>
<div class="field">
  <label id="q1-label" for="q1">Do you have a valid driver's license?*</label>
  <div class="select__container">
    <div class="select__control"><div class="select__value-container"></div>
      <input id="q1" role="combobox" aria-labelledby="q1-label" aria-required="true"
             aria-haspopup="true" aria-autocomplete="list" class="select__input">
    </div>
    <input required tabindex="-1" aria-hidden="true" class="requiredInput" style="opacity:0">
    <div class="select__menu"></div>
  </div>
</div>
<script>
  const input = document.getElementById('q1');
  const menu = document.querySelector('.select__menu');
  const shown = document.querySelector('.select__value-container');
  const all = ['Yes', 'No', 'Prefer not to say'];
  const draw = () => {
    menu.innerHTML = '';
    for (const c of all.filter(c => c.toLowerCase().includes(input.value.toLowerCase()))) {
      const d = document.createElement('div');
      d.setAttribute('role', 'option'); d.className = 'select__option'; d.textContent = c;
      d.onmousedown = () => { shown.innerHTML = '<div class="select__single-value">' + c + '</div>';
                              input.value = ''; menu.innerHTML = ''; };
      menu.appendChild(d);
    }
  };
  input.addEventListener('input', draw);
  input.addEventListener('focus', draw);
  input.addEventListener('blur', () => setTimeout(() => { input.value = ''; menu.innerHTML = ''; }, 50));
</script></form></body></html>"""


@pytest.fixture
def greenhouse(chromium):
    page = chromium.new_page()
    page.set_content(GREENHOUSE)
    yield CloudBrowser("about:blank", app_id=2, page=page)
    page.close()


def test_react_select_is_one_dropdown_not_two_text_fields(greenhouse):
    fields = greenhouse.read_form()
    assert [(f["name"], f["type"], f["required"]) for f in fields] == \
        [("Do you have a valid driver's license?", "select", True)]


def test_react_select_is_filled_by_choosing(greenhouse):
    greenhouse.read_form()
    out = greenhouse.fill_field("Do you have a valid driver's license?", "yes")
    assert "picked 'Yes'" in out
    assert greenhouse.current_values() == {"Do you have a valid driver's license?": "Yes"}


def test_react_select_names_its_choices_when_none_match(greenhouse):
    greenhouse.read_form()
    out = greenhouse.fill_field("Do you have a valid driver's license?", "Sometimes")
    assert out.startswith("ERROR") and "'Yes', 'No', 'Prefer not to say'" in out
