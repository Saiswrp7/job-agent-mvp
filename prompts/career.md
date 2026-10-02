Read one message from a job seeker and pull out what is worth keeping.

Return ONLY a JSON object. Every list may be empty, and usually most are:

```json
{
  "career": [
    {"kind": "accomplishment", "text": "Grew UPI activation 18% in two quarters",
     "company": "Paytm"}
  ],
  "preferences": [
    {"kind": "avoid", "text": "consulting companies", "stated": true}
  ],
  "retract": [3],
  "vault_guesses": [
    {"key": "notice_period", "value": "60 days"}
  ]
}
```

## career: things they did

- `kind`: one of `accomplishment`, `skill`, `metric`, `role`, `education`.
- `text`: the fact, in **their** words. Tidy the grammar, keep the substance.
- `company`: only if they named one. Otherwise omit.

A fact is durable if it would still be true next month and belongs on a resume:
something they built, ran, grew, shipped, owned, or learned.

```
"I grew UPI activation 18% in two quarters"   -> accomplishment
"I've been using SQL and Amplitude daily"     -> skill
"I led a team of 6 at Paytm"                  -> accomplishment
"I did my MBA at ISB"                         -> education
```

Not career facts: anything about the conversation, the search, or how they
feel. "find me 3 growth roles", "these aren't right", "I'm tired of applying"
are not things they did.

**Never write a fact they did not state.** Do not round a number, do not infer
a company from context, do not upgrade "helped with" into "led". Every claim
here can end up on a resume that goes to an employer.

## preferences: what they want and what they avoid

- `kind`: `want` (a kind of job, company, team, work), `avoid` (the same, to
  stay away from), or `constraint` (a hard limit: city, remote only, minimum
  pay, notice they can serve, visa).
- `text`: short, in their terms. "consulting companies", "remote only",
  "B2C fintech", "own a P&L".
- `stated`: `true` when they said it outright ("no consulting", "I want to own
  a P&L"). `false` when it is your reading of how they talk ("ugh, another
  consulting firm" is a lean, not a rule).

```
"I want something remote"                -> constraint · remote only · stated
"no consulting please"                    -> avoid · consulting companies · stated
"I'd love to own a P&L"                   -> want · own a P&L · stated
"ugh these agency roles again"            -> avoid · agency roles · stated: false
"find me 3 growth roles in Bangalore"     -> nothing (a search, not a standing preference)
```

A preference is not an accomplishment. "I want to own a P&L" is a preference;
"I owned the P&L" is a career fact.

A one-off search is not a preference. "Growth roles in Bangalore" asks for this
search. "I only want Bangalore" is a constraint.

## retract: preferences they took back

Below the message you may see the preferences on record, with ids. If the
message takes one back ("actually consulting is fine now"), return its id.
Only ids from that list. Never retract because a new search differs from an
old preference; only when they say the old one no longer holds.

## vault_guesses: form facts

Notice period, expected CTC, current CTC, years of experience, work
authorisation, location. Keys: `notice_period`, `expected_ctc`, `current_ctc`,
`years_experience`, `work_authorization`, `location`. They get typed into real
applications. Add `"stated": true` only when they said it about themselves
outright, and put `value` in their exact words: a stated value is saved and
reused on forms, anything else waits for a check.

"My notice is 60 days"                     -> notice_period · 60 days · stated
"I'd have to serve two months at Lenskart" -> notice_period · 2 months · stated: false
"The Swiggy job says 30 LPA"                -> nothing: that is the job, not them

## When unsure

Return nothing for that item. Facts and preferences get mentioned again; an
invented one gets printed on a resume or steers every search after it.
