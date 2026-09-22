"""The nightly pass. The only thing allowed to write inferred facts.

The split this file implements (chat reads, a background job writes) is taken
from Instinct's memory architecture, but the reason it matters here is specific:
**the vault gets typed into real applications.**

If the chat agent could write it live, one misread sentence — "the Swiggy job
says 30 LPA" becoming `expected_ctc = 30 LPA` — ends up on a submitted form
that cannot be retracted.

Facts the user *states* do not come through here at all. Those go straight to
the vault via `vault.put()` and are echoed back in the same breath, which is a
better guard than any delay. This pass exists only for what the model *guessed*.
"""

from __future__ import annotations

import json
import subprocess
from datetime import date
from pathlib import Path

import llm
import vault
from engine import db

HERE = Path(__file__).resolve().parent
PROFILE = HERE / "profile.md"
PLATFORMS = HERE / "platforms"

SYSTEM = """You maintain a job-seeker's profile. Below are facts a model
inferred from conversation today, plus what is already known.

Return ONLY JSON:

{
  "vault": {"key": "value"},
  "profile_additions": ["a durable preference, in one line"],
  "corrections": [{"key": "...", "old": "...", "new": "...", "why": "..."}],
  "discard": ["a staged fact not worth keeping, with no explanation needed"]
}

Rules:

- **Promote to `vault` only what is unambiguous and durable.** A vault value
  gets typed into real job applications, so a guess is worse than a gap.
  "Mentioned 30 LPA while comparing two job postings" is NOT an expected
  salary — discard it. "My expected CTC is 30 LPA" is.
- **`profile_additions`** are soft preferences: what they like, what they
  avoid, how they talk about their work. Prose is fine here.
- **Generalise repeated examples into a trait.** Three separate remarks about
  small companies become one line: "prefers sub-200-person companies".
- **`corrections`** only when a new fact genuinely contradicts a known one.
  Say why.
- **Discard anything ambiguous.** A fact not saved today will be said again if
  it matters. A wrong fact on a submitted application will not be undone.
- Never delete a known fact unless it was explicitly contradicted."""


def staged(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT id, fact, context, created_at FROM staging "
        "WHERE promoted = 0 ORDER BY id")]


def read_profile() -> str:
    return PROFILE.read_text() if PROFILE.exists() else ""


def write_profile(lines: list[str]) -> None:
    PROFILE.parent.mkdir(parents=True, exist_ok=True)
    body = read_profile()
    stamp = f"\n## {date.today().isoformat()}\n"
    body += stamp + "".join(f"- {line}\n" for line in lines)
    PROFILE.write_text(body)


def git_commit(message: str) -> bool:
    """Git history is the undo. The reconciler rewrites the profile while the
    user sleeps; without a diff they would have no idea what changed."""
    try:
        subprocess.run(["git", "-C", str(HERE.parent), "add",
                        str(PROFILE.relative_to(HERE.parent)),
                        str(PLATFORMS.relative_to(HERE.parent))],
                       capture_output=True, check=False)
        r = subprocess.run(["git", "-C", str(HERE.parent), "commit", "-m", message],
                           capture_output=True, text=True)
        return r.returncode == 0
    except Exception:                                 # noqa: BLE001
        return False


def reconcile(dry_run: bool = False) -> dict:
    conn = db.connect()
    try:
        rows = staged(conn)
        if not rows:
            return {"staged": 0, "promoted": 0, "profile": 0, "discarded": 0}

        user = json.dumps({
            "staged_today": [{"fact": r["fact"], "context": r["context"]}
                             for r in rows],
            "known_vault": vault.all_keys(conn),
            "known_profile": read_profile()[-3000:],
        }, indent=1)

        out = llm.complete_json(SYSTEM, user, max_tokens=4096)

        report = {"staged": len(rows), "promoted": 0, "profile": 0,
                  "discarded": len(out.get("discard", [])),
                  "corrections": out.get("corrections", [])}

        if dry_run:
            report["proposed"] = out
            return report

        for key, value in (out.get("vault") or {}).items():
            vault.put(key, value, source="reconciler", conn=conn)
            report["promoted"] += 1

        additions = out.get("profile_additions") or []
        for c in out.get("corrections") or []:
            additions.append(
                f"corrected {c.get('key')}: {c.get('old')} → {c.get('new')} "
                f"({c.get('why')})"
            )
        if additions:
            write_profile(additions)
            report["profile"] = len(additions)

        conn.execute("UPDATE staging SET promoted = 1 WHERE promoted = 0")
        conn.commit()
        report["committed"] = git_commit(
            f"reconcile {date.today().isoformat()}: "
            f"{report['promoted']} vault, {report['profile']} profile"
        )
        return report
    finally:
        conn.close()


def note_platform(ats: str, lesson: str) -> None:
    """A lesson from a failed application.

    Keyed to the platform, not the person — ten ATS platforms cover most
    applications, so this is the only memory here that gets more valuable the
    more you apply.
    """
    PLATFORMS.mkdir(parents=True, exist_ok=True)
    path = PLATFORMS / f"{ats}.md"
    existing = path.read_text() if path.exists() else f"# {ats}\n\n"
    if lesson in existing:
        return
    path.write_text(existing + f"- {lesson}  ({date.today().isoformat()})\n")


def platform_notes(ats: str) -> str:
    path = PLATFORMS / f"{ats}.md"
    return path.read_text() if path.exists() else ""
