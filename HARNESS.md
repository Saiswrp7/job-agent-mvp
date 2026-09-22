# Harness — loop, prompts, tools, context, memory

Everything the agent layer needs. Structure in `AGENTS.md`, this is the mechanics.

Model: `claude-opus-5`, `thinking: {type: "adaptive"}`. No `budget_tokens` — it's a
400 on this model. Effort `high` for the apply loop, `low` for the filter call.

---

## 1. The loop

An agent is a `while`. Everything else is machinery around it.

```python
while steps < BUDGET:
    reply = client.messages.create(
        model="claude-opus-5",
        system=system_blocks,          # cached, see §6
        tools=TOOLS,
        messages=history,
        thinking={"type": "adaptive"},
    )
    history.append({"role": "assistant", "content": reply.content})

    if reply.stop_reason != "tool_use":
        break

    results = [run_tool(b) for b in reply.content if b.type == "tool_use"]
    history.append({"role": "user", "content": results})   # ALL of them, one message
    steps += 1
```

Four rules that are not optional:

- **Append `reply.content`, not the text.** Extracting the string and appending
  that silently drops thinking blocks and breaks the next turn.
- **Return every `tool_result` in a single user message.** Splitting them across
  messages trains the model out of parallel calls.
- **A failing tool returns `tool_result` with `is_error: True`.** Never drop it,
  never crash the loop. The model recovers from an error it can read.
- **`BUDGET = 40`.** Without it a confused model retries one field forever and
  you learn about it from the bill.

### Parking

`ask_user` is the one tool that doesn't return.

```python
def ask_user(question):
    save(app_id, history, status="waiting")
    queue_question(app_id, question)
    raise Park
```

`run(app_id)` loads the saved history or starts fresh, so resuming six hours
later costs nothing. Captchas park the same way.

### Traces

Every prompt, tool call and result to `agent_logs`. An application that fails at
2am is undebuggable otherwise.

---

## 2. System instructions

Four prompts, four files, versioned like code. **These are the product logic.**

### `prompts/chat.md`

```
You are a job agent. You help one person find work and apply to it.

Voice: short, plain, read on a phone. No preamble.

Tools: search_jobs, get_job, tailor_resume, start_application,
application_status.

- Never invent a job, company or link. Everything comes from a tool.
- Never start an application without explicit approval.
- At most one clarifying question per search.
- If the last search is in context, "tell me about 2" is a lookup,
  not a new search.
- When an application is parked with a question, ask it in your own words.
```

### `prompts/filters.md`

```
Turn the message into filter JSON. Fill only what was explicitly said.
Every unstated field stays null.

Under-filling is safe: SQL returns more and the ranker cuts it down.
Over-filling silently deletes good jobs. When unsure, null.
```

### `prompts/rank.md`

```
Pick {count} from the list. One line each on why.

Kill immediately:
- titles: CRM Manager, Lifecycle Marketing, Retention Marketing
- 3+ of: MoEngage, CleverTap, Braze, push notification, lifecycle campaigns
  (1-2 mentions is fine. CRM as 20% of a role is fine, as the whole job is not.)

Also apply: {soft_criteria}

Judge from the description, not the title.
```

### `prompts/apply.md`

```
You are filling exactly one application form.

Call read_form first. Diff every required field against the vault.
If anything is missing, call ask_user ONCE with all the gaps together.
Then fill. Then submit.

- Never invent a value. Not in the vault means ask.
- Never navigate away from this form.
- submit() only when every required field is filled.
- Captcha: screenshot, then ask_user. Do not attempt it.
```

**Operator instructions mid-conversation** go in `messages[]` as
`{"role": "system", "content": "..."}` — supported on Opus 5, no beta header.
Editing top-level `system` changes the prefix ahead of the whole history and
re-processes every cached turn uncached.

---

## 3. Tool calling

Only the apply agent gets tools. `search`, `tailor` and `remember` are one-shot
calls with no loop, so they're functions.

```python
TOOLS = [{
    "name": "fill_field",
    "description": "Type a value into one form field.",
    "input_schema": {
        "type": "object",
        "properties": {
            "name":  {"type": "string"},
            "value": {"type": "string"},
        },
        "required": ["name", "value"],
        "additionalProperties": False,
    },
    "strict": True,
}]
```

- **`strict: True`** with `additionalProperties: False` + `required` guarantees
  the input validates. Free correctness on a tool that types into real forms.
- **Parse `tool_use.input` with `json.loads`.** Never string-match it — escaping
  varies.
- Six tools, and **Jev never appears in one**. `read_form` wraps
  `browser_snapshot`, `fill_field` wraps `browser_type`. Swapping to Playwright
  is then one file.

The filter and tailor calls use **structured outputs**
(`output_config: {format: ...}`), not tools. They aren't agentic; they're a
shape contract.

---

## 4. Context management

Three different lifetimes, which is the whole reason these are three things.

### Chat agent — per turn

```
[cached] system prompt
[cached] identity profile      ~1,000 tokens, from memory (§5)
         vault one-pager       name, role, city, notice, CTC
         open applications     pulled fresh from DB every turn
         compaction recap      ~200 tokens when history was trimmed
         last ~20 turns
         reference table       id, title, company for the last 5 results
```

Pinned: system prompt, profile, vault one-pager, reference table. Trimmed
first: oldest turns.

**Never enters:** full job descriptions, form HTML, the resume JSON, the apply
agent's tool log.

The reference table is what makes "apply to 2" resolve — five short rows, not
five job descriptions.

### Apply agent — per application

Its context fills with DOM junk within five steps. Use the API rather than
hand-rolling the pruning:

```python
client.beta.messages.create(
    betas=["context-management-2025-06-27"],
    context_management={"edits": [{"type": "clear_tool_uses_20250919"}]},
    ...
)
```

That clears stale tool results automatically. Keep the vault slice small —
retrieve **by the field names `read_form` returned**, never the whole vault.

### Functions — none

Built fresh per call, discarded. That's what makes them unit-testable.

### Compaction

At ~30k tokens, summarise everything but the last 10 turns into ~200 tokens.

**It loses less than it looks like it does,** because durable facts already left
the conversation into memory (§5). The recap only has to hold the thread — what
was searched, what was shown, what's in flight — not the facts.

Application status is never compacted because it's never in context to begin
with: it's queried fresh from the DB each turn.

---

## 5. Memory — the Instinct split

Taken from Instinct's architecture (Shah's teardown, 2026-09-21), because it
solves a problem this product specifically has.

### The split

**The chat agent has read access only.** A separate background agent on a daily
cron has write access and does all reconciliation.

Why it matters here and not just in general: the vault feeds **form filling**.
A live-write path means one misparsed sentence — *"I'm looking at 30 LPA roles"*
becoming `expected_ctc = 30` — gets typed into a real application that cannot be
retracted. A read-only chat agent makes that impossible without a reconciliation
pass in between.

```
user message
  → remember()  writes to a staging log          (never to the vault)
  → chat agent  greps memory, read-only

daily cron
  → reconciler  promotes staged facts to the vault and profile
                generalises repeated examples into traits
                overwrites contradicted facts with dated corrections
                deletes nothing that wasn't explicitly contradicted
```

### Two shapes, split by consequence

| | Shape | Retrieved by | Holds |
|---|---|---|---|
| `vault` | SQLite typed rows | exact key | phone, CTC, notice period, work auth |
| `profile` | git-tracked markdown | grep | "prefers early-stage", "avoid consulting" |

The rule: **if a wrong value causes an irreversible action, it is a database
row, not prose.** A markdown blob is a fine place for a preference and a
terrible place for a salary figure.

Markdown files carry `aliases` in frontmatter so they surface on related
queries, and `[[wikilinks]]` between them. No embeddings — grep is enough at
this size.

### Forgetting

Only on explicit contradiction. Passive facts persist. Git history keeps old
versions recoverable, which matters the first time the reconciler is wrong.

---

## 6. Caching

Render order is `tools` → `system` → `messages`. **Any byte change in the prefix
invalidates everything after it.**

### Placement

One explicit breakpoint on the last static system block — that's tools plus
system cached together, with a guaranteed read point that survives whatever
happens later in `messages`:

```python
system=[
    {"type": "text", "text": CHAT_PROMPT + IDENTITY_PROFILE,
     "cache_control": {"type": "ephemeral", "ttl": "1h"}},
]
```

Then top-level automatic caching for the growing conversation tail.

### TTL — use `1h` for the chat agent

Choose by the **start-to-start gap** between requests sharing the prefix:

| Gap | TTL |
|---|---|
| under 5 min | default 5-minute — every request refreshes it, strictly cheaper |
| 5–60 min | **`1h`** — the only window where the 2× write pays off |
| over an hour | neither; accept the cold miss |

A texting user replies after ten or twenty minutes, which lands squarely in the
middle band. The apply loop, whose turns are seconds apart, stays on the
default.

### Economics

- cache **write**: 1.25× base input (5-min TTL), **2× (1h)**
- cache **read**: ~0.1× base input
- break-even: 2 requests on 5-min, 3 requests on 1h
- a read refreshes the timer at no cost

### Rules

- Max **4** breakpoints per request.
- Minimum cacheable prefix on Opus 5 is **512 tokens**. Shorter silently doesn't
  cache — no error, just `cache_creation_input_tokens: 0`.
- **Nothing volatile before a breakpoint.** A `datetime.now()` in the system
  prompt makes every request a miss, silently, forever.
- The open-applications board and the reference table change every turn, so they
  go **after** the last breakpoint.

### Verify

```python
assert reply.usage.cache_read_input_tokens > 0
```

Put that in a test. The costliest caching failure is silent: requests keep
succeeding, the bill is just higher, and nothing announces it. Re-check whenever
prompt assembly changes.

---

## 7. What runs when

| | Trigger | Model work |
|---|---|---|
| `sync()` | hourly cron | none |
| `remember()` | every user message | 1 cheap call → staging |
| `search()` | user asks | 2 calls |
| `tailor()` | user picks a job | 1 call |
| `apply()` | user approves | a loop, 10–40 steps |
| `reconcile()` | daily cron | 1 call, write access to memory |

Two crons, three functions, one agent, one chat layer.
