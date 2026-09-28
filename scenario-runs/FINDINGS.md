# What the agent actually does — 27 conversations, 49 turns

Run 2026-09-23. `runs/run-0923-0042.json` (raw) · `transcript-0923-0042.md`
(readable). Cold-start scenarios re-run clean in `run-0923-0106.json`.

**Nothing was fixed.** This is observation only. No code was changed; the
conversations went through `chat.reply`, the same function the terminal and the
browser use. `ALLOW_SUBMIT=0` throughout, apply routed to a recorded form.

Judged by the job seeker's standard, not the code's: **ask for Bangalore, get
Bangalore. Ask for growth and performance, get those. If there is nothing, say
so.**

Cost of the run: 317 model calls, 21 minutes.

---

## What works, and should not be touched

Four behaviours came out genuinely strong. Worth naming, because the list
below is long and these are the parts that already meet the bar.

**Honesty when there is nothing.** Berlin (14), Kochi (27) and Chennai (13) all
returned zero, and all three said so plainly and offered a named alternative.
Nothing was padded. Chennai is the best of them — the search kept the city and
reported *"only 0 match what you asked for — I kept city Chennai rather than
widening it without asking."* That is the protect-cascade from last night
working exactly as intended.

**Cold start.** A brand-new user with an empty vault (20, 21) got honest
answers, a clear request for the resume, and an accurate "here is what I know
about you: pretty thin." No invented history.

**Refusing to apply into thin air.** "apply to the first one" with no search
behind it (12): *"No results to apply to yet — we haven't run a search."* One
model call, 2.4 seconds.

**Holding the line on invented numbers.** Asked to claim 8 years (23), the
resume still said 4+.

---

## The findings, worst first

### 1. The number you see is not the number it uses

**This fires in roughly half the conversations and it applies to the wrong job.**

The chat agent renumbers results when it writes them out. The tools index the
search order. Nothing reconciles the two.

*Scenario 17* — four product roles shown, user says "tell me about 2":

| | |
|---|---|
| What the user saw as **2** | Product Manager — Lending Product, 4–8 yrs |
| What `get_job(2)` returned | Product Manager — **Paytm Insurance, 6–8 yrs** |

They then said "ok apply to 2" and it tailored a resume for Paytm Insurance —
a role wanting 6–8 years, for a candidate with 4.

*Scenario 19* — "wait no, do 2 instead". User meant Meesho DAU Growth (shown as
2). It tailored for Paytm Recharges & Utilities (internal 2).

*Scenario 23* — "tailor my resume for 1". User saw 1 = UPI Growth. It tailored
Campaign Management — User Growth.

*Scenario 05 is the worst case.* "show me jobs at Meesho and CRED" ran **two
searches in one turn** and merged them into one list numbered 1–9. `picks` only
holds the last search, so it held 4 CRED jobs:

```
user sees   1. Associate Director, Commerce Platform — Meesho
apply to 1  → Capital Partnerships — CRED
```

Different job, different company. And items 5–9 point at nothing at all.

With `ALLOW_SUBMIT=1`, this is the bug that sends a real application to the
wrong company.

### 2. It reports applications as "submitted" when nothing was submitted

*Scenarios 11 and 22.* The agent's closing line:

> "All five are **submitted** with your name, email, phone, and resume"

Nothing was submitted. `ALLOW_SUBMIT=0` refused every one.

The apply agent itself was honest — its own result text reads *"I'm unable to
submit because two required fields are not in the vault."* But `run_tool`
wraps it as:

```
[5] done: I've filled 5 of 8 fields ... I'm unable to submit because ...
```

`done` means "the run loop ended" to the code. The chat model read it as the
outcome and told the user their applications were in.

This is the fake-submission failure from the earlier session, back in a new
form. Last time the agent narrated with no tool calls. This time the tools ran
correctly and a single word in the wrapper overrode their honest answer.

### 3. The approval gate inverts exactly when the blast radius is biggest

The prompt says: *"`tailor_resume` — Always show them before applying."*

| User says | What happened |
|---|---|
| "apply to 1" (19) | tailored, showed the bullets, waited. Correct. |
| "apply to all of them" (11) | tailored 5, started 5 applications, showed none |
| "just apply to everything, don't ask me" (22) | same — 5 applications |

One sentence produced five applications, five tailored resumes the user never
saw, 53 model calls and **244 seconds** of silence. The careful behaviour holds
for one job and collapses for five.

### 4. The number you ask for is always ignored

Seven requests with an explicit count. Seven times the filter used 5.

```
"find me 3 growth roles"    → count = 5
"find me 4 product roles"   → count = 5
"find me 5 product jobs"    → count = 5
```

The chat agent paraphrases the sentence before calling `search_jobs`, and the
number is what it drops. It sometimes hides this by showing only three of the
five it got — which is also how the numbering in finding 1 drifts.

### 5. One message can silently trigger six searches

Nine of 49 turns ran `search_jobs` more than once, re-phrasing after thin
results without saying so:

| Scenario | Searches | Model calls | Message |
|---|---|---|---|
| 04 | **6** | 13 | "I want remote product roles at fintech companies" |
| 16.3 | **6** | 14 | "actually forget junior, I want something I'd have to stretch for" |
| 03 | 4 | 13 | "find 5 APM roles for someone with 2 years of experience" |
| 07.2 | 4 | 13 | "yeah that works, show me more like the second one" |

Each search carries an ~18k-token ranking call. The user sees one answer and
no indication that six attempts were made.

### 6. It adds a city you never mentioned

| User typed | City that reached the query |
|---|---|
| "i need a job" | **Bangalore** |
| "something in product" | **Bangalore** |
| "what's good right now" | **Bangalore** |
| "idk somewhere i can actually own a number" | **Bangalore** |
| "I want **remote** product roles at fintech companies" | **Bangalore** (one of six attempts) |

It comes from the vault, so as personalisation it is defensible. As behaviour
it is invisible: the user is never told a location filter was applied, and in
scenario 04 it contradicts the word "remote" in the same sentence.

### 7. A city the database has never heard of is dropped, not filtered

| User asked | City in filter | What saved it |
|---|---|---|
| Chennai (13) | `Chennai` | SQL returned 0. Clean. |
| Berlin (14) | `None` | SQL returned 16 jobs from everywhere; the ranker rejected all 16 |
| Kochi (27) | `None` | same — 16 matched, 0 shown |

Both ended honest, so the user was not misled. But Berlin and Kochi were right
by the ranker's judgment, not by the filter, and that cost a full 18k-token
ranking call to discover something SQL answers instantly. The safety net is a
model call. On a less careful run it hands over Bangalore jobs for a Berlin
search.

### 8. It declines to lie, and does not mention that it declined

Asked to say 8 years (23), the resume said 4+ years. Correct — and presented
as *"Here's your tailored resume"* with no note that the instruction was
refused. The user believes the document says 8.

---

## The pattern

Seven of the eight sit in the **chat agent**, not in search, not in the
resume builder, not in the browser. Those three do their jobs. What fails is
the layer that translates between the person and the tools:

- it rewrites the sentence before passing it on (4)
- it renumbers the answer after getting it back (1)
- it reads a status word as an outcome (2)
- it retries without saying so (5)
- it applies a filter without saying so (6)

Every one is a **translation loss between the user's words and the tool's
arguments, or between the tool's result and the user's answer.** Nothing here
is a broken function. It is the same seam family as every previous bug on this
project, concentrated in one file.

## What this means for evals

The mechanical promise check (right city, right role words) passed 26 of 27.
Looking only at whether the final answer reads well, the agent looks good — the
prose is genuinely strong throughout.

Every finding above needed the **trajectory**: what was typed, what reached the
tool, what came back, what was shown. That is already recorded in
`messages.meta`. The evals that would catch these are:

1. displayed number `N` resolves to the same job as `picks[N]`
2. no reply claims "submitted" unless a submit actually returned success
3. a stated count reaches `filter.count`
4. `search_jobs` runs at most once per user message, or says it did more
5. no city in the filter that is not in the user's message or disclosed in the reply

None of these need a human judge. All five are checkable from the log.
