"""Widgets the form eval (2026-09-30) found the filler could not work, each on
a local copy of its shape. No network, nothing submitted."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pw = pytest.importorskip("playwright.sync_api")

from apply import cloud                          # noqa: E402
from apply.cloud import CloudBrowser             # noqa: E402

# Teamtailor: a click on the radio "did not change its state".
TEAMTAILOR = """<html><body><form>
<fieldset><legend>Do you have experience coding in Java?*</legend>
 <input type="radio" id="y" name="java" value="true" required><label for="y">Yes</label>
 <input type="radio" id="n" name="java" value="false"><label for="n">No</label>
</fieldset>
<label><input type="checkbox" name="li"> Apply with LinkedIn</label>
</form>
<script>
 // Blocks the default toggle on the input itself; the label still works.
 for (const r of document.querySelectorAll('input[type=radio]'))
   r.addEventListener('click', e => { if (e.isTrusted) e.preventDefault(); });
</script></body></html>"""

# Workable: a react-datepicker text box with MM/DD/YYYY; a numbers-only box.
DATES = """<html><body><form>
<div class="react-datepicker-wrapper"><label for="d">When will you be available to start?*
SVGs not supported by this browser.</label>
<input id="d" type="text" placeholder="MM/DD/YYYY" required></div>
<label for="pay">Desired pay rate*</label><input id="pay" type="text" required>
<label for="dob">Date of birth</label><input id="dob" type="date">
</form><script>
 document.getElementById('pay').addEventListener('input', e => {
   e.target.value = e.target.value.replace(/[^0-9.]/g, ''); });
</script></body></html>"""

# Greenhouse / SmartRecruiters: the input empties, a chip names the file.
CHIP = """<html><body><form>
<div class="upload"><label for="r">Resume/CV*</label>
 <input id="r" type="file" required><span class="chip"></span></div>
</form><script>
 const r = document.getElementById('r');
 r.addEventListener('change', () => {
   const name = r.files[0].name; const box = r.parentElement;
   r.remove(); box.querySelector('.chip').textContent = '10MB size limit ' + name; });
</script></body></html>"""

# A two-page form in one document, like SmartRecruiters.
PAGES = """<html><body><form onsubmit="return false">
<div id="p1"><label for="a">First name*</label><input id="a" required>
 <button type="button" id="next">Next</button></div>
<div id="p2" style="display:none"><label for="b">Notice period*</label><input id="b" required>
 <button type="button">Submit</button></div>
</form><script>
 document.getElementById('next').onclick = () => {
   if (!document.getElementById('a').value) return;
   document.getElementById('p1').style.display = 'none';
   document.getElementById('p2').style.display = 'block'; };
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
def make(chromium):
    pages = []

    def _make(html):
        page = chromium.new_page()
        page.set_content(html)
        pages.append(page)
        return CloudBrowser("about:blank", app_id=1, page=page)
    yield _make
    for p in pages:
        p.close()


def test_a_radio_that_blocks_clicks_is_still_chosen(make):
    b = make(TEAMTAILOR)
    f = {x["name"]: x for x in b.read_form()}
    assert "chose 'Yes'" in b.fill_field("Do you have experience coding in Java?", "Yes")
    assert b.current_values()["Do you have experience coding in Java?"] == "Yes"
    assert not any("LinkedIn" in n for n in f)          # apply-with boxes are hidden


def test_date_fields_take_the_format_they_show(make):
    b = make(DATES)
    f = {x["name"]: x for x in b.read_form()}
    start = "When will you be available to start?"
    assert f[start]["type"] == "date" and f[start]["format"] == "MM/DD/YYYY"
    assert "SVGs" not in start
    assert b.fill_field(start, "2026-11-02") == f"filled {start} with '11/02/2026'"
    assert b.fill_field("Date of birth", "1 Jan 1990").endswith("'1990-01-01'")
    assert b.fill_field(start, "next month").startswith("ERROR")


def test_a_numbers_only_box_says_so(make):
    b = make(DATES)
    b.read_form()
    out = b.fill_field("Desired pay rate", "thirty dollars")
    assert out.startswith("ERROR") and "numbers only" in out
    assert b.fill_field("Desired pay rate", "30") == "filled Desired pay rate"


def test_a_file_shown_as_a_chip_counts_as_uploaded(make, tmp_path):
    cv = tmp_path / "Asha_Rao.pdf"
    cv.write_bytes(b"%PDF")
    b = make(CHIP)
    b.read_form()
    assert b.upload_file("upload: Resume/CV", str(cv)).startswith("uploaded")
    assert b.current_values()["upload: Resume/CV"] == "Asha_Rao.pdf"


def test_next_page_keeps_earlier_answers_and_refuses_early(make):
    b = make(PAGES)
    b.read_form()
    assert b.next_page().startswith("ERROR: fill these first")
    b.fill_field("First name", "Asha")
    out = b.next_page()
    assert out.startswith("on the next page now") and "Notice period" in out
    b.fill_field("Notice period", "30")
    vals = b.current_values()
    assert vals["First name"] == "Asha" and vals["Notice period"] == "30"
    assert b.next_page().startswith("ERROR: this page has a submit button")


def test_date_helpers():
    assert cloud._date_format("DD/MM/YYYY", "text", False) == "DD/MM/YYYY"
    assert cloud._date_format("", "date", False) == "YYYY-MM-DD"
    assert cloud._date_format("Your name", "text", False) is None
    assert cloud._format_date(date(2026, 1, 5), "DD.MM.YYYY") == "05.01.2026"
    assert cloud._parse_date("15/10/2026") == date(2026, 10, 15)


# Breezy: a spam trap off-screen, and labels only as placeholders.
BREEZY = """<html><body><form>
<h3>Personal Details</h3>
<input type="text" name="cName" placeholder="Full Name" required>
<input type="email" name="cEmail" placeholder="Email Address" required>
<div aria-hidden="true" style="position:absolute;left:-9999px;height:0;overflow:hidden;opacity:0">
 <label for="hp"></label><input id="hp" type="text" name="hp_7f2b" tabindex="-1"></div>
<input type="text" name="d" placeholder="DD/MM/YYYY">
</form></body></html>"""

# An employer's 404 page: a cookie panel of switches and a site search box.
NOT_FOUND = """<html><body>
<div class="wcc-preference-body-wrapper">
 <label><input type="checkbox" id="wccSwitchanalytics"> Analítica</label>
 <label><input type="checkbox" id="wccSwitchads"> Anuncio</label></div>
<label for="q">Search</label><input id="q" type="search">
<h1>404</h1><p>No hemos podido encontrar la página que buscas</p>
</body></html>"""


def test_spam_traps_are_skipped_and_placeholders_name_fields(make):
    b = make(BREEZY)
    names = [f["name"] for f in b.read_form()]
    assert names[:2] == ["Full Name", "Email Address"]
    assert not any("hp" in n.lower() for n in names)
    assert b.page_state()["fields"] == 3


def test_a_cookie_panel_is_not_a_form(make):
    b = make(NOT_FOUND)
    assert b.page_state()["fields"] == 0
    assert all(f["type"] in ("checkbox", "radio") for f in b.read_form())
