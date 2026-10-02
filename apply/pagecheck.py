"""What a page says about an application, decided by code, not the model.

Written after the first three live runs (2026-09-29), none of which sent
anything, and one of which was recorded as sent:

- Swiggy (SmartRecruiters): the reader saw no fields, the agent called
  `submit`, and the submit tool clicked the first button with "apply" in it:
  "Apply With Indeed". The URL did not change and there was no captcha, so it
  returned "submitted". The screenshot shows the form still empty.
- Clickpost (Keka): the stored link was the job description. The form was one
  "Apply for this job" click away. The agent, which cannot see a screenshot,
  guessed it was a sign-in page.

So: a button that applies through someone else (Indeed, LinkedIn...) is never
a submit button; a click is only "submitted" when the page confirms it; and a
page with no form is opened by its own Apply button before anyone guesses.
"""

from __future__ import annotations

import re

#: A button that finishes the application.
SUBMIT = re.compile(r"\b(submit|send application|apply( now)?|finish|complete application)\b"
                    r"|^\s*send\s*$", re.I)            # Recruitee's button says "Send"
#: Buttons that are never ours to press as "submit": another company applies
#: with the person's account there, or it is page furniture.
FOREIGN = re.compile(r"\bwith\b|linkedin|indeed|google|facebook|seek|glassdoor|"
                     r"cookie|share|save for later|refer", re.I)
#: The form goes on to another page. The agent fills one page only.
NEXT = re.compile(r"^\s*(next|continue|next step|save (and|&) continue|proceed)\b", re.I)
#: A button on a job description that opens the application.
ENTRY = re.compile(r"^\s*(apply( now| online)?|apply (for|to) (this |the )?(job|position|role|opening)"
                   r"|i'?m interested|start (your )?application|easy apply)"
                   r"( here)?\s*[!.›»→>]*\s*$", re.I)     # "Apply Now!", "Apply for this job here"
#: What a page says once an application went through.
CONFIRMED = re.compile(
    r"thank(s| you)[^.!\n]{0,40}\b(appl|submi|interest)|"
    r"application (has been |was |is )?(successfully )?(submitted|received|sent|complete)|"
    r"(we('ve| have)|has been) received your application|"
    r"successfully (submitted|applied)|you('ve| have) (successfully )?applied|"
    r"application submitted", re.I)
CONFIRMED_URL = re.compile(r"thank|confirm|success|submitted|/applied|complete", re.I)
#: What a page says when the click was refused.
INVALID = re.compile(r"(is|are) required|required field|please (fill|enter|select|complete)"
                     r"|invalid|must be|can(no|')t be (blank|empty)", re.I)


def submit_button(labels: list[str]) -> int | None:
    """Index of the button to press to send, or None.

    "Submit" wins over "Apply now": a page can hold both, and the Apply one is
    often the job description's own button repeated above the form."""
    ok = [(i, t) for i, t in enumerate(labels) if t and SUBMIT.search(t) and not FOREIGN.search(t)]
    for i, t in ok:
        if re.search(r"submit|send application", t, re.I):
            return i
    return ok[0][0] if ok else None


def next_button(labels: list[str]) -> str | None:
    return next((t for t in labels if t and NEXT.search(t)), None)


#: A page that says how many steps it has: its Continue is not the last one.
STEPS = re.compile(r"\b(step|page)\s*\d+\s*(of|/)\s*\d+|\b\d+\s*/\s*\d+\s*steps?\b", re.I)


def may_send(label: str, page_text: str) -> bool:
    """A bare "Continue" or "Proceed" can be the button that sends: unlike
    "Next" it does not promise another page. Without a step count on the
    page it is treated as a send, so it goes through the approval first."""
    return bool(re.match(r"^\s*(continue|proceed)\s*[›»→>]*\s*$", label or "", re.I)) \
        and not STEPS.search(page_text or "")


def entry_button(labels: list[str]) -> int | None:
    """Index of the job page's own "Apply" button, never an apply-with-X one."""
    return next((i for i, t in enumerate(labels)
                 if t and ENTRY.search(t) and not FOREIGN.search(t)), None)


#: The site said no. Ashby: "We couldn't submit your application. Your
#: application submission was flagged as possible spam." It was read as
#: "unconfirmed, may have gone" (first real submit, metaforms, 2026-10-01).
REFUSED = re.compile(r"couldn'?t submit|could not submit|unable to submit|"
                     r"submission (was )?(failed|unsuccessful|rejected)|"
                     r"flagged as (possible )?spam|application (was )?not submitted|"
                     r"something went wrong", re.I)


def refused(before_text: str, after_text: str) -> str:
    """The new line on the page that says the submit was refused, or ""."""
    old = set((before_text or "").splitlines())
    for line in (after_text or "").splitlines():
        line = line.strip()
        if line and line not in old and REFUSED.search(line):
            return line[:200]
    return ""


def confirmed(before_text: str, after_text: str, before_url: str, after_url: str) -> bool:
    """Did the page say it was sent? New confirming words, or a URL that moved
    to a thank-you page. Words already on the page before the click do not
    count ("we will review your application once it is submitted")."""
    new = len(CONFIRMED.findall(after_text or "")) > len(CONFIRMED.findall(before_text or ""))
    moved = after_url != before_url and bool(CONFIRMED_URL.search(after_url or ""))
    return new or moved


def proof(before_text: str, after_text: str, before_url: str, after_url: str) -> str:
    """What the page showed that counts as confirmation, in its own words: the
    first new line with confirming words, else the thank-you address. Sent to
    the person with the picture, so "sent" is something they can check."""
    old = set((before_text or "").splitlines())
    for line in (after_text or "").splitlines():
        line = line.strip()
        if line and line not in old and CONFIRMED.search(line):
            return line[:160]
    if after_url != before_url and CONFIRMED_URL.search(after_url or ""):
        return f"the page moved to {after_url}"
    return ""


def complaints(text: str, limit: int = 3) -> list[str]:
    """Short validation messages on the page, for the agent to act on."""
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if 3 < len(line) < 160 and INVALID.search(line) and line not in out:
            out.append(line)
        if len(out) >= limit:
            break
    return out


def claims_sent(text: str) -> bool:
    """Does a sentence say it was sent, without saying it was not?"""
    for s in re.split(r"(?<=[.!?])\s+", text or ""):
        if re.search(r"\b(submitted|sent|went through|applied)\b", s, re.I) and not re.search(
                r"\bnot\b|n't|\bnever\b|\bnothing\b|\bno\b|unable|couldn|blocked|refus|without|until|once|before|"
                r"\bif\b|\bwhen\b", s, re.I):
            return True
    return False


#: Said by code when a run ends unsent but the model's last line claimed
#: otherwise. The Swiggy run's last line was "The application was submitted".
NOT_SENT = ("Nothing was sent: the page never confirmed the application, so it "
            "is not submitted.")


def no_form(company: str | None, link: str | None) -> str:
    return (f"I couldn't find an application form on {company or 'this'}'s page, so "
            f"nothing was filled or sent. You can apply yourself here: {link}")
