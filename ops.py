"""Keeping the bot and the engine up, and restarting them without hurting anyone.

Two things went wrong on 2026-10-05:

- **Both were found dead.** They ran as plain background processes, so a Mac
  reboot ended them and nothing brought them back (the engine had been dead
  since 3 Oct). `install` hands both to launchd, which starts them at login and
  again whenever one exits.
- **A restart killed a send.** Sai said "submit" on Zeta a few seconds before a
  restart, and the run died half way. `busy` lists what a restart would cut
  off, and `restart` refuses while anything is on it unless forced.
"""

from __future__ import annotations

import os
import plistlib
import sqlite3
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
AGENTS = Path.home() / "Library" / "LaunchAgents"
LOGS = Path.home() / "Library" / "Logs" / "job-agent"
SERVICES = {"telegram": "com.jobagent.telegram", "engine": "com.jobagent.engine"}

#: Someone typed this recently: a restart now may land mid-turn.
RECENT_MESSAGE_S = 180
#: An approval card this fresh may be answered any second.
FRESH_CARD_S = 900


def busy(profiles: Path | None = None) -> list[str]:
    """What a bot restart would cut off right now, one line each. Empty = safe."""
    out = []
    for db_path in sorted((profiles or HERE / "profiles").glob("tg-*/jobs.db")):
        who = db_path.parent.name
        try:
            c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
            for app_id, company, status in c.execute(
                    "SELECT id, company, status FROM applications "
                    "WHERE status IN ('running', 'queued')"):
                out.append(f"{who}: application {app_id} ({company}) is {status}")
            for app_id, company in c.execute(
                    "SELECT id, company FROM applications WHERE status = 'waiting' "
                    "AND question LIKE 'READY TO SUBMIT%' AND updated_at >= "
                    f"datetime('now', '-{FRESH_CARD_S} seconds')"):
                out.append(f"{who}: application {app_id} ({company}) is waiting for "
                           f"their 'submit'")
            n = c.execute("SELECT COUNT(*) FROM messages WHERE role = 'user' AND "
                          f"created_at >= datetime('now', '-{RECENT_MESSAGE_S} seconds')"
                          ).fetchone()[0]
            if n:
                out.append(f"{who}: messaged in the last {RECENT_MESSAGE_S // 60} min")
            c.close()
        except sqlite3.Error as exc:
            out.append(f"{who}: could not check ({exc})")
    return out


def _plist(name: str) -> dict:
    LOGS.mkdir(parents=True, exist_ok=True)
    return {
        "Label": SERVICES[name],
        "ProgramArguments": [sys.executable, "-u", str(HERE / "cli.py"),
                             "telegram" if name == "telegram" else "engine",
                             *([] if name == "telegram" else ["run"])],
        "WorkingDirectory": str(HERE),
        "RunAtLoad": True,
        "KeepAlive": True,
        # A crash loop waits between starts instead of spinning.
        "ThrottleInterval": 30,
        "StandardOutPath": str(LOGS / f"{name}.log"),
        "StandardErrorPath": str(LOGS / f"{name}.log"),
        "EnvironmentVariables": {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                                 "PYTHONUNBUFFERED": "1"},
    }


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def _domain() -> str:
    return f"gui/{os.getuid()}"


def installed(name: str) -> bool:
    return _launchctl("print", f"{_domain()}/{SERVICES[name]}").returncode == 0


def install(name: str) -> str:
    """Write the launchd file and start it. Any copy started by hand must be
    stopped first, or two bots poll Telegram and both get "Conflict"."""
    path = AGENTS / f"{SERVICES[name]}.plist"
    AGENTS.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        plistlib.dump(_plist(name), f)
    if installed(name):
        _launchctl("bootout", f"{_domain()}/{SERVICES[name]}")
    r = _launchctl("bootstrap", _domain(), str(path))
    return f"{name}: {'started' if r.returncode == 0 else 'FAILED ' + r.stderr.strip()} ({path})"


def restart(name: str, force: bool = False) -> str:
    if name == "telegram" and not force:
        why = busy()
        if why:
            return "NOT restarted, it would cut off:\n  " + "\n  ".join(why)
    if not installed(name):
        return f"{name}: not installed as a service; run `cli.py service install` first"
    if name == "telegram":
        # Workers load code once; a left-over one would answer on old code.
        subprocess.run(["pkill", "-f", "telegram_bot.py worker"], capture_output=True)
    r = _launchctl("kickstart", "-k", f"{_domain()}/{SERVICES[name]}")
    return f"{name}: {'restarted' if r.returncode == 0 else 'FAILED ' + r.stderr.strip()}"
