# Apply plan: a smooth flow (2026-10-01)

Sai: "I need smooth flow, tell me how to fix it." Written after the Loop run
(Keka, Sai's own profile, application 1) was filled wrong.

## Status (2026-10-01 night): fixes 1-4 BUILT, uncommitted, bot not restarted

Sai: "If glm has a model that can fix use that AI + driver" -> built:
- Reader (`apply/cloud.py` `_READ`, `read_form`): read-only display boxes skipped
  (calendar boxes kept); select2 / Keka dropdowns read as their hidden <select>
  (labelled by their question, options "India (+91)"); shared labels told apart by
  the box's own name ("Experience (in years) (months)", "Mobile Phone (country
  code)"); jQuery UI `hasDatepicker` recognised; calendar popups, Workday-style
  "robots only" traps and cookie-banner consent skipped; the application's OWN
  consent box no longer skipped (Keka keeps Apply Now greyed out until
  #candidateConsent is ticked); tick boxes named by the sentence after them.
- Box handlers (`_shape`, `fill_field`, `_date`, `_select`, `_pick_hidden`): salary
  to rupees or lakhs per label (`_money`; hourly/monthly/USD untouched), phone to 10
  digits beside a country-code list or a short box (`_phone`), ALL-CAPS names
  title-cased, a middle name that is the whole name refused, read-only calendars set
  through jQuery UI or the native setter, hidden lists chosen through jQuery;
  money/phone/number read back as digits (mismatch = ERROR); 8 s action timeout.
- Before approval: `_greyed_out` (disabled Submit -> the page's red text, empty
  required boxes, unticked consent boxes) and `_look` = `apply/vision.py`
  (glm-4.6v via Zhipu's OpenAI-style endpoint; first finding goes back to the agent,
  later ones ride on the approval as "Check these").
- Approval (`apply/confirm.py`): cleaned comparison (`_plain`, `same`), one row per
  resume file, `notes`.
- Profile (`vault.py`, `resume/parse.py`): middle_name, date_of_birth keys, more
  experience / notice aliases, whitespace-normalised labels; small boxes (currency,
  country code, experience months) never mapped to the big answer; country code
  derived from the phone; a resume-guessed split of a 3+ word name is asked, not
  used; the resume reader no longer guesses first/last for 3+ word names.
- Tests: `tests/test_keka_form.py` (29). Suite 593 passing.
- Live (real GLM, Browserbase, made-up applicant, submit off): Loop's Keka form went
  from wrong values + greyed Submit (335 s) to every value right, consent ticked,
  zero questions, reached Submit (96 s). 9-site regression: see the report.

Not built yet: Telegram [Send]/[Change] buttons (fix 5), real-key typing for the
Ashby/Greenhouse bot score, Oracle/Workday sign-ups.

## What smooth means, in Telegram

1. Person: "apply to Loop". Jojo: "On it."
2. Only if something is missing: **one** message asking for all of it ("Need 3 things
   for Loop: date of birth, notice period, total experience"). Answered once, kept
   for every later form.
3. Jojo fills every box **correctly**.
4. **One** clean approval: the real questions only, in plain words, the screenshot,
   and two buttons: [Send] [Change something].
5. Tap Send. "Sent" + the confirmation screenshot.

## What broke on Loop (from the run's own log)

| What the person saw | Cause | Where |
|---|---|---|
| First "BUGATA", Middle "BUGATA SAI SWAROOP", Last "SWAROOP" | Resume reader split the name by position (vault `first_name = BUGATA`, `last_name = SWAROOP`); no middle name stored, so the AI put the full name there | resume parse → `vault.py` |
| Salary "12" and "18" (meant 12 LPA, 18 LPA) | "12 LPA" typed into a numbers-only box became "12"; the tool said "the field rewrote it" and the run carried on; vault `expected_ctc = 18` has no unit | `apply/cloud.py` fill path |
| Experience shows "Months", date of birth blank | Keka's experience is 4 boxes with the same label; date of birth and INR are pickers you can't type into; 6 tries x 30 s timeouts | `cloud._READ`, fill path, no Keka helper |
| Fields "Oct", "field 24: 2011", resume listed under "Back to all job openings" | The date picker's month/year dropdowns and a page link were read as questions | `cloud._READ` |
| Submit greyed out, run failed at the click | Keka disables Submit while something is invalid; nothing read why | submit path |
| Asked to approve again: "changed since you said submit: Mobile Phone" | Phone went +91-9876543210 → 9876543210 → back; approval compares exact text | `apply/confirm.py` `Approval.check` |
| "Submitted" not taken as a yes | Approval is typed text, read strictly (on purpose) | `apply/confirm.py` `said_submit` |
| Test said Keka 5/5 | The test checks "every required box has something", with a fake person with a simple name and plain numbers | `evals/apply_agent.py` |

## Fixes, in order

### 1. One complete profile, asked once (fixes most of Loop, on every site)
- Standard facts every Indian form asks: first / middle / last name, phone, email,
  date of birth, city, total experience (years + months), current CTC (fixed,
  variable), expected CTC, notice period (days), willing to relocate, gender
  (optional), highest degree + college + year, LinkedIn.
- Stored as typed vault rows; money stored in full rupees.
- Name: never split by position. Ask once: "First name Sai Swaroop, surname Bugata?"
  Title case.
- Asked when the first application needs them, all in one message, then never again.

### 2. Code formats every value and checks it after
- The AI only decides which fact goes in which box. Code formats it for that box:
  money → full rupees (12 LPA → 1200000), phone → 10 digits when the box has +91 or a
  country picker, dates → the box's format, experience → years + months, names → title case.
- After each fill, read the box back. A different value = ERROR, never "filled".

### 3. A clean field list, and Keka's widgets
- The reader joins widgets into one question (experience years + months, salary +
  INR, date picker) and drops picker parts, page links and nameless "field N" boxes.
- Read-only boxes are marked so nothing tries to type into them.
- Keka helper first (date of birth picker, INR, experience), the same way the Keka
  captcha got its own setting.
- A stuck box gives up after 5 s, not 30 s.

### 4. Check the page before asking for approval
- If Submit is greyed out, read the page's red error text, fix it or ask.
- The approval shows only real questions, plus the screenshot.

### 5. Approval buttons
- Telegram buttons [Send] [Change something] (bot reads `callback_query` too).
- The approval compares cleaned values, so +91 vs no +91 doesn't ask again.

### 6. Test for correctness, then real sends
- New test person: Indian, surname-first name, LPA salaries, date of birth, notice.
- Each site: 5 real forms, stop before Submit, read back every box, compare with
  the expected value. A site goes live only when every value is right.
- Then one real send per site with jobs Sai wants: Loop again (Keka), Swiggy
  (SmartRecruiters), metaforms (Ashby, with real-key typing for its spam check).

## First step
1 + 2: they fix the name, salary and phone problems on every site, not just Keka.

## Which "driver" (form-filling engine) to use (researched 2026-10-01)

Sai's test: can it sign in, keep cookies, fill complicated forms. All run in our
Browserbase cloud Chrome.

| Driver | Sign in, AI never sees the password | Keeps cookies | Complicated forms | Licence | Catch |
|---|---|---|---|---|---|
| [browser-use](https://github.com/browser-use/browser-use) (117k stars, Python) | yes: `sensitive_data` placeholders per domain, TOTP 2FA built in | yes (storage state; or Browserbase Contexts) | general AI agent; their own cloud agents score 54-76% on hard tasks | MIT | the AI can click Submit by itself, so we'd need a hard guard; more AI calls per form (unmeasured) |
| [Stagehand](https://github.com/browserbase/stagehand) (25k, Browserbase's own) | yes: `%password%` variables are never sent to the AI | yes (Browserbase Contexts) | `act()` one box at a time; caching turns repeat forms into fixed scripts | MIT | TypeScript first; the old Python SDK repo is archived (v4 docs show Python) |
| [Skyvern](https://github.com/Skyvern-AI/skyvern) (23k) | yes: own vault / Bitwarden / 1Password, TOTP | yes (sessions, profiles) | best AI agent on "write" tasks (forms, logins) in WebBench, its own benchmark | AGPL | heavy (own server + Postgres); copyleft risk for a paid product |
| [Notte](https://github.com/nottelabs/notte) (2k) | yes: vaults; "Personas" give an agent an email and phone for sign-ups and codes | yes | AI agent | SSPL | can't self-host inside a paid service; cloud only |
| Ours, rebuilt the JobApplyAgent / Workday_Auto way | plan: Jojo-made passwords typed by code | Browserbase Contexts (tested) | profile + fixed-order answers + code per box type + read-back; AI only for odd questions | ours | we build the box handlers |

Verdict: no GitHub driver does all three reliably on job forms by itself. The AI-only
ones handle sign-in and cookies well but sit around the 54-76% tier on hard tasks and
can press Submit on their own. Use ours as the main driver (it already has the hard
Submit guard, approval, keep-alive) and Stagehand `act()` as the fallback for a box
our code can't handle. Test 5 on the same 5 real forms (Loop Keka, Swiggy SR, an
Ashby, a Greenhouse, a Lever; never submit): ours after fixes 1-2, ours + Stagehand
fallback, Stagehand alone, browser-use, Skyvern. Score: every value right, time,
cost, never pressed Submit.

## Borrowed from GitHub (researched 2026-10-01)

How the open-source fillers that work are built. Every one keeps the AI for the
odd question only; the common answers come from code.

| Idea | Who does it | What we take |
|---|---|---|
| One profile file with every standard answer | [ApplyPilot](https://github.com/Pickle-Pixel/ApplyPilot) `profile.json` (1.6k stars); [job-application-autofill](https://github.com/cyyecao-lappland/job-application-autofill) JSON template with a source per fact | fix 1 |
| Answers in a fixed order, AI last: contact → EEO → work permit → standard (salary, notice, "how did you hear") → education → saved answers → ask | [JobApplyAgent](https://github.com/shankswhite/JobApplyAgent) "layered answer resolver"; [Workday_Auto](https://github.com/ajithchandraapplywizz/Workday_Auto): profile → verified Q&A memory → resume facts → AI picks only from the options on the page | fix 2 |
| A filler per widget type (combobox, date, file, radio/checkbox, select) + per-site adapters; set values with the page framework's own setter and fire input events so React/Vue keep them | [FormFilla](https://github.com/AhmadHassan-BTed/FormFilla) | fix 3 |
| Fill, read back, re-scan for boxes that appear after an answer, then the next page | Workday_Auto ("scan → intent → evidence → validate → fill → verify → rescan → advance"); JobApplyAgent's 3 passes (fill, conditional reveal, "+ Add another" rows) | fixes 2, 3 |
| Match all boxes to all facts at once, not one by one, so two boxes never get the same fact | FormFilla (maximum-weight matching) | fix 3 |
| Drop hidden twins (Greenhouse's hidden input behind each dropdown was ~30% false fields) | JobApplyAgent | fix 3 (we did Greenhouse; do Keka) |
| Answer cache that learns: every question asked once is saved and reused when the wording is close | JobApplyAgent `form-answers-cache.json` | new: after fix 1 |
| Paid captcha solver (hCaptcha, reCAPTCHA, Turnstile); a captcha that can't be solved fails cleanly | ApplyPilot (CapSolver) | hCaptcha decision |
| "Filled is not applied": only a confirmed send counts | [job-application-agent](https://github.com/vaibhavarora14/job-application-agent) | done already |

What people say (Reddit, 2026): Browserbase sessions "just work" and its saved
logins + replays are its strength; gripes are the 1-minute billing floor and
confusing product names; nobody's stealth or captcha solving is guaranteed.
Site-specific autofill tools claim 90-95% accuracy
([Jobply](https://www.reddit.com/r/SideProject/comments/1uagq07/), Simplify),
while the best pure-AI browser agent scores 64.4% on WebBench
([Skyvern](https://github.com/Skyvern-AI/skyvern)). That gap is why fixes 1-3 are
code, not prompts.
