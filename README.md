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
pip install httpx anthropic playwright pytest
playwright install chromium
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
| `sync` | poll all boards, upsert, close what vanished |
| `verify` | which board slugs are live |
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
- **A failed fetch closes nothing.** A timeout must never read as "that
  company closed every role."
- **Tests never touch live data.** In-memory database, fixture forms.

## Cron

```cron
0 * * * *  cd /path/to/Job\ Agent\ MVP && python cli.py sync
30 2 * * * cd /path/to/Job\ Agent\ MVP && python cli.py reconcile
```

## Tests

```bash
python -m pytest tests/ -q      # 43 tests, no API key needed
```

## Boards

`engine/boards.json` — 19 live boards, ~670 jobs.

Built by probing 137 candidate companies across Greenhouse, Lever and Ashby.
**20 had a live public board.** That 15% is the India coverage gap: most Indian
employers post to Workday, Darwinbox, Keka or their own careers page.

To extend: add candidates, run `python cli.py verify`, keep what answers.

An agent that reads a careers page belongs in the hourly cron, where nobody is
waiting. Never in the query path.
