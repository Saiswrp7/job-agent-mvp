# Build Plan — MVP

Design: `fetch.md` (fetch + search), `plan.md` (overall architecture).
This is the order to build in, in one sitting.

## Architecture in one line

One cron, three functions, one agent, one chat layer.

| Piece | Type | Why |
|---|---|---|
| `sync()` | function | cron, no loop |
| `search()` | function | 2 calls: message → JSON → SQL → pick 5 |
| `tailor()` | function | 1 call: JD + resume JSON → patch |
| `remember()` | function | 1 call after each message → vault row |
| `apply()` | **agent** | loops, branches, parks for hours |

Only `apply()` loops, so only `apply()` gets tools and a harness.

## Files

```
engine/
  boards.json       company list, hand-written
  exclusions.json   kill titles + kill signals
  db.py             schema, upsert, close_missing
  adapters.py       lever, greenhouse
  sync.py           the hourly job
search/
  filters.py        message -> filter JSON      (LLM call 1)
  query.py          filter JSON -> SQL          (no LLM)
  rank.py           20 rows -> 5 with reasons   (LLM call 2)
resume/
  master.json       the source of truth
  parse.py          PDF -> master.json (run once)
  tailor.py         JD + master -> patch        (LLM call)
  verify.py         patch sanity gate           (no LLM)
  render.py         patch -> HTML -> PDF
  template.html     single column, ATS-safe
apply/
  browser.py        six functions wrapping Jev
  harness.py        loop, budget, log, park/resume, traces
  agent.py          the apply agent
prompts/
  filters.md  rank.md  apply.md  chat.md
vault.py            typed rows, retrieved by key
cli.py              sync | search | tailor | apply | verify
jobs.db
```

---

## P0 — Setup (15 min)

Folders, SQLite, `.env` with the API key. `cli.py` with empty subcommands.

**Done when:** `python cli.py --help` lists every command.

## P1 — Fetch engine (2 hr)

Build in this order: `db.py` → `adapters.py` → `boards.json` → `sync.py`.

- `UNIQUE(source, source_id)` with `ON CONFLICT DO UPDATE`
- one request per company, `?content=true` on Greenhouse
- concurrent, semaphore 8, `return_exceptions=True`
- **a failed board closes nothing**

`cli.py verify` hits every board and reports 404s, so `boards.json` is: write
50 guesses, run verify, fix or drop.

**Done when:** the table holds a few thousand open jobs and a second sync adds
roughly zero.

## P2 — Search (1.5 hr)

`query.py` first, with hand-written filter dicts and no LLM. Then `filters.py`
and `rank.py` on top.

- null fields drop out: `(:city IS NULL OR city = :city)`
- skills go in `ORDER BY`, never `WHERE`
- `LIMIT count * 4` — ask for 20 to return 5
- `exclusions.json` is applied in `rank.py`, not in SQL

**Done when:** `cli.py search "five growth roles in Bangalore in B2C companies"`
returns 5 real jobs with a reason line each.

## P3 — Apply rail (2 hr) — **this is the gate**

Six functions wrapping Jev. **No LLM in this phase.**

```
read_form() fill_field() upload_file() ask_user() submit() screenshot()
```

Feed them a hardcoded profile dict and fill real forms. Save every form's HTML
to `fixtures/`.

The agent must only ever see these six signatures. No `jev_task` call leaks
upward, or the whole thing is welded to this laptop.

`submit()` refuses unless `ALLOW_SUBMIT=1`, and that flag is never set in a test.

**Done when:** 5 real applications submitted by script across Greenhouse and
Lever.

> **If this phase runs long, stop and rescope the rest.** The overrun is
> information about the domain, not a reason to compress later phases.

## P4 — Harness (1.5 hr)

```python
while not done and steps < BUDGET:
    reply = model(system_prompt, tools, history)
    result = run_tool(reply.tool, reply.args)
    history.append(reply, result)
```

Around it: step budget (40), tool registry with JSON schemas, message log in
SQLite, traces of every prompt and result, and a throwing tool returning its
error as a tool result rather than killing the run.

**Pre-flight before the loop:** read the whole form, diff against the vault, ask
for every gap in one message. Without it the agent asks six questions over
twenty minutes.

**Parking:** `ask_user` saves the log and exits. `run(application_id)` reloads
and continues. Captchas park the same way.

**Context rule:** prune stale page snapshots. Keep the latest, drop the rest.

**Done when:** an agent completes a multi-step task and its full trace reads
cleanly.

## P5 — Resume (2 hr)

- `parse.py` once → `master.json`, then **read it and fix it by hand**
- `tailor.py` returns a patch, never a document
- `verify.py`: every number traces to the original bullet, companies/titles/
  dates byte-identical, no new proper nouns. A failing bullet reverts.
- `render.py`: template → `page.pdf()` with `emulate_media("print")`
- gate on `pdftotext`: every company, date and the contact line come back in
  order, or the render fails

Single column, no tables, no graphics, contact in the body not the header.

**Done when:** a tailored PDF passes the extraction gate.

## P6 — End to end (1 hr)

```
search → pick jobs → tailor → approve → apply → screenshot
```

Tailoring and approval happen **before** the apply loop starts. The apply agent
never sits on a half-filled form waiting for someone to like a bullet.

**Done when:** it passes twice in a row.

---

## Definition of done

> Ask for growth roles in Bangalore. Get 5 back with reasons. Pick three.
> Approve three tailored PDFs. Three applications submit. Three screenshots.

## Non-negotiable

- `submit()` refuses without the env flag. A careless afternoon otherwise fires
  real applications at real companies.
- Every filled value traces to a vault row. A value that doesn't was invented.
- Resumes rephrase, never invent.
- A failed fetch closes nothing.

## If today runs short

`P1 + P2 + P3` is already a real tool: it finds jobs and applies to them from
the terminal. Ship that and pick up at P4.

## Deferred

Workday, careers-page crawling, summaries at ingest, embeddings, multi-user,
Gmail OAuth, confirmation detection, proactive nudges.
