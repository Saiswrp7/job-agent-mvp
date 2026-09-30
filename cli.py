#!/usr/bin/env python3
"""Job Agent MVP — terminal interface.

    python cli.py sync                  fill the jobs table: boards + LinkedIn (no API key needed)
    python cli.py verify                which boards are live
    python cli.py stats                 what's in the table
    python cli.py supply                usable jobs per realistic search
    python cli.py discover [--add]      find company boards from known employers
    python cli.py search "..."          5 jobs with reasons
    python cli.py parse-resume <pdf>    build master.json (run once)
    python cli.py resume                build a resume from everything you've said
    python cli.py career [kind text]    read or add a career fact
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

import paths  # noqa: E402
from engine import db  # noqa: E402

LAST = paths.LAST_SEARCH


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
    r = asyncio.run(sync(with_linkedin=not (args.boards_only or args.no_linkedin),
                         linkedin_cap=args.linkedin_cap,
                         with_adzuna=not (args.boards_only or args.no_adzuna),
                         adzuna_calls=args.adzuna_calls,
                         with_labels=not args.no_labels))
    print(f"\n{r['ok']}/{r['boards']} boards ok, {r['rows']} rows, "
          f"{r['closed']} closed")
    ru = r.get("rules")
    if ru:
        print(f"Rule labels (no model) on {ru['read']} unread jobs: "
              f"{ru['role_family']} role, {ru['level']} level, "
              f"{ru['country']} country, {ru['work_mode']} work mode")
    lab = r.get("labels")
    if lab:
        print(f"Labels: {lab['labelled']} jobs labelled"
              + (f", stopped early ({lab['stopped']})" if lab["stopped"] else ""))
    li = r.get("linkedin")
    if li:
        print(f"LinkedIn: {li.get('queued', 0)} new links from "
              f"{li.get('searches', 0)} searches, {li.get('added', 0)} jobs added, "
              f"{li['waiting']} still waiting for a description, "
              f"{li['closed']} closed by age")
        if li["blocked"]:
            print(f"  LinkedIn stopped early ({li['blocked']}). Kept what it "
                  f"had, closed nothing because of it. Try again later.")
    az = r.get("adzuna")
    if az and az.get("skipped"):
        print(f"Adzuna: skipped ({az['skipped']})")
    elif az:
        print(f"Adzuna: {az['added']} jobs added from {az['calls']} calls, "
              f"{az['repeats']} repeats read, {az['skipped_pages']} pages "
              f"skipped by the page search, "
              f"{az['duplicates']} already had from a fuller source, "
              f"{az['closed']} closed by age, {az['superseded']} replaced by a "
              f"fuller copy")
        if az["blocked"]:
            print(f"  Adzuna stopped early ({az['blocked']}). Kept what it "
                  f"had, closed nothing because of it.")
        if az.get("narrowed"):
            print(f"  {az['narrowed']} window(s) narrowed a day: where the last "
                  f"run stopped was past Adzuna's 100-page limit")
        if az.get("stuck"):
            print(f"  could not reach where these stopped, even one day wide "
                  f"(split them): {', '.join(az['stuck'])}")
    print(f"table: {r['stats']}")
    if r["errors"]:
        print("failures (these closed nothing):")
        for e in r["errors"]:
            print(f"  {e}")


def cmd_label(args):
    import time
    from engine import labels
    conn = db.connect()
    if args.redo:
        conn.execute("UPDATE jobs SET label_hash = NULL")
        conn.commit()
    print(f"labelling with {labels.provider()} / {labels.model()}")
    t = time.time()
    r = labels.run(conn, limit=args.limit)
    done, total = labels.coverage(conn)
    print(f"\n{r['labelled']} labelled, {r['skipped']} skipped, "
          f"{r['failed_batches']} failed batches in {time.time() - t:.0f}s")
    if r["stopped"]:
        print(f"stopped early: {r['stopped']}")
    print(f"coverage: {done}/{total} open jobs labelled")
    conn.close()


def cmd_verify(args):
    from engine.sync import verify
    rows = asyncio.run(verify())
    live = [r for r in rows if r["live"]]
    for r in sorted(rows, key=lambda x: (not x["live"], x["company"])):
        mark = f"{r['jobs']:>4} jobs" if r["live"] else f"DEAD  {r['error']}"
        print(f"  {r['company']:<20} {r['source']:<11} {mark}")
    print(f"\n{len(live)}/{len(rows)} boards live")


def cmd_discover(args):
    """Guess board slugs for every employer Adzuna and LinkedIn named, ask each
    platform, and (with --add) put the live Indian ones in boards.json."""
    from engine import discover
    from engine.sync import BOARDS
    boards = json.loads(BOARDS.read_text())
    if args.add:
        _add_discovered(discover.live(discover.OUT), boards, args.min_india,
                        set(args.skip or []))
        return
    have = {b["company"].lower() for b in boards}
    if args.names_file:
        # A hand-typed list: employers the table has not named yet.
        lines = Path(args.names_file).read_text().splitlines()
        names = list(dict.fromkeys(
            n.strip() for n in lines
            if n.strip() and not n.startswith("#") and n.strip().lower() not in have))
    else:
        conn = db.connect()
        names = discover.companies(conn, have)
        conn.close()
    if args.limit:
        names = names[:args.limit]
    print(f"probing {len(names)} companies on "
          f"{', '.join(args.platforms or discover.PROBES)}")
    r = asyncio.run(discover.run(names, discover.OUT, platforms=args.platforms))
    for platform, s in r.items():
        print(f"  {platform:<16} {s['probed']:>5} probed  {s['live']:>4} live"
              + (f"  stopped: {s['stopped']}" if s["stopped"] else ""))
    print(f"\nanswers in {discover.OUT}; add them with: "
          f"python cli.py discover --add")


def _add_discovered(found: list[dict], boards: list[dict], min_india: int,
                    skip: set[str]):
    """Append live boards with Indian roles: one board per company (the one
    with the most Indian roles, since `found` is sorted that way), only where
    the board's own name (when it gives one) matches ours, never an
    aggregator, and never a slug named in --skip."""
    from engine import discover
    from engine.sync import BOARDS
    have = {(b["source"], b["slug"].lower()) for b in boards}
    companies = {b["company"].lower() for b in boards}
    added = []
    for r in found:
        key = (r["platform"], r["slug"].lower())
        if (r.get("india") or 0) < min_india or key in have \
                or r["company"].lower() in companies \
                or r["slug"].lower() in skip | discover.NOT_EMPLOYERS:
            continue
        if not discover.same_company(r["company"], r.get("name")):
            continue
        boards.append({"company": r["company"], "source": r["platform"],
                       "slug": r["slug"], "found_by": "discover"})
        have.add(key)
        companies.add(r["company"].lower())
        added.append(r)
    if added:
        # Appended as lines, one board each, so the hand-typed ones above keep
        # their layout and a diff shows exactly what discovery added.
        text = BOARDS.read_text().rstrip()
        assert text.endswith("]")
        lines = ",\n".join("  " + json.dumps(boards[-len(added) + i], ensure_ascii=False)
                           for i in range(len(added)))
        BOARDS.write_text(text[:-1].rstrip() + ",\n\n" + lines + "\n]\n")
        json.loads(BOARDS.read_text())          # still valid, or fail loudly
    for r in added:
        print(f"  + {r['company']:<32} {r['platform']:<16} {r['slug']:<24} "
              f"{r['jobs']:>4} jobs, {r.get('india', 0)} in India")
    print(f"\n{len(added)} boards added to {BOARDS.name}")


def cmd_supply(args):
    """Jobs the product can fully handle (full text + employer apply link,
    open and listed now, last 30 days) for the searches beta users will
    make, split by what applying takes. The gate is 15 forms we can fill."""
    from engine import supply
    conn = db.connect()
    short = 0
    print(f"  {'':5} {'form':>5} {'account':>8}")
    for role, place, form, account in supply.report(conn, args.days):
        mark = "ok  " if form >= supply.GATE else "SHORT"
        short += form < supply.GATE
        print(f"  {mark} {form:>5} {account:>8}  {role} @ {place}")
    conn.close()
    print(f"\n{short} search(es) under {supply.GATE} forms"
          f"\n(form: we fill it; account: their site wants a sign-up first)")


def cmd_engine(args):
    """The always-on engine, or where it stands."""
    from engine import loop
    if args.action == "status":
        conn = db.connect()
        s = loop.status(conn)
        conn.close()
        print(f"boards {s['boards']}  due now {s['due_now']}  never read "
              f"{s['never_read']}  failing {s['failing']}  retired {s['retired']}")
        print(f"last 24 h: {s['reads_24h']} reads, {s['ok_24h'] or 0} ok, "
              f"{s['new_24h'] or 0} new jobs, {s['closed_24h'] or 0} closed")
        print(f"searchable now: {s['visible']}  by apply kind: {s['by_kind']}")
        for f in s["failing_boards"]:
            print(f"  failing x{f['fail_streak']}: {f['source']:<15} "
                  f"{f['company'][:28]:<28} {(f['last_error'] or '')[:70]}")
        return
    totals = asyncio.run(loop.run(once=args.action == "once", batch=args.batch))
    print(f"\n{totals}")


def cmd_registry(args):
    """The company list: pull the dataset's India employers, add them."""
    from engine import registry
    if args.action == "pull":
        r = registry.pull()
        print(f"{r['employers']} employers with India jobs in the dataset; "
              f"{r['boards']} boards matched to a careers site, "
              f"{r['unmatched']} not matched, {r['staffing']} staffing firms "
              f"left out -> {registry.DATASET_BOARDS.name}")
        return
    conn = db.connect()
    if args.action == "seed":
        print(registry.sync_json(conn))
        print(registry.seed(conn))
    for r in registry.counts(conn):
        print(f"  {r['source']:<16} {r['boards']:>5} boards  {r['read_ok'] or 0:>5} "
              f"read ok  {r['failing'] or 0:>4} failing  {r['retired'] or 0:>3} retired")
    conn.close()


def cmd_stats(args):
    conn = db.connect()
    print(db.stats(conn))
    print("\nby source:")
    for r in db.stats_by_source(conn):
        print(f"  {r['source']:<11} {r['open']:>5} open  {r['companies']:>4} companies")
    print("\ntop cities:")
    for r in conn.execute("SELECT city, COUNT(*) n FROM jobs "
                          "WHERE closed_at IS NULL GROUP BY city "
                          "ORDER BY n DESC LIMIT 8"):
        print(f"  {r['n']:>4}  {r['city']}")
    conn.close()


def cmd_cost(args):
    """What the AI cost, per person and per task, from the tokens each call
    saved (usage.py). Every profile is counted, evals and your own included,
    because they all bill the same GLM account: the total is the number to
    hold against the bigmodel.cn billing page for the same days."""
    import usage
    import paths as p_
    dbs = [("you (terminal, web, labels)", p_.SHARED_DB)]
    for f in sorted((p_.ROOT / "profiles").rglob("jobs.db")):
        name = f.parent.relative_to(p_.ROOT / "profiles").as_posix()
        kind = ("Telegram user " + name[3:] if name.startswith("tg-") else
                "eval" if name.startswith("eval") else
                "scenario" if name.startswith("scen") else "test")
        dbs.append((f"{name} ({kind})" if not name.startswith("tg-") else kind, f))
    head = (f"{'who':<34}{'calls':>6}{'new in':>10}{'cached':>10}{'out':>9}"
            f"{'$':>9}{'₹':>8}")
    days = None if args.all else args.days
    print(f"AI cost, {'all time' if days is None else f'last {days} days'} "
          f"(prices in usage.py, ₹{usage.RUPEES:g} per $)\n{head}")
    total = {"calls": 0, "input": 0, "cached": 0, "output": 0, "usd": 0.0}
    tasks: dict[str, float] = {}
    for who, db_ in dbs:
        rows = usage.summary(db_, days)
        if not rows:
            continue
        t = {k: sum(r[k] for r in rows) for k in total}
        for k in total:
            total[k] += t[k]
        for r in rows:
            tasks[r["purpose"]] = tasks.get(r["purpose"], 0) + r["usd"]
        print(f"{who[:33]:<34}{t['calls']:>6}{t['input']:>10,}{t['cached']:>10,}"
              f"{t['output']:>9,}{t['usd']:>9.3f}{t['usd'] * usage.RUPEES:>8.1f}")
        if args.detail:
            for r in rows:
                print(f"   {r['purpose'][:19]:<20}{r['model'][:11]:<11}{r['calls']:>6}"
                      f"{r['input']:>10,}{r['cached']:>10,}{r['output']:>9,}"
                      f"{r['usd']:>9.3f}")
    print(f"{'TOTAL':<34}{total['calls']:>6}{total['input']:>10,}"
          f"{total['cached']:>10,}{total['output']:>9,}{total['usd']:>9.3f}"
          f"{total['usd'] * usage.RUPEES:>8.1f}")
    if total["usd"]:
        split = " · ".join(f"{k} {v / total['usd']:.0%}" for k, v in
                           sorted(tasks.items(), key=lambda kv: -kv[1]))
        print(f"\nby task: {split}")
    print("\nHold the TOTAL against the bigmodel.cn billing page for the same "
          "days. If they differ, fix PRICES in usage.py.")


def cmd_search(args):
    from search.run import experience_years, search
    conn = db.connect()
    result = search(" ".join(args.query), conn=conn,
                    fit_years=experience_years(conn), live=True)
    conn.close()
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
    # The uploaded document goes on the record too. Without it the store
    # starts empty, and the first generated resume would be everything they
    # mentioned in chat and nothing they actually did.
    import career
    n = career.seed_from_master(master)
    print(f"wrote {parse.MASTER}")
    print(f"vault filled: {', '.join(rows)}")
    print(f"career store seeded: {n} claims from the resume")
    print("\nRead master.json and fix it by hand. Every future resume comes "
          "from that file, so an error there propagates forever.")


def cmd_resume(args):
    """A resume with no job attached, from the career store."""
    from resume import generate, tailor as t
    result = generate.build(out_name=args.out)
    print(t.preview(result))
    print(f"\nPDF: {result['pdf']}")


def cmd_career(args):
    import career
    if args.kind and args.text:
        added = career.add(args.kind, " ".join(args.text), company=args.company,
                           source="user")
        print("added" if added else "already there")
        return
    rows = career.all(kind=args.kind)
    if not rows:
        print("nothing on the record yet — run parse-resume, or add one")
        return
    for r in rows:
        where = f" [{r['company']}]" if r["company"] else ""
        print(f"{r['id']:>4} {r['kind']:<14}{where} {r['text'][:80]}")
        if r["heard_in"]:
            print(f"      heard: {r['heard_in'][:72]!r}")
    print(f"\n{len(rows)} claim(s)")


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


def cmd_telegram(args):
    """The chat on Telegram, one process per invited person (telegram_bot.py)."""
    import telegram_bot
    telegram_bot.serve()


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

    # First line, before any rail: every check below is about *this* profile,
    # and a green report against the wrong one is worse than a red one.
    print(f"Profile\n  {paths.label()}\n")

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
    from apply.jev import Bridge, JEV_DIR
    if Bridge._healthy():
        print("  UP   jev          Chrome extension connected")
    elif (JEV_DIR / "mcp" / "server.mjs").exists():
        print("  DOWN jev          open Chrome and click the Jev icon "
              "(the bridge starts itself when applying)")
    else:
        print(f"  DOWN jev          Jev Browser Control not found at {JEV_DIR}")
        print("  -> blocks: apply against a live form (fixtures still work)")
        blocked = True
    import os
    from apply import cloud
    from apply.agent import BROWSER_ENV
    ok, detail = cloud.probe()
    print(f"  {'UP  ' if ok else 'DOWN'} cloud        {detail}")
    in_use = os.environ.get(BROWSER_ENV, "jev").lower()
    print(f"       in use:      {in_use}  (set {BROWSER_ENV}=cloud|jev)")
    if in_use == "cloud" and not ok:
        print("  -> blocks: apply (the cloud browser is the one in use)")
        blocked = True

    print("\nResume")
    for tool in ("tectonic", "pdftotext"):
        up = shutil.which(tool)
        print(f"  {'UP  ' if up else 'DOWN'} {tool:<12} {up or 'not installed'}")
        if not up:
            blocked = True
    # paths.MASTER, not HERE — doctor has to ask the profile the question, or
    # it reports a resume that this profile cannot actually read.
    master = paths.MASTER
    print(f"  {'UP  ' if master.exists() else 'DOWN'} master.json  "
          f"{'ok' if master.exists() else 'run parse-resume'}")

    print("\nLinkedIn (job supply, optional)")
    from engine import linkedin
    ok, detail = asyncio.run(linkedin.probe())
    print(f"  {'UP  ' if ok else 'DOWN'} guest search  {detail}")
    if not ok:
        # Boards still work, so this narrows supply without blocking anything.
        print("  -> sync still runs the boards; LinkedIn jobs just stop growing")

    # No network call: the free tier is 250 calls a day, and a probe that
    # spends one every time someone runs doctor spends them on nothing.
    print("\nAdzuna (job supply, optional)")
    from engine import adzuna
    if adzuna.keys():
        print("  UP   keys         ADZUNA_APP_ID / ADZUNA_APP_KEY set (not probed)")
    else:
        print("  DOWN keys         set ADZUNA_APP_ID and ADZUNA_APP_KEY "
              "(free at developer.adzuna.com)")
        print("  -> sync skips Adzuna; boards and LinkedIn still run")

    print("\nData")
    conn = db.connect()
    from engine import labels
    done, total = labels.coverage(conn)
    print(f"  {'UP  ' if done else 'DOWN'} labels       {done}/{total} open jobs "
          f"labelled via {labels.provider()}"
          + ("" if done == total else "  -> python cli.py label"))
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

    s = sub.add_parser("sync", help="fill the jobs table: boards, then LinkedIn")
    s.add_argument("--boards-only", action="store_true",
                   help="skip LinkedIn (seconds instead of minutes)")
    s.add_argument("--linkedin-cap", type=int, default=None,
                   help="descriptions to fetch this run (default 300)")
    s.add_argument("--no-labels", action="store_true",
                   help="skip labelling new jobs")
    s.add_argument("--no-adzuna", action="store_true",
                   help="skip Adzuna (needs ADZUNA_APP_ID / ADZUNA_APP_KEY)")
    s.add_argument("--no-linkedin", action="store_true",
                   help="skip LinkedIn but still run boards and Adzuna "
                        "(for when LinkedIn is answering 429)")
    s.add_argument("--adzuna-calls", type=int, default=None,
                   help="most Adzuna API calls this run; also runs it even if it ran today (free tier: 2,500 a month)")
    s.set_defaults(fn=cmd_sync)

    s = sub.add_parser("label", help="label jobs that have none (cheap model)")
    s.add_argument("--limit", type=int, default=None)
    s.add_argument("--redo", action="store_true",
                   help="relabel every job, e.g. after changing the model")
    s.set_defaults(fn=cmd_label)
    sub.add_parser("verify").set_defaults(fn=cmd_verify)
    sub.add_parser("stats").set_defaults(fn=cmd_stats)
    s = sub.add_parser("cost", help="what the AI cost, per person and task")
    s.add_argument("--days", type=int, default=30)
    s.add_argument("--all", action="store_true", help="all time")
    s.add_argument("--detail", action="store_true", help="split by task and model")
    s.set_defaults(fn=cmd_cost)
    s = sub.add_parser("supply", help="usable jobs per realistic search")
    s.add_argument("--days", type=int, default=30)
    s.set_defaults(fn=cmd_supply)
    s = sub.add_parser("engine", help="re-read every board: run | once | status")
    s.add_argument("action", nargs="?", default="run", choices=["run", "once", "status"],
                   help="run: forever; once: until nothing is due; status: where it stands")
    s.add_argument("--batch", type=int, default=5000, help="most boards picked per round")
    s.set_defaults(fn=cmd_engine)
    s = sub.add_parser("registry", help="the company list: pull | seed | show")
    s.add_argument("action", nargs="?", default="show", choices=["pull", "seed", "show"],
                   help="pull: dataset -> engine/dataset_boards.csv (network); "
                        "seed: boards.json + that csv -> boards table")
    s.set_defaults(fn=cmd_registry)

    s = sub.add_parser("discover", help="find company boards from known employers")
    s.add_argument("--platforms", nargs="+", default=None,
                   help="greenhouse lever ashby workable smartrecruiters")
    s.add_argument("--limit", type=int, default=None, help="first n companies only")
    s.add_argument("--names-file", default=None,
                   help="probe these names (one per line) instead of the table's")
    s.add_argument("--add", action="store_true",
                   help="append live boards with Indian roles to boards.json")
    s.add_argument("--min-india", type=int, default=1,
                   help="Indian roles a board needs to be added (default 1)")
    s.add_argument("--skip", nargs="+", default=None,
                   help="slugs to leave out (a generic word that is not this company)")
    s.set_defaults(fn=cmd_discover)
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

    s = sub.add_parser("telegram", help="conversational mode, on Telegram (invited ids only)")
    s.set_defaults(fn=cmd_telegram)

    s = sub.add_parser("search"); s.add_argument("query", nargs="+")
    s.set_defaults(fn=cmd_search)

    s = sub.add_parser("parse-resume"); s.add_argument("pdf")
    s.set_defaults(fn=cmd_parse_resume)

    s = sub.add_parser("resume", help="build a resume from the career store")
    s.add_argument("--out", default=None)
    s.set_defaults(fn=cmd_resume)

    s = sub.add_parser("career", help="read or add a career fact")
    s.add_argument("kind", nargs="?", default=None)
    s.add_argument("text", nargs="*")
    s.add_argument("--company", default=None)
    s.set_defaults(fn=cmd_career)

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
