"""Telegram: the chat, for the 3-4 people who are not at this Mac.

    python3 cli.py telegram

**Sign-in is the Telegram account.** Every message carries the sender's
numeric id, which they cannot fake, so there is no password here to build or
leak. Only ids in `TELEGRAM_ALLOWED` (in `.env`, comma-separated) get through;
anyone else is told it is a private beta, and their id is printed (and sent to
`TELEGRAM_ADMIN`, if set) so you can add them. The list is re-read on every
message from a stranger, so adding someone needs no restart.

**One process per person.** A profile is chosen when a process starts
(`paths.HOME` is fixed at import), so one process cannot be three people.
The router here reads Telegram and hands each person's messages to their own
worker process, started with `JOB_AGENT_HOME=profiles/tg-<id>` — the same split
`tests/test_two_users.py` proves: jobs shared, everything personal private.
Workers reply to Telegram themselves.

**No public address needed.** The router long-polls Telegram (asks for new
messages) instead of Telegram calling in, so it runs on this Mac as it is. The
Mac has to stay awake.

**The chat can now speak first.** On the web page a finished application waited
for the person's next message. Here the worker checks every few seconds and
sends it, with the last screenshot of the form.
"""

from __future__ import annotations

import html
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"
API = "https://api.telegram.org/bot{token}/{method}"
FILES = "https://api.telegram.org/file/bot{token}/{path}"
#: Telegram's cap is 4096 characters; the HTML tags added after splitting
#: need room.
CHUNK = 3500
#: How often an idle worker looks for finished applications.
PUSH_EVERY = 5
#: Said the moment slow work starts. Only the slow tools: a lookup is instant.
PROGRESS = {
    "search_jobs": "Searching jobs, about 20 seconds…",
    "tailor_resume": "Tailoring your resume, about 30 seconds…",
    "build_resume": "Building your resume…",
    "send_resume": "Sending your resume…",
    "start_application": "Starting the application…",
}

PRIVATE = ("Hi! This is a private beta of a job-application assistant, open by "
           "invitation only. I've passed your request on.")
WELCOME = ("Hi! I find jobs that fit you and fill in the applications.\n\n"
           "Start by sending me your resume as a PDF file (📎 → File). Then tell me "
           "what you're looking for, like \"product manager jobs in Bangalore\".\n\n"
           "Nothing is ever submitted without you saying yes.")


def env(key: str, path: Path = ENV_FILE) -> str:
    """A setting: the real environment first, else `.env` read fresh, so an
    edit to `.env` counts without a restart."""
    if os.environ.get(key):
        return os.environ[key]
    try:
        for line in path.read_text().splitlines():
            k, _, v = line.partition("=")
            if k.strip() == key:
                return v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return ""


def allowed(path: Path = ENV_FILE) -> set[int]:
    raw = env("TELEGRAM_ALLOWED", path)
    return {int(x) for x in re.split(r"[,\s]+", raw) if x.strip().isdigit()}


# --- talking to Telegram ------------------------------------------------------

def to_html(md: str) -> str:
    """The chat writes Markdown; Telegram renders a small HTML subset. Escape
    first, so a "<" in a job title can never break the message."""
    t = html.escape(md, quote=False)
    t = re.sub(r"^#{1,6}\s*(.+)$", r"<b>\1</b>", t, flags=re.M)
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"(?<![\w*])\*(?![\s*])(.+?)(?<![\s*])\*(?![\w*])", r"<i>\1</i>", t)
    t = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", t)
    t = re.sub(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', t)
    return t


def chunks(text: str, size: int = CHUNK) -> list[str]:
    """Split on paragraph, then line, boundaries — never inside a word unless
    one line alone is too long."""
    out, cur = [], ""
    for part in re.split(r"(\n\n|\n)", text):
        if len(cur) + len(part) <= size:
            cur += part
            continue
        if cur.strip():
            out.append(cur.strip("\n"))
        cur = part
        while len(cur) > size:
            out.append(cur[:size])
            cur = cur[size:]
    if cur.strip():
        out.append(cur.strip("\n"))
    return out or [""]


class Bot:
    def __init__(self, token: str, client: httpx.Client | None = None):
        self.token = token
        self.http = client or httpx.Client(timeout=60)

    def call(self, method: str, **params):
        r = self.http.post(API.format(token=self.token, method=method), json=params)
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"telegram {method}: {data.get('description')}")
        return data["result"]

    def send(self, chat_id: int, text: str) -> None:
        for part in chunks(text):
            try:
                self.call("sendMessage", chat_id=chat_id, text=to_html(part),
                          parse_mode="HTML", link_preview_options={"is_disabled": True})
            except RuntimeError:
                # Markup Telegram would not take: the words still go out.
                self.call("sendMessage", chat_id=chat_id, text=part)

    #: What they read when a file did not go. The reply above it already said
    #: "attached", so silence here would leave them waiting for nothing.
    NO_FILE = "Couldn't attach the file. Say 'resume' and I'll try again."

    def _upload(self, method: str, field: str, chat_id: int, path: Path,
                caption: str) -> bool:
        """One multipart send. True when Telegram took it. A refusal is
        printed with Telegram's reason, so it shows up in the process log
        instead of vanishing: `send_document` used to drop the reply unread."""
        with open(path, "rb") as f:
            r = self.http.post(API.format(token=self.token, method=method),
                               data={"chat_id": chat_id, "caption": caption[:1000]},
                               files={field: (path.name, f)})
        try:
            data = r.json()
        except ValueError:
            data = {}
        if data.get("ok"):
            return True
        print(f"[telegram] {method} {path.name} refused: {data.get('description')}")
        return False

    def send_photo(self, chat_id: int, path: Path, caption: str) -> None:
        if self._upload("sendPhoto", "photo", chat_id, path, caption):
            return
        # A full-page shot can be too tall for a photo; a document usually goes.
        if not self._upload("sendDocument", "document", chat_id, path, caption):
            self.send(chat_id, self.NO_FILE)

    def send_document(self, chat_id: int, path: Path, caption: str = "") -> None:
        if not self._upload("sendDocument", "document", chat_id, path, caption):
            self.send(chat_id, self.NO_FILE)

    def send_files(self, chat_id: int, paths: list[str]) -> None:
        """Resumes and previews the chat built this turn. A PNG shows inline
        as a photo; a PDF goes as a file they can open and forward."""
        for p in dict.fromkeys(paths):
            path = Path(p)
            if not path.exists():
                # Queued, then gone (a failed rebuild deletes its PDF). Say so
                # rather than skip: the reply has already promised a file.
                print(f"[telegram] file missing at send time: {path}")
                self.send(chat_id, self.NO_FILE)
                continue
            if path.suffix.lower() == ".png":
                self.send_photo(chat_id, path, path.stem.replace("_", " "))
            else:
                self.send_document(chat_id, path)

    def typing(self, chat_id: int) -> None:
        try:
            self.call("sendChatAction", chat_id=chat_id, action="typing")
        except Exception:                             # noqa: BLE001
            pass

    def download(self, file_id: str) -> bytes:
        path = self.call("getFile", file_id=file_id)["file_path"]
        r = self.http.get(FILES.format(token=self.token, path=path))
        r.raise_for_status()
        return r.content


# --- the router ---------------------------------------------------------------

def profile_dir(user_id: int) -> Path:
    return ROOT / "profiles" / f"tg-{user_id}"


def spawn(user_id: int) -> subprocess.Popen:
    """This person's own process, on their own profile, applying in the cloud."""
    child = dict(os.environ,
                 JOB_AGENT_HOME=str(profile_dir(user_id)),
                 JOB_AGENT_BROWSER=env("TELEGRAM_BROWSER") or "cloud",
                 PYTHONUNBUFFERED="1")
    return subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                             "worker", str(user_id)],
                            stdin=subprocess.PIPE, text=True, env=child, cwd=ROOT)


class Router:
    def __init__(self, bot: Bot, start=spawn, env_file: Path = ENV_FILE):
        self.bot, self.start, self.env_file = bot, start, env_file
        self.workers: dict[int, subprocess.Popen] = {}
        self.told: set[int] = set()

    def route(self, update: dict) -> None:
        msg = update.get("message")
        if not msg or msg.get("chat", {}).get("type") != "private":
            return
        who = msg["from"]
        uid = who["id"]
        if uid not in allowed(self.env_file):
            self._stranger(msg["chat"]["id"], who)
            return
        w = self.workers.get(uid)
        if w is None or w.poll() is not None:
            w = self.workers[uid] = self.start(uid)
            print(f"[telegram] started {uid} on {profile_dir(uid).name}")
        try:
            w.stdin.write(json.dumps(msg) + "\n")
            w.stdin.flush()
        except (BrokenPipeError, OSError):
            # Died between poll() and write: start again and hand it over.
            w = self.workers[uid] = self.start(uid)
            w.stdin.write(json.dumps(msg) + "\n")
            w.stdin.flush()

    def _stranger(self, chat_id: int, who: dict) -> None:
        name = " ".join(filter(None, [who.get("first_name"), who.get("last_name")]))
        handle = f"@{who['username']}" if who.get("username") else "no username"
        print(f"[telegram] not invited: {who['id']} ({name}, {handle}) — add the id "
              f"to TELEGRAM_ALLOWED in .env to let them in")
        if who["id"] in self.told:
            return
        self.told.add(who["id"])
        self.bot.send(chat_id, PRIVATE)
        admin = env("TELEGRAM_ADMIN", self.env_file)
        if admin.isdigit() and int(admin) != who["id"]:
            self.bot.send(int(admin), f"{name} ({handle}) wants in. Their id is "
                                      f"{who['id']}: add it to TELEGRAM_ALLOWED in .env.")

    def stop(self) -> None:
        for w in self.workers.values():
            try:
                w.stdin.close()
                w.wait(timeout=10)
            except Exception:                         # noqa: BLE001
                w.kill()


def serve() -> None:
    token = env("TELEGRAM_BOT_TOKEN")
    if not token:
        sys.exit("TELEGRAM_BOT_TOKEN is not in .env")
    bot = Bot(token)
    me = bot.call("getMe")
    router = Router(bot)
    print(f"telegram · @{me['username']} · invited: {sorted(allowed()) or 'nobody yet'}")
    print("message the bot from Telegram; ctrl-c to stop")
    offset = None
    try:
        while True:
            try:
                updates = bot.call("getUpdates", offset=offset, timeout=30,
                                   allowed_updates=["message"])
            except Exception as exc:                  # noqa: BLE001
                print(f"[telegram] poll failed ({exc}); retrying")
                time.sleep(3)
                continue
            for u in updates:
                offset = u["update_id"] + 1
                try:
                    router.route(u)
                except Exception:                     # noqa: BLE001
                    traceback.print_exc()
    except KeyboardInterrupt:
        print()
    finally:
        router.stop()


# --- one person's worker ------------------------------------------------------

class Worker:
    """One person's conversation. Everything it needs is passed in, so a test
    can run it without Telegram, GLM or a browser."""

    def __init__(self, chat_id: int, bot, conn, reply, take_resume, unreported,
                 news, shots: Path):
        self.chat_id, self.bot, self.conn = chat_id, bot, conn
        self.reply, self.take_resume = reply, take_resume
        self.unreported, self.news, self.shots = unreported, news, shots

    def handle(self, msg: dict) -> None:
        try:
            if (msg.get("text") or "").startswith("/start"):
                self.bot.send(self.chat_id, WELCOME)
            elif msg.get("document"):
                self._document(msg["document"])
            elif msg.get("text"):
                self._text(msg["text"])
            else:
                self.bot.send(self.chat_id, "I can read text messages and resumes "
                                            "sent as a PDF file.")
        except Exception as exc:                      # noqa: BLE001
            traceback.print_exc()
            self.bot.send(self.chat_id, f"Something broke on my side "
                                        f"({type(exc).__name__}). Please try again "
                                        f"in a minute.")

    def _document(self, doc: dict) -> None:
        name = doc.get("file_name") or "resume.pdf"
        if not name.lower().endswith(".pdf"):
            self.bot.send(self.chat_id, f"{name} isn't a PDF. Please send your "
                                        f"resume as a PDF file.")
            return
        # Reading takes 10-20 s, and once took 7 minutes on a slow model
        # reply. Silence that long reads as broken.
        self.bot.send(self.chat_id, "Got it. Reading your resume, this takes "
                                    "about 20 seconds.")
        self.bot.typing(self.chat_id)
        out = self.take_resume(name, self.bot.download(doc["file_id"]), self.conn)
        self.bot.send(self.chat_id, out.get("reply") or out.get("error"))

    def _text(self, text: str) -> None:
        # "typing…" for as long as the turn takes (15-40 s on GLM); Telegram
        # shows it for 5 s per call.
        done = threading.Event()

        def keep_typing():
            while not done.is_set():
                self.bot.typing(self.chat_id)
                done.wait(4)

        threading.Thread(target=keep_typing, daemon=True).start()
        try:
            answer = self.reply(text)
        finally:
            done.set()
        files: list[str] = []
        if isinstance(answer, tuple):
            answer, files = answer
        self.bot.send(self.chat_id, answer)
        if files:
            self.bot.send_files(self.chat_id, files)

    def push(self) -> None:
        """Finished or parked applications, sent without waiting to be asked."""
        for row in self.unreported(self.conn):
            text = self.news([row])
            shot = self.shots / f"app_{row['id']}.png"
            if shot.exists():
                self.bot.send_photo(self.chat_id, shot, text)
            else:
                self.bot.send(self.chat_id, text)


def work(user_id: int) -> None:
    """The worker process. The router set JOB_AGENT_HOME before starting it."""
    sys.path.insert(0, str(ROOT))
    import paths
    paths.ensure()
    import chat
    import chatlog
    import web
    from apply import worker as apply_worker
    from engine import db

    conn = db.connect()
    apply_worker.recover(conn)
    history: list = []
    # `channel` makes the chat send files instead of paths, and drop em dashes.
    state = {"picks": chat.load_last(), "resumes": {}, "fixture": None,
             "session": chatlog.new_session(), "channel": "telegram"}

    shown: set[str] = set()

    def on_step(ev: dict) -> None:
        """One short line when slow work starts, once per kind per message: a
        search is 15-40 s, and "typing…" alone that long reads as stuck."""
        name = ev.get("name")
        if ev.get("kind") == "tool" and name in PROGRESS and name not in shown:
            shown.add(name)
            me.bot.send(user_id, PROGRESS[name])

    def reply(text: str) -> tuple[str, list[str]]:
        nonlocal history
        shown.clear()
        answer, history = chat.reply(text, history, state, conn, on_step=on_step)
        return answer, list(state.get("outbox") or [])

    me = Worker(user_id, Bot(env("TELEGRAM_BOT_TOKEN")), conn, reply,
                web.take_resume, apply_worker.unreported, chat.news_lines,
                paths.SHOTS)

    inbox: queue.Queue = queue.Queue()

    def read_stdin():
        for line in sys.stdin:
            if line.strip():
                inbox.put(json.loads(line))
        inbox.put(None)                               # the router is gone

    threading.Thread(target=read_stdin, daemon=True).start()
    print(f"[telegram {user_id}] ready · {paths.label()}")
    while True:
        try:
            msg = inbox.get(timeout=PUSH_EVERY)
        except queue.Empty:
            msg = False
        if msg is None:
            break
        if msg:
            me.handle(msg)
        try:
            me.push()
        except Exception:                             # noqa: BLE001
            traceback.print_exc()


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "worker":
        work(int(sys.argv[2]))
    else:
        serve()
