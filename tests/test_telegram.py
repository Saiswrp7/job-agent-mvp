"""The Telegram bot, with no Telegram, GLM or browser. The router is handed a
fake start function; the worker is handed fakes for everything it calls."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import telegram_bot as tg                         # noqa: E402


class FakeBot:
    def __init__(self):
        self.sent, self.photos, self.files = [], [], {}

    def send(self, chat_id, text):
        self.sent.append((chat_id, text))

    def send_photo(self, chat_id, path, caption):
        self.photos.append((chat_id, Path(path).name, caption))

    def typing(self, chat_id):
        pass

    def download(self, file_id):
        return self.files[file_id]


class FakeProc:
    def __init__(self):
        self.lines = []
        self.alive = True
        me = self

        class In:
            def write(self, s):
                me.lines.append(s)

            def flush(self):
                pass

            def close(self):
                pass
        self.stdin = In()

    def poll(self):
        return None if self.alive else 0


def _msg(uid, text="hi", **extra):
    return {"message": {"from": {"id": uid, "first_name": "Asha", "username": "asha"},
                        "chat": {"id": uid, "type": "private"}, "text": text, **extra}}


def _env(tmp_path, **kv):
    p = tmp_path / ".env"
    p.write_text("\n".join(f"{k}={v}" for k, v in kv.items()))
    return p


# --- invite list and routing ------------------------------------------------

def test_allowed_reads_env_fresh(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_ALLOWED", raising=False)
    p = _env(tmp_path, TELEGRAM_ALLOWED="111, 222")
    assert tg.allowed(p) == {111, 222}
    p.write_text("TELEGRAM_ALLOWED=111,222,333\n")
    assert tg.allowed(p) == {111, 222, 333}            # no restart needed


def test_stranger_is_turned_away_once_and_admin_told(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_ALLOWED", raising=False)
    monkeypatch.delenv("TELEGRAM_ADMIN", raising=False)
    bot, started = FakeBot(), []
    r = tg.Router(bot, start=lambda uid: started.append(uid) or FakeProc(),
                  env_file=_env(tmp_path, TELEGRAM_ALLOWED="111", TELEGRAM_ADMIN="111"))
    r.route(_msg(999))
    r.route(_msg(999, "hello?"))
    assert started == []
    to_stranger = [t for c, t in bot.sent if c == 999]
    to_admin = [t for c, t in bot.sent if c == 111]
    assert to_stranger == [tg.PRIVATE]                 # told once, not per message
    assert len(to_admin) == 1 and "999" in to_admin[0]


def test_each_person_gets_their_own_worker(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_ALLOWED", raising=False)
    procs = {}

    def start(uid):
        procs[uid] = FakeProc()
        return procs[uid]

    r = tg.Router(FakeBot(), start=start,
                  env_file=_env(tmp_path, TELEGRAM_ALLOWED="111,222"))
    r.route(_msg(111, "one"))
    r.route(_msg(222, "two"))
    r.route(_msg(111, "three"))
    assert set(procs) == {111, 222}
    assert ['"one"' in l for l in procs[111].lines] == [True, False]
    assert len(procs[222].lines) == 1
    procs[111].alive = False                           # crashed: started again
    r.route(_msg(111, "four"))
    assert '"four"' in procs[111].lines[0]


def test_groups_are_ignored(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_ALLOWED", raising=False)
    started = []
    r = tg.Router(FakeBot(), start=lambda uid: started.append(uid) or FakeProc(),
                  env_file=_env(tmp_path, TELEGRAM_ALLOWED="111"))
    m = _msg(111)
    m["message"]["chat"]["type"] = "group"
    r.route(m)
    assert started == []


def test_profile_is_per_telegram_id():
    assert tg.profile_dir(42).name == "tg-42"
    assert tg.profile_dir(42).parent.name == "profiles"


# --- formatting -------------------------------------------------------------

def test_to_html_escapes_then_formats():
    out = tg.to_html("## Top picks\n**PM** at <Acme> & co — *remote* `id 3` "
                     "[apply](https://x.co/a?b=1&c=2)\n* bullet stays")
    assert "<b>Top picks</b>" in out and "<b>PM</b>" in out
    assert "&lt;Acme&gt; &amp; co" in out
    assert "<i>remote</i>" in out and "<code>id 3</code>" in out
    assert '<a href="https://x.co/a?b=1&amp;c=2">apply</a>' in out
    assert "* bullet stays" in out


def test_chunks_split_on_lines_under_the_limit():
    text = "\n".join(f"line {i} " + "x" * 50 for i in range(200))
    parts = tg.chunks(text, size=1000)
    assert all(len(p) <= 1000 for p in parts)
    assert "".join(p.replace("\n", "") for p in parts) == text.replace("\n", "")
    assert tg.chunks("short") == ["short"]


# --- the worker ---------------------------------------------------------------

def _worker(tmp_path, bot, replies=None, news_rows=None):
    got = []
    rows = list(news_rows or [])

    def unreported(conn):
        out, rows[:] = rows[:], []
        return out

    w = tg.Worker(7, bot, conn=None,
                  reply=lambda text: got.append(text) or (replies or {}).get(text, "ok"),
                  take_resume=lambda name, data, conn: {"reply": f"Read {name} ({len(data)} bytes)"},
                  unreported=unreported,
                  news=lambda rs: f"Update — {rs[0]['company']}: filled",
                  shots=tmp_path)
    return w, got


def test_start_text_and_reply(tmp_path):
    bot = FakeBot()
    w, got = _worker(tmp_path, bot, replies={"/start": "Hi, Jojo here.",
                                             "pm jobs": "Here are 5."})
    w.handle(_msg(7, "/start")["message"])
    w.handle(_msg(7, "pm jobs")["message"])
    # /start is answered by the model, not a canned welcome.
    assert bot.sent[0] == (7, "Hi, Jojo here.")
    assert bot.sent[1] == (7, "Here are 5.") and got == ["/start", "pm jobs"]


def test_resume_pdf_is_taken_and_other_files_are_not(tmp_path):
    bot = FakeBot()
    bot.files["f1"] = b"%PDF-1.4 hello"
    w, got = _worker(tmp_path, bot)
    w.handle({"document": {"file_name": "Asha.pdf", "file_id": "f1"}})
    w.handle({"document": {"file_name": "Asha.docx", "file_id": "f2"}})
    # No fixed text (Sai, 2026-10-01): the chat is told, and answers in its
    # own words, carrying on with what they were doing.
    assert got[0].startswith("(resume received: Asha.pdf. Read it:")
    assert got[1].startswith("(they sent Asha.docx, which is not a PDF")
    assert [t for _, t in bot.sent] == ["ok", "ok"]
    # Nothing in the note reads as a resume choice they did not make.
    import chat
    assert chat.said_resume({"user_message": got[0]}, "tailored") == set()


def test_a_crash_is_told_not_swallowed(tmp_path):
    bot = FakeBot()
    w, _ = _worker(tmp_path, bot)
    w.reply = lambda text: 1 / 0
    w.handle(_msg(7, "hi")["message"])
    assert "Something broke" in bot.sent[0][1]


def test_push_sends_the_screenshot_when_there_is_one(tmp_path):
    (tmp_path / "app_3.png").write_bytes(b"png")
    bot = FakeBot()
    w, _ = _worker(tmp_path, bot, news_rows=[
        {"id": 3, "company": "Acme"}, {"id": 4, "company": "Beta"}])
    w.push()
    w.push()                                           # nothing twice
    assert bot.photos == [(7, "app_3.png", "Update — Acme: filled")]
    assert bot.sent == [(7, "Update — Beta: filled")]


def test_a_budget_is_read_per_person_from_the_settings(monkeypatch):
    """Sai, 2026-10-01: "add 1 dollar budget to him". USER_BUDGETS holds
    id:usd pairs; anyone not listed has no cap."""
    monkeypatch.setenv("USER_BUDGETS", "1000000002:1, 123:2.5")
    assert tg.budget_for(1000000002) == 1.0
    assert tg.budget_for(123) == 2.5
    assert tg.budget_for(1000000001) is None
