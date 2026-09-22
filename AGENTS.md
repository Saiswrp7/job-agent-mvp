# Agents — prompts, tools, context

Two agents. Three prompted functions. One rule decides which is which:
**does it loop an unpredictable number of times?**

| | Loops | Tools | Context grows |
|---|---|---|---|
| Chat agent | shallow, 1–3 steps | 5 | across the conversation |
| Apply agent | deep, 10–40 steps | 6 | within one application |
| `filters` / `rank` / `tailor` | no | none | built fresh, discarded |

---

## 1. Chat agent

The only thing the user talks to. Owns voice and routing. Does no work itself.

### System prompt (`prompts/chat.md`)

Holds four things:

- **Voice.** Short, plain, read on a phone. No preamble, no "I'd be happy to".
- **What it can do.** Search, tailor, apply, report status. Nothing else.
- **Routing rules.** When to call a tool vs. answer from what's already in
  context. If the last search is still in context, "tell me more about 2" is a
  lookup, not a new search.
- **Hard nos.** Never invent a job or a link. Never start an application without
  explicit approval. Never ask more than one clarifying question per search.

### Tools

```
search_jobs(query_text)        → 5 jobs with reasons
get_job(job_id)                → full detail for one
tailor_resume(job_id)          → bullets as text, for approval
start_application(job_id)      → hands off, returns immediately
application_status(app_id)     → where it got to
```

Each wraps a plain function. The agent never sees SQL, adapters, or Jev.

### Context

**In:**
- system prompt (static)
- vault *summary* — name, current role, location, target titles. Four lines.
- last ~20 turns
- the last result set as a **reference table**: `id, title, company` only

**Never in:**
- full job descriptions
- form HTML or page snapshots
- the resume JSON
- the apply agent's tool log

The reference table is the mechanism that makes "apply to 2" work. Five short
rows, not five job descriptions. When the table would fall out of a trim, drop
the turns around it first.

**Trimming:** oldest turns go first. The system prompt, the vault summary and
the current reference table are pinned.

---

## 2. Apply agent

Fills one application form. Runs in the background. Parks for hours.

### System prompt (`prompts/apply.md`)

- **You are filling exactly one form.** Do not navigate away from it.
- **Never invent a value.** If a field has no vault entry, call `ask_user`.
- **Pre-flight first.** Call `read_form` before filling anything, diff the
  whole field list against the vault, and ask for every gap in one message.
- **`submit` last,** only when every required field is filled.
- **Captcha → `screenshot`, then `ask_user`.** Do not attempt it.

### Tools

```
read_form()                  → [{label, name, type, required, options}]
fill_field(name, value)      → confirmation string
upload_file(name, path)      → confirmation string
ask_user(question)           → does not return; parks the run
submit()                     → refuses unless ALLOW_SUBMIT=1
screenshot()                 → image
```

Six. Jev sits behind them and never appears in a tool definition.

### Context

**In:**
- system prompt
- the job: title, company, apply_url. Three lines, not the description.
- a **vault slice**, retrieved by the field names `read_form` returned
- the **latest** page snapshot
- its own tool log

**Pruning rule:** a page snapshot from ten steps ago is dead weight. Keep the
most recent, replace the rest with `[snapshot pruned]`. Keep every
`fill_field` result — they are short, and they are the record of what was
actually entered.

Without this the context is 80% stale DOM by step fifteen, which is exactly why
this agent cannot also be the chat agent.

---

## 3. Prompted functions

No tools, no loop, no memory. Context assembled per call and thrown away. This
is what makes them unit-testable.

**`prompts/filters.md`** — message → filter JSON.
Contains the schema and one rule: every field defaults to `null`, fill only what
was explicitly said. Under-filling is safe; over-filling silently deletes good
jobs.

**`prompts/rank.md`** — 20 rows + the original message → 5 with reasons.
Contains `exclusions.json`: the kill titles and kill signals. This is where the
CRM trap is enforced, because SQL cannot express it.

**`prompts/tailor.md`** — JD + `master.json` → patch.
Contains the editable/locked field split and one rule: rephrase, never invent.

---

## 4. The vault

Not an agent. One cheap call after every user message: *"is there a durable fact
here?"* → write a typed row.

Retrieved **by key**, never model-mediated, never dumped into a prompt whole.

The rule: if a wrong value causes an irreversible action, it is a database row,
not prose. A markdown blob is a fine place for "prefers early-stage" and a
terrible place for expected CTC.

---

## 5. How the handoff works

The chat agent must never hold the apply agent's context. They talk through the
database.

```
user: "apply to 2"
  ↓
chat agent → start_application(job_id)
  ↓ creates applications row, spawns asyncio task, returns "started"
chat agent replies immediately: "on it"

apply agent runs ... hits a gap
  ↓ ask_user("notice period?") → saves log, status=waiting, exits

chat agent sees the pending question, delivers it
user answers
  ↓ answer written to the row
run(application_id) reloads the log and continues from that exact step
```

Three consequences worth stating:

- The apply agent's DOM junk never reaches the chat agent.
- A restart loses nothing that was parked, because the log is in SQLite.
- Three applications are three independent runs, each parking on its own.

---

## 6. One request, end to end

```
"five growth roles in Bangalore in B2C companies"
  │
  ├─ remember()          one call, writes nothing new this time
  ├─ chat agent          routes → search_jobs()
  │    ├─ filters.md     → {count:5, city:"Bangalore", company_type:"b2c", ...}
  │    ├─ query.py       → SQL, LIMIT 20, no LLM, no network
  │    └─ rank.md        → 5 picks + reasons, exclusions applied
  └─ reply, reference table pinned into context

"tailor for 2 and 4"
  ├─ tailor.md           → patch per job
  ├─ verify.py           → mechanical gate, failures revert
  └─ reply: bullets as text, for approval

"looks good, apply"
  ├─ render.py           → PDF, pdftotext gate
  ├─ start_application() × 2
  └─ two apply agents loop independently, park independently
```

Two LLM calls for a search. One per resume. One loop per application. Nothing
else runs a model.
