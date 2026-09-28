# Research B: How ATS parsing works, keyword matching, layouts, and an ATS rulebook

Researched 2026-09-24 for the Job Agent MVP resume feature (LaTeX/tectonic render, `pdftotext -layout` gate).
Source quality note: many "ATS guide" pages online are SEO content written by resume-tool vendors, and they copy claims from each other. Where possible I've used primary sources: vendor docs (Oracle, Greenhouse, Textkernel, Ashby, Workday), published studies (Enhancv, HBS/Accenture, Duke/hireEZ, Ladders, ResumeGo), and template source code. Where a claim rests only on a vendor blog, I say so.

---

## 1. How ATS parsing actually works

### The pipeline, in plain terms
1. **File to text.** The ATS turns the PDF or DOCX into plain text (with OCR only for scanned files). Textkernel says about 5% of documents need OCR ([Textkernel Tx docs, via search summary](https://developer.textkernel.com/tx-platform/v10/resume-parser/overview/getting-started/)).
2. **Text to fields.** A parser splits the text into sections by heading, then pulls out name, contact details, employers, titles, dates, degrees and skills, and puts them into a candidate profile ([Enhancv on Greenhouse](https://enhancv.com/blog/how-does-greenhouse-work/)).
3. **Fields to search and ranking.** Recruiters search, filter and sort the profiles. Newer systems also score them against the role's criteria. The recruiter can still open the original file ([Enhancv on Greenhouse](https://enhancv.com/blog/how-does-greenhouse-work/); [Ashby AI-assisted review](https://www.ashbyhq.com/product-updates/ai-assisted-application-review)).

Most breakage happens in **step 1, reading order**. If the text comes out scrambled, step 2 puts the wrong dates on the wrong job. Textkernel's own engineering blog puts it this way: a basic left-to-right, top-down read of a column layout "would generate a rendering where the information from different sections of the CV is mixed together." At least 15% of CVs use columns. Textkernel's machine-learning column detector raised its "well-rendered CV" rate from **62% to 90%**, so roughly 1 in 10 column CVs still render badly even with a market-leading parser ([Textkernel: improving extraction from column resumes](https://www.textkernel.com/learn-support/blog/improving-extraction-from-column-resumes/)).

### Who parses for whom

| ATS | Parser / scoring engine | Notes | Source |
|---|---|---|---|
| **Workday** | Its own parser for autofill. **HiredScore** (acquired 2024) handles AI grading and ranking in "Spotlight" | Workday is the top ATS in the Fortune 500 (39.2% in 2025, over 40% in 2026). HiredScore grades candidates A to D, and Workday says it supports rather than replaces human decisions | [Workday HiredScore](https://www.workday.com/en-us/products/talent-management/ai-recruiting.html); [Josh Bersin on the acquisition](https://joshbersin.com/2024/03/workday-to-acquire-hiredscore-a-potential-shakeup-in-hr-technology/); [Jobscan ATS usage report](https://www.jobscan.co/blog/fortune-500-use-applicant-tracking-systems/) |
| **SAP SuccessFactors** | **Textkernel** | SAP documents Textkernel as its third-party parser | [Textkernel x SAP SuccessFactors](https://www.textkernel.com/integrations/sap-successfactors/); [SAP Help: resume parsing](https://help.sap.com/docs/SAP_SUCCESSFACTORS_RECRUITING/8477193265ea4172a1dda118505ca631/07b6d03076a149b78f4f7a615e3025fd.html) |
| **iCIMS** | **Textkernel** (formerly Sovren) | Only vendor blogs say this. Textkernel bought Sovren in 2021 and merged the brands in 2023 | [Textkernel/Sovren](https://www.textkernel.com/sovren/); [Resumap parser test](https://resumap.io/articles/textkernel-parsing-test) |
| **Oracle Taleo / Oracle Recruiting Cloud** | Oracle's own parser, with **Textkernel** and **HireAbility** integrations available | Deloitte runs Textkernel on top of Taleo | [Textkernel on Oracle Recruiting](https://www.textkernel.com/learn-support/blog/oracle-recruiting-cloud-resume-parsing/); [Onrec Deloitte case study](https://www.onrec.com/news/news-archive/case-study-how-deloitte-is-unleashing-the-power-of-its-oracle-taleo-database-with); [HireAbility](https://www.hireability.com/) |
| **Greenhouse** | Built in (no vendor named) | Official list of what causes a failed parse (below). **Parsing is skipped above 2.5 MB** | [Greenhouse: Unsuccessful resume parse](https://support.greenhouse.io/hc/en-us/articles/200989175-Unsuccessful-resume-parse) |
| **Lever** | Built in | Accepts DOCX, PDF, RTF, HTML, ODF. Does not expand acronyms in search, but does stem words. No auto-scoring | [Jobscan on Lever](https://www.jobscan.co/blog/lever-ats/) |
| **Ashby** | LLM-based "AI-Assisted Application Review" | Marks each recruiter-defined criterion Meet, Not Meet or Unknown, with citations. "It is up to the reviewer to advance or reject." | [Ashby product update](https://www.ashbyhq.com/product-updates/ai-assisted-application-review) |
| **SmartRecruiters** | Built in, with a public parse API | Parses DOCX and PDF. Image-only PDFs fail | [SmartRecruiters API: parse a resume](https://developers.smartrecruiters.com/reference/candidatesresumeparse) |
| **Zoho Recruit** (India) | Used **RChilli**. That integration has ended and Zoho now uses its own "Resume inbox" parser | Resumap found Zoho dropped "React" from skills on 8 of 36 templates | [Zoho blog: RChilli](https://www.zoho.com/blog/general/parsing-resumes-in-zoho-recruit-with-rchilli-integration.html); [Zoho Recruit parser](https://www.zoho.com/recruit/parse-resumes.html); [Resumap on DEV](https://dev.to/resumap/we-ran-the-same-resume-through-4-real-ats-parsers-in-36-layouts-same-text-different-parses-3mfc) |
| **Naukri RMS / Resdex** (India) | Built in. Recruiters search the Naukri profile **and** the resume | Recruiters use Boolean search (AND/OR/NOT, quoted phrases) and "EZ Keywords" (Any/All/Exclude). The Naukri **profile headline** is a separate, heavily weighted field | [Naukri Resdex FAQ](https://www.naukri.com/faq/recruiter-resdex); [Naukri Resdex support](https://naukricom.freshdesk.com/support/solutions/folders/273151) |
| **Darwinbox / Keka** (India) | Built in. Darwinbox adds AI "stack ranking" of CVs | Parser vendors aren't public | [Keka glossary: resume parser](https://www.keka.com/glossary/resume-parser); [HROne: Darwinbox vs Keka](https://hrone.cloud/blog/darwinbox-vs-keka) |
| **Freshteam** | n/a | **Being discontinued.** Freshworks stops renewals from March 2026 (some contracts run to April 2027), so don't build anything for it | [People Matters](https://www.peoplematters.in/news/business/freshworks-to-end-freshteam-hr-product-stop-renewals-from-march-2026-47939) |

Parser vendors seen across the market: Textkernel (with Sovren), HireAbility, RChilli, DaXtra, Affinda ([Eden AI parser comparison](https://www.edenai.co/post/best-resume-parser-apis); [RecTech Media list](https://www.rectechmedia.com/blog/2021/2/16/resume-parsing-software)).

### What breaks parsers (official lists first)
**Greenhouse's own support article** lists these causes of a failed parse ([Greenhouse](https://support.greenhouse.io/hc/en-us/articles/200989175-Unsuccessful-resume-parse)):
- files larger than 2.5 MB
- spaced-out letters ("J O H N")
- graphics, photos, word art
- images uploaded instead of documents
- tables, headers and footers
- name or contact details in headers, footers or text boxes
- column layouts
- inconsistent section formatting
- placeholder data that looks fake ("Company 1")

**Harvard FAS career office** says to avoid text boxes, tables, shading and pictures, and to use month and year dates ([Harvard FAS: create a strong resume](https://careerservices.fas.harvard.edu/resources/create-a-strong-resume/)).

**Controlled tests:**
- **Enhancv** ran 357 extraction runs over 17 templates with 7 extractors. Contact details came through in *every* run, even with two columns. What broke was **structure**. With geometry-sorted extraction (which is what `pdftotext -layout` does), section structure was kept 35% of the time in two-column layouts against 60% in single-column. When an LLM read the damaged text, employer names were only 83% correct, against 100% for single-column ([Enhancv two-column test](https://enhancv.com/blog/two-column-resume-ats-test/)).
- **Resumap** ran the same resume in 36 layouts through Textkernel, Workable, Zoho and Manatal. Email and phone were extracted 36/36. The real failure was **work-history segmentation**: Textkernel merged or split jobs on 5 of 36 templates. Whether a two-column layout parses depends on the order the PDF writes its text, not how the columns look. Their conclusion: "nothing is uniformly ATS-friendly" ([Resumap on DEV](https://dev.to/resumap/we-ran-the-same-resume-through-4-real-ats-parsers-in-36-layouts-same-text-different-parses-3mfc); [Resumap Textkernel test](https://resumap.io/articles/textkernel-parsing-test)).

**Specific breakers:**
- **Icon fonts** (FontAwesome phone and envelope icons) extract as junk glyphs.
- **Ligatures** ("fi", "fl", "ffi") can come out as a single Unicode character such as U+FB01, which breaks keyword search: "Financial" no longer matches. The fixes are `cmap` or `glyphtounicode` under pdfTeX, or turning off common ligatures (`Ligatures=NoCommon`) under XeTeX/fontspec, which is what tectonic uses ([ATS Verification: LaTeX traps](https://atsverification.com/blog/latex-resume-ats-friendly/)). Jake's Resume includes `\pdfgentounicode=1` for exactly this reason ([jakegut/resume source](https://raw.githubusercontent.com/jakegut/resume/master/resume.tex)).
- **Skill bars, star ratings and progress circles** are drawings with no text behind them, so the skill level disappears ([Hiration on skill bars](https://www.hiration.com/blog/skill-bars-resume/), a vendor blog).
- **Links shown as anchor text** ("LinkedIn") can be stored as the word "LinkedIn" with the URL dropped ([ATS Verification on hyperlinks](https://atsverification.com/blog/do-ats-read-hyperlinks-resume/), a vendor blog).

**PDF or DOCX?** Both are accepted everywhere: Greenhouse takes .pdf/.docx/.doc/.rtf/.txt ([Enhancv on Greenhouse](https://enhancv.com/blog/how-does-greenhouse-work/)), Taleo indexes .doc/.docx/.pdf/.rtf/.txt/.html/.odt ([Oracle Taleo docs](https://docs.oracle.com/en/cloud/saas/taleo-enterprise/21c/otrcg/c-advancedsearchkeywords.html)), and Lever takes DOCX/PDF/RTF/HTML/ODF ([Jobscan on Lever](https://www.jobscan.co/blog/lever-ats/)). A text-based PDF from a single-column layout is fine. Indian blogs say "upload DOCX to Naukri", but I found no primary evidence for it, so treat it as folklore and consider offering DOCX as an *optional* second output.

### Myth vs fact: "75% of resumes are rejected by ATS"
- **Origin:** a 2012 sales pitch by **Preptel**, a resume-optimisation company that closed in 2013. No method, sample or data was ever published. HR consultant Christine Assaf traced it back and debunked it ([HiringThing](https://blog.hiringthing.com/applicant-tracking-system-myths); [JobCannon research](https://jobcannon.io/research/stats/ats-myth-preptel); [The Interview Guys](https://blog.theinterviewguys.com/ats-resume-rejection-myth/)).
- **What the evidence says instead:** Enhancv interviewed 25 US recruiters in Sep–Oct 2025, working across 10+ ATSs (Workday, iCIMS, Greenhouse, SuccessFactors and others). **92% said their ATS does not auto-reject** for formatting, design or missing keywords. **Knockout questions** (work authorisation, required licence, location) were the only automated filter, and every team used them. Resumes mostly go unseen because of **volume**: 400–600 applicants for entry-level roles, 1,000+ for remote roles, and 52% of recruiters review in the order applications arrive, so applying early matters ([Enhancv: 25 recruiters](https://enhancv.com/blog/does-ats-reject-resumes/)).
- **The real risk, fairly stated:**
  - HBS/Accenture's 2021 "Hidden Workers" study found **88% of employers** agree their systems screen out qualified candidates who don't match exact criteria. This is about rigid *criteria and knockouts*, not formatting ([JobCannon summary of HBS/Accenture](https://jobcannon.io/research/stats/hbs-accenture-hidden-workers-2021)).
  - Taleo lets employers set up workflows that reject automatically on criteria like a missing certification or a failed assessment ([Jobscan on Taleo](https://www.jobscan.co/blog/taleo-popular-ats-ranks-job-applications/)).
  - *Mobley v. Workday* was certified as a nationwide age-discrimination collective action in May 2025, because AI screening that ranks or rejects is legally treated as a hiring function ([Holland & Knight](https://www.hklaw.com/en/insights/publications/2025/05/federal-court-allows-collective-action-lawsuit-over-alleged); [SHRM](https://www.shrm.org/topics-tools/news/technology/workday-ai-lawsuit-wake-up-call-hr)).
  - **Bottom line:** a badly parsed resume usually isn't rejected. It ranks lower, or never turns up in a search.

### How recruiters actually search
- **Taleo (Oracle's own documentation)** ([Oracle Taleo docs](https://docs.oracle.com/en/cloud/saas/taleo-enterprise/21c/otrcg/c-advancedsearchkeywords.html)):
  - "Exact Term" mode joins words with AND by default and supports quoted phrases, wildcards (`comput*`) and parentheses.
  - "Related Terms" adds automatic OR plus words sharing the same first 6 letters.
  - "Conceptual" search takes whole blocks of text.
  - Special characters are not indexed.
  - The search covers resume attachments, employer, job function and education fields.
- **Greenhouse:** "Full Text Search" covers resumes and notes, with Boolean AND/OR/NOT, quoted phrases and parentheses ([Greenhouse: search resumes for keywords](https://support.greenhouse.io/hc/en-us/articles/115004600186-Search-resumes-for-keywords); [Greenhouse: Boolean](https://support.greenhouse.io/hc/en-us/articles/202360199-Search-candidates-using-Boolean-queries)).
- **Jobscan recruiter survey** ([Jobscan ATS overview](https://www.jobscan.co/applicant-tracking-systems); [Jobscan ATS resume guide](https://www.jobscan.co/blog/ats-resume/)):
  - 99.7% of recruiters use keyword filters.
  - 76.4% filter by skills from the job description.
  - 55.3% filter by job title.
  - Resumes containing the target job title got **10.6x more interviews**. This is Jobscan's own data, and it is correlational.

---

## 2. Keyword matching

- **Exact phrases vs synonyms.**
  - Older engines are literal. Oracle's docs show Taleo's default is exact-term AND, with synonyms only if the recruiter switches to Related or Conceptual mode ([Oracle](https://docs.oracle.com/en/cloud/saas/taleo-enterprise/21c/otrcg/c-advancedsearchkeywords.html)). Jobscan says Taleo won't match "project manager" to "project management", or "CPA" to "Certified Public Accountant" ([Jobscan on Taleo](https://www.jobscan.co/blog/taleo-popular-ats-ranks-job-applications/)).
  - Lever stems word forms ("collaborate/collaborated") but doesn't expand acronyms ([Jobscan on Lever](https://www.jobscan.co/blog/lever-ats/)).
  - Newer LLM or semantic scorers (Ashby, HiredScore, Taleo's "Suggested Candidates") match on meaning ([Ashby](https://www.ashbyhq.com/product-updates/ai-assisted-application-review); [Jobscan on Taleo](https://www.jobscan.co/blog/taleo-popular-ats-ranks-job-applications/)).
  - **Implication:** write for the literal engine, because the semantic ones will cope anyway.
- **Acronym plus full form.** On first use, write "Search Engine Optimization (SEO)", then use either form after that ([Jobscan ATS resume guide](https://www.jobscan.co/blog/ats-resume/); [Jobscan on Lever](https://www.jobscan.co/blog/lever-ats/)).
- **Job title.** Put the target title (as the JD words it, if truthful) in the headline under the name. That's the source of the 10.6x stat ([Jobscan](https://www.jobscan.co/blog/ats-resume/)). On Naukri the separate *profile headline* does this job, and anything past ~80 characters is cut off on mobile ([CV Ninja](https://www.cvninja.in/guides/naukri-resume-headline-examples), a vendor blog).
- **Repetition.** No vendor publishes a density threshold. The common practitioner advice is to use each key term 2–3 times in different places (summary, skills, one bullet) ([ScoreMyResume on Naukri](https://www.scoremyresume.co/blog/naukri-ats-resume-tips), a vendor blog). Jobscan warns against repeating keywords unnaturally ([Jobscan match rate](https://www.jobscan.co/blog/what-jobscan-match-rate-should-i-aim-for/)).
- **Keyword stuffing and hidden white text.**
  - Parsers pull out plain text, so white text *is visible* in the parsed profile the recruiter sees. Some recruiters plant a nonsense trap word in the JD to catch people who pasted it in ([Hiration on white text](https://www.hiration.com/blog/white-text-resume-hack/); [Prosple](https://prosple.com/applying/debunking-the-white-font-resume-hack-from-tiktok)).
  - A Duke-led study of **200,000 resumes on hireEZ** found hidden injections in at least 1% of them, up **sevenfold between Jul 2024 and Nov 2025**. More than 90% were hidden "professional content" (extra skills or experience) rather than commands ([Duke Pratt](https://pratt.duke.edu/news/thwarting-prompt-injection/); [UNC CS](https://cs.unc.edu/news-article/thwarting-hidden-resume-hacks-targeting-ai-hiring-tools/)).
  - In Greenhouse's 2025 survey, 22% of hiring managers said they had caught prompt injections in resumes ([Greenhouse newsroom](https://www.greenhouse.com/newsroom/an-ai-trust-crisis-70-of-hiring-managers-trust-ai-to-make-faster-and-better-hiring-decisions-only-8-of-job-seekers-call-it-fair)).
  - **Our agent must never generate hidden text, and should strip it from uploaded resumes.**
- **Match-score tools.**
  - **Jobscan** recommends **80%**, and says 75% often works. It warns explicitly against chasing 100%, keyword cramming and white text ([Jobscan match rate](https://www.jobscan.co/blog/what-jobscan-match-rate-should-i-aim-for/)).
  - **Resume Worded's** Relevancy Score counts required qualifications more heavily than preferred ones. Above 80 counts as well matched ([Resume Worded score guide](https://resumeworded.com/score-guide); [Targeted Resume](https://resumeworded.com/targeted-resume)).
  - Neither tool shows you the employer's actual score. They estimate keyword overlap.
- **What "match score" should mean for our agent.** A **coverage report, not a pass/fail gate**:
  - (a) required hard skills from the JD found verbatim (or as acronym plus full form) in the extracted PDF text, weighted about 2x
  - (b) preferred skills
  - (c) the target title, or a close variant, in the headline
  - (d) every hit backed by a bullet, not just the skills list

  Target 75–85%. Anything above 90% should raise a "possible stuffing" warning, not celebration. **Never add a keyword the user can't back with a real bullet.** Knockout questions (work authorisation, notice period, CTC, location) matter more than score, so the application-form stage should handle them.

---

## 3. ATS fix checklist (the reasoning; the checkable list is at the end)

- **One column, and the text must come out in reading order.** Our gate uses `pdftotext -layout`, which is geometry-sorted, the harshest extraction family in Enhancv's test ([Enhancv](https://enhancv.com/blog/two-column-resume-ats-test/)). If a layout passes `-layout` *and* default `pdftotext` (the reading-order family), it's safe across all three families. Also check `-raw` (content-stream order) ([Enhancv methodology](https://enhancv.com/blog/two-column-resume-ats-test/)).
- **Nothing in the header or footer.** Greenhouse names this directly ([Greenhouse](https://support.greenhouse.io/hc/en-us/articles/200989175-Unsuccessful-resume-parse)). In LaTeX, avoid `fancyhdr` content. Page numbers on page 2 are fine only if they're trivial.
- **Contact details in the body as plain text.** No icon glyphs (write "Email:" or just the address), and write URLs out in full ([ATS Verification: LaTeX traps](https://atsverification.com/blog/latex-resume-ats-friendly/); [ATS Verification: hyperlinks](https://atsverification.com/blog/do-ats-read-hyperlinks-resume/)).
- **Standard section names:** Summary, Experience or Work Experience, Education, Skills, Projects, Certifications ([Jobscan](https://www.jobscan.co/blog/ats-resume/); [Enhancv on Ashby](https://enhancv.com/blog/how-does-ashby-work/)).
- **Dates.** "Mon YYYY – Present" or MM/YYYY, *one style throughout*. No seasons, no '21, no day-month-year ([Jobscan resume dates](https://www.jobscan.co/blog/resume-dates/)). Harvard uses month and year ([Harvard FAS](https://careerservices.fas.harvard.edu/resources/create-a-strong-resume/)). Parsers work out years of experience from these dates, and recruiters filter on that number.
- **Keep each role's employer, title and dates together**, in the same order for every role, because parsers segment jobs by that pattern ([Resumap](https://dev.to/resumap/we-ran-the-same-resume-through-4-real-ats-parsers-in-36-layouts-same-text-different-parses-3mfc)).
- **Fonts** must extract as real letters: no ligature code points, no private-use-area glyphs ([ATS Verification](https://atsverification.com/blog/latex-resume-ats-friendly/)).
- **Bullets.** Standard characters (•, –) or LaTeX `itemize`. Check they come out as a single known glyph and not as "" or "?".
- **No tables, text boxes, shading, images, skill bars or spaced-out letters** ([Greenhouse](https://support.greenhouse.io/hc/en-us/articles/200989175-Unsuccessful-resume-parse); [Harvard FAS](https://careerservices.fas.harvard.edu/resources/create-a-strong-resume/)).
- **File size under 2.5 MB** (Greenhouse's parse limit, [Greenhouse](https://support.greenhouse.io/hc/en-us/articles/200989175-Unsuccessful-resume-parse)). Our LaTeX PDFs are about 50–150 KB, so set the limit at 1 MB to catch accidental embedded images.
- **File name** `Firstname_Lastname_Resume.pdf`, optionally with the role (practitioner convention; [GDPIWAT IIM guide](https://gdpiwat.in/resume/iim-resume-format/)).
- **Length.** One page for early career. Two pages is fine for experienced people: in ResumeGo's 2018 study (482 recruiters), recruiters were 2.3x more likely to prefer two-page resumes for experienced candidates ([ResumeGo](https://www.resumego.net/research/one-or-two-page-resumes/); [CNBC](https://www.cnbc.com/2018/12/19/resumego-hiring-managers-prefer-candidates-with-two-page-resumes.html)).
- **Human skim test.** In the Ladders 2018 eye-tracking study (30 recruiters), people skimmed for an average of 7.4 seconds. Simple layouts with clear headings, bold titles and bullets did well. Multiple columns, clutter and long sentences did badly ([Ladders PDF](https://www.theladders.com/static/images/basicSite/pdfs/TheLadders-EyeTracking-StudyC2.pdf); [HR Dive](https://www.hrdive.com/news/eye-tracking-study-shows-recruiters-look-at-resumes-for-7-seconds/541582/)).

---

## 4. Layouts that work in the market

### Font, size and margin guidance
- **Body size 10–12 pt.** Harvard OCS guidance is 10–12 pt with a common font ([Harvard GSAS handout](https://apsanet.org/Portals/54/web/Handout%204%20-%20Harvard%20Resume%20Examples.pdf?ver=BQ3JG_ifefqqOOsrYio64A%3D%3D&timestamp=1653587429108); [Microsoft Word blog](https://word.cloud.microsoft/create/en/blog/best-resume-fonts/)). Our current 10.5 pt is fine. Don't go below 10 pt to fit a page; cut content instead.
- **Margins.**
  - Our 0.45 in margins are at the aggressive end. Jake's Resume effectively uses about 0.5 in: the default margins are cut by 0.5 in, on letter paper ([jakegut source](https://raw.githubusercontent.com/jakegut/resume/master/resume.tex)).
  - Most guides say 0.5–1 in, and IIM-style guides say 0.5–0.75 in ([GDPIWAT](https://gdpiwat.in/resume/iim-resume-format/)).
  - Butterick's main complaint about resumes is that they're "uncomfortably dense" ([Practical Typography](https://practicaltypography.com/resumes.html)).
  - **Recommendation:** 0.5 in minimum for dense templates and 0.6–0.75 in for the others. For India, use **A4** by default and Letter for US jobs.
- **Fonts.** All of these are free and embeddable with tectonic:
  - **Charter** (XCharter on CTAN, free Bitstream licence, has small caps) ([CTAN XCharter](https://ctan.org/pkg/xcharter?lang=en))
  - **Latin Modern / Computer Modern** (the Jake's Resume default)
  - **Source Sans 3** (used by Awesome-CV) ([Awesome-CV](https://github.com/posquit0/Awesome-CV))
  - **Lato** (used by Deedy) ([Deedy-Resume](https://github.com/deedy/Deedy-Resume))
  - **EB Garamond** (a Garamond clone)
  - **Carlito** (metric-compatible with Calibri)

  Calibri and Garamond are widely recommended ([Monster](https://www.monster.com/career-advice/resume/best-fonts-for-resumes)), but Calibri is proprietary, so **Carlito** is the substitute. Butterick advises against system fonts like Calibri on quality grounds ([Practical Typography](https://practicaltypography.com/resumes.html)). No font is "ATS-unsafe" as long as it's embedded, text-based and has a proper ToUnicode map. What matters is that extraction works, which our gate checks.

### Layout table

| # | Name | Look | Best for | ATS safety | Reference |
|---|---|---|---|---|---|
| 1 | **Jake's / sb2nov "Tech Compact"** | Single column. Computer Modern 11 pt, about 0.5 in margins, small-caps section titles with a rule underneath. Company and title on the left, dates right-aligned on the same line, tight bullets, no colour | SWE, data, tech PM, students and early career. The most recommended template on r/cscareerquestions and r/EngineeringResumes | **High.** Includes `\pdfgentounicode=1`. One risk: tabular right-aligned dates can separate from their line in `-layout` output, so the gate must check the date stays with its role | [jakegut/resume](https://github.com/jakegut/resume), [Overleaf](https://www.overleaf.com/latex/templates/jakes-resume/syzfjbzwjncs), [sb2nov/resume](https://github.com/sb2nov/resume) |
| 2 | **Harvard Classic** | Single column. Serif (Times or Garamond style) 10–12 pt, name centred, all-caps or bold section titles with a rule, Education first for students, month-year dates, no colour | Consulting, finance, business, policy; students and MBAs; conservative Indian corporates | **High.** Harvard explicitly bans tables, text boxes and shading | [Harvard FAS templates](https://careerservices.fas.harvard.edu/resources/category/resume-cv-cover-letter-templates/), [Harvard guide](https://careerservices.fas.harvard.edu/resources/create-a-strong-resume/), [RenderCV "harvard" theme](https://docs.rendercv.com/user_guide/yaml_input_structure/design/) |
| 3 | **Modern Minimal (one accent)** | Single column. Sans body (Source Sans 3 or Lato 10–10.5 pt), name large in the accent colour, section titles in the accent colour, thin rules, no icons. Similar to Awesome-CV's look but without its icon header | Product, growth, marketing, design-adjacent roles; people who want "modern" without the risk | **High** if icons are dropped and the accent is kept to headings (colour doesn't affect extraction). Awesome-CV as shipped uses FontAwesome icons in the header, which is a risk | [Awesome-CV](https://github.com/posquit0/Awesome-CV) (style reference), [RenderCV ember/opal/ink themes](https://github.com/rendercv/rendercv) |
| 4 | **Engineering Resumes (r/EngineeringResumes)** | Single column. Serif or sans 10–11 pt, left-aligned headings, no summary for students, skills at the top or bottom, dense but with clear spacing | Non-software engineers (mechanical, electrical, core engineering), freshers | **High** | [RenderCV "engineeringresumes"/"engineeringclassic" themes](https://docs.rendercv.com/user_guide/yaml_input_structure/design/) |
| 5 | **Charter Professional** (our current layout, tidied) | Single column. Charter 10.5 pt, 0.5–0.6 in margins, bold company, italic title, Summary > Experience > Education > Skills | General default for experienced professionals in any function | **High** (already gated) | [CTAN XCharter](https://ctan.org/pkg/xcharter?lang=en), [Practical Typography](https://practicaltypography.com/resumes.html) |
| 6 | **Executive Two-Page** | Single column. Serif 11 pt, 0.75 in margins, headline plus a 3–4 line summary, "Core Competencies" as a comma-separated line (not a grid), career highlights, full history over two pages with the name repeated in the *body* at the top of page 2 (not in a running header) | Directors, VPs, 12+ years | **High**, provided there are no header/footer running names and no competency tables | [ResumeGo study](https://www.resumego.net/research/one-or-two-page-resumes/), [Briefcase Coach](https://www.briefcasecoach.com/how-long-can-my-executive-resume-be-are-two-or-three-pages-in-length/) |
| 7 | **Indian B-School / Placement (ATS-safe version)** | Single column, one page, Calibri/Carlito or Times 10–11 pt, 0.5–0.75 in margins. Education *first* with CGPA/% and year on one line per degree (written as text, not a grid). Sections: Education, Work Experience, Internships, Positions of Responsibility, Achievements, Extra-curriculars. Bold numbers | MBA grads, IIT/IIM/NIT freshers, Indian consulting, BFSI and FMCG roles | **Medium-high** as a text version. The *real* institute template (bordered tables, vertical side labels, logo) is **Low** because tables break parsers | [GDPIWAT IIM format](https://gdpiwat.in/resume/iim-resume-format/), [Scribd IIM CV template](https://www.scribd.com/document/729931367/IIM-CV-Template) |
| 8 | **Academic CV** | Single column, multi-page. Serif 11 pt. Publications in citation style, then Grants, Teaching, Talks. Dates on the left or right | Research, PhD, faculty, research scientists | **High** (text-only). ATS matters less here because these are usually read by people | [RenderCV academic CV guide](https://rendercv.com/blog/academic-cv-template), [moderncv on CTAN](https://ctan.org/pkg/moderncv) |
| 9 | **Deedy (two-column)** | Two uneven columns. Lato and Raleway, skills and education in a sidebar, experience in the main column | CS undergraduates wanting maximum density | **Low-medium.** Sidebar text gets interleaved or scrambled under geometry-sorted extraction | [Deedy-Resume](https://github.com/deedy/Deedy-Resume); [ATS Verification test](https://atsverification.com/blog/latex-resume-ats-friendly/); [Enhancv test](https://enhancv.com/blog/two-column-resume-ats-test/) |
| 10 | **AltaCV / creative sidebar** | Two columns, icons, colour, rating bars, sometimes a photo | Designers applying by portfolio or referral | **Low.** Columns, icon fonts and skill bars all hit Greenhouse's failure list | [AltaCV](https://github.com/liantze/AltaCV), [Modified-AltaCV (ATS fix)](https://github.com/bradley-rose/Modified-AltaCV-LaTeX-Resume-Template) |

**About "Google/FAANG style":** this isn't a distinct visual layout. In practice it's the Jake's/sb2nov single column plus X-Y-Z bullets ("Accomplished [X] as measured by [Y], by doing [Z]", from Laszlo Bock) ([UT Law copy of Inc. article](https://law.utexas.edu/wp-content/uploads/sites/44/2020/09/Google-Recruiters-Say-Using-the-X-Y-Z-Formula-on-Your-Resume-Will-Improve-Your-Odds-of-Getting-Hired-at-Google-_-Inc.com_.pdf)). Treat it as a content style on top of layout #1, not a separate template.

### Recommendation: build these 7 (all single column)
1. **Tech Compact** (Jake's style, Latin Modern 10.5–11 pt)
2. **Charter Professional** (the current default, with margins raised to 0.5 in)
3. **Harvard Classic** (serif, name centred, Education-first option)
4. **Modern Accent** (Source Sans 3 or Lato, one user-selectable accent colour on the name and headings, no icons)
5. **Executive Two-Page**
6. **Indian Placement (text version)** (Education-first with CGPA/%, a Positions of Responsibility section, no tables)
7. **Academic CV**

Optional 8th: **Engineering Classic** (a sans variant of #1 for core engineering).

**Don't build:**
- Deedy, AltaCV or any two-column sidebar layout. Textkernel still only renders about 90% of column CVs well, and geometry-sorted extraction loses structure.
- The real IIM table grid.
- Anything with icons, photos, skill bars or header/footer content. Each one appears on Greenhouse's official failure list.

If users ask for "creative", offer Modern Accent and point them to a portfolio link.

### RenderCV as prior art
RenderCV turns one YAML/JSON-schema CV into 9 themes (classic, engineeringresumes, sb2nov, moderncv, engineeringclassic, harvard, ink, opal, ember). It now renders with **Typst**, and "all themes are identical except for their default values" ([RenderCV GitHub](https://github.com/rendercv/rendercv); [RenderCV design docs](https://docs.rendercv.com/user_guide/yaml_input_structure/design/)). That's the architecture we want: **one JSON, and each layout is a set of design parameters on shared LaTeX macros.** It also keeps the ATS gate identical across layouts.

---

## 5. Keeping the format of a resume the user uploads

| Option | How | Pros | Cons | Tooling |
|---|---|---|---|---|
| **(a) Edit the DOCX in place** | Open the user's DOCX and replace text run by run, keeping paragraph and run styles | Keeps their exact look, fonts and spacing. Recruiters see "their" resume | Word splits text into many runs, so a phrase can span several runs. Setting `paragraph.text` wipes bold and italic. python-docx has no helpers to split or copy runs. Adding or removing bullets means cloning paragraphs. Users' DOCX files often *use* tables and text boxes, so we'd be keeping ATS problems too. Needs LibreOffice to convert to PDF, then our gate | [python-docx text API](https://python-docx.readthedocs.io/en/latest/api/text.html); [issue #415](https://github.com/python-openxml/python-docx/issues/415); [python-docx-replace](https://pypi.org/project/python-docx-replace); [docxtpl](https://docxtpl.readthedocs.io/) (Jinja tags work only within a single run) |
| **(b) Detect their layout and map it to our nearest template** | Extract fonts, sizes, section order, date style, column count and accent colour from the PDF/DOCX. Pick the nearest of our 7 layouts, apply their font and colour if allowed, then render from JSON | Always passes our ATS gate. One rendering path. Can *fix* their ATS problems (columns become one column) and tell them what changed | Not pixel-identical. The user may notice it "looks different" | [pdfplumber](https://github.com/jsvine/pdfplumber) (per-character `fontname`, `size`, `x0`/`top`, `non_stroking_color`, plus lines, rects and tables), PyMuPDF |
| **(c) Replace text inside the PDF** | Redact the old text and insert new text at the same coordinates | Pixel-identical when the edit is tiny | PDFs don't reflow, so longer text overlaps the next word. The embedded (subset) font usually **can't** be reused for new text. Inserted text is added to the end of the page's content, so it's "read last by naive text extraction", which **breaks reading order, the very thing our gate checks** | [Artifex blog](https://artifex.com/blog/how-to-search-and-replace-text-in-pdfs-using-pymupdf); [PyMuPDF discussion #3422](https://github.com/pymupdf/PyMuPDF/discussions/3422); [PyMuPDF #3396](https://github.com/pymupdf/PyMuPDF/discussions/3396) |

**Recommendation:** make **(b) the default**. Offer **(a) only for DOCX uploads that already pass the ATS gate**: single column, no tables or text boxes, nothing in headers or footers (python-docx can inspect `section.header`, tables and inline shapes to check). Limit (a) to edits within a paragraph plus cloning existing bullet paragraphs. **Don't build (c).**

---

## 6. AI-written text detection in resumes (short)

- **The major ATSs don't run AI-writing detectors.** Jobscan reports statements from Workday/HiredScore, Greenhouse, Lever and iCIMS that "there is no feature that detects AI-generated writing", and Oracle says nothing penalises it ([Jobscan: can ATS detect AI](https://www.jobscan.co/blog/can-ats-detect-ai-resume/); these are relayed by Jobscan, not quoted from vendor docs). Greenhouse's own AI feature list covers talent matching, resume anonymisation and keyword suggestions, with no AI-writing detector ([Greenhouse AI features](https://support.greenhouse.io/hc/en-us/articles/33043749845403-Greenhouse-AI-features)).
- **LinkedIn:** recruiters see only the final submitted content and aren't told whether AI wrote it (reported by third parties; see [LinkedIn Hiring Assistant news](https://news.linkedin.com/2025/hiring-assistant-globally-available)).
- **Humans do judge it:**
  - TopResume, May 2025, 600 US hiring managers: **19.6% would reject** an application they thought was fully AI-generated, and 33.5% say they can spot one in under 20 seconds ([TopResume survey](https://topresume.com/career-advice/ai-in-hiring-survey)).
  - Resume Now, 2025: 62% would reject AI resumes *that lack personalisation* ([Resume Now](https://www.resume-now.com/job-resources/careers/ai-applicant-report)).
  - Greenhouse 2025 (4,136 respondents): 65% of hiring managers have caught deceptive AI use ([Greenhouse newsroom](https://www.greenhouse.com/newsroom/an-ai-trust-crisis-70-of-hiring-managers-trust-ai-to-make-faster-and-better-hiring-decisions-only-8-of-job-seekers-call-it-fair)).
- **What gives AI resumes away:** generic phrasing, every bullet built the same way, vague power verbs with no proof, skills that don't match the experience level ([Jobscan](https://www.jobscan.co/blog/can-ats-detect-ai-resume/)).
- **Takeaway for the agent:** the risk is *generic* text, not detection software. Every bullet needs a specific noun (product, metric, tool) that came from the user.

---

## ATS rules our code can check automatically

Run these on the **rendered PDF**, plus the JSON where noted. The tools are `pdftotext` (poppler), `pdffonts`, `pdfinfo` and pdfplumber/PyMuPDF.

1. **Text layer exists.** `pdftotext` output has more than N characters per page, and `pdffonts` lists embedded fonts. *Fail* if it's empty (image-only PDF).
2. **Reading order survives all three extraction modes.** Extract with `pdftotext -layout`, default `pdftotext` and `pdftotext -raw`. In each one, check that employers appear in JSON order, and that each role's date string appears within K lines of its employer and title. *Fail* if any mode reorders them.
3. **Required tokens present.**
   - Normalise the extracted text with NFKC and collapse whitespace.
   - Assert that the email, phone, name, every employer, every degree and institution, and every date range are found verbatim.
   - Also check the **raw (not NFKC-normalised) text**. If a token only matches after NFKC, a ligature or compatibility character leaked through.
4. **No ligature or junk code points.** Scan the extracted text for U+FB00–U+FB06 (ﬀ ﬁ ﬂ ﬃ ﬄ), Private Use Area U+E000–U+F8FF (icon fonts), U+FFFD, and control characters. *Fail* if any are found. Fix in LaTeX with `Ligatures=NoCommon` (fontspec/tectonic).
5. **Nothing in header or footer zones.** With pdfplumber, flag any character whose `top` is less than about 0.4 in from the page top or whose `bottom` is within about 0.4 in of the page bottom (the thresholds depend on the template's margins), except a page-number pattern on pages 2 and later. Also check the name and contact line sit inside the body box.
6. **Single column.** Using pdfplumber word boxes, build a histogram of word `x0` values per line band. *Flag* if one line band has two or more text clusters separated by a gap wider than about 25% of the text width, and that happens on more than 3 lines that aren't date lines. (This allows the right-aligned date on a role line.)
7. **No tables, text boxes or images.** Using pdfplumber `rects` and `images`, and `page.find_tables()`, *fail* on any image or detected table, and on any rect enclosing text other than thin horizontal rules. (DOCX: `doc.tables`, `inline_shapes`, and `w:txbxContent` in the XML.)
8. **No hidden text.** Using pdfplumber characters, *fail* if `non_stroking_color` is white or near-white, if `size` is under 6 pt, or if text sits outside the page's MediaBox. (Also strips prompt-injection text from uploaded resumes.)
9. **Standard section headings.** Match the headings (bold, larger or small-caps lines, or straight from the template's JSON) against an allow-list: Summary, Professional Summary, Experience, Work Experience, Professional Experience, Education, Skills, Technical Skills, Projects, Certifications, Publications, Awards, Achievements, Positions of Responsibility, Volunteering. *Warn* on anything else.
10. **Consistent date format.** Regex over the JSON dates and the extracted text. Every date matches `^(Jan|Feb|…|Dec) \d{4}$` or `^\d{2}/\d{4}$` or `Present`, and **only one style** is used in the document. *Fail* on seasons, `'21`, day-month-year, or mixed styles. Also check start ≤ end and roles are in reverse-chronological order.
11. **Contact info in plain text.** The email matches an email regex and the phone matches the Indian `+91` or E.164 pattern. Each URL appears as visible text (`linkedin.com/in/…`), not only as a link annotation: compare the PDF's `/URI` annotations with the extracted text.
12. **Bullets extract cleanly.** Each bullet line starts with one allowed glyph (`•`, `-`, `–`, `▪`) or none. No `?` or U+FFFD at the start of a line.
13. **No spaced-out letters.** Regex `\b(?:[A-Za-z] ){3,}[A-Za-z]\b` on the extracted text catches letter-spaced names and headings, a named Greenhouse failure. Also check letter-spacing in the LaTeX template.
14. **Fonts embedded.** `pdffonts` shows `emb=yes` for every font, and `uni=yes` (a ToUnicode map is present).
15. **File size and pages.** File size under 1 MB (hard limit 2.5 MB, Greenhouse). Page count is 1 or 2 by layout (academic CV exempt). `pdfinfo` reports the page size as A4 or Letter as chosen.
16. **File name.** `^[A-Z][a-z]+_[A-Z][a-z]+_Resume(_[A-Za-z]+)?\.pdf$`, with no spaces and nothing like "final_v3".
17. **Acronyms expanded once.** For each JD acronym found in the resume (from a dictionary: SEO, SQL, PMP, CRM, …), check that the full form appears at least once, or `Full Form (ACR)` appears. *Warn* if not.
18. **Target title in the headline.** A fuzzy match (≥0.8 token-set ratio) between the JD title and the headline or summary line. *Warn* if it's missing.
19. **Keyword coverage score.** Required JD skills found in the extracted text, weighted 2x, plus preferred skills. Target 75–85%. *Warn* at 90% or above ("possible stuffing"). *Warn* if any keyword appears **only** in Skills with no supporting bullet, or appears more than 4 times.
20. **Body font size.** Using pdfplumber character sizes, the most common body size must be at least 10 pt (at least 9.5 pt allowed only in Tech Compact), and the smallest text at least 8 pt.
21. **Margins.** Using pdfplumber, the text bounding box sits at least 0.45–0.5 in from each edge.
22. **Metadata.** `pdfinfo` Title is set to "Firstname Lastname – Resume" and Author to the user's name. Some parsers and recruiters see these, and "Microsoft Word – Document1" looks careless. This is a low-stakes nice-to-have.

## Myths to ignore
- **"75% of resumes are auto-rejected by ATS."** It comes from a 2012 Preptel sales pitch with no data. 92% of recruiters in Enhancv's 2025 interviews said their ATS doesn't auto-reject on format or keywords ([JobCannon](https://jobcannon.io/research/stats/ats-myth-preptel); [Enhancv](https://enhancv.com/blog/does-ats-reject-resumes/)).
- **"Never send a PDF."** Every ATS above accepts text-based PDFs, and Greenhouse, Lever and Taleo index them ([Oracle Taleo](https://docs.oracle.com/en/cloud/saas/taleo-enterprise/21c/otrcg/c-advancedsearchkeywords.html); [Jobscan on Lever](https://www.jobscan.co/blog/lever-ats/)).
- **"Colour or a serif font will get you rejected."** Colour and typeface don't change the extracted text. What matters is embedding and the ToUnicode map (a checkable rule). Harvard allows Times or Arial; either is fine ([Harvard GSAS](https://apsanet.org/Portals/54/web/Handout%204%20-%20Harvard%20Resume%20Examples.pdf?ver=BQ3JG_ifefqqOOsrYio64A%3D%3D&timestamp=1653587429108)).
- **"Hide JD keywords in white text."** It shows up in the parsed text, can be flagged, and is treated as deception ([Hiration](https://www.hiration.com/blog/white-text-resume-hack/); [Duke](https://pratt.duke.edu/news/thwarting-prompt-injection/)).
- **"Aim for a 100% match score."** Jobscan itself says 75–80% and warns against over-optimising ([Jobscan](https://www.jobscan.co/blog/what-jobscan-match-rate-should-i-aim-for/)).
- **"ATSs run AI detectors on your resume."** Major vendors say they don't. The risk is human reviewers reacting to generic text ([Jobscan](https://www.jobscan.co/blog/can-ats-detect-ai-resume/)).
- **"Resumes must be one page."** Two pages is fine, and often preferred, for experienced candidates ([ResumeGo](https://www.resumego.net/research/one-or-two-page-resumes/)).
- **"Two-column resumes always fail."** It's more nuanced: it depends on the parser and the order the PDF writes its text ([Resumap](https://dev.to/resumap/we-ran-the-same-resume-through-4-real-ats-parsers-in-36-layouts-same-text-different-parses-3mfc)). We still avoid them because we can't control which parser each employer runs, and even Textkernel only renders about 90% of column CVs well ([Textkernel](https://www.textkernel.com/learn-support/blog/improving-extraction-from-column-resumes/)).
- **"Specific ATS X only reads DOCX / needs special formatting."** Most "Workday/Taleo/iCIMS secret rules" pages are vendor SEO with no primary source. One clean single-column text PDF works across all of them.

---

## Source list (distinct, 60+)
Primary or vendor docs: Greenhouse support (failed parse, keyword search, Boolean, AI features); Oracle Taleo docs; Textkernel column blog; Textkernel SAP page; Textkernel/Sovren; SAP Help; Ashby product update; Workday HiredScore; SmartRecruiters API; Zoho blog and Zoho Recruit; Naukri Resdex FAQ and support; Keka glossary; Harvard FAS (guide and templates); Harvard GSAS handout; Jake's Resume source; sb2nov; Deedy; Awesome-CV; AltaCV; Modified-AltaCV; RenderCV (GitHub, docs, 2 blogs); CTAN XCharter; moderncv (CTAN); pdfplumber; python-docx docs and issue #415; python-docx-replace; docxtpl; Artifex blog; PyMuPDF discussions #3422 and #3396; Overleaf Jake's.
Studies and surveys: Enhancv 25-recruiter study; Enhancv two-column test; Resumap (DEV and site); HBS/Accenture (via JobCannon); Duke/UNC prompt-injection study; Greenhouse 2025 AI in Hiring; TopResume 2025; Resume Now 2025; Ladders 2018 eye-tracking (PDF and HR Dive); ResumeGo 2018 (and CNBC); Jobscan ATS usage report.
Practitioner and other: Jobscan (ATS resume, match rate, dates, Lever, Taleo, AI detection, ATS overview); HiringThing; The Interview Guys; ATS Verification (LaTeX traps, hyperlinks); Hiration (white text, skill bars); Prosple; Practical Typography; Monster fonts; Microsoft Word blog; GDPIWAT; Scribd IIM template; CV Ninja; ScoreMyResume; People Matters (Freshteam); Holland & Knight and SHRM (Mobley); Josh Bersin; Eden AI; RecTech Media; HROne; Resume Worded (score guide, Targeted Resume); UT Law copy of the Inc. X-Y-Z article; Briefcase Coach.
