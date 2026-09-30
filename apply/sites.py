"""Which job sites the form filler has been tested on, and so may apply to.

`engine.adapters.FORM_ATS` says a site's apply button leads to a form anyone
can fill. That is about the site. This file is about us: whether our filler
has filled a real form there, end to end, stopping just before submit. On
2026-09-29 SmartRecruiters and Keka jobs went live on the first list without
being on this one, and all three runs that night failed in ways a single test
form would have shown (see apply/pagecheck.py).

A site joins by passing `python3 evals/apply_forms.py` (real forms, filled with
a test identity, never submitted); the report says which sites passed and why
the rest did not. Everything else gets the link and a tailored resume.

`APPLY_SITES` in .env overrides the list (comma-separated), for a test run.
"""

from __future__ import annotations

import os

#: Passed the form eval. Keep the reason next to each; it is the evidence.
#: Form eval of 2026-09-30 (evals/out/apply_forms/), 5 live forms each, local
#: Chrome, never submitted. Numbers are from the last run after each fix.
TESTED = {
    "greenhouse": "5/5 after the upload-chip and 'No options' fixes (3/5 before)",
    "lever": "5/5; hCaptcha may show at submit",
    "keka": "5/5; the stored link is the job page, open_form presses its Apply",
    "ashby": "5/5",
    "workable": "5/5 after date fields (react-datepicker) were typed key by key (4/5 before)",
    "teamtailor": "5/5 after radios got a label-click fallback and 'Apply Now!' / "
                  "'Apply for this job here' open the form (0/5 before); a required "
                  "video question, where a form has one, cannot be answered",
    "recruitee": "4/4 plus 1 dead job link, after 'Send' was taught as a submit button (0/5 before)",
    "breezy": "5/5 after 'Apply To Position', next_page for its 'Continue' page and "
              "skipping its off-screen spam-trap field (0/5 before)",
}
#: Failed or not yet shown to pass. They get the link and a tailored resume.
NOT_YET = {
    "smartrecruiters": "0/5 counted. Two forms filled page 1 with no required field "
                       "empty and stopped only because that eval run had no next_page "
                       "step; three could not open (Browserbase free minutes used up). "
                       "A manual cloud run on Swiggy reached the Submit page via the "
                       "resume autofill + next_page. Needs one cloud eval to pass.",
}


def tested() -> set[str]:
    raw = os.environ.get("APPLY_SITES")
    if raw is not None:
        return {s.strip().lower() for s in raw.split(",") if s.strip()}
    return set(TESTED)


def can_fill(source: str | None) -> bool:
    return (source or "").lower() in tested()
