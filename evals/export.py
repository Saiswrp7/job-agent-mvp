"""Every Telegram conversation as eval rows, for a sheet a person labels.

    python3 -m evals.export                 # all rows, JSON on stdout
    python3 -m evals.export --skip keys.txt # only rows whose key is not listed

One row per message a person sent, plus one per finished application (the
update the bot sent on its own). Columns:

    User ID | Time (IST) | User said | LLM reply | What happened behind | Expected outcome | key

"What happened behind" is written from what was recorded at the time — the
steps under each reply (`messages.meta`) and the apply run's own log
(`agent_logs`) — and left blank when nothing ran. `Expected outcome` is for
the person labelling; this never writes it. `key` is how a re-export adds new
rows without touching the labelled ones.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

IST = timezone(timedelta(hours=5, minutes=30))
HEADER = ["User ID", "Time (IST)", "User said", "LLM reply",
          "What happened behind", "Expected outcome", "key"]
#: A sheet cell holds 50k characters; a reply that long is unreadable anyway.
CELL = 4000


def when(ts: str | None) -> str:
    """Stored UTC (SQLite's clock); shown as the person's wall clock."""
    if not ts:
        return ""
    dt = datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return dt.astimezone(IST).strftime("%d %b %H:%M")


def _secs(ms: int | None) -> str:
    s = (ms or 0) / 1000
    return f"{s:.0f} s" if s < 90 else f"{s / 60:.1f} min"


def _first(text: str) -> str:
    line = next((l.strip() for l in (text or "").splitlines() if l.strip()), "")
    return line[:200]


def _error(result: str) -> str:
    """The reason, not the word ERROR: its second line when the first is a
    heading ("ATS extraction gate failed:")."""
    lines = [l.strip(" -") for l in (result or "").splitlines() if l.strip()]
    head = lines[0].removeprefix("ERROR:").strip() if lines else ""
    return f"{head} {lines[1]}" if head.endswith(":") and len(lines) > 1 else head


def step_line(s: dict) -> str:
    tool, args, ok = s.get("tool"), s.get("input") or {}, s.get("ok")
    took = _secs(s.get("ms"))
    if tool == "search_jobs":
        f = s.get("funnel") or {}
        if f:
            looked = ", ".join(f.get("looked_for") or [])
            relaxed = f"; loosened: {', '.join(f['relaxed'])}" if f.get("relaxed") else ""
            return (f'Searched "{args.get("query", "")}": {f.get("open", "?"):,} open → '
                    f'{f.get("matched", "?")} matched ({looked}) → '
                    f'{f.get("shown", "?")} shown{relaxed} · {took}')
        return f'Searched "{args.get("query", "")}" · {took}'
    extra = ", ".join(f"{k}: {v}" for k, v in args.items() if k != "ref")
    name = f"{tool}({extra})" if extra else tool
    if not ok:
        return f"{name}: FAILED, {_error(s.get('result', ''))} · {took}"
    if tool == "start_application":
        # The result also carries instructions to the model ("Tell them...");
        # the sheet wants only what happened.
        said = _first(s.get("result", "")).split(" Tell them")[0]
        return f"{said} · {took}"
    return f"{name}: ok · {took}"


def behind(meta: dict | None) -> str:
    steps = (meta or {}).get("steps") or []
    if not steps:
        return ""
    lines = [f"{i}. {step_line(s)}" if len(steps) > 1 else step_line(s)
             for i, s in enumerate(steps, 1)]
    lines.append(f"Whole turn: {_secs(meta.get('ms'))}, "
                 f"{meta.get('model_calls', '?')} AI calls")
    return "\n".join(lines)


def apply_behind(conn: sqlite3.Connection, app: dict) -> tuple[str, str]:
    """The run's own log, as steps a person can check. Returns (text, last ts)."""
    logs = conn.execute("SELECT kind, payload, created_at FROM agent_logs "
                        "WHERE app_id=? ORDER BY id", (app["id"],)).fetchall()
    if not logs:
        return "", app.get("updated_at") or ""
    out, calls, replay = [], [], ""
    for kind, payload, _ts in logs:
        if kind == "browser":
            replay = json.loads(payload).get("replay", "")
        elif kind == "tool_call":
            calls.append(json.loads(payload))
        elif kind == "tool_result" and calls:
            calls[-1]["result"] = payload
        elif kind == "park":
            out.append(f"Asked the person: {payload[:300]}")
    filled, other = [], []
    for c in calls:
        name, inp, res = c["name"], c.get("input") or {}, c.get("result", "")
        if name == "read_form":
            try:
                other.append(f"Read form: {len(json.loads(res))} fields")
            except Exception:                         # noqa: BLE001
                other.append("Read form")
        elif name == "fill_field":
            bad = res.startswith(("ERROR", "REFUSED"))
            picked = res.split("picked ", 1)[1].split(" from")[0] if "picked " in res else ""
            filled.append(f"{inp.get('name')} = {inp.get('value')}"
                          + (f" (picked {picked})" if picked else "")
                          + (f" — FAILED: {_first(res)}" if bad else ""))
        elif name == "upload_file":
            other.append(f"Upload: {_first(res)}")
        elif name == "submit":
            other.append("Submit: " + ("refused, ALLOW_SUBMIT is off"
                                       if "ALLOW_SUBMIT" in res else _first(res)))
    lines = [f"Application #{app['id']} · {app['company']} — {app['title']}"]
    lines += [o for o in other if o.startswith("Read form")]
    if filled:
        lines.append("Filled: " + "; ".join(filled))
    lines += [o for o in other if not o.startswith("Read form")] + out
    first, last = logs[0][2], logs[-1][2]
    took = (datetime.fromisoformat(last) - datetime.fromisoformat(first)).total_seconds()
    lines.append(f"Took {_secs(took * 1000)}, ended {app['status']}")
    if replay:
        lines.append(f"Recording: {replay}")
    return "\n".join(lines), last


def rows_for(profile: Path) -> list[list[str]]:
    import chat                                        # news_lines: what was pushed
    user = profile.name.removeprefix("tg-")
    conn = sqlite3.connect(profile / "jobs.db")
    conn.row_factory = sqlite3.Row
    out: list[tuple[str, list[str]]] = []
    msgs = [dict(r) for r in conn.execute("SELECT * FROM messages ORDER BY id")]
    by_turn: dict[tuple, dict] = {}
    for m in msgs:
        t = by_turn.setdefault((m["session"], m["turn"]), {})
        t[m["role"]] = m
    for (session, turn), t in by_turn.items():
        u, a = t.get("user"), t.get("assistant")
        if not u:
            continue
        meta = json.loads(a["meta"]) if a and a.get("meta") else None
        out.append((u["created_at"], [
            user, when(u["created_at"]), u["content"][:CELL],
            (a["content"] if a else "(no reply recorded)")[:CELL],
            behind(meta)[:CELL], "", f"{user}:{session}:{turn}"]))
    try:
        apps = [dict(r) for r in conn.execute(
            "SELECT * FROM applications WHERE status NOT IN ('queued', 'running') "
            "ORDER BY id")]
    except sqlite3.OperationalError:
        apps = []
    for app in apps:
        text, last = apply_behind(conn, app)
        if not text:
            continue
        out.append((last, [
            user, when(last), "(no message: the bot sent this on its own)",
            chat.news_lines([app])[:CELL], text[:CELL], "",
            f"{user}:app:{app['id']}:{app['status']}"]))
    conn.close()
    return [r for _, r in sorted(out, key=lambda x: x[0] or "")]


def all_rows(root: Path = ROOT / "profiles") -> list[list[str]]:
    rows = []
    for p in sorted(root.glob("tg-*")):
        if (p / "jobs.db").exists():
            rows += rows_for(p)
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip", help="file of keys already in the sheet, one per line")
    args = ap.parse_args()
    skip = set()
    if args.skip:
        skip = {l.strip() for l in Path(args.skip).read_text().splitlines() if l.strip()}
    rows = [r for r in all_rows() if r[-1] not in skip]
    json.dump(rows, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
