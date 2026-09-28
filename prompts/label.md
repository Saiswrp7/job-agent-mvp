Label each job below with facts about the job itself. You are filling in
columns a database will filter on, not judging whether the job is good.

Return ONLY a JSON array, one object per job, in the same order:

```json
[
  {"id": "the id shown", "role_family": "growth", "level": "mid",
   "years_min": 3, "owns_pnl": false, "country": "India",
   "work_mode": "hybrid"}
]
```

When the description does not say, use null. A wrong label hides a job from
someone who wanted it; a null only means "unknown". Never guess.

## Fields

`role_family` — what the person in this job does all day. Exactly one of:

engineering, data, product, design, growth, crm_lifecycle, marketing, sales,
business_development, customer_success, support, operations, supply_chain,
finance, hr, legal, content, research, healthcare, education, admin, other

When the title names a function, that is the family: every kind of Product
Manager (data PM, platform PM, AI PM) is `product`; every kind of engineer is
`engineering`. Read the description only when the title is vague ("Associate",
"Manager, CEO's Office", "Specialist").

Three that are easy to mix up, told apart by the work, never the title:
- `growth` — owns a user or revenue metric (acquisition, activation,
  retention, monetisation) and moves it through product changes, experiments
  or channels.
- `crm_lifecycle` — runs campaigns to existing users through tools like
  MoEngage, CleverTap, Braze: push, email, SMS, in-app. Often titled "Growth
  Manager".
- `marketing` — brand, performance ads, social, PR, events.

`level` — one of: intern, junior (0-2 yrs, associate, entry), mid (2-5 yrs),
senior (5-8 yrs, "senior" roles), lead (manages a team, or lead / principal /
staff), leadership (director, head, VP, C-level). Use stated years first,
then the title. `intern` is only for internships.

`years_min` — the smallest number of years of experience asked for, as an
integer. Null if no number is stated.

`owns_pnl` — true only when the job holds direct accountability for a P&L, a
revenue line, or a business unit's results: "own the P&L", "revenue owner",
"business head". Supporting revenue ("campaigns that drive revenue") is false.

`country` — the country the job is based in, in English ("India", "United
States"). For remote jobs, the country they hire from; "Global" if anywhere.

`work_mode` — onsite, hybrid or remote. Null if not stated.
