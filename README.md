# Job Agent MVP

Finds jobs, tailors a resume, applies. Terminal.

```bash
python cli.py doctor        # what's up, what's blocked
python cli.py sync          # fill the jobs table (no API key needed)
python cli.py search "5 growth jobs in Bangalore, B2C, last 30 days"
python cli.py apply 1 2
python cli.py chat          # conversational mode
python cli.py log           # what was said, and what ran underneath it
```

## Setting up

```bash
pip install httpx anthropic websocket-client pytest
# Applying drives your own Chrome through Jev Browser Control:
# load ../jev-browser-control/extension unpacked in Chrome and keep it on.
brew install tectonic poppler          # LaTeX + pdftotext

export ANTHROPIC_API_KEY=sk-ant-...    # or GLM_API_KEY, or OPENROUTER_API_KEY
python cli.py sync
python cli.py parse-resume "../Interview Prep/Sai Swaroop Product - Resume.pdf"
python cli.py doctor                    # should exit 0
```

`doctor` probes every rail for real, including calling each configured LLM
provider. An exported key that 401s looks exactly like a working one until
something calls it, which is why this exists.

## Commands

| | |
|---|---|
| `doctor` | probe every rail; non-zero exit means real work is blocked |
| `sync` | poll all boards, then LinkedIn (`--boards-only` to skip it) |
| `label [--limit n]` | label jobs that have none (cheap model, runs inside `sync` too) |
| `verify` | which board slugs are live |
| `discover [--add]` | find boards for employers already in the table |
| `supply` | usable jobs per realistic search (gate 15) |
| `stats` | what's in the table |
| `search "..."` | 5 jobs with reasons |
| `parse-resume <pdf>` | build `resume/master.json` (once) |
| `tailor <n>` | tailor for result n, preview as text |
| `apply <n>...` | start applications (`--tailor`, `--fixture`) |
| `answer <id> "..."` | resume a parked application |
| `pending` | applications waiting on you |
| `vault [key value]` | read or set a fact |
| `reconcile [--dry]` | the nightly memory pass |
| `chat` | conversational mode |

## How it fits together

```
hourly cron ──→ jobs table ──→ search ──→ you
                                            ↓
                                 tailor ──→ apply ──→ submitted
daily cron ──→ memory ──────────────────────┘
```

**Nothing touches the network while you wait.** The jobs are already
downloaded; search is a database query.

Two agents (`chat.py`, `apply/agent.py`), three functions (search, tailor,
remember), two crons (sync, reconcile). Only the two agents loop, so only they
get tools and a harness.

Design docs: `PLAN.md` · `fetch.md` · `AGENTS.md` · `HARNESS.md` · `BUILD.md`

## Safety

- **`submit()` refuses unless `ALLOW_SUBMIT=1`.** Not set by default, never set
  in tests. Everything up to the submit button runs either way.
- **Every filled value traces to a vault row.** No vault entry means the agent
  asks rather than guesses.
- **Resumes rephrase, never invent.** Checked mechanically: a rewritten bullet
  introducing a number or a proper noun the original lacked is reverted.
- **No resume goes out that the person didn't pick.** Every apply asks
  your file / updated / tailored (last answer offered as the default), and
  `start_application` refuses unless their own message answered it.
- **Scenario runs never apply.** They stop at the built resume; the
  application launch is stubbed.
- **A failed fetch closes nothing.** A timeout must never read as "that
  company closed every role."
- **Tests never touch live data.** In-memory database, fixture forms.

## Resumes

Three per person, chosen on every apply (details in `AGENTS.md`):
**your file** (their upload, untouched), **updated** (upload + facts told in
chat since, fitted to the page), **tailored** (per job). The full record is
never sent. Nothing new since the upload → only "your file or tailored?".
"Apply to all" asks once; tailored then builds one per job, the other two
send the same file to all.

The render gate (`resume/render.py`) blocks a PDF when:
- text is missing or out of order;
- merged-letter or icon glyphs are present (the AppsFlyer → "Appsﬂyer" bug:
  fixed with XCharter + `Ligatures=NoCommon`);
- a name, email or employer only matches after Unicode normalisation;
- a link isn't visible as text.

Contact links (portfolio, LinkedIn, GitHub, X) show as short visible
addresses that are also clickable. Employers receive `Firstname_Company.pdf`.

**Layouts** (`resume/layouts/`): seven looks, each a set of values over one
shared template (`base.tex`), so the one gate checks them all. A4 by default.

| Layout | Font | Body | Margins |
|---|---|---|---|
| charter (default) | XCharter | 10.5pt | 0.5in |
| tech_compact | Latin Modern, small-caps headings | 10pt | 0.5in |
| harvard | EB Garamond, name centred | 11pt | 0.75in |
| modern_accent | Fira Sans, one blue accent | 10.5pt | 0.6in |
| indian_placement | Carlito, education first, PoR, no summary | 10.5pt | 0.6in |
| executive | TeX Gyre Termes, up to 2 pages | 11pt | 0.75in |
| engineering_classic | TeX Gyre Heros | 10pt | 0.5in |

The person picks one in chat by name or description ("make it look like
Harvard"); it becomes `resume_layout` in the vault and every later build uses
it. `resume_layouts` lists them, suggests two by role and years, and renders
page 1 of *their* resume in any of them as a PNG. The gate also fails a PDF
that lost a character the font lacks (XCharter silently dropped ₹), and
hyphenation is off so no keyword is split. Section order (Phase 6) is not
built: each layout has one fixed order.

What's next (slop check, JD top-5 placement, stricter ATS, layouts, India-first
defaults): `RESUME_PLAN.md`. Phases 1-2 are built; 3-9 are not.

## Cron

```cron
0 * * * *  cd /path/to/Job\ Agent\ MVP && python cli.py sync
30 2 * * * cd /path/to/Job\ Agent\ MVP && python cli.py reconcile
```

## Tests

```bash
python -m pytest tests/ -q      # 231 tests, no API key, no network
```

## Where jobs come from

**1. Company boards** — `engine/boards.json`, 165 boards on Greenhouse,
Lever, Ashby, SmartRecruiters (and Workable, adapter built). One request per
board gives every job with its full description and the real apply link
(SmartRecruiters: one list request, then one per *new* posting, filtered to
India). The first 19 were hand-picked from 137 guesses. The other 146 came
from `python cli.py discover` (2026-09-28): every employer Adzuna and
LinkedIn named (2,325), up to three slug guesses each, asked of all five
platforms one request at a time. 4,790 slugs per platform; 259 boards
answered with jobs, 167 of them with Indian roles; aggregators, recruiters
and generic-word matches were left out. The answers are in
`engine/discovered.jsonl`. To extend: `discover` again once the table has
new employers, then `discover --add`.

`python cli.py supply` counts what the product can fully handle per realistic
search (full description, employer apply link, last 14 days; gate 15).

**2. LinkedIn, logged out** — `engine/linkedin.py`, searches in
`engine/linkedin_searches.json` (every role in every place). No login, no key,
covers nearly every employer. Two steps:

- *Collect links.* Each search, newest first, until a page has nothing new.
  New links wait in `linkedin_queue`. A repeat run is a page or two per search.
- *Fill in descriptions.* One request per job, 300 per run, newest first. A job
  reaches `jobs` only once it has a description.

Rules: one request at a time with a pause; anything but a 200 stops the run and
closes nothing; a job a board already has is skipped (the board copy has the
real apply link); LinkedIn jobs close by age (30 days), never because a search
stopped showing them. Logged out, LinkedIn hides where "Apply" leads, so
`apply_url` is the LinkedIn page.

To widen supply: add a role or a place to the JSON.

**3. Adzuna** — `engine/adzuna.py`, config in `engine/adzuna_searches.json`
(India, every role). Free API: 50 jobs a call, ~25 calls a minute, 2,500 a
month. India gets ~4,000 new Adzuna jobs a day, so the free tier only just
keeps up, and a wasted page matters. Paging is oldest first from where the
last run stopped (whole-day window, then a short page search past what was
already read), so jobs landing mid-run go to the end instead of repeating on
the next page. `sync --adzuna-calls N` caps a run. City comes from
`location.area`; a row Adzuna places only in "India" has no city unless its
title names one. Needs `ADZUNA_APP_ID` and `ADZUNA_APP_KEY`; without them
`sync` skips it. The description is only a snippet and the link is an
adzuna.in redirect, so Adzuna runs last, never adds a job a fuller source
has, and its row is closed once a fuller copy arrives (usually from a board
`discover` found for that employer). Following the redirect to the employer
is not built: the hop is behind a bot wall (see `fetch.md`). Predicted
salaries are dropped. Job detail credits "Jobs by Adzuna" as their terms ask.
`sync --no-adzuna` skips it; `sync --no-linkedin` skips LinkedIn only.

An agent that reads a careers page belongs in the hourly cron, where nobody is
waiting. Never in the query path.

## Users

Jobs are shared; everything about a person is not. The default profile keeps
both in `jobs.db`. Any other profile (`JOB_AGENT_HOME=profiles/<name>`) gets its
own `jobs.db` holding only vault, career, messages and applications, and reads
jobs from the shared file — so a new user sees every job on their first search
and nothing of anyone else's. A profile that already has its own `jobs` table
(the scenario profiles, frozen for replay) keeps using it.

```bash
JOB_AGENT_HOME=profiles/asha python3 cli.py web     # a brand-new user
```

## Applying: the Jev browser

The apply agent drives **your own Chrome** through Jev Browser Control
(`apply/jev.py`), joining its local bridge as a peer — no Claude Code needed;
the bridge starts itself if nothing else has. GLM decides every value; Jev does
the typing, choosing and uploading (0.04 s to type a field, 0.3 s for a
dropdown). Playwright was removed on 2026-09-24.

Needs: Chrome open, the extension loaded from `../jev-browser-control/extension`
(`JEV_BROWSER_DIR` to move it), `pip install websocket-client`. The form opens
in a new tab; leave it alone while the agent works — if it is closed, it is
reopened once and filled again from empty. `ALLOW_SUBMIT=0` still stops every
submit.

## Applying: the cloud browser (for people not at this Mac)

`JOB_AGENT_BROWSER=cloud` swaps Jev for a Browserbase Chrome (`apply/cloud.py`,
Playwright over CDP): one fresh session per run, closed when the run ends or
parks, so nobody pays for a browser waiting on a reply. A resumed run opens the
form again empty and is told so. Each run's recording link is in its log
(`agent_logs`, kind `browser`). Keys in `.env`: `BROWSERBASE_API_KEY`,
`BROWSERBASE_PROJECT_ID`. `python cli.py doctor` checks them without opening a
browser.

- Region `ap-southeast-1` by default (`CLOUD_REGION`): from India a fill takes
  ~1 s there against ~5.5 s in their US default. Move it when the code moves.
- Live 2026-09-24, no submit: Lever (12 fields, upload, location suggestion,
  radios) and Greenhouse (27 fields, react-select dropdowns) fill correctly;
  Ashby reads cleanly. About 2 s a text field, 1 s a radio, 5-8 s a dropdown.
- A suggestion list is only clicked when an option matches what was typed; on
  no match the field returns the choices it saw. Taking the first one turned
  "Bengaluru" into "Luru, Jharkhand".
- Lever has an hCaptcha on submit. Not solved yet; matters once submitting is on.
- Free plan: 1 browser-hour a month, 3 at once, 15-min sessions. $20: 100 h.

## Models

Everything runs on GLM 5.3: `glm-5.3` for chat, ranking and apply,
`glm-5.3-flash` for labels. Both always think; `effort="low"` skips it. GLM
accepts low, high and max, not medium (`llm.effort_level` rounds medium up).
OpenRouter is the backup labelling uses if GLM stops mid-run.

## Labels

Every job is read once by a cheap model (`engine/labels.py`, `prompts/label.md`)
and given: role family, level, years, owns a P&L, country, work mode, B2C/B2B.
Paid once per job, used by every search. Same provider as chat
(`LABEL_PROVIDER`, `LABEL_MODEL` to change); 823 jobs took two minutes on
glm-5.3-flash.

Rows the model has not read get **free rule labels** at every sync
(`engine/rules.py`): role family from the title (or Adzuna's category when
the title says nothing and the category names one family), level from the
title, country when exactly one is named, remote/hybrid when the title or
place says so. Only empty columns on rows the model has not read; a title
that fits two families, or anything in the growth/CRM/marketing tangle, gets
no family. The model's labels replace them when it reads the row.

Labels are sometimes wrong, so none of them can hide a job on its own:

- **Role:** title matches *or* label matches.
- **Level, P&L, the person's experience:** order the list, never cut it.
- **Country:** filters, but unknown and "Global" pass.

## When the table comes up short

1. Widen only what they did not say (`query.relax`).
2. **Live LinkedIn search** — two pages, eight jobs, about 15 seconds, then
   labelled and stored. Runs when SQL finds too few rows, or when the ranker
   keeps too few. `live=True` from chat and `cli.py search` only;
   `JOB_AGENT_LIVE=0` turns it off (the scenario harness does).
3. Still short with a city named: count remote and other-city matches and
   offer them as a **choice**, never as results.

Every ask with a role becomes a row in `saved_searches`, which the daily
LinkedIn fetch runs from then on. Not built: a web-search last resort (needs a
search API key) and telling someone later that new jobs arrived.
