# Resume Plan

Written 2026-09-24. **Phases 1-2 built and tested 2026-09-24. Phase 7 (layouts) built the same day: 7 layouts, see README "Layouts". Phases 3-6, 8, 9 not built.**

Evidence: two research reports, 100+ sources between them.
- `research/resume/A_rules_structures.md`: career-office rules (Harvard, MIT, Stanford, Wharton, IIT Madras, IIM/SPJIMR/ISB), structures, bullets, tone, JD matching.
- `research/resume/B_ats_layouts.md`: how ATS tools really parse, keyword matching, 10 layouts rated, how to keep an uploaded format.

Every rule below has its source in those two files.

---

## 0. What you asked for, and where it is answered

| # | You asked | Section |
|---|---|---|
| 1 | On "apply", ask: master, initial or tailored? | §2 |
| 2 | No resume? Build one with them | §4 |
| 3 | 5-10 layouts, researched, suggested | §6 |
| 4 | Bare-minimum rules from Harvard, IIMs, SPJIMR, ISB | §8 |
| 5 | 3-4 structures by role and experience, and ask the user | §5 |
| 6 | ATS research and the best fixes | §10 |
| 7 | Must not sound like AI slop; use the no-ai-slop skill | §9 |
| 8 | Existing resume: keep its format and structure, offer two options | §3 |
| 9 | JD's top 5 things must appear in the first 50% | §11 |
| 10 | One page or two: your call | §7 |
| 11 | How my resume will look; rules; tones; how AI, slop and ATS fit | §12, §13 |

---

## 1. The whole thing in one paragraph

One **master resume** per person holds every verified fact: the uploaded PDF plus everything they told us since. From it the agent makes three things: the **initial** file they uploaded (sent untouched), the **master** rendered in their chosen layout, and a **tailored** version per job. Tailoring can only select, reorder and reword verified facts. That was already true; it stays true. Every generated resume then passes four checks that run in code, not in a prompt: **invention** (exists), **slop** (new), **ATS** (exists, made stricter) and **top-5 placement** (new). A check that fails reverts or blocks. It never ships quietly. The person sees text first, then the PDF.

Why checks in code: the same lesson three times on this project (password ask, ignored news line, "rephrase never invent"). *A prompt line gets talked around; a returned error does not.*

---

## 2. The "apply" question (your rule 1)

### The three choices, defined

| Choice | What goes out | Built by | Model call? |
|---|---|---|---|
| **Initial** | The exact file they uploaded, byte for byte | nothing | no |
| **Master** | Their full resume: upload + what they've told us since, in their layout, no job-specific changes | `resume/generate.py` (exists) | no |
| **Tailored** | Master, re-selected and re-ordered for this JD | `resume/tailor.py` (exists, improved) | yes, one call |

Today the code has two modes (`existing`, `tailored`). It needs a third, `master`. The tool exists (`build_resume`) but apply can't send it.

### When it is asked

Your rule says **every time they say "apply"**. Today it is asked once and saved forever (`resume_mode` in the vault). Proposed middle path:

- Ask on every apply, **with the last answer as the default**: *"Which resume for this one: initial, master, or tailored? (Last time: tailored.)"* One tap or one word.
- **"Apply to all 5" asks once for the batch**, not five times.
- If there is no resume at all, don't ask. Go to §4.

→ **Decision for you (D1):** ask every time as above, or ask every time with no default?

### Fix that comes with it

Tailored today starts from `master.json` (the parsed PDF only) and gets the chat facts only as allowed numbers. It should start from **generate's output** (PDF + chat facts), so a fact they told us on Tuesday can be *selected*, not only permitted. One line in `tailor.build`.

---

## 3. They already have a resume (your rule 8)

**Two options, offered in one message**, after parsing:

> "I've read your resume. Two ways I can work with it:
> **1. Keep your format.** Same sections, same order, same look as closely as I can.
> **2. Switch to one of my layouts.** I'll suggest two that suit [role] at [N] years.
> Either way your words stay yours. I'll only flag lines that hurt you."

### What "keep your format" can honestly mean

| Upload | What we do | Why |
|---|---|---|
| **DOCX, clean** (one column, no tables or text boxes, nothing in header/footer) | Edit their file in place: swap text inside existing paragraphs, clone bullet paragraphs to add one | Pixel-true. python-docx can check cleanliness first |
| **DOCX, not clean** | Same as PDF below, and say why: "your file uses two columns, which ATS tools scramble" | Keeping it would keep their ATS problem |
| **PDF** | Read their layout (fonts, sizes, section order, date style, accent colour) with pdfplumber, map to our **closest layout**, keep their **section order and section names** | Editing a PDF in place breaks reading order, the exact thing the ATS gate checks. Research B, §5 |

**Not built:** editing text inside a PDF. The text doesn't reflow, the embedded font usually can't be reused, and new text is read last.

Honest wording for PDFs: "Same structure and a very close look; not pixel-identical."

### Things we flag in their resume, but never change without asking

Their original lines are their voice (no-ai-slop: *preserve the writer's voice, minimum effective edit*). The checks in §9-§10 **report** on original text; they only **enforce** on text the model wrote.

---

## 4. They have no resume: build one together (your rule 2)

A short interview, not a form. The 200-form autofill measurement from the Job SaaS build: asking more questions bought 3 points, generating from what they already said bought 26. So: **ask the least, dig on numbers**.

| Step | Ask | Why |
|---|---|---|
| 1 | "What role are you going for?" (often known from search) | Picks structure + role conventions |
| 2 | "Where do you work now, and since when? Before that?" (company, title, month-year) | The 6 fields recruiters read first (Ladders) |
| 3 | Per role, newest first: "What did you own there? What changed because of you?" | One open question gets more than a checklist |
| 4 | Follow-ups **only for numbers**: "Roughly how many users? What % or ₹? Before vs after?" Ranges and "~" accepted | Every bullet needs a result or a scale (Wharton). We never invent the number, so we must ask for it |
| 5 | Education: school, degree, year; score only if they're < 2 yrs out (exact, never rounded: IIT-M) | Fresher structure leads with it |
| 6 | Skills/tools: "What tools do you use every week?" | Skills line, ATS keywords |
| 7 | Show the draft as text. "Could you talk about each line for 2 minutes?" Remove any they hesitate on | IIM "2-minute" rule; interview defensibility |

Every answer goes through the existing `career.capture` → career store with `source="chat"` and their raw sentence in `heard_in`. So every number on the finished resume can answer "where did this come from?" with their own words. That is already built; the interview only feeds it.

Output: a `master.json` built from the store, then they pick a structure (§5) and layout (§6).

Stopping rule: after step 4 for the latest role, offer a draft. Don't interrogate all five jobs before showing anything.

---

## 5. Structures: what order the sections go in (your rule 5)

The agent **recommends one and asks**: "You're at 5 years in growth, so I'd lead with experience and put a 2-line summary on top. OK, or would you rather lead with projects?" Saved as `resume_structure`.

| # | Structure | Who | Section order |
|---|---|---|---|
| **A** | **Fresher** | Student, 0-2 yrs, campus placement | Header → **Education** (exact CGPA, class 12/10 for India) → Internships/Work → Projects → Positions of Responsibility → Achievements → Skills |
| **B** | **Early career** | 2-6 yrs | Header with target title → 2-line summary (optional) → **Experience** → Projects (builders only) → Education → Skills |
| **C** | **Senior** | 6-15+ yrs | Header → 3-4 line summary with 2 proof numbers → **Experience** (last 10-15 yrs in detail, older compressed into "Earlier career") → Education (no grades) → Skills/Certifications |
| **D** | **Switcher** (hybrid) | Changing field: eng→PM, ops→product, consulting→PM | Header → summary naming the **new** role → **evidence for the new field** (projects, certifications, relevant skills) → full dated Experience, bullets reframed to transferable work → Education |

Plus one variant, **E (Builder)**: projects above experience, only when projects are stronger evidence for the JD than jobs (self-taught dev, indie builder going for a founding role). Experience with dates still appears.

**How the agent picks the default:**
- Years: computed from their dates, not asked.
- Switcher: the JD's role family (already labelled per job) differs from their last two titles' family.
- Builder: they have ≥ 2 projects with links and fewer than 2 years in the target field.

**Never built:** a purely functional (skills-only, no timeline) resume. Recruiters read it as "what are they hiding?" (Jobscan).

**Per-JD nudge:** inside a structure, section order can still shift for one job (Harvard: "order by importance to the role"). E.g. Skills goes to the top for a tech/data JD and to the bottom for a business JD.

### Role conventions (applied on top of any structure)

| Role | What every bullet should carry |
|---|---|
| Product | Product metric (activation, retention, revenue) + scale (users, GMV) |
| Engineering | What was built + scale/perf; GitHub links; honest skills list |
| Data / ML | Model metric **paired with** a business metric |
| Design | Portfolio URL written out in the header |
| Sales | % of quota, quota size, period, rank ("4th of 31") |
| Growth / Marketing | Channel + experiment + KPI (CAC, conversion, LTV) |
| Consulting / Finance | 1 page, 2-4 bullets per role, outcome or scope |
| Operations | Cost, turnaround, error rate; team size and volume |

---

## 6. Layouts: what it looks like (your rule 3)

**7 layouts, all single column.** Research B looked at 10. The three dropped (Deedy two-column, AltaCV sidebar, the real IIM table grid) all hit Greenhouse's official "failed parse" list: columns, tables, icons.

| # | Layout | Look | Suggest for |
|---|---|---|---|
| 1 | **Charter Professional** (today's, tidied) | Charter 10.5pt, bold company, italic title, rule under headings | Default for experienced people, any function |
| 2 | **Tech Compact** (Jake's-style) | Computer Modern, small-caps headings, dense, company left / dates right | Engineering, data, tech PM, early career |
| 3 | **Harvard Classic** | Serif, name centred, all-caps headings, very conservative | Consulting, finance, business, MBA |
| 4 | **Modern Accent** | Sans (Source Sans 3), **one** accent colour on name + headings, no icons | Product, growth, marketing, design-adjacent |
| 5 | **Indian Placement** (text version) | Education-first with scores, PoR section, looks like an IIM CV but **no tables** | Freshers, MBA campus, Indian BFSI/FMCG/consulting |
| 6 | **Executive Two-Page** | Serif 11pt, wider margins, core-competencies line, name repeated at top of page 2 in the body | 12+ years, director/VP |
| 7 | **Academic CV** | Multi-page, publications in citation style | Research, PhD, faculty |

Colour and serif-vs-sans don't change what the ATS reads (Research B, "myths"). **Structure and layout are separate choices**: structure = section order, layout = the look. Any structure renders in any layout.

**How it's offered:** the agent suggests **two** (by role + years) and shows each as a small image **made from their own data**, not a sample person. That's a PNG of page 1 via `pdftoppm`, ~1 s each. Asking someone to pick from seven names they can't see is a bad question.

**How it's built** (RenderCV, an open-source CV tool, does exactly this): one JSON, shared LaTeX macros, and each layout = a small set of parameters (font, size, margins, heading style, accent colour, date position). Why: one render path means **one ATS gate covers all seven**. Seven hand-written templates would drift apart.

→ **Decision (D2):** all 7 now, or the first 4 now (covers ~90% of your users: product/growth/tech/business) and 5-7 later?

### Specs for the first 4 (decided D2: these 4 now)

Ranges come from Research B §4. The default is the value we build with.

| Layout | Font | Body size | Margins | Headings | Other |
|---|---|---|---|---|---|
| **Charter Professional** | XCharter | 10.5pt | **0.5in** (was 0.45) | Rule under each heading | Bold company, italic title |
| **Tech Compact** | Latin Modern | 10.5pt (9.5 is the floor, this layout only) | **0.5in** | Small caps with a rule | Company left, dates right on the same line; no colour |
| **Harvard Classic** | EB Garamond | 11pt | **0.75in** (range 0.6-0.75) | All caps with a rule | Name centred; no colour |
| **Modern Accent** | Source Sans 3 | 10.5pt | **0.6in** (range 0.6-0.75) | Accent colour with a thin rule | One accent colour on name + headings; no icons |

All four:
- Single column.
- Page size A4 for India, Letter for US.
- Body text never below 10pt (Tech Compact 9.5) and nothing below 8pt. Cut content to fit; never shrink the font.
- `Ligatures=NoCommon` (the AppsFlyer fix).
- URLs written out as visible text.
- Dates `Mon YYYY`, right-aligned.
- Nothing in the header or footer.

---

## 7. One page or two (your rule 10: I've decided)

| Experience | Pages |
|---|---|
| Student / 0-2 yrs | **1, hard.** Indian campus is always 1 |
| 3-7 yrs | **1** by default. 2 only if page 2 would be at least half full of *relevant* content |
| 8-15 yrs | **1 or 2**; 2 is fine |
| 15+ yrs | **2 max**, older roles compressed. (Academic CV exempt) |
| Any, applying to UK/EU | 2 is normal |

**Never pad to fill page 2.** The one study that found 2 pages preferred (ResumeGo, 482 recruiters) got that result from more *relevant* detail, not more words.

**Enforced in code:** the renderer knows the page count. Over the limit → drop the lowest-ranked bullets from the oldest role and re-render, and say what was cut. Don't shrink the font below 10pt to fit.

---

## 8. The rulebook (your rule 4: bare minimum, where all the schools agree)

Harvard, MIT, Stanford, Wharton, IIT Madras and the IIM/SPJIMR student guides agree on these. Each is tagged by **who enforces it**: `code` = a check that blocks or reverts, `prompt` = told to the model, `ask` = asked of the person.

### Truth
1. **Never invent** a number, name, tool, title or date. `code` (verify.py, exists)
2. **One verified master; tailored versions only select, reorder, reword.** (IIT-M's Master + 5 F-resumes model.) `code`
3. **Scores exact, never rounded** ("7.99 is not 8"). Unfinished things labelled ("expected May 2027", "[course project]"). `code` (numbers can't change) + `prompt`
4. **Could you talk about this for 2 minutes?** `ask`, on the first build only

### Content
5. Reverse chronological, inside every section. `code`
6. Every bullet starts with an action verb. No "I", no "responsible for", "helped with", "responsibilities included". `code`
7. Every bullet shows a **result**. If there is none, it shows **scale**. `prompt` + `ask` for numbers
8. Pair a % with an absolute where they gave one: "12% (₹1.2 Cr)". `prompt`
9. Bullet length **12-30 words**, max 2 lines. `code` (warn)
10. Bullets per role: newest 4-6, previous 3-4, older 1-2. `code` (tailor selects to this)
11. Tense: present for ongoing work in the current role, past elsewhere. `prompt`
12. Summary: freshers none (or one-line headline); 2-6 yrs optional, 2 lines; 6+ yrs or switchers 2-4 lines with at least one number. No personality adjectives. `code` (length, number present) + `prompt`
13. One date style (`Mon YYYY`), one currency style, one punctuation style. `code`

### Never on the resume
14. Photo, DOB/age, gender, marital status, religion, caste, father's name, "I hereby declare", signature, CTC, references, "Objective", full street address, work email. `code` (field allow-list: these fields don't exist in the JSON, so they can't render)
    India exception: only if a specific employer or government form asks. The DPDP Act 2023 (India's data-protection law) pushes employers toward collecting less, which supports leaving these out.

---

## 9. Tone and no-AI-slop (your rule 7)

### Why this matters, with numbers
- About **half of hiring managers** reject resumes that read as AI-written: 49% (Resume Genius, June 2026, 1,500 managers); 49% dismiss them automatically (Resume.io, 3,000).
- **No major ATS runs an AI detector** (Workday, Greenhouse, Lever, iCIMS, via Jobscan). The risk is the human reader.
- Polishing helps: grammar and clarity help raised hires 8% in a 480k-person experiment (NBER). **Generic** text is what hurts. Once AI can tailor anything, only **specific, checkable facts** still signal (Galdin & Silbert).

So the target is not "sound less AI". It is **every line carries a specific fact that came from the person**, in plain words.

### Tone, in one line
Harvard's: *written to express, not impress.* Specific over general, active, fact-based, no adjectives about yourself.

### How the no-ai-slop skill comes in: three places

| Where | What | Enforced by |
|---|---|---|
| **1. The tailor prompt** | The skill's rules condensed for resumes: portability test ("could this line sit on someone else's resume unchanged? then it's filler"), protect the specific fact, verbs do the work, no puffery | prompt |
| **2. `resume/slop.py` (new)** | A check with no model, like `verify.py`. It runs on every line the model wrote | code |
| **3. Detect mode on uploads** | Their original resume gets a report (the skill's "Detect" job: name the pattern, quote the line, say the fix). **Not rewritten** unless they say yes | code + ask |

**What `slop.py` checks** (lists from Research A §5 + the skill):

| Check | Rule | On fail |
|---|---|---|
| Banned words | spearheaded, leveraged, orchestrated, utilize, synergy, results-driven, passionate, dynamic, delve, robust, tapestry, testament, go-getter, team player, detail-oriented, self-starter, proven track record, think outside the box, … | model's line → one retry, then revert to their original + note |
| Soft words | foster, enhance, showcase, pivotal, crucial, key, cross-functional | warn; allowed when literally true ("cross-functional team of 6: eng, design, ops") |
| Filler (Wharton) | responsible for, helped, various, multiple, very, extremely | revert |
| Pronouns | I, me, my, we | revert |
| Em dashes | none in bullets or summary (your rule for client copy) | replace with comma/period mechanically |
| Verb repeats | same first verb > 2 times, or on two bullets in a row | warn to the model on retry |
| Robotic rhythm | > 50% of bullets share one shape, or all within ±3 words of the same length | warn |
| Binary contrast / colon reveal / "-ing" tails | "not just X but Y", "highlighting…", "showcasing…" | revert |

### A correction to what I said earlier
I said `verify.py` "quietly lets through" spearheaded and orchestrated. More precisely: they're on its list of words that are **not names**, which is correct. Otherwise every "Spearheaded …" bullet would be reverted as an invented entity. The invention check was never meant to judge tone. The gap is that **nothing else** does, which is what `slop.py` fills.

### Evidence from your own master resume (measured today)
- Summary uses **"I"** ("I build and ship AI agents"). Harvard, MIT and Wharton all say no pronouns.
- Summary ends in abstraction: "turning data and experimentation into durable levers". That fails the portability test; it fits anyone.
- **"Built"** opens 4 of 13 bullets (the rule is at most 2).
- "driving" ×3, "drive" ×2, "orchestrating" ×1.
- Bullet length is fine: 19-34 words, average 25, none over 35.

---

## 10. ATS (your rule 6)

### What's true (Research B)
- **"75% of resumes are auto-rejected by ATS" is made up.** It comes from a 2012 sales pitch by a company that closed in 2013. 92% of recruiters (Enhancv, 2025) say their ATS does not auto-reject on format. The real automatic filters are **knockout questions** (work authorisation, location, notice), which our apply agent already handles on forms.
- What actually goes wrong: the resume **parses badly** (wrong dates on the wrong job), so it **ranks lower or never shows up in a recruiter's keyword search.**
- Search is often **literal**. Taleo's default is exact-term AND; Lever doesn't expand acronyms. So write "Search Engine Optimization (SEO)" once, and put the target title in the headline (resumes with the JD's title got 10.6× more interviews in Jobscan's data; correlation, not proof).

### A real bug found today
The PDFs the app sends now turn **AppsFlyer into "Appsﬂyer"**: "fl" comes out as one merged symbol (U+FB02) in all three `pdftotext` modes. A recruiter searching "AppsFlyer" misses you. The current gate doesn't look for it. Fix: turn off common ligatures in the template, then add the check below so it can't come back.

### The ATS gate, upgraded (render.py)
Kept: text layer exists, company/dates/email survive, roles in order. **Added**:

| # | Check | How |
|---|---|---|
| 1 | Order survives **all three** read modes (`-layout`, default, `-raw`); each date stays within 2 lines of its company | pdftotext ×3 |
| 2 | **No merged-letter or junk characters** (U+FB00-FB06, private-use icons, U+FFFD) | regex on extracted text |
| 3 | Every token also found **without** normalising (catches the AppsFlyer case) | compare raw vs NFKC |
| 4 | Nothing in header/footer zones | pdfplumber positions |
| 5 | Single column (only the right-aligned date may share a line) | pdfplumber word x-positions |
| 6 | No tables, images, text boxes | pdfplumber |
| 7 | **No hidden text** (white, < 6pt, off-page). Also **stripped from uploads**: a Duke study found hidden injected text in 1%+ of 200k resumes, up 7× in 16 months | pdfplumber colour/size |
| 8 | Fonts embedded with Unicode maps | `pdffonts` (today's PDFs pass) |
| 9 | Standard headings only (Summary, Experience, Education, Skills, Projects, Certifications, Positions of Responsibility, Achievements…) | allow-list |
| 10 | One date format, reverse chronological, start ≤ end | regex on JSON |
| 11 | URLs visible as text (`linkedin.com/in/…`), not only behind the word "LinkedIn" | compare link annotations vs text |
| 12 | File < 1 MB, page count within §7, A4 for India / Letter for US | `pdfinfo` |
| 13 | File name `Firstname_Lastname_Resume.pdf` (today: `BUGATA_SAI_SWAROOP_Paytm_Growth_Manager_Postpaid.pdf`, which is fine to keep internally but not what gets uploaded) | regex |
| 14 | PDF metadata Title/Author = their name | `pdfinfo` |

### Keyword match: a report, not a score to chase
- Required JD skills (counted 2×) + preferred skills + title, found in the **extracted** text, each backed by a bullet, not only the skills list.
- Target **75-85%**. **≥ 90% → "possible stuffing" warning**, not praise (Jobscan's own advice).
- A keyword with no real bullet behind it is **never added**. It's reported as a gap.
- Acronyms: full form once where the JD uses it.

---

## 11. The JD's top 5 in the first 50% (your rule 9)

This one is new, and it's the rule that most changes what gets built.

### Step 1: find the top 5 (one small model call, JSON out)
Score every requirement in the JD:

| Signal | Points |
|---|---|
| In the job title, or first 2 bullets of responsibilities/requirements | +3 |
| Under "Required / Must have / Minimum", or a years/degree/cert item | +2 |
| Each extra time it's repeated across sections | +2 |
| Hard skill or tool | +1 |
| Soft skill ("communication") | 0 |
| Under "Nice to have / Preferred / Bonus" | −2 |

Take the top 5. Cached per job (`content_hash`), like labels: paid once, reused for every user who applies to that job.

### Step 2: match each to their verified facts
- **Matched**: a real bullet or skill proves it → must be placed in the top half.
- **Adjacent**: honest reframing ("wrote SQL for weekly cohort reports" → SQL).
- **Missing** → **never written in.** Told to them: "The JD's #3 is Kafka. Nothing on your record shows it. If you've used it, tell me where and I'll add it."

The model proposes the match *and quotes the bullet*; code checks the quote really exists in the master. Same mechanism as provenance.

### Step 3: place them (honest moves only)
In this order, because this is where eyes land (Ladders: ~80% of the 6-7 seconds goes to name, current title/company, dates, previous role, education):
1. **Headline** under the name: the JD's title wording if their real title is equivalent. Never a fake job title.
2. **Summary**: names 2-3 of the matched items, with a number.
3. **First 2 bullets of the latest role**: reordered so they map to top-5 items #1 and #2.
4. **Skills line**: matched hard skills in the JD's order.

### Step 4: check in code, after rendering
`resume/topfive.py` (new) finds where each matched item's text lands on the **rendered PDF** (pdfplumber gives the y-position). **"First 50%" = the top half of page 1**, for 1-page and 2-page resumes alike, because that's what gets read. A matched item below the line → re-order and re-render once → if still below, block and say why.

**Why a position check and not a prompt:** the model can say "I front-loaded it" while a long summary pushes it to line 40. Only the PDF knows.

---

## 12. How it all fits together

```
                 ┌──────────── "apply" ────────────┐
                 │ ask: initial / master / tailored │   (§2)
                 └───────┬─────────┬────────────────┘
          initial ───────┘         │ master / tailored
     (their file, as-is)           ▼
                          career store (PDF + chat facts)        ← §4 interview feeds this
                                   │
              tailored only:  JD → top-5 (cached per job)        ← §11 step 1-2
                                   │
                          TAILOR  (one model call: select, order, reword,
                                   summary, headline, section order)
                                   │
                 ┌─────────────────┼──────────────────┐
                 ▼                 ▼                  ▼
            verify.py          slop.py           rules (§8)       ← all code, no model
         (invented fact?)   (AI-sounding?)    (pronouns, dates…)
          → revert line     → retry once,     → revert / warn
                              then revert
                                   │
                          RENDER  (their structure §5 × layout §6)
                                   │
                 ┌─────────────────┴──────────────────┐
                 ▼                                    ▼
            ATS gate (§10)                    top-5 position (§11)
          → block, say why                  → re-order once, then block
                                   │
                  PREVIEW as text + "what didn't make it in" + gaps
                                   │
                                  PDF
```

**Where AI is used:** two model calls per tailored resume (top-5 extraction, cached per job; tailoring). Everything that decides whether a line may ship is code.

---

## 13. What your resume would look like

Measured from your `master.json`: 3 roles, May 2021-now = **~5 years** in growth/product.

| Choice | Default for you | Why |
|---|---|---|
| Structure | **B, early career** (close to C) | 5 yrs; Experience first; 2-line summary |
| Pages | **1** | 3-7 yrs rule; you have 13 bullets, and a tailored one keeps ~9-10 |
| Layout | **Charter Professional** (your current look), margins 0.45 → 0.5in | Your existing preference file; 0.45 is the tightest in any guide |
| Alternative offered | **Modern Accent** | Product/growth |

Top to bottom:
```
SAI SWAROOP BUGATA
Growth Product Manager                        ← headline = JD title if equivalent (new)
Bengaluru · email · phone · linkedin.com/in/bugata-sai-swaroop   ← URL visible (new)

SUMMARY (2 lines, no "I", ≥1 number, names 2-3 of the JD's top 5)
EXPERIENCE
  Lenskart  ·  Growth Manager, CRM & Revenue  ·  Apr 2026 – Present
    4 bullets, first two = JD's top-5 #1 and #2
  Paytm First Games  ·  Growth Product Manager  ·  Jan 2023 – Jun 2025
    4 bullets (of 8), chosen per JD
  Simply Tax (SaaS)  ·  Growth Marketing Manager  ·  May 2021 – Dec 2022
    1-2 bullets
EDUCATION   (3 lines, no scores: past the 2-year mark)
SKILLS      (bottom: business role; matched tools in JD order)
```
Changes to your text the checks would **flag, not force** (it's your original text, §3): the "I" in the summary, "durable levers", "Built" ×4.

One thing a recruiter will notice, flagged for you to decide rather than for the agent: **Jun 2025 → Apr 2026 is a 9-month gap.** Leave it, or say something true about it in one line (the Job Agent MVP is a real, shipped project and would fit a Projects line). Your call; the agent will never fill a gap on its own.

---

## 14. Build order

Each step ships with tests (fixtures only, no real data, no network), and each is useful even if the next never happens.

| Phase | What | Files | Size |
|---|---|---|---|
| **1. Fix what's wrong now** | Ligature fix + AppsFlyer regression test; margins 0.5; contact line shows every link the person has (LinkedIn, portfolio/website, GitHub, X) as visible text, and skips any that's missing; upload file name | `template.tex` (now `resume/layouts/base.tex`), `render.py`, `parse.py` | small |
| **2. Three modes on apply** | `master` mode; ask per apply with default; batch asks once; tailor starts from generate's output | `chat.py`, `prompts/chat.md`, `tailor.py` | small |
| **3. slop.py** | Word lists, filler, pronouns, em dashes, verb repeats, rhythm; retry once then revert; detect-report for uploads | new `resume/slop.py`, `tailor.py`, `prompts/tailor.md` | medium |
| **4. Top 5** | Extract + cache per job; match with quotes; place; position check on the PDF | new `resume/topfive.py`, `prompts/topfive.md`, `render.py` | medium |
| **5. ATS gate upgrade** | Checks 1-14 in §10; hidden-text strip on upload | `render.py`, `parse.py` | medium |
| **6. Structures** | A-E section orders; auto-pick + ask; schema gains `headline`, `projects`, `positions`, `achievements`, `certifications`, `section_order` | `render.py`, `parse.py`, `chat.py` | medium |
| **7. Layouts** ✅ built 2026-09-24 | 7 layouts over one template (charter, tech_compact, harvard, modern_accent, indian_placement, executive, engineering_classic); PNG previews of their own data; chat picks/saves/suggests. Body size now set exactly (was silently 10pt). Found on the way: XCharter drops ₹ (fixed with a fallback font + a gate check), hyphenation split keywords (off), education rows ran together, a trailing \\ made a blank page 2. Source Sans 3 not in tectonic; Source Sans Pro and Lato extract "-" as U+2011/U+2010, so Modern Accent uses Fira Sans. Executive does not repeat the name on page 2 (needs a header) | `resume/layouts/`, `render.py`, `generate.py`, `tailor.py`, `chat.py` | large |
| **8. Build-from-scratch interview** | §4 flow on top of `career.capture` | `chat.py`, `prompts/chat.md` | medium |
| **9. Keep-their-format** | DOCX in-place for clean files; PDF → closest layout + their section order | new `resume/docx_edit.py`, `resume/detect_layout.py` | large |

Phases 1-5 make every resume we send **better and safer** without changing what the person sees. Phases 6-9 add **choice**. Recommended order is as listed. Phase 1 is a real bug in resumes we already send.

**Proof for each phase:** a before/after on your own master (text + PDF), and a scenario-harness run showing the chat asking the right question at the right time. Read the trajectory, not the final answer.

---

## 15. Decisions for you

| # | Question | My recommendation |
|---|---|---|
| D1 | On every "apply": ask with last answer as default, or ask cold? | With default. Batch = one question |
| D2 | 7 layouts now, or 4 now + 3 later? | 4 now (Charter, Tech Compact, Harvard, Modern Accent) |
| D3 | Offer DOCX as a second download? | Later. No primary evidence Naukri needs it |
| D4 | Some Indian campuses (e.g. IIMS Pune) require a photo | Only if the person says their college asks. Never default |
| D5 | Build-from-scratch: how many roles before showing a draft? | After the latest role; add older ones after |
| D6 | Slop words in **their own** original bullets: flag only, or offer a one-tap rewrite? | Flag + offer; never silently change |
| D7 | Start with Phase 1-2 this session? | Yes: a live bug + your rule 1 |

### Sai's answers (2026-09-24)
- **D1 yes:** ask on every apply, last answer as the default, one question per batch.
- **D2 yes:** 4 layouts now (specs in §6), 3 later.
- **D3 yes:** DOCX download later, not now.
- **D4 dropped:** no photo support at all.
- **D5 changed:** two ways in. (a) Build from scratch and show a draft after the latest role, or (b) start from an existing resume they already have and edit it. Offer both.
- **D6 yes:** flag slop in their own lines and offer a rewrite; never change it silently.
- **D7 yes:** start with Phases 1-2.
- **"Master" is not a send option (Sai, later that day).** Nobody applies with everything. The master is the record behind the scenes; the three choices are **your file / updated / tailored**. "Updated" = file + facts told since, fitted to the page. Nothing new since upload → ask only "your file or tailored?". Built.

### Added by Sai (2026-09-24, same session)
- **India first.** Every default is Indian: A4, ₹ lakh/crore, Indian campus conventions (exact CGPA, class 10/12 for freshers), no biodata fields. **Indian Placement moves into the first batch → 5 layouts now** (Charter, Tech Compact, Harvard, Modern Accent, Indian Placement). Other countries only change page size, page count, currency and a work-authorisation line; no per-country research for now.
- **The person can change anything by asking in chat:** layout, section order, a line, page count, links. The agent does what they ask.
- **When a request breaks a rule, the agent says so once, with the reason and a better option, then follows their choice.** Example: "Rounding 7.99 to 8 is the one thing IIT/IIM placement checks catch. Keep 7.99?" The only hard stop is invention: no fact, number, title or date that isn't on their record. Hidden text is also never generated.
- Links: portfolio/website, LinkedIn, GitHub and X shown as visible URLs when present (Phase 1).

---

## Not in this plan
- **Cover letters.** Still not built. They'd reuse top-5 + slop + invention checks unchanged, which is one reason to build those as separate files.
- Naukri profile headline (separate field on Naukri, not the resume).
- WhatsApp formatting of the layout choice (interactive list message; comes with the WhatsApp adapter).
