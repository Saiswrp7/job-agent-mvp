# Job Engine Plan: latest jobs we can apply to

> **Superseded 2026-09-29** by the engine that was built (one loop, no phases):
> see `fetch.md` § "The engine". Kept for the research tables.

Written 2026-09-28. Extends `fetch.md` (the design stays: cron fills a table,
search never goes online). This file is about *supply*: which rows get in.

## The goal, as one number

**Usable job** = open, in India (or remote-India), posted in the last 14 days,
full description, and an apply link that lands on a form our cloud browser can
fill. Track it per role x city with `cli.py supply`.

Targets for launch: every role x city a real user asks for has **50+ usable
jobs**, and the newest one is **under 24 hours old**.

## Where we are (measured 2026-09-28)

| Source | Open | In India | Full JD | We can apply |
|---|---|---|---|---|
| Greenhouse / Lever / Ashby / SmartRecruiters (180 boards) | 18,792 | ~6,100 | yes | yes |
| Adzuna | 13,391 | 13,391 | no (snippet) | unknown (never tested) |
| LinkedIn | 210 | 209 | yes | no (sign-in wall) |

So ~6,000 usable, mostly SDE. PM and growth are short because Indian startups
don't use Greenhouse/Lever much; they sit on Keka, Darwinbox, Zoho, Workday,
and are *seen* by us only through Adzuna and LinkedIn.

Why the Traya search returned only LinkedIn links: search sorts by fit, then
"owns revenue", then date. Nothing in it knows whether we can apply. Adzuna rows
have no full JD, so they never get the "owns revenue" label and always sink.

## The one idea

**Aggregators find jobs; employer boards are where we apply.** Adzuna, LinkedIn
and Google Jobs tell us *which companies are hiring for what*. We then read that
company's own board (full JD, real apply link), and the aggregator copy is
closed as a duplicate. `discover.py` + `close_superseded` already do this for
four ATSs. The plan widens it to the ATSs Indian companies actually use.

Every row gets `apply_kind`:
- `form`: employer ATS page we can fill (Greenhouse, Lever, Ashby, SmartRecruiters, Workday, Keka, ...)
- `link`: we send the link + tailored resume; they apply themselves (LinkedIn, Naukri, unresolved Adzuna)

Search orders `form` first. `link` rows only fill gaps and are labelled
"link only, apply yourself". The apply tool refuses `link` rows.

## Research findings (what each source can really give)

| Source | Access | Full JD | Apply link | Cost | Verdict |
|---|---|---|---|---|---|
| **Workday** (`*.myworkdayjobs.com`) | public JSON (`/wday/cxs/{tenant}/{site}/jobs`, POST) | yes (per-job GET) | yes, employer | free | **Build.** Tested: Accenture returns 2,000 India jobs, no auth. Covers most MNCs that fill Adzuna (Accenture, PwC, JLL, State Street, GE Vernova, ABB...). |
| **Keka** (`{co}.keka.com`) | public JSON (`/careers/api/embedjobs/default/active/{guid}`) | yes | yes, employer | free | **Build.** Tested: full HTML JD returned. Needs each company's guid, read from its careers page. Keka is the top HRMS for Indian mid-size tech. |
| **Darwinbox** (`{co}.darwinbox.in/ms/candidate/careers`) | page is an app; its API sits behind Cloudflare | via browser | yes, employer | Browserbase minutes | **Later**, in the cron, with the cloud browser. Enterprise + funded startups (smallcase, Arvind). |
| **Zoho Recruit**, Workable, Recruitee | public career pages / widgets | yes | yes | free | Build after Keka if discovery shows enough companies. Workable returned 429 earlier: slow down, don't drop. |
| **Adzuna** | API: 500-char snippet + `redirect_url` | no | behind Adzuna's redirect | free tier 2,500 calls/mo | **Signal.** Use the company names (4,371 employers) to discover boards. Their `redirect_url` exists to send a real user to the ad, so a cloud browser opening it for our user is the intended use; lifting their page text is not (their terms). Step 0 decides if the redirect route is worth building. |
| **Google Jobs** via SerpApi | API, `apply_options` = every apply link incl. employer site | yes | yes, often employer | free 250/mo, $25 per 1,000 searches | **Test.** Could be the best "latest jobs in India" feed, since Google indexes employer career pages directly. Measure in Phase 0. |
| **LinkedIn** via Apify | actor, ~$0.50 per 1,000 jobs | yes | usually empty (behind login) | cheap | **Signal only.** Feed company names to discovery. Never an apply target. |
| **Naukri** via Apify | ~$2.40-3.20 per 1,000 | yes | Naukri login | ok for PM/growth only | **Link only**, and only if users ask for Naukri roles. |
| Fantastic.jobs (Active Jobs DB) | API, 200k career sites, 54 ATSs, hourly | yes | employer | from $1 per 1,000 jobs | **Buy-instead option.** Global-leaning; India coverage unverified. Trial it in Phase 0 against our own numbers before building Phase 4. |

How the big players do it (Google Jobs, hiring.cafe, Fantastic.jobs): crawl
employer ATS boards and `JobPosting` JSON-LD on career pages directly, refresh
hourly, dedupe, and send the user to the employer. That's the same shape as
this plan; the difference is only how many ATSs and companies are covered.

## Phases (each ends with a number, and the next starts only if it's met)

### Phase 0: measure before building (about 1 day, ~$0)
1. **Step 0, Adzuna redirect**: open 20 Adzuna growth/PM links in Browserbase, record where each lands: employer form / employer ATS page / Naukri or other board / login / dead. Never submit. Gate: build the resolver only if 30%+ land on a form.
2. **Google Jobs**: 20 SerpApi searches (free tier) for real asks (growth Blr, PM Mumbai, SDE Pune, sales Delhi). Count results with an employer apply link, and how fresh. Gate: adopt if 40%+ are employer links and most are under 7 days old.
3. **Fantastic.jobs trial**: same 20 asks. If it already gives 50+ usable per ask in India, buying beats building Phase 2-4.
4. **ATS census**: for the top 300 Adzuna + LinkedIn employers (by job count), detect which ATS their careers page links to. This tells us which adapters are worth writing, in order.
5. `cli.py supply` gains the `apply_kind` split so every later phase is measured the same way.

### Phase 1: stop showing jobs we can't apply to (2 days)
1. `apply_kind` column, filled at sync from source + apply-link host.
2. Search: `ORDER BY apply_kind='form' DESC` before fit; ranker prompt says prefer `form`; `link` rows labelled in the reply.
3. Apply tool refuses `link` rows with "here's the link and your tailored resume" instead of starting a run (the Traya failure).
4. Seniority drops only when we know the user's years (no resume = no drop).
5. Hourly `launchd` sync + `sync_runs` table (start, end, rows added/closed per source, errors), so "latest" is provable.

Done when: the Traya search returns form jobs first, and `sync_runs` shows 24 runs a day.

### Phase 2: the ATSs India uses (4-5 days, free)
1. **Workday adapter** (list + detail, polite: 1 tenant at a time, pause, stop on 429).
2. **Keka adapter** (guid from careers page, stored in `boards.json`).
3. **Discovery v2**: for each employer name from Adzuna/LinkedIn/Google Jobs, find the careers page, read which ATS it links to (host match: `myworkdayjobs.com`, `keka.com`, `darwinbox`, `zohorecruit`, `greenhouse`, `lever`, `ashby`, `smartrecruiters`, `workable`), add the board. Weekly, logged to JSONL like `discover.py` does today.
4. Dedupe across sources on normalised company + title + city; keep the `form` row, close the aggregator copy.

Done when: PM Blr and growth Blr each have 50+ usable (today 10 and 8).

### Phase 3: fill the gaps with signals (3 days, ~$25-50/month)
Only the ones Phase 0 passed:
1. Google Jobs daily for each saved search (role x city); employer links become rows, companies feed discovery.
2. Adzuna resolver: for jobs a user actually picks, the cloud browser follows `redirect_url`, records the final URL; if it's a known ATS, we apply there and add the board.
3. Apify LinkedIn (PM/growth, daily): company names to discovery, rows stay `link`.

### Phase 4: career pages with no ATS API (later)
Darwinbox, Zoho, custom sites: a reader in the cron (cloud browser, `JobPosting` JSON-LD first, LLM last), only for companies users' searches keep hitting. Skip entirely if Fantastic.jobs covers them.

## Cost at 10 users

| Item | Monthly |
|---|---|
| Board adapters (GH, Lever, Ashby, SR, Workday, Keka) | $0 |
| Adzuna free tier | $0 |
| SerpApi Google Jobs (1,000 searches) | $25 |
| Apify LinkedIn (~20k jobs) | ~$10 |
| Browserbase for resolver/reader | inside the $20 plan |
| **Total** | **~$35-55** |

## Rules that carry over from fetch.md
- Nothing goes online while a user waits.
- A failed fetch closes nothing.
- Aggregator text is for finding, not for reposting; the JD we tailor from comes from the employer's page.
- Every new source ships with its `supply` numbers before and after.

## Sources
- Workday CXS API: https://jobo.world/ats/workday, https://apify.com/johnvc/workday-careers-api
- Keka public endpoint: https://help.keka.com/hc/en-us/articles/39946782104721-How-to-Use-the-Embed-Code-for-Your-Career-Site-in-Keka-Hire
- Darwinbox careers portals: https://smallcase.darwinbox.in/ms/candidate/careers
- Indian ATS market: https://www.thepeoplesboard.com/tools/top-15-ats-tools-in-india-2026/
- SerpApi Google Jobs + pricing: https://serpapi.com/google-jobs-api, https://serpapi.com/pricing
- Fantastic.jobs: https://fantastic.jobs/api, https://fantastic.jobs/article/best-job-posting-apis
- Apify LinkedIn jobs (applyUrl mostly empty): https://apify.com/curious_coder/linkedin-jobs-scraper
- Adzuna API terms: https://developer.adzuna.com/docs/terms_of_service
- hiring.cafe approach: https://apify.com/blackfalcondata/hiringcafe-scraper
