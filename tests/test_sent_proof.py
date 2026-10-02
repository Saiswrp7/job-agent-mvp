"""Sent means proven: the confirmation page is pictured and quoted, and a chat
reply may not say an application went through unless its row says so.

Sai, 2026-09-30: Swiggy ended blocked, nothing sent, and the bot twice said it
"already went through"."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import chat                                        # noqa: E402
import telegram_bot as tg                          # noqa: E402
from apply import pagecheck                        # noqa: E402
from engine import db                              # noqa: E402
from tests.test_telegram import FakeBot, _worker   # noqa: E402


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    c.execute("INSERT INTO applications (id, source, source_id, company, title, status) "
              "VALUES (1, 'smartrecruiters', '1', 'Swiggy', 'Manager - Growth', 'blocked')")
    c.commit()
    yield c
    c.close()


# --- 3: the reply check ---------------------------------------------------------

@pytest.mark.parametrize("said", [
    "Your \"submit\" already went in, and the Swiggy run has finished, nothing's pending.",
    "The Swiggy Growth & Storefront one already went through, it's closed.",
    "Done, your application went through.",
])
def test_a_false_sent_claim_is_replaced_by_the_truth(conn, said):
    out = chat.no_false_sent(said + "\n\nWant another one?", conn)
    assert out.startswith("Manager - Growth at Swiggy was NOT sent")
    assert "went" not in out and out.endswith("Want another one?")


@pytest.mark.parametrize("said", [
    "Submitting it now, I'll confirm once it's through.",
    "Just sent it, it's attached above.",
    "Nothing was sent. The application was not submitted.",
    "Have you applied to Swiggy before?",
    "Want me to apply to CRED next?",
])
def test_honest_replies_pass_untouched(conn, said):
    assert chat.no_false_sent(said, conn) == said


def test_a_confirmed_application_may_be_called_sent(conn):
    conn.execute("UPDATE applications SET status = 'submitted'")
    assert chat.no_false_sent("The Swiggy one went through!", conn) == "The Swiggy one went through!"


def test_with_no_applications_nothing_can_have_gone(conn):
    conn.execute("DELETE FROM applications")
    out = chat.no_false_sent("Your application went through.", conn)
    assert out.startswith("Nothing has been sent")


# --- 2: proof of a send ------------------------------------------------------------

def test_proof_quotes_the_new_confirming_line():
    before = "Apply\nWe will review your application once it is submitted"
    after = "Thank you for applying to Swiggy!\nWe will be in touch."
    assert pagecheck.proof(before, after, "u", "u") == "Thank you for applying to Swiggy!"
    assert pagecheck.proof("x", "x", "https://a/apply", "https://a/thank-you") \
        == "the page moved to https://a/thank-you"


def test_a_sent_update_carries_the_confirmation_picture(tmp_path):
    (tmp_path / "app_5.png").write_bytes(b"form")
    (tmp_path / "app_5_sent.png").write_bytes(b"thanks")
    bot = FakeBot()
    w, _ = _worker(tmp_path, bot, news_rows=[{"id": 5, "company": "Acme", "status": "submitted"}])
    w.push()
    assert bot.photos[0][1] == "app_5_sent.png"


def test_a_sent_update_without_a_picture_says_so(tmp_path):
    (tmp_path / "app_6.png").write_bytes(b"form")      # the form, not proof
    bot = FakeBot()
    w, _ = _worker(tmp_path, bot, news_rows=[{"id": 6, "company": "Acme", "status": "submitted"}])
    w.push()
    assert bot.photos == [] and "No picture of the confirmation page" in bot.sent[0][1]


def test_the_sent_update_quotes_the_site_whole():
    line = chat.news_lines([{"title": "PM", "company": "Co", "status": "submitted",
                             "outcome": 'The page confirms it: "Thanks. We got it." (u)\n\nDone.'}])
    assert 'The page confirms it: "Thanks. We got it." (u)' in line
