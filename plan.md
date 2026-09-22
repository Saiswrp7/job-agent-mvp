# Job Agent MVP

You ask it for jobs, it finds them, tailors a resume, and applies.

Terminal first. Chat surface later — it's a thin layer over the same code.

---

## 1. How it works

```
hourly cron ──→ jobs table ──→ search ──→ you
                                            ↓
                                 tailor ──→ apply ──→ submitted
                                            ↑
daily cron ──→ memory ──────────────────────┘
```

**The one rule everything follows from:** nothing touches the network while you
wait. The jobs are already downloaded. Search is a database query.

---

## 2. The five engines

### Fetch engine — fills the table
Every hour: ask 50 company boards for their jobs, normalize, upsert, mark
vanished ones closed. One request per company, descriptions included. No model
involved. → `fetch.md`

### Search engine — table to 5 jobs
Message → filter JSON → SQL returns 20 → LLM picks 5 with reasons.
SQL filters columns. The LLM judges what isn't a column (B2C, CRM-in-disguise).
→ `fetch.md`

### Resume engine — master to tailored PDF
Resume parsed once into structured JSON. Per job, one call returns a **patch**
(which bullets, reworded how), verified mechanically, rendered to PDF, gated on
`pdftotext`. Locked fields — dates, companies, titles — are never in the model's
output, so they cannot drift.

### Apply engine — one form, start to finish
Read the whole form, diff against the vault, ask for every gap in one message,
park, resume, fill, submit, screenshot. Jev drives the browser, behind six of our
own function signatures. → `AGENTS.md`

### Memory engine — what it learns
Facts you state save instantly. Facts it guessed wait for a nightly pass. A
separate cron agent is the only thing allowed to write. → `HARNESS.md`

---

## 3. Two agents, three functions, two crons

| | Type | Runs | Model calls |
|---|---|---|---|
| Chat agent | **agent** | per message | 1–3 |
| Apply agent | **agent** | per application | 10–40 |
| `search()` | function | on request | 2 |
| `tailor()` | function | on request | 1 |
| `remember()` | function | per message | 1 |
| `sync()` | cron | hourly | 0 |
| `reconcile()` | cron | daily | 1 |

**The test:** does it loop an unpredictable number of times? If no, it's a
function. Only the two agents loop, so only they get tools and a harness.

---

## 4. Structure

```
Job Agent MVP/
├── PLAN.md          this file
├── fetch.md         fetch + search design
├── AGENTS.md        prompts, tools, context per agent
├── HARNESS.md       the loop, caching, memory mechanics
├── BUILD.md         phase-by-phase build order
│
├── engine/          THE FETCH ENGINE
│   ├── boards.json      50 companies, tagged b2c/stage/industry
│   ├── exclusions.json  kill titles + kill signals (the CRM trap)
│   ├── db.py            schema, upsert, close_missing
│   ├── adapters.py      lever, greenhouse
│   └── sync.py          the hourly job
│
├── search/          THE SEARCH ENGINE
│   ├── filters.py       message → filter JSON     (LLM)
│   ├── query.py         filter JSON → SQL         (no LLM)
│   └── rank.py          20 rows → 5 + reasons     (LLM)
│
├── resume/          THE RESUME ENGINE
│   ├── master.json      source of truth
│   ├── parse.py         PDF → master.json (once)
│   ├── tailor.py        JD + master → patch       (LLM)
│   ├── verify.py        sanity gate               (no LLM)
│   ├── render.py        patch → HTML → PDF
│   └── template.html    single column, ATS-safe
│
├── apply/           THE APPLY ENGINE
│   ├── browser.py       six functions wrapping Jev
│   ├── harness.py       loop, budget, log, park/resume
│   └── agent.py         the apply agent
│
├── memory/          THE MEMORY ENGINE
│   ├── profile.md       prose, git-tracked
│   ├── platforms/       per-ATS lessons
│   └── reconcile.py     nightly, the only writer
│
├── prompts/
│   └── chat.md  filters.md  rank.md  tailor.md  apply.md
│
├── vault.py         typed rows, retrieved by key
├── cli.py           sync | search | tailor | apply | verify
└── jobs.db
```

---

## 5. Build order

Full detail in `BUILD.md`. Summary:

| | Phase | Needs a key | Done when |
|---|---|---|---|
| P0 | Setup | no | `cli.py --help` works |
| P1 | Fetch engine | no | table full, second sync adds ~0 |
| P2a | `query.py` | no | hand-written filters return rows |
| P2b | Search LLM calls | **yes** | plain-English query returns 5 jobs |
| P3 | Apply rail (Jev) | no | 5 forms filled, screenshots taken |
| P4 | Harness | **yes** | multi-step task completes, trace readable |
| P5 | Resume engine | **yes** | tailored PDF passes the extraction gate |
| P6 | End to end | **yes** | passes twice |

**P3 is the gate.** If it runs long, stop and rescope rather than compress P4
onward. The overrun is information about the domain.

P0, P1 and P2a need no API key and can start immediately.

---

## 6. Definition of done

> `cli.py search "5 growth jobs in Bangalore, B2C gaming, last 7 days"` returns
> 5 real jobs with reasons. `cli.py apply 1 2` asks once for what's missing,
> submits both, and returns two screenshots.

Twice in a row.

---

## 7. Non-negotiable

- **`submit()` refuses unless `ALLOW_SUBMIT=1`**, and that flag is never set in a
  test. Otherwise a careless afternoon fires real applications at real companies.
- **Every filled value traces to a vault row.** A value that doesn't was
  invented. Mechanically checkable in milliseconds.
- **Resumes rephrase, never invent.** Fabricated experience reaching a recruiter
  is permanent damage.
- **A failed fetch closes nothing.** A timeout must never read as "that company
  closed every role."
- **Tests never touch live data.** Fixtures only.

---

## 8. Cost

| | Per unit | At 10 searches/day |
|---|---|---|
| Fetch | free | $0 |
| Search | ~$0.08 | ~$25/mo |
| Resume | ~$0.01 | negligible |

All of it is the 20 descriptions in the ranking call. Levers if it grows:
cheaper model for ranking, then summaries cached at ingest.

---

## 9. Deferred

Chat surface (Telegram), Workday, Darwinbox, careers-page crawling, summaries at
ingest, embeddings, Gmail OAuth, confirmation detection, proactive nudges,
multi-user, billing.

All additive. None of it changes the shape above.

---

## 10. Known gap

Greenhouse + Lever is a US-startup assumption. Most Indian employers post to
Workday, Darwinbox, Keka or their own careers page. Fix by extending
`boards.json` and the adapter count, not by changing the design.

An agent that reads a careers page belongs in the hourly cron, where nobody is
waiting. Never in the query path.

---

## 11. Status — 2026-09-21

Built and tested (`python -m pytest tests/ -q` → **43 passed**, no key needed):

- fetch engine — 19/19 boards, **670 jobs**, idempotent
- SQL search, relaxation cascade, vault matching, pre-flight, submit guard
- resume invention guard, LaTeX render, ATS extraction gate
- `cli.py doctor` probes every rail and exits non-zero when work is blocked

Written but **never executed** — every one calls a model, and no key answers:
`search/filters.py`, `search/rank.py`, `resume/tailor.py`, `resume/parse.py`,
`apply/harness.py`, `memory/reconcile.py`, `chat.py`.

### Blockers

1. **No working LLM key.** Probed 2026-09-21: `ANTHROPIC_API_KEY` → 401,
   `OPENROUTER_API_KEY` → 401, no `GLM_API_KEY`. Jev works because its key
   lives in the Chrome extension, not the environment.
   `llm.py` takes any of the three, so one working key unblocks everything.
2. **`master.json` not built** — `parse-resume` needs a key.
3. **No real form fixtures** — `fixtures/greenhouse_sample.json` is invented.
   Jev can collect real ones without a key.
4. **Real submissions need explicit approval.** `ALLOW_SUBMIT` stays 0.

### Measured

137 candidate companies probed across Greenhouse, Lever and Ashby →
**20 live boards (15%)**. The India coverage gap is now a number, not a guess.
