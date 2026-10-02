"""Keka's application form, as Loop's showed it on 2026-10-01, and the box
handlers that fill it the way a person would. No network: a local copy in a
headless Chromium on this machine.

What went wrong on the real form, and is pinned here:
- read-only "Years", "Months" and "INR" boxes were handed to the agent as questions;
- the phone's country code (a select2 dropdown) was a field called "Select";
- four boxes were all called "Experience (in years)";
- the date of birth is a read-only calendar box: every typed attempt waited 30 s;
- "+91-9876543210" in a 13-character box became "+91987654321";
- "12 LPA" in a digits-only salary box became 12 (rupees);
- the middle name got the whole name;
- Submit stayed greyed out and nobody read why.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pw = pytest.importorskip("playwright.sync_api")

import vault                                    # noqa: E402
from apply import confirm, vision               # noqa: E402
from apply.cloud import CloudBrowser, _money, _phone, _qualifier   # noqa: E402

KEKA = """<html><body><form id="f">
<a class="resume-upload-link"><span>Upload resume</span><input type="file" id="resume-upload" class="resume-upload" required></a>
<div class="validation"><label for="firstName">First Name <span class="text-danger">*</span></label>
  <input type="text" id="firstName" name="firstName" required></div>
<div class="validation"><label for="middleName">Middle Name</label>
  <input type="text" id="middleName" name="middleName"></div>
<div class="validation"><label for="lastName">Last Name <span class="text-danger">*</span></label>
  <input type="text" id="lastName" name="lastName" required></div>
<div class="validation"><label for="phone">Mobile Phone <span class="text-danger">*</span></label>
  <div class="row">
    <div class="col-3">
      <select class="select2-hidden-accessible" name="mobilePhone.countryCode" id="mobilePhone.countryCode"
              aria-hidden="true" tabindex="-1" style="position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)">
        <option></option><option title="Afghanistan" value="+93">+93</option>
        <option title="India" value="+91">+91</option><option title="United States" value="+1">+1</option>
      </select><span class="select2 select2-container"><span class="selection"><span class="select2-selection" role="combobox" tabindex="0"><span>Select</span></span></span></span>
    </div>
    <input type="text" id="mobilePhone.number" name="mobilePhone.number" maxlength="13" placeholder="Mobile Phone" required>
  </div>
  <label class="error" id="phone-error"></label></div>
<div class="validation"><label for="dateOfBirth">Date of Birth</label>
  <div class="input-calendar"><input type="text" readonly class="date-of-birth hasDatepicker" id="dateOfBirth"
       name="standardFields.dateOfBirth.answer" onchange="this.dataset.changed='yes'"><span class="ki-calendar"></span></div></div>
<div class="validation"><label for="workExperience">Experience (in years)</label>
  <div class="row"><input type="text" name="workExperience.years" id="workExperience">
    <input value="Years" readonly>
    <select name="workExperience.months"><option value="0">0</option><option value="1">1</option><option value="2">2</option></select>
    <input value="Months" readonly></div></div>
<div class="validation"><label for="currentSalary">Current Salary <span class="text-danger">*</span></label>
  <div class="row">
    <select class="select2-hidden-accessible" name="currentSalary.currency" id="currentSalary.currency" aria-hidden="true"
            style="position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)">
      <option></option><option title="Indian Rupee" value="INR" selected>INR</option><option title="US Dollar" value="USD">USD</option>
    </select><span class="select2 select2-container"><span class="selection"><span class="select2-selection" role="combobox" tabindex="0"><span>INR</span></span></span></span>
    <input type="text" id="currentSalary" name="currentSalary.amount" class="digits" required></div></div>
<div class="validation"><label for="expectedSalary">Expected Salary <span class="text-danger">*</span></label>
  <div class="row"><input id="expectedSalary.currency" name="expectedSalary.currency" value="INR" readonly>
    <input type="text" id="expectedSalary" name="expectedSalary.amount" class="digits" required></div></div>
<div class="validation"><label for="availability">Available To Join (in days)</label>
  <input type="number" id="availability" name="standardFields.availability.answer"></div>
<button type="button" id="go" disabled>Submit Application</button>
</form>
<script>
  // Keka's salary boxes keep digits only.
  for (const el of document.querySelectorAll('.digits'))
    el.addEventListener('input', () => { el.value = el.value.replace(/\\D/g, ''); });
  // Submit stays greyed out until the phone is a 10-digit number, with a red
  // message saying so.
  const phone = document.getElementById('mobilePhone.number');
  const check = () => {
    const ok = /^\\d{10}$/.test(phone.value);
    document.getElementById('go').disabled = !ok;
    document.getElementById('phone-error').textContent = ok ? '' : 'Please enter a valid mobile number';
  };
  phone.addEventListener('input', check); phone.addEventListener('change', check); check();
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
def keka(chromium):
    page = chromium.new_page()
    page.set_content(KEKA)
    yield CloudBrowser("about:blank", app_id=1, page=page)
    page.close()


def _by_label(fields):
    return {f["label"]: f for f in fields}


def test_display_boxes_are_not_questions(keka):
    fields = keka.read_form()
    labels = [f["label"] for f in fields]
    assert not any(l in ("Years", "Months", "INR", "Select") for l in labels), labels
    assert "Expected Salary (currency)" not in labels       # read-only INR box


def test_select2_country_code_is_a_real_list_under_its_question(keka):
    by = _by_label(keka.read_form())
    code = by["Mobile Phone (country code)"]
    assert code["type"] == "select"
    assert "India (+91)" in code["options"]
    assert "Current Salary (currency)" in by


def test_shared_label_boxes_get_told_apart(keka):
    by = _by_label(keka.read_form())
    assert "Experience (in years)" in by
    assert "Experience (in years) (months)" in by
    assert by["Experience (in years) (months)"]["options"] == ["0", "1", "2"]


def test_calendar_box_is_a_date_and_takes_a_date_without_typing(keka):
    by = _by_label(keka.read_form())
    assert by["Date of Birth"]["type"] == "date"
    out = keka.fill_field(by["Date of Birth"]["name"], "14 March 1995")
    assert out.startswith("filled"), out
    assert keka.page.input_value("#dateOfBirth") == "14/03/1995"
    assert keka.page.get_attribute("#dateOfBirth", "data-changed") == "yes"


def test_country_code_is_chosen_on_the_hidden_list(keka):
    by = _by_label(keka.read_form())
    out = keka.fill_field(by["Mobile Phone (country code)"]["name"], "+91")
    assert out.startswith("filled"), out
    assert keka.page.eval_on_selector("[name='mobilePhone.countryCode']", "e => e.value") == "+91"


def test_phone_goes_in_as_ten_digits_beside_a_country_code(keka):
    by = _by_label(keka.read_form())
    out = keka.fill_field(by["Mobile Phone (number)"]["name"], "+91-9876543210")
    assert out.startswith("filled"), out
    assert keka.page.input_value("[name='mobilePhone.number']") == "9876543210"


def test_salary_in_lpa_goes_in_as_rupees_and_is_read_back(keka):
    by = _by_label(keka.read_form())
    out = keka.fill_field(by["Current Salary (amount)"]["name"], "12 LPA")
    assert out.startswith("filled") and "1200000" in out, out
    assert keka.page.input_value("#currentSalary") == "1200000"
    out = keka.fill_field(by["Expected Salary"]["name"], "18")      # a bare 18: lakh
    assert keka.page.input_value("#expectedSalary") == "1800000", out


def test_middle_name_never_takes_the_whole_name(keka):
    by = _by_label(keka.read_form())
    assert keka.fill_field(by["First Name"]["name"], "SAI SWAROOP").startswith("filled")
    assert keka.page.input_value("#firstName") == "Sai Swaroop"
    assert keka.fill_field(by["Last Name"]["name"], "Bugata").startswith("filled")
    out = keka.fill_field(by["Middle Name"]["name"], "BUGATA SAI SWAROOP")
    assert out.startswith("ERROR") and "blank" in out
    assert keka.page.input_value("#middleName") == ""


def test_a_greyed_out_submit_is_explained_before_anyone_is_asked(keka):
    fields = keka.read_form()
    said = keka._greyed_out(keka.page_state(), fields)
    assert said.startswith("ERROR: NOT SENT") and "valid mobile number" in said
    keka.fill_field(_by_label(fields)["Mobile Phone (number)"]["name"], "9876543210")
    assert keka._greyed_out(keka.page_state(), keka.read_form()) == ""


def test_look_is_skipped_without_a_cloud_session(keka):
    assert keka._look(keka.read_form()) == ""                       # tests never call out


# --- the rules, without a browser --------------------------------------------

@pytest.mark.parametrize("value,label,want", [
    ("12 LPA", "Current Salary", "1200000"),
    ("18", "Expected Salary", "1800000"),
    ("1200000", "Current CTC", "1200000"),
    ("12,00,000", "Current CTC", "1200000"),
    ("₹12 lakh", "Expected CTC", "1200000"),
    ("1.2 cr", "Current CTC", "12000000"),
    ("12 LPA", "Current CTC (in Lakhs)", "12"),
    ("1200000", "Expected CTC in LPA", "12"),
    ("30", "Desired pay rate", None),            # dollars an hour, not lakh
    ("50000", "Expected salary per month", None),
    ("5", "Years of experience", None),
])
def test_money(value, label, want):
    assert _money(value, label) == want


def test_phone_rules():
    assert _phone("+91-9876543210", 13, True) == "9876543210"
    assert _phone("+91 9876543210", 20, False) == "+91 9876543210"   # room, no code box
    assert _phone("+91-9876543210", 10, False) == "9876543210"       # too long for the box
    assert _phone("09876543210", 0, True) == "9876543210"


def test_qualifier():
    assert _qualifier("workExperience.months") == "months"
    assert _qualifier("mobilePhone.countryCode") == "country code"
    assert _qualifier("standardFields.dateOfBirth.answer") == "date of birth"
    assert _qualifier("") == ""


def test_approval_ignores_phone_formatting():
    before = {"Mobile Phone": "+91-9876543210", "Name": "Sai"}
    assert confirm.same(before, {"Mobile Phone": "9876543210", "Name": "sai "})
    assert not confirm.same(before, {"Mobile Phone": "9876543211", "Name": "Sai"})
    q = confirm.question({"title": "PM", "company": "X"}, {"Name": "Sai"},
                         notes=["Experience: shows Months"])
    assert "Check these" in q and "shows Months" in q


def test_vision_reply_parsing():
    assert vision._problems('```json\n{"problems": ["Date of Birth: empty"]}\n```') == ["Date of Birth: empty"]
    assert vision._problems('Looks fine. {"problems": []}') == []
    assert vision._problems("no json here") is None


def test_small_boxes_beside_an_answer_are_not_that_answer():
    assert vault.match_key("Mobile Phone (country code)") is None
    assert vault.match_key("Current Salary (currency)") is None
    assert vault.match_key("Experience (in years) (months)") is None
    assert vault.match_key("Experience (in years)") == "years_experience"
    assert vault.match_key("Middle Name") == "middle_name"
    assert vault.match_key("Date of Birth") == "date_of_birth"


def _vault_conn(rows):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE vault (key TEXT PRIMARY KEY, value TEXT, source TEXT, updated_at TEXT)")
    c.executemany("INSERT INTO vault (key, value, source) VALUES (?, ?, ?)", rows)
    return c


def test_a_guessed_split_of_a_three_word_name_is_asked_not_used():
    c = _vault_conn([("full_name", "Bugata Sai Swaroop", "resume"),
                     ("first_name", "BUGATA", "resume"), ("last_name", "SWAROOP", "resume"),
                     ("phone", "+91-9876543210", "resume")])
    got = vault.slice_for(["First Name", "Last Name", "Mobile Phone (country code)"], c)
    assert "First Name" not in got and "Last Name" not in got
    assert got["Mobile Phone (country code)"] == "+91"
    c.execute("UPDATE vault SET value = 'Sai Swaroop', source = 'user' WHERE key = 'first_name'")
    assert vault.slice_for(["First Name"], c) == {"First Name": "Sai Swaroop"}


CONSENT = """<html><body>
<div id="cookie-consent-banner">We use cookies. <label><input type="checkbox" name="cookie_ok"> Allow analytics cookies</label></div>
<form>
<label for="name">Full name *</label><input id="name" name="name" required>
<label for="site">Enter website. This input is for robots only, do not enter if you're human.</label>
<input id="site" name="website">
<div id="ui-datepicker-div" class="ui-datepicker"><select class="ui-datepicker-month"><option>Jan</option><option>Feb</option></select>
  <select class="ui-datepicker-year"><option>1990</option></select></div>
<h3>Apply for this job</h3>
<div class="d-flex"><input type="checkbox" id="candidateConsent" name="candidateConsent" value="1">
  <span id="candidateConsentLabel">By applying, you hereby accept the data processing terms under the Privacy Policy.</span></div>
<button type="button" id="go" disabled>Apply Now</button>
</form>
<script>
  document.getElementById('candidateConsent').addEventListener('change', e => {
    document.getElementById('go').disabled = !e.target.checked; });
</script></body></html>"""


@pytest.fixture
def consent(chromium):
    page = chromium.new_page()
    page.set_content(CONSENT)
    yield CloudBrowser("about:blank", app_id=1, page=page)
    page.close()


def test_the_application_consent_box_is_a_question_named_by_its_sentence(consent):
    fields = consent.read_form()
    names = [f["name"] for f in fields]
    assert any(n.startswith("By applying, you hereby accept") for n in names), names
    assert not any("cookie" in n.lower() for n in names)          # the banner's box is not
    assert not any("robots" in n.lower() or n == "website" for n in names)   # the trap is not
    assert not any(n in ("Feb", "Jan", "1990") for n in names)      # an open calendar is not


def test_greyed_out_submit_names_the_consent_box_to_tick(consent):
    fields = consent.read_form()
    consent.fill_field("Full name", "Kavita Iyer")
    said = consent._greyed_out(consent.page_state(), fields)
    assert "Tick these consent boxes" in said and "By applying" in said
    box = next(f["name"] for f in fields if f["name"].startswith("By applying"))
    assert consent.fill_field(box, "yes").startswith("checked")
    assert consent._greyed_out(consent.page_state(), consent.read_form()) == ""


TEAMTAILOR = """<html><body><form>
<div><h3>Personal information</h3><button type="button">Apply with LinkedIn</button></div>
<label for="fn">First name *</label><input id="fn" name="candidate[first_name]" required>
<div><input type="checkbox" id="c1" name="candidate[consent_given]">
  <label for="c1"><span>Required.</span>By submitting this application, I agree that I have read the Privacy Policy.</label></div>
<div><input type="checkbox" id="li" name="apply_with_linkedin"><label for="li">Apply with LinkedIn</label></div>
</form></body></html>"""


def test_consent_box_under_an_apply_with_linkedin_heading_is_kept(chromium):
    page = chromium.new_page()
    page.set_content(TEAMTAILOR)
    try:
        names = [f["name"] for f in CloudBrowser("about:blank", app_id=1, page=page).read_form()]
        assert any("By submitting this application" in n for n in names), names
        assert not any(n.lower().startswith("apply with") for n in names)    # the hand-off box still goes
    finally:
        page.close()


MULTI = """<html><body><form>
<label id="q-label" for="q">Processing of Personal Data *</label>
<div class="select-shell"><div class="select__control"><div class="select__value-container select__value-container--is-multi">
  <div class="select__input-container"><input class="select__input" id="q" role="combobox" aria-autocomplete="list"
       aria-haspopup="true" aria-labelledby="q-label" aria-required="true" value=""></div></div></div>
  <div class="menu"></div></div>
</form>
<script>
  // react-select, multi: picking an option leaves a chip and an empty input.
  const inp = document.getElementById('q');
  const box = document.querySelector('.select__value-container');
  const menu = document.querySelector('.menu');
  inp.addEventListener('input', () => {
    menu.innerHTML = '';
    const o = document.createElement('div'); o.setAttribute('role', 'option'); o.textContent = 'Acknowledge and confirm';
    o.onmousedown = () => {
      const chip = document.createElement('div'); chip.className = 'select__multi-value';
      chip.innerHTML = '<div class="select__multi-value__label">Acknowledge and confirm</div><div role="button">x</div>';
      box.insertBefore(chip, box.firstChild); inp.value = ''; menu.innerHTML = ''; };
    menu.appendChild(o);
  });
</script></body></html>"""


def test_a_multi_select_choice_is_read_back_from_its_chip(chromium):
    page = chromium.new_page()
    page.set_content(MULTI)
    try:
        b = CloudBrowser("about:blank", app_id=1, page=page)
        f = next(f for f in b.read_form() if f["name"].startswith("Processing of Personal Data"))
        out = b.fill_field(f["name"], "Acknowledge and confirm")
        assert not out.startswith("ERROR"), out
        assert b.current_values().get(f["name"]) == "Acknowledge and confirm"
    finally:
        page.close()
