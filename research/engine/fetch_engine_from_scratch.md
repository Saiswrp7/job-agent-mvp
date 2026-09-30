# Fetch engine from scratch: how I would find the latest jobs that match intent

Written 2026-09-29 after online research. Nothing here is built. Companion to
`ENGINE_PLAN.md` (2026-09-28); this is the "if we started clean" version plus
what the research changed.

## 1. Why the MVP feels broken (diagnosis)

Not one problem, three, and they compound:

1. **We find jobs where we cannot apply.** Adzuna gives a 500-char snippet and a
   redirect; LinkedIn guest pages hide the employer link behind a login wall.
   13,391 of 32,393 open rows are Adzuna, 210 LinkedIn. Neither can be filled by
   the cloud browser. The boards we CAN apply on (Greenhouse/Lever/Ashby/SR,
   ~18.8k rows) are mostly SDE, because Indian PM/growth employers do not use
   those ATSs.
2. **Ranking does not know applyability.** Search sorts by fit, then "owns
   revenue", then date. So the Traya query returned 5 LinkedIn links.
3. **"Latest" is not provable.** No scheduler, no sync log, Adzuna paging repeats,
   LinkedIn 429-blocked since 09-24.

The engine is not weak in code; it is pointed at the wrong layer. Every serious
aggregator (hiring.cafe, Fantastic.jobs, Jobright, Google for Jobs) does the same
thing: **aggregators tell you which companies are hiring; the employer's own ATS
is where the job record and the apply link live.**

## 2. How the real players do it (research)

| Who | Discovery of companies | Extraction | Refresh | Matching / dedupe |
|---|---|---|---|---|
| hiring.cafe (solo founder, 2.1M jobs) | 30k company URLs from Apollo, Node crawler finds the careers page, LLM classifies "has listings"; weeks of manual vetting to drop staffing agencies | raw HTML -> GPT-4o-mini -> 90+ structured fields | 2-3x/day; Oxylabs proxies only where rate-limited | Elasticsearch boolean search; ghost jobs = same-company near-duplicate JD by embedding, keep earliest post date |
| Fantastic.jobs (Active Jobs DB) | 200k career sites across 54 ATSs (Greenhouse, Lever, Ashby, Workday, SmartRecruiters, Workable, Recruitee, Zoho, SuccessFactors, Taleo, iCIMS, Personio, Teamtailor, ...) | ATS public JSON per board | polls 99% of boards **hourly**; DB updated twice an hour | resold from ~$1 per 1,000 jobs; India coverage unverified; **no Keka, no Darwinbox** |
| Jobright | ingests Greenhouse/Lever/Ashby/Workday + YC/HN, 400k new/day | LLM enrichment | continuous | users still report ghost/expired jobs: volume trades against freshness |
| Google for Jobs | employers put `JobPosting` JSON-LD on each job page + sitemap/Indexing API | JSON-LD | Indexing API = hours; sitemap = days | visibility decays after 30 days |
| Apna (India RecSys) | own listings | two-tower embeddings + LLM | n/a | vector retrieval then LLM/business ranker (India, blue/grey collar) |

Academic consensus (arXiv 2609.23307, TalentCLEF 2026): candidate-job matching
works best as **hybrid retrieval** (BM25 + dense vectors fused by reciprocal
rank fusion) over LLM-extracted structured views (skills, level, tasks), then a
cross-encoder or LLM rerank of the top few dozen. No single model does it.

## 3. The plan from scratch (seven layers)

### L1. Company universe: who hires in India for the roles our users ask
Seeds, all cheap:
- Adzuna employer names (4,371 already in DB) and LinkedIn company names.
- Google Jobs results per saved search (`countryCode=in`, `postedWithinDays=1`).
- India startup boards where PM/growth actually live: Instahyre, Cutshort,
  Wellfound, Hirist, Apna, Internshala via one Apify feed
  (`getascraper/india-job-inventory-feed`, $1.49 per 1,000, `changesOnly` mode,
  lifecycle NEW/UPDATED/CLOSED).
- Funding lists (Tracxn/Inc42 style) for growth-stage startups.
Output: a `companies` table with name, domain, careers URL, ATS, first_seen.
This table is the compounding asset. It only grows.

### L2. ATS resolution: for every company, where is the board
- Fetch careers page, follow links, match host: `boards.greenhouse.io`,
  `jobs.lever.co`, `jobs.ashbyhq.com`, `smartrecruiters`, `apply.workable.com`,
  `recruitee.com`, `myworkdayjobs.com`, `*.keka.com/careers`,
  `*.darwinbox.in/ms/candidate/careers`, `zohorecruit.com`, `freshteam`,
  `personio`, `teamtailor`.
- Fallback for custom sites: `sitemap.xml` -> job URLs -> `JobPosting` JSON-LD
  (open-source pattern: Jobmap `jobposting_discovery.py`). Google requires this
  markup, so most modern career sites carry it.
- Store `boards.json` rows with adapter + id (Keka needs the guid from the
  page). Run weekly, log to JSONL like `discover.py` does today.

### L3. Board polling: the only rows that count as "form"
Public JSON endpoints, no auth, free:
- Greenhouse `api.greenhouse.io/v1/boards/{co}/jobs?content=true`
- Lever `api.lever.co/v0/postings/{co}?mode=json`
- Ashby `api.ashbyhq.com/posting-api/job-board/{co}`
- SmartRecruiters (have), Workable `apply.workable.com/api/v1/widget/accounts/{co}` (slow down on 429, never drop)
- Recruitee `{co}.recruitee.com/api/offers`
- Workday `/wday/cxs/{tenant}/{site}/jobs` POST (verified: Accenture 2,000 India jobs)
- Keka `/careers/api/embedjobs/default/active/{guid}` (verified: full JD)
- Darwinbox: behind Cloudflare, read with the cloud browser inside the cron
Cadence: hourly, one tenant at a time, stop on any non-200, a failed fetch
closes nothing. Each row: full JD, employer apply URL, posted date,
`apply_kind=form`.

### L4. Aggregator sweep: "latest" and gap-filling
- **Google Jobs** per saved search, daily, 1-day window. Via Apify
  `farside/google-jobs-scraper` at $0.003/job (SerpApi is $25 per 1,000
  searches; the Apify route is ~8x cheaper for our volume). `apply_options` lists
  every apply link including the employer site; if the host is a known ATS ->
  add board (L2), else store as `link`.
- **India startup feed** (L1 source) daily in `changesOnly` mode for PM,
  growth, marketing, ops. Rows are `link` (Instahyre/Cutshort/Wellfound apply on
  their site), but they are the PM/growth supply we are missing today.
- **Adzuna**: keep for employer discovery + as `link` rows; resolver only if the
  Step 0 test (20 redirect clicks in Browserbase) shows 30%+ land on a form.
- **LinkedIn** via Apify (~$0.50 per 1,000): company names into L1 only. Never
  an apply target.

### L5. Normalise, dedupe, expire
- Dedupe key: normalised company + normalised title + city. Precedence:
  board row (form) > Google Jobs employer link > India feed > Adzuna > LinkedIn.
  Closing the aggregator copy when the board copy appears
  (`close_superseded`, already exists for 4 ATSs).
- `content_hash` per row; relabel only on change.
- Ghost/repost detection: same company, JD cosine similarity > 0.95 -> keep the
  earliest `posted_at`, mark `repost_count`. Surface "reposted 3x since June" to
  the user instead of hiding it.
- Expiry: board rows close when the board stops listing them (two consecutive
  clean polls, not one failed poll). Aggregator rows close by age (30 d) or by
  the feed's CLOSED event.

### L6. Matching to intent (the part users feel)
1. **Structured extraction once per job** (have: `labels.py` role_family,
   level, years_min, owns_pnl, country, work_mode). Add: skills list, comp if
   stated, team/function, `apply_kind`. Cheap model, ~$1 per 5k jobs; rule-based
   first (`rules.py`) and LLM only for the ~20 candidates a search touches.
2. **Query understanding**: turn "growth PM in Bangalore, 3 yrs, fintech" plus
   the resume into a structured query: role families, level band, cities
   (with CITY_GROUPS), years, must-haves vs nice-to-haves. Already half there
   in `search/`.
3. **Hybrid retrieval**: SQL hard filters (open, India/remote, posted <= 14 d,
   `apply_kind`) -> BM25 via SQLite FTS5 on title+JD -> dense vectors (small
   embedding model, one vector per job, stored in SQLite) -> RRF fuse -> top 50.
4. **Rerank** top 30 with the LLM reading resume + soft criteria + JD; output a
   fit score and one-line reason per job. Never drop on seniority without known
   years.
5. **Final order**: `form` first, then fit, then recency. `link` rows appear
   only to fill gaps and are labelled "apply yourself".
6. **Learning loop**: `saved_searches` (supply grows toward demand), thumbs on
   results, applied/ignored -> features for a light business ranker later. No
   fine-tuning until there are thousands of labelled pairs.

### L7. Prove "latest": measurement built in
- `sync_runs` table: start, end, rows added/closed per source, errors.
- `cli.py supply` per role x city: usable count, % form, age of newest row.
- Launch gate: every role x city a real user asked for has 50+ usable and the
  newest is under 24 h.

## 4. Build vs buy

Trial Fantastic.jobs Active Jobs DB (~$1 per 1,000) on 20 real asks. If India
PM/growth comes back with 50+ usable each, buy it and skip most of L3 for the
global ATSs. It will NOT cover Keka, Darwinbox or the India startup boards, so
L1/L2 for Indian companies plus the Keka/Darwinbox adapters and the India feed
get built either way.

## 5. Order, if starting clean

| Week | Build | Done when |
|---|---|---|
| 0 (1 day) | Phase 0 measurements from ENGINE_PLAN (Adzuna redirect click test, 20 Google Jobs asks, Fantastic.jobs trial, ATS census of top 300 employers) | numbers decide L3/L4 order |
| 1 | L2 resolver + L3 pollers for the 8 ATSs, hourly launchd, `sync_runs`, `apply_kind` | PM Blr, growth Blr 50+ form rows |
| 2 | L4 Google Jobs + India feed, L5 dedupe/ghost/expiry | newest row per saved search < 24 h |
| 3 | L6 hybrid retrieval + rerank + ordering, L7 supply dashboard | Traya-style query returns form rows first with reasons |

## 6. Cost at 10 users (monthly)

| Item | Cost |
|---|---|
| ATS pollers (8 adapters) | $0 |
| Google Jobs via Apify, ~5k jobs | ~$15 |
| India startup feed, ~10k jobs | ~$15 |
| Apify LinkedIn (discovery only, 20k) | ~$10 |
| Embeddings + labels | ~$5 |
| Browserbase for Darwinbox/resolver | inside $20 plan |
| Total | ~$45-65 |

## 7. What changed vs ENGINE_PLAN.md (2026-09-28)

- Google Jobs via Apify pay-per-result instead of SerpApi (8x cheaper at our volume).
- India startup feed (Instahyre/Cutshort/Wellfound/Hirist) added as the PM/growth supply source.
- Generic sitemap + JSON-LD reader as the fallback for custom career sites.
- Ghost/repost detection by JD similarity; expiry on two clean polls.
- Matching spelled out as hybrid retrieval + RRF + LLM rerank, with applyability in the final order.
- Everything else (aggregators find, boards apply, Workday + Keka first, Phase 0 gates) stands.

## Sources
- hiring.cafe pipeline: https://gist.github.com/thoroc/21601e286d9d4fec8505a88d71145ad9 , https://apify.com/blackfalcondata/hiringcafe-scraper
- Fantastic.jobs about/ATS list: https://fantastic.jobs/about , https://rapidapi.com/fantastic-jobs-fantastic-jobs-default/api/active-jobs-db
- ATS public endpoints: https://fantastic.jobs/article/ats-with-api
- Google JobPosting + Indexing API: https://developers.google.com/search/docs/appearance/structured-data/job-posting , https://developers.google.com/search/blog/2018/06/introducing-indexing-api-for-job
- JSON-LD monitoring: https://firstpost.io/blog/json-ld-jobposting-jobs , Jobmap https://github.com/Altman42/Jobmap , Common Crawl job ads https://skeptric.com/common-crawl-job-ads/
- Hybrid retrieval paper: https://arxiv.org/abs/2609.23307 ; TalentCLEF 2026 https://arxiv.org/pdf/2606.31692 ; Apna RecSys https://medium.com/apna-technology-blog/how-we-transformed-apnas-job-recsys-engine-using-vector-embeddings-llms-f9cb25b04f8f
- Jobright sourcing + ghost jobs: https://www.wobo.ai/blog/jobright-review/ , https://jobright.ai/compare/simplify
- Google Jobs via Apify: https://apify.com/farside/google-jobs-scraper ; SerpApi: https://serpapi.com/google-jobs-api
- India startup feed: https://apify.com/getascraper/india-job-inventory-feed ; Wellfound/Instahyre/Cutshort scrapers on Apify
- Indian ATS market: https://www.zappyhire.com/blog/top-applicant-tracking-system-ats-in-india , https://www.thepeoplesboard.com/tools/top-15-ats-tools-in-india-2026/
- TheirStack/Bright Data overview of job APIs: https://brightdata.com/blog/web-data/best-job-apis
