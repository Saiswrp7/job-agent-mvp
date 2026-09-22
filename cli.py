#!/usr/bin/env python3
"""Job Agent MVP — terminal interface.

    python cli.py sync                  fill the jobs table (no API key needed)
    python cli.py verify                which boards are live
    python cli.py stats                 what's in the table
    python cli.py search "..."          5 jobs with reasons
    python cli.py parse-resume <pdf>    build master.json (run once)
    python cli.py tailor <n>            tailor for result n, preview as text
    python cli.py apply <n> [<n>...]    start applications
    python cli.py answer <id> "..."     resume a parked application
    python cli.py pending               applications waiting on you
    python cli.py vault [key value]     read or set a fact
    python cli.py log [-n 30]           what was said, and what ran underneath
    python cli.py eval [filters|rank|replay]  score the steps that have a right answer
    python cli.py reconcile [--dry]     the nightly memory pass
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from engine import db  # noqa: E402

LAST = HERE / "artifacts" / "last_search.json"


def _save_last(picks: list[dict]) -> None:
    LAST.parent.mkdir(parents=True, exist_ok=True)
    LAST.write_text(json.dumps(picks, indent=1, default=str))


def _load_last() -> list[dict]:
    if not LAST.exists():
        sys.exit("No previous search. Run `python cli.py search \"...\"` first.")
    return json.loads(LAST.read_text())


def _pick(n: int) -> dict:
    picks = _load_last()
    if not 1 <= n <= len(picks):
        sys.exit(f"No result {n} — the last search returned {len(picks)}.")
    return picks[n - 1]


# --------------------------------------------------------------------------


def cmd_sync(args):
    from engine.sync import sync
    r = asyncio.run(sync())
    print(f"\n{r['ok']}/{r['boards']} boards ok, {r['rows']} rows, "
          f"{r['closed']} closed")
    print(f"table: {r['stats']}")
    if r["errors"]:
        print("failures (these closed nothing):")
        for e in r["errors"]:
            print(f"  {e}")


def cmd_verify(args):
    from engine.sync import verify
    rows = asyncio.run(verify())
    live = [r for r in rows if r["live"]]
    for r in sorted(rows, key=lambda x: (not x["live"], x["company"])):
        mark = f"{r['jobs']:>4} jobs" if r["live"] else f"DEAD  {r['error']}"
        print(f"  {r['company']:<20} {r['source']:<11} {mark}")
    print(f"\n{len(live)}/{len(rows)} boards live")


def cmd_stats(args):
    conn = db.connect()
    print(db.stats(conn))
    print("\ntop cities:")
    for r in conn.execute("SELECT city, COUNT(*) n FROM jobs "
                          "WHERE closed_at IS NULL GROUP BY city "
                          "ORDER BY n DESC LIMIT 8"):
        print(f"  {r['n']:>4}  {r['city']}")
    conn.close()


def cmd_search(args):
    from search.run import search
    result = search(" ".join(args.query))
    _save_last(result["picks"])
    print(result["text"])
    print(f"\n({result['candidates']} candidates considered)")


def cmd_parse_resume(args):
    from resume import parse
    import vault as v
    master = parse.parse(Path(args.pdf))
    rows = parse.vault_rows(master)
    for k, val in rows.items():
        v.put(k, val, source="resume")
    # `apply` reads this key to attach the untailored resume; without it every
    # application starts with nothing attached.
    v.put("master_resume_path", str(Path(args.pdf).resolve()), source="resume")
    rows["master_resume_path"] = str(Path(args.pdf).resolve())
    print(f"wrote {parse.MASTER}")
    print(f"vault filled: {', '.join(rows)}")
    print("\nRead master.json and fix it by hand. Every future resume comes "
          "from that file, so an error there propagates forever.")


def cmd_tailor(args):
    from resume import tailor as t
    job = _pick(args.n)
    result = t.build(job)
    print(t.preview(result))
    print(f"\nPDF: {result['pdf']}")


def cmd_apply(args):
    from apply import agent
    from resume import tailor as t

    for n in args.n:
        job = _pick(n)
        resume_path = None
        if args.tailor:
            built = t.build(job)
            resume_path = str(built["pdf"])
            print(t.preview(built))
            if input("\nUse this resume? [y/N] ").strip().lower() != "y":
                print("skipped")
                continue
        else:
            import vault as v
            resume_path = v.get("master_resume_path")

        app_id = agent.create(job, resume_path)
        browser = agent.browser_for(job, app_id, fixture=args.fixture)
        print(f"\n[{app_id}] {job['title']} — {job['company']}")
        r = agent.start(app_id, job, browser, resume_path)
        print(f"  {r['status']}: {r.get('question') or r.get('message') or r.get('detail','')}")
        if r["status"] == "waiting":
            print(f"  answer with: python cli.py answer {app_id} \"...\"")


def cmd_answer(args):
    from apply import agent
    conn = db.connect()
    row = conn.execute("SELECT * FROM applications WHERE id=?",
                       (args.id,)).fetchone()
    if row is None:
        sys.exit(f"no application {args.id}")
    job = {"title": row["title"], "company": row["company"],
           "apply_url": row["apply_url"], "source": row["source"],
           "source_id": row["source_id"]}
    browser = agent.browser_for(job, args.id, fixture=args.fixture)
    r = agent.resume_run(args.id, args.answer, browser, conn)
    print(f"{r['status']}: {r.get('question') or r.get('message') or r.get('detail','')}")
    conn.close()


def cmd_pending(args):
    from apply import agent
    rows = agent.pending()
    if not rows:
        print("nothing waiting")
        return
    for r in rows:
        print(f"[{r['id']}] {r['title']} — {r['company']}\n    {r['question']}")


def cmd_vault(args):
    import vault as v
    if args.key and args.value:
        v.put(args.key, " ".join(args.value))
        print(f"{args.key} = {' '.join(args.value)}")
    elif args.key:
        print(v.get(args.key) or "(not set)")
    else:
        for k, val in v.all_keys().items():
            print(f"  {k:<20} {val}")


def cmd_log(args):
    """What was said, and what ran underneath it."""
    import chatlog

    conn = db.connect()
    try:
        if args.sessions:
            rows = chatlog.sessions(conn)
            if not rows:
                print("No chat sessions recorded yet.")
                return
            for r in rows:
                n = r["turns"] or 0
                print(f"{r['session']}  {n:>3} turn{'' if n == 1 else 's'}  "
                      f"{r['started'][:10]} "
                      f"{chatlog.local_clock(r['started'])[:5]} → "
                      f"{chatlog.local_clock(r['ended'])[:5]}")
            return

        rows = chatlog.turns(conn, session=args.session, limit=args.n)
        if not rows:
            print("No chat turns recorded yet. Start one with "
                  "`python cli.py chat` or `python cli.py web`.")
            return
        print(chatlog.render(rows))
    finally:
        conn.close()


def cmd_eval(args):
    """Replay a fixed set of sentences and score what came back."""
    from evals.run import main
    sys.exit(1 if main(args.which, args.n) else 0)


def cmd_reconcile(args):
    from memory import reconcile
    print(json.dumps(reconcile.reconcile(dry_run=args.dry), indent=1, default=str))


def cmd_chat(args):
    import chat
    chat.repl(fixture=args.fixture)


def cmd_web(args):
    """Same agent, same `chat.reply`, rendered in a browser instead."""
    import web
    web.main(fixture=args.fixture, port=args.port, open_browser=not args.no_open)


def cmd_doctor(args):
    """Probe every rail. A rail that blocks nothing is reported but does not
    fail the check, so a non-zero exit always means real work is blocked."""
    import shutil
    import llm

    blocked = False

    print("LLM providers")
    any_live = False
    for name in llm.PROVIDERS:
        ok, detail = llm.probe(name)
        any_live = any_live or ok
        print(f"  {'UP  ' if ok else 'DOWN'} {name:<12} {detail}")
    if not any_live:
        blocked = True
        print("  -> blocks: search, tailor, apply, reconcile, chat")

    print("\nBrowser")
    try:
        import playwright  # noqa: F401
        print("  UP   playwright")
    except ImportError:
        print("  DOWN playwright   pip install playwright && playwright install chromium")
        print("  -> blocks: apply against a live form (fixtures still work)")
        blocked = True

    print("\nResume")
    for tool in ("tectonic", "pdftotext"):
        up = shutil.which(tool)
        print(f"  {'UP  ' if up else 'DOWN'} {tool:<12} {up or 'not installed'}")
        if not up:
            blocked = True
    master = HERE / "resume" / "master.json"
    print(f"  {'UP  ' if master.exists() else 'DOWN'} master.json  "
          f"{'ok' if master.exists() else 'run parse-resume'}")

    print("\nData")
    conn = db.connect()
    s = db.stats(conn)
    ok = (s.get("open") or 0) > 0
    print(f"  {'UP  ' if ok else 'DOWN'} jobs table   {s}")
    if not ok:
        print("  -> run: python cli.py sync")
        blocked = True
    conn.close()

    print("\nSubmit guard")
    import os
    armed = os.environ.get("ALLOW_SUBMIT") == "1"
    print(f"  {'ARMED' if armed else 'SAFE '} ALLOW_SUBMIT="
          f"{os.environ.get('ALLOW_SUBMIT', '0')}"
          f"{'  — real applications WILL be submitted' if armed else ''}")

    sys.exit(1 if blocked else 0)


# --------------------------------------------------------------------------


def main():
    p = argparse.ArgumentParser(
        prog="cli.py", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("sync").set_defaults(fn=cmd_sync)
    sub.add_parser("verify").set_defaults(fn=cmd_verify)
    sub.add_parser("stats").set_defaults(fn=cmd_stats)
    sub.add_parser("pending").set_defaults(fn=cmd_pending)
    sub.add_parser("doctor").set_defaults(fn=cmd_doctor)

    s = sub.add_parser("chat", help="conversational mode")
    s.add_argument("--fixture", help="apply against a saved form instead of a live site")
    s.set_defaults(fn=cmd_chat)

    s = sub.add_parser("web", help="conversational mode, in a browser")
    s.add_argument("--fixture", help="apply against a saved form instead of a live site")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--no-open", action="store_true", help="do not open a browser")
    s.set_defaults(fn=cmd_web)

    s = sub.add_parser("search"); s.add_argument("query", nargs="+")
    s.set_defaults(fn=cmd_search)

    s = sub.add_parser("parse-resume"); s.add_argument("pdf")
    s.set_defaults(fn=cmd_parse_resume)

    s = sub.add_parser("tailor"); s.add_argument("n", type=int)
    s.set_defaults(fn=cmd_tailor)

    s = sub.add_parser("apply")
    s.add_argument("n", type=int, nargs="+")
    s.add_argument("--tailor", action="store_true",
                   help="build a tailored resume per job and ask before using it")
    s.add_argument("--fixture", help="run against a saved form instead of a live site")
    s.set_defaults(fn=cmd_apply)

    s = sub.add_parser("answer")
    s.add_argument("id", type=int); s.add_argument("answer")
    s.add_argument("--fixture")
    s.set_defaults(fn=cmd_answer)

    s = sub.add_parser("vault")
    s.add_argument("key", nargs="?"); s.add_argument("value", nargs="*")
    s.set_defaults(fn=cmd_vault)

    s = sub.add_parser("log", help="what was said, and what ran underneath")
    s.add_argument("-n", type=int, default=30, help="messages to show (default 30)")
    s.add_argument("--session", help="one session id; default is all, newest last")
    s.add_argument("--sessions", action="store_true", help="list sessions instead")
    s.set_defaults(fn=cmd_log)

    s = sub.add_parser("eval", help="replay fixed sentences and score them")
    s.add_argument("which", nargs="?", default="all",
                   choices=["all", "filters", "rank", "replay"])
    s.add_argument("-n", type=int, default=40,
                   help="how many past searches to replay (default 40)")
    s.set_defaults(fn=cmd_eval)

    s = sub.add_parser("reconcile")
    s.add_argument("--dry", action="store_true")
    s.set_defaults(fn=cmd_reconcile)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
