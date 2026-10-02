# Applying from Telegram with Browserbase: forms and sign-ins

Research, 2026-10-01. Sai's question: how should the agent use Browserbase to apply
(1) on plain forms and (2) when the site asks for a login, with the person doing
everything from the Telegram chat?

## The answer

1. **Forms: filling is built, sending is not proven.** The Browserbase pieces are in
   place (one cloud browser per run, 15-minute keep-alive, captcha solving, a
   screenshot as proof). But 3 of the 4 biggest form sites check for bots exactly
   at the Submit click, which every eval stopped short of: Greenhouse and Ashby
   (Google's invisible score), Lever (hCaptcha). The first real send today
   (metaforms, Ashby) was rejected there as "possible spam".
2. **"Login" on our jobs means making an account on the company's own job site**
   (Workday, Oracle...), not using the person's own password. That is 32,918 of
   39,046 live jobs (84%).
3. **Oracle needs no password.** Email (or phone), then a 6-digit code. Jojo asks
   "paste the code here" in Telegram. 13,856 jobs.
4. **Workday needs email + password, one account per company.** Jojo makes a strong
   password itself, keeps it locked, and code types it. The person never sees or
   types a password. If the company sends a "verify your email" mail, the person
   taps it (or pastes it to Jojo) and says done. 15,582 jobs.
5. **Browserbase "Contexts" keep a login between sessions** (tested live today: it
   works). **Live View is not for phones**: Browserbase says phone keyboards don't
   work in it, so it stays out of the user flow, which matches your earlier no.

Words used below:
- **Context**: Browserbase's saved browser profile (cookies, logins). Pass its id
  when opening a session and the browser starts already logged in.
- **Keep-alive**: the cloud browser stays open after we disconnect, so a run waiting
  for the person's answer comes back to the same filled page.
- **Live View**: a link that shows the cloud browser's screen and lets someone click
  and type in it.
- **Mini App**: a small web page that opens inside Telegram.
- **Honeypot**: a box hidden from people. Only bots fill it, so filling it flags
  the application as a bot.

## What I measured today

### Where the jobs are (live DB)

| Gate | Jobs | Share |
|---|---:|---:|
| Form, no login | 6,079 | 16% |
| Account first | 32,918 | 84% |
| Link only | 49 | 0% |

| Site | Jobs | Companies | What its gate asks (from the page check below) |
|---|---:|---:|---|
| Workday | 15,582 | 564 | create an account: email + password |
| Oracle | 13,856 | 146 | email or phone, then a 6-digit code |
| Phenom | 2,646 | 46 | depends on the company; our stored links are broken |
| SuccessFactors | 720 | 89 | create an account or sign in |
| iCIMS | 73 | 22 | email + hCaptcha |
| Cornerstone, Dayforce, UKG, Jobvite, Paycom | 41 | | not checked |

Titles with "product manager" or "growth": 206 behind an account, 143 on forms.
Sign-ins more than double what Jojo can apply to for PM and growth users.

### 19 real apply pages, opened in Browserbase (read only)

Only Apply / Apply Now / Apply Manually were pressed. Nothing typed, no account
made. Script: `research/apply/gate_probe.py` (output in `evals/out/gate_probe/`).

| Site | Pages | What the gate showed |
|---|---|---|
| Workday | 5 of 5 | Apply, then "Autofill with Resume / Apply Manually / Use My Last Application", then **Create Account**: email, password, repeat password, consent box (4 of 5), "Sign In" and "Forgot your password?" links. Password rule shown on Murex: 8+ characters with upper, lower, number and special. Steps shown: Create Account/Sign In, My Information, My Experience, Voluntary Disclosures, Review. |
| Oracle | 5 of 5 | Apply Now, then an **email page**: email, terms box (4 of 5), "communicate by phone instead?" (2 of 5). **No password box on any.** 3 standard flow, 2 "easy apply". |
| Phenom | 3 usable | Our stored links (`/job/<id>`) bounce to the careers home page (4 of 4, HTTP 303). With the locale path (`/us/en/job/<id>`) they open. WEX and Fiserv: Phenom's own multi-page form, **no login** (WEX has an invisible reCAPTCHA). TJX: hands over to Workday. |
| SuccessFactors | 2 of 3 | Dynatrace: one-page create account (email twice, password twice, name, gender). Ferrero: sign-in page with "Create an account". Lupin: Apply opens a menu, not reached. |
| iCIMS | 2 of 2 | Email + consent box + **hCaptcha**, before anything else. |

Also: **every Workday and every Oracle page had a honeypot** (Workday: "Enter
website. This input is for robots only"; Oracle: a box named `honeypot`). The
filler must skip them, as it already does for Breezy. None of the 19 pages blocked
the Browserbase browser.

### Live checks on our Browserbase plan (Developer, $20)

| Check | Result |
|---|---|
| Saved login (Context) | Logged in once on a public practice site (expandtesting.com, its published test account), closed, opened a new session with the same Context: **still logged in** (Logout link on the members page). Control without the Context: sent back to the login page. Session start with a Context 1.04 s, without 0.99 s. |
| reCAPTCHA (Google's demo) | **Solved in 37 s**, with Browserbase's "solving started" and "solving finished" signals. |
| hCaptcha (hCaptcha's demo) | **Not solved, 0 of 3**: 150 s plain, 150 s through an India proxy, 120 s after clicking the box. "Solving started" fired each time, "finished" never did. |
| Session recordings | Video of the screen (HLS/MP4), not page code, so a password box shows dots. Can be switched off per session (`recordSession: false`). |
| Google's bot score (reCAPTCHA v3 demo, 1.0 = human, 0.0 = bot) | **0.9**, with Browserbase's captcha solver on and off. The browser hides the usual automation flag (`navigator.webdriver` is false). |

### Bot checks at the Submit click (page code of 2 live jobs per site)

| Site | Jobs | Check at Submit | Can we pass it today? |
|---|---:|---|---|
| SmartRecruiters | 2,408 | none seen in the page | yes, as far as we know |
| Greenhouse | 1,669 | Google reCAPTCHA Enterprise (invisible score) | unknown: never sent |
| Lever | 730 | hCaptcha | doubtful: Browserbase solved 0 of 3 |
| Keka | 658 | picture captcha | yes: solved in ~1 s with selectors |
| Ashby | 398 | Google reCAPTCHA (invisible score) | **no: first real send flagged as spam** |
| Workable | 52 | Cloudflare Turnstile | unknown |

The evals filled forms and stopped before Submit, so none of them ever met these
checks. Another open-source apply bot hit the same Ashby message under Playwright,
even when a person clicked Submit inside the automated window, while the same
answers went through from normal Chrome (career-ops issue #4269).

Cost of all checks: about 50 Browserbase minutes of the month's 6,000. No session
left running; the test Context was deleted.

## Part 1: forms only

**Built, all inside Telegram.** Person picks a job, Jojo fills the form in a cloud
browser, questions it can't answer go to the chat, the page parks on keep-alive
(15 min) while it waits, then Jojo sends every answer + a screenshot and waits for
the person's own "submit". After sending, the confirmation page's screenshot is
the proof. Captchas: Browserbase solves Google's reCAPTCHA on its own, and Keka's
picture captcha once told where the picture and answer box are. Not hCaptcha.

What's left:

1. **Passing the bot check at Submit** (the real blocker, see the table above).
   Google's score for our Browserbase browser on its demo page is 0.9, so the
   browser and network alone look human. What differs from a person is behaviour:
   our filler sets most boxes with `fill()` (`apply/cloud.py`, the plain text
   path), which puts the value in with no key presses, and clicks without any
   mouse movement. Google's invisible check watches both. This is a guess until a
   real send proves it. Cheapest first:
   - a. Type with real keys (short delay per letter) and move the mouse before
     clicks; spend a normal amount of time on the page. Free.
   - b. India home-network proxy (Browserbase proxies, 1 GB included, then $12/GB).
   - c. Browserbase "Verified" browser (made to pass bot checks; Scale plan only,
     but Browserbase offers a trial on request: hello@browserbase.com).
   - Test each on a real job you actually want, one site at a time: Ashby, then
     Greenhouse (the biggest), then Lever.
2. **hCaptcha** (Lever at Submit, iCIMS at the start). Browserbase solved 0 of 3
   today. Lever's is invisible and may not pop up for a session that looks human,
   so the real send is the test. If it pops up: a paid outside solver, Verified,
   or the link + resume.
3. **Buttons instead of typing "yes"** (optional). Telegram buttons ("Submit",
   "Change something") under the approval message. A tap is still the person's own
   action that the AI can't fake. The bot reads only `message` updates today
   (`allowed_updates`), so it would add `callback_query`.
4. **Phenom links** (2,646 jobs): add the locale path. Some Phenom companies then
   become no-login forms (WEX, Fiserv), others hand over to Workday (TJX).

## Part 2: when the site asks to sign in

### Ways to handle it from Telegram

| Way | How it works | Phone-only person? | Verdict |
|---|---|---|---|
| A. Jojo makes the account | Creates it with the person's email and a password Jojo generates, stores it encrypted, code types it | yes, nothing to type | **Use for Workday, SuccessFactors, iCIMS** |
| B. Code relay | The site sends a code to the person's email or phone; they paste it in the chat | yes, one paste | **Use for Oracle and any "verify your email" step** |
| C. Password box inside Telegram (Mini App) | A small page opens inside Telegram; the person types an existing password; it is locked on their phone with our key and reaches the bot as a service message, not as chat text; the AI never sees it | yes | Later, only for "an account already exists for this email" |
| D. Live View | The person sees the cloud browser and logs in themselves | **no**: Browserbase says phone keyboards don't work in Live View; it also sends them to the job site, which you ruled out | Admin and debugging only |
| E. 1Password autofill (Browserbase partner) | Each login approved in the 1Password app | no: every user would need 1Password | No |
| F. Read their inbox for the code | Mail access, so no paste | yes | Later; Google's review for apps that read mail is heavy |

Way C details (when we need it): Telegram lets a Mini App send data to the bot
with `sendData` (up to 4,096 bytes) only when it was opened from a keyboard
button, and then no server of ours is needed: the page can be a static HTTPS page
(e.g. GitHub Pages). Lock the password on the phone with our public key first, so
Telegram only carries scrambled text.

### What the chat looks like

Oracle (new candidate):
```
Jojo: Emerson uses Oracle. I'll start with your email sai@... (or your phone, if you prefer).
      [fills the form, sends the approval as today]
You:  submit
Jojo: Sent. Emerson's site just emailed a 6-digit code to sai@... Paste it here to confirm.
You:  482913                      <- Jojo deletes this message after reading it
Jojo: Confirmed. Emerson has your application. [screenshot]
```
For a new Oracle candidate the code comes **after** Submit (Oracle's docs), so
"sent" must mean submitted **and** code accepted. A returning candidate (or one
with a saved draft) gets the code **before** the form.

Workday:
```
Jojo: Murex uses Workday, which needs an account on their site. I'll make one with
      sai@... and a strong password that I keep for you. You never need to type it.
      OK? (once only)
You:  ok
Jojo: [creates the account] Murex sent a "verify your email" mail to sai@...
      Tap the link in it, then say done. Or paste the link here.
You:  done
Jojo: [fills My Information, My Experience... sends the approval as today]
```
If Murex asks for no verification, the middle step disappears. How often Workday
companies ask for it is the main unknown (step 0 below).

### Rules that keep it safe and working

- **One yes before the first account**, kept like the submit yes: "Jojo may create
  job-site accounts in my name with my email."
- **Passwords:** one per site, generated (meets Workday's rule), stored encrypted
  with the key outside the database, typed by code. The AI never sees it and it
  is never logged. The existing rule (the AI may never ask for a password) stays.
  If the person wants to log in themselves later, "Forgot password" on that site
  works with their email.
- **Codes:** Jojo names the company when asking, treats the person's next message
  as the code, deletes that message (Telegram lets a bot delete messages a person
  sent it in a private chat, within 48 hours), and types it.
- **One Oracle sign-in at a time per person.** All Oracle companies send codes from
  the same Oracle address. Another open-source apply bot typed one company's code
  into another company's form and got "Too many attempts, try again in 30
  minutes" (OSApplyTrack issue #318). Oracle locks after 5 wrong codes for 30
  minutes; Jojo stops after 2.
- **Never two browser sessions on the same Context at once** (Browserbase: the site
  may log the person out).
- **Skip honeypots** on Workday and Oracle.
- **No "Sign in with Google / LinkedIn"**: that would put the person's main
  account inside an automated browser. Same reason we never automate LinkedIn or
  Naukri accounts; those jobs keep getting the link + tailored resume.

### Which Browserbase features, and how

| Feature | Use | Status |
|---|---|---|
| Sessions over CDP + Playwright | everything, as today | built |
| Keep-alive (15 min) | wait on the same page for a code or answer | built |
| Contexts | one per person per company site (e.g. `murex.wd3.myworkdayjobs.com`): sign in once, later runs start logged in. `persist: true` only on the run that signs in; Browserbase saves it when that session closes | tested, not built |
| `recordSession: false` | optional: sign in in its own short session with recording off, saving into the Context, then apply in a normal recorded session | not built |
| Captcha solving | reCAPTCHA works; image captchas with selectors (Keka) work; hCaptcha not | built |
| Proxies (1 GB included, India available) | not needed to reach sign-in pages (none of the 19 blocked us); option b for the bot check at Submit | off |
| Live View | debugging only | not used |
| Not useful | Secrets (only for code hosted on Browserbase Functions), hosted Agents (no hard submit guard, ruled out earlier), Verified (Scale plan only) | |

If a code takes longer than the 15-minute keep-alive, Jojo signs in again with the
stored password. Oracle reopens a started application as a draft (seen in the
OSApplyTrack runs); whether Workday keeps progress is a step 0 check.

## Build order

0. **Measure with your real email (needs your yes, about an hour).** 5 Workday +
   5 Oracle jobs you would actually apply to; create the accounts, stop before
   submit. Count: verify mail asked? Link or code? Minutes until it arrives? Does
   it reach Saiswrp7@live.com? (One Oracle forum thread reports codes not reaching
   Hotmail/Outlook addresses.) Pages per application and minutes per run.
1. **Gate reader:** recognise Oracle email page, Workday create-account / sign-in,
   SuccessFactors, iCIMS; skip honeypots.
2. **Accounts store + code typing:** encrypted password per person per site; a
   tool that types a stored secret without the AI seeing it; the one-time yes.
3. **Code relay in Telegram:** ask with the company name, read the next message,
   delete it, type it; one Oracle sign-in at a time; stop after 2 wrong codes.
4. **Contexts** per person per site.
5. **Multi-page Workday and Oracle** (the biggest piece): Workday's 5-7 steps with
   "Add" blocks for experience and education; Oracle's own dropdown widgets, which
   ordinary page readers miss (OSApplyTrack: `oj-select-single`), dependent
   country, state and city lists, and resumed drafts. Then the same eval as the 9
   form sites: 5 Workday + 5 Oracle, never submitting.
6. **Later:** Mini App password box, Telegram buttons, Phenom link fix, hCaptcha plan.

Forms track (in parallel, small): real-key typing + mouse movement, then one real
send each on Ashby, Greenhouse and Lever with jobs you want; ask Browserbase for a
Verified trial if Ashby still says spam.

## Decisions for you

1. Forms: try real-key typing first on the next real send, before proxies or Verified?
2. May Jojo create job-site accounts in a user's name after one yes from them?
3. Step 0 with your email: go?
4. hCaptcha: wait for the real Lever send, or add a paid solver now?
5. Phenom link fix (2,646 jobs): now or later?

## Sources

- Browserbase: [Contexts](https://docs.browserbase.com/platform/browser/core-features/contexts),
  [Website authentication](https://docs.browserbase.com/platform/identity/authentication),
  [Session Live View](https://docs.browserbase.com/platform/browser/observability/session-live-view)
  (phone keyboards: "Mobile keyboards aren't officially supported"),
  [Keep alive](https://docs.browserbase.com/platform/browser/long-sessions/keep-alive),
  [CAPTCHA solving](https://docs.browserbase.com/platform/identity/captcha-solving),
  [Proxies](https://docs.browserbase.com/platform/identity/proxies),
  [Agent Identity / Verified](https://docs.browserbase.com/platform/identity/overview),
  [Secrets](https://docs.browserbase.com/platform/secrets/overview),
  [Session replay](https://docs.browserbase.com/platform/browser/observability/session-replay),
  [Pricing](https://www.browserbase.com/pricing),
  [1Password agentic autofill](https://www.browserbase.com/blog/1password-agentic-autofill).
- Telegram: [Mini Apps](https://core.telegram.org/bots/webapps) (sendData, keyboard
  button Mini Apps), [Bot API deleteMessage](https://core.telegram.org/bots/api#deletemessage).
- Oracle: [Email and SMS for candidate identity verification](https://docs.oracle.com/en/cloud/saas/talent-management/faush/email-and-sms-for-candidate-identity-verification-and.html)
  (6-digit code by email or SMS; after submit for new candidates; 5 wrong = 30 min
  lockout), [Customer Connect thread on codes not reaching Hotmail/Outlook](https://community.oracle.com/customerconnect/discussion/955772/email-with-verification-pin-not-being-sent-to-external-candidates).
- Workday: [Set Up Candidate Home Accounts](https://doc.workday.com/admin-guide/en-us/human-capital-management/recruiting/career-sites/gtv1538650489786.html)
  (each company chooses "Require Candidate Home Account" and "Enable Candidate
  Verification Email").
- [OSApplyTrack issue #318](https://github.com/CryptoJones/OSApplyTrack/issues/318):
  another apply bot's notes on Oracle's code boxes, drafts, dropdowns and code mix-ups.
- Ashby: [Application spam protection](https://docs.ashbyhq.com/job-board-application-spam-protection)
  (each company picks Strict / Less permissive / Permissive / None),
  [career-ops issue #4269](https://github.com/career-ops-hq/career-ops/issues/4269)
  (same "flagged as possible spam" under Playwright).
- Google's [reCAPTCHA v3 score demo](https://recaptcha-demo.appspot.com/recaptcha-v3-request-scores.php)
  (used for the 0.9 measurement).
