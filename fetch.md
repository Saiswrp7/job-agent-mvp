# Fetch Engine

Settled 2026-09-21. Chat surface is irrelevant to this document.

```
cron (hourly)  →  jobs table  →  SQL + LLM  →  user
```

Nothing goes online while a user is waiting. That single rule is what makes the
whole thing fast and cheap.

---

## Fetch: six steps

**1. A company list.** Start with 50 companies you'd actually work at.

```json
{"company": "Swiggy", "source": "lever", "slug": "swiggy",
 "type": "b2c", "stage": "series-f", "industry": "food delivery"}
```

The tags are typed once per company and land on every job row, which turns
"B2C companies" into a SQL filter for free.

**2. One board link per company.** One request returns every job *with* its
full description.

```
lever       api.lever.co/v0/postings/{slug}?mode=json
greenhouse  boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true
```

`content=true` is load-bearing. Without it you need one request per job.

**3. Hourly script.** Hits all boards concurrently (semaphore ~8, since every
Greenhouse board shares one host). Takes seconds. Nobody is waiting.

**4. Normalize.** Every board returns a different shape; write one. Clean the
location into `city` here, at write time, never at query time.

**5. Upsert** on `UNIQUE(source, source_id)`. This is what stops the table
filling with 24 copies of every job per day.

**6. Close what vanished.** A job in the table but not in today's response was
taken down: set `closed_at`.

> **If a board's fetch failed, skip it and close nothing.** A timeout must never
> read as "that company closed every role."

## Table

```
source, source_id, company, title, department, employment_type,
location, city, remote, salary, description,
url, apply_url, posted_at, updated_at,
company_type, stage, industry, size,
seniority, years_min, years_max,
content_hash, first_seen, closed_at
```

A column earns its place only if SQL filters or sorts on it. Everything else is
already in `description`, and the LLM reads that anyway.

- From the API: `department`, `employment_type`, `salary`, `apply_url`
- From the company list: `company_type`, `stage`, `industry`, `size`
- Regex at fetch time: `seniority` (title), `years_min/max` (description)
- Missing `years` is neutral, never a mismatch

---

## Search: two LLM calls, one query

**1. Message → JSON.** One call that only fills in a form.

```json
{"count": 5, "title_keywords": ["growth"], "city": "Bangalore",
 "company_type": "b2c", "posted_within_days": 45, "soft_criteria": ""}
```

Anything the user didn't say stays `null`. Under-filling is safe (SQL returns
more, stage 2 cuts). Over-filling silently deletes good jobs.

**2. At most one clarifying question,** and only when the role is genuinely
ambiguous. Never a second one in the same search.

**3. One SQL query.** Filled fields become filters, `null` fields vanish:
`(:city IS NULL OR city = :city)`.

```sql
SELECT * FROM jobs
WHERE closed_at IS NULL
  AND city = 'Bangalore'
  AND company_type = 'b2c'
  AND title LIKE '%growth%'
  AND posted_at > date('now', '-45 days')
ORDER BY posted_at DESC
LIMIT 20;
```

**Ask for 20 to return 5.** The gap is where the judgment happens.

Skills go in `ORDER BY`, never `WHERE`. A JD saying "split testing" instead of
"A/B test" should rank lower, not disappear.

**4. Too few rows? Relax in code,** in a fixed logged order: widen 45→90 days,
then drop the weakest keyword, then say so honestly. The LLM does not decide
this.

**5. Second LLM call picks 5 from the 20** and writes one line each. This is
where everything SQL can't express gets handled: is this company really B2C, is
this a growth job or a CRM job in disguise, are the skills actually required.

## The exclusion list

Do **not** maintain a synonym list. The model already knows "experimentation"
and "A/B testing" are the same thing.

Maintain the opposite, because the model does *not* know what you personally
want to avoid:

```
kill_titles:  CRM Manager, Lifecycle Marketing, Retention Marketing
kill_signals: MoEngage, CleverTap, Braze, push notification, lifecycle campaigns
              3+ hits in the description = drop, 1-2 = fine
```

Half the roles titled "Growth Manager" in India are CRM jobs wearing a growth
hat. Separating those two is the point of this system.

---

## Cost

| | Tokens | Cost |
|---|---|---|
| Call 1 (message → JSON) | ~600 | ~$0.00 |
| Call 2 (pick 5 from 20) | ~24,000 | ~$0.07 |
| Cron | no LLM | free |

~$0.08 per search, ~$25/month at 10 searches a day. All of it is the 20
descriptions in call 2.

Levers, when it matters: swap call 2 to a cheaper model (test against the CRM
cases first), then summarize each job at ingest and rank on 200-token summaries
instead of full text (~6x cheaper, break-even around 6 searches/day).

Anything paid per query gets expensive. Anything paid per job gets cheaper every
time you search. Push work toward ingest.

## Deliberately skipped

ETag caching, async enrichment workers, role taxonomy tables, embeddings,
Elasticsearch, scraping, live browsing.

Live browsing is right in exactly one case: the user pastes a specific job URL.
That's fetching a known page, not searching.

## Known gap

Greenhouse + Lever is a US-startup assumption. Most Indian employers post to
Workday, Darwinbox, Keka or their own careers page. Fix by extending the company
list and adapter count, not by changing the design above. An agent that reads a
careers page belongs in the cron, where nobody is waiting, never in the query
path.

---

## Adzuna enrichment: designed, not built (2026-09-28)

The ask: for a search's ~20 Adzuna candidates, fetch the full description and
the employer's own apply link, outside the user's wait.

What the links actually do, checked on three live rows:

- `redirect_url` is an adzuna.in `/details/<id>` or `/land/ad/<id>` page.
- `/details/<id>` returns the full job text (or 404 once expired). The text is
  there, but it is Adzuna's page, not an API.
- "Apply for this job" on that page is `/land/ad/<id>?aztt=...`, which
  answered **403 Access Denied** to a scripted request. Getting past it would
  mean getting past a bot wall on purpose.
- Their API terms: a breach is "any usage that appears to be an attempt to
  extract Confidential Information for commercial reuse". Their API returns a
  snippet on purpose ("we currently only provide a snippet").

So the half that matters (the real apply link) is behind a wall, and the other
half is lifting their page text into our product. Not built.

What gets the same result without either:

1. **Supersede, don't enrich.** Most Adzuna rows from real employers also sit
   on that employer's own board. `python cli.py discover` finds those boards
   from the company names Adzuna gives us; once a board is in `boards.json`,
   the next sync fetches the full job and `close_superseded` closes the
   Adzuna copy (same company, same title). This already runs every sync.
2. **Company-name lookup at ingest.** Next step: when an Adzuna row's company
   has a board in `boards.json` but no exact title match (titles differ in
   punctuation), match on normalised title and keep a pointer instead of
   closing, so search can show the board copy.
3. **Careers-page reader, in the cron.** For employers on Workday, Darwinbox or
   their own site (most of India), an agent that reads a careers page belongs
   in the hourly cron, over companies named by `saved_searches`, never in the
   query path. Same rule as above.

A job that stays Adzuna-only keeps its snippet and "Jobs by Adzuna" credit;
it can be found and ranked, not tailored for or applied to by the agent.
