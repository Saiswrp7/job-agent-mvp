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
search_jobs(query_text)             → 5 jobs with reasons
get_job(ref)                        → full detail for one
tailor_resume(ref, instruction?, layout?) → tailored bullets + PDF, for approval
build_resume(instruction?, layout?) → their updated resume, no job attached
resume_layouts(preview?)            → the layouts, two suggested, PNG previews
send_resume(which)                  → their upload or the newest built PDF, as a file
remember_experience(kind, text)     → puts a fact they stated on the record
set_resume_preference(choice)       → saves the default for the resume question
start_application(ref, resume?)     → hands off, returns immediately
answer_application(app_id, answer)  → resumes a parked run
application_status(app_id)          → where it got to
```

Each wraps a plain function. The agent never sees SQL, adapters, or Jev.

### Which resume goes out (2026-09-24)

Three resumes, and the person picks one **on every apply**:

| Choice (they see) | What goes out | Vault value |
|---|---|---|
| your file | the one they uploaded, byte for byte | `existing` |
| updated | their file + facts told in chat since, fitted to the page limit (`generate.build`; weakest lines cut, oldest role first, cuts reported) | `updated` (old `master` reads as this) |
| tailored | rebuilt for this job (`tailor.build`) | `tailored` |

- The record (career store) holds every verified fact. **It is never sent
  whole**: nobody applies with everything they have done.
- The agent asks: *"Which resume for this one: your file, updated or
  tailored? (Last time: tailored.)"* When nothing has been told since the
  upload (`generate.additions() == 0`), "updated" would be their file again, so
  the question is only **your file or tailored**, and a request for "updated"
  sends their file. The vault's `resume_mode` is only the **default**.
- **The gate is code, not prompt.** `start_application` sends only a mode that
  `said_resume()` finds in the person's own message this turn: a named mode,
  "same"/"as before" when a default exists, or a bare "yes" right after a reply
  that asked. Anything else returns an ERROR carrying the question. That is
  why "just apply to everything, don't ask me" still gets asked.
- **A batch is one question.** "Apply to all five" asks once. Then:
  **your file or updated** means the same PDF for all five; **tailored** builds
  one resume per job inside `start_application`, not shown first.
- A tailored resume they saw and approved on an earlier turn goes out as they
  saw it. No resume on file means no question.
- Generated resumes reach the apply agent as `render.upload_path(pdf)`, a
  copy named `Firstname_Lastname_Resume.pdf`. Their own file goes out
  untouched.

**Layouts:** `resume_layouts` lists the 7, suggests two and previews page 1 of
their own resume as PNG; `layout` on build_resume/tailor_resume picks one by
name or description, saves it as `resume_layout`, and "apply" builds in it.
A layout that hurts them (a two-pager at 3 years) is built anyway, with one
warning.

Plan and research: `RESUME_PLAN.md`, `research/resume/`.

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

**Decided in code, not by the model (2026-09-30, after the first live runs
sent nothing and one was recorded as sent):**

- Before the model starts, `agent._no_form_here` presses the job page's own
  Apply button if the page is a job description (Keka), then ends the run as
  `sign_in` (a password field) or `no_form` (no fields) with the link.
- `read_form` reads inside web components (SmartRecruiters' shadow DOM).
- `screenshot` returns the page in words (URL, fields, buttons, text): the
  apply model reads text only, and handed a file name it guessed "sign-in".
- `submit` never presses an apply-with-Indeed/LinkedIn button, refuses a form
  that goes on to a Next page, and says `submitted` only when the page
  confirms it. A click that changed the page without a confirmation is
  `unconfirmed` (may have gone; never retried). Rules: `apply/pagecheck.py`.
- Only sites that passed the form eval (`evals/apply_forms.py`, real forms,
  never submitted) are applied to: `apply/sites.py`. The rest get the link
  and a tailored resume.

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
  ├─ said_resume()       → they approved the tailored one, so no question
  ├─ render.py           → PDF, ATS gate, upload copy Firstname_Lastname_Resume.pdf
  ├─ start_application() × 2
  └─ two apply agents loop independently, park independently
```

Two LLM calls for a search. One per resume. One loop per application. Nothing
else runs a model.
