"""The patch sanity gate. No model — that is the point.

"Rephrase, never invent" is a prompt instruction, and prompt instructions get
talked around. This is the mechanical version: a rewritten bullet that
introduces a number, a company, or a product name the original did not contain
is reverted to its original text.

Fabricated experience reaching a recruiter is permanent damage, and it is
cheap to make impossible.
"""

from __future__ import annotations

import re

from resume import slop

NUM = re.compile(r"\d[\d,\.]*\s*(?:%|x|k|m|cr|lakh|lpa|bn)?", re.I)
PROPER = re.compile(r"\b[A-Z][a-zA-Z0-9]*(?:\.[a-z]+)?\b")

#: Sentence-initial and common words that capitalize without being entities.
STOPWORDS = {
    "I", "A", "An", "The", "And", "Or", "But", "For", "With", "By", "At", "In",
    "On", "To", "From", "Of", "As", "Led", "Built", "Drove", "Owned", "Grew",
    "Ran", "Managed", "Launched", "Scaled", "Reduced", "Increased", "Improved",
    "Designed", "Created", "Delivered", "Partnered", "Worked", "Developed",
    "Shipped", "Defined", "Established", "Coordinated", "Analyzed", "Drove",
    # Seen reverting honest rewrites in real runs — all verbs or adjectives
    # that happen to start a clause, none of them entities.
    "Orchestrated", "Drives", "Owns", "Leads", "Builds", "Proven", "Spearheaded",
    "Executed", "Optimized", "Optimised", "Streamlined", "Accelerated",
}


def _numbers(s: str) -> set[str]:
    return {m.group(0).replace(" ", "").lower().rstrip(".") for m in NUM.finditer(s)}


#: A capitalised word that opens a sentence, the slot where a bullet's verb
#: sits: "Collaborated with...", "Wrote the...".
_OPENER = re.compile(r"(?:^|[.;:!?]\s+)([A-Z][a-z]+)\b")
#: Irregular past tenses and bare verbs that open bullets without an -ed.
_VERBS = {"Wrote", "Run", "Rebuilt", "Built", "Led", "Won", "Set", "Cut", "Made",
          "Grew", "Took", "Began", "Sold", "Taught", "Drew", "Bought", "Met",
          "Kept", "Brought", "Held", "Sent", "Found", "Spent", "Shipped", "Use",
          "Partner", "Own", "Lead", "Build", "Drive", "Scale", "Rewrote", "Ran",
          "Oversaw", "Chose", "Gave", "Got", "Hit", "Put", "Saw", "Told"}


def _opener_verbs(s: str) -> set[str]:
    """Sentence openers that are verbs, not names. Only an -ed/-ing form or a
    listed verb is let off: a tool name opening a sentence ("Amplitude
    dashboards...") still counts as a name. This list exists because "Used",
    "Collaborated", "Leveraged" and "Migrated" read as invented names and threw
    away honest rewrites in the 2026-09-28 evals."""
    return {w for w in _OPENER.findall(s)
            if w in _VERBS or w.endswith(("ed", "ing"))}


def _propers(s: str) -> set[str]:
    skip = STOPWORDS | _opener_verbs(s)
    return {w for w in PROPER.findall(s) if w not in skip and len(w) > 2}


def check_bullet(original: str, rewritten: str,
                 supported: set[str] | None = None) -> list[str]:
    """Problems with a rewrite. Empty means it may stand.

    `supported` is every number the person has actually told us, from the
    career store. Without it the test is "was this in the original bullet",
    which is correct while the uploaded PDF is the only source and wrong the
    moment the corpus grows — it would block the person's own words.

    Passing the set does not loosen the guard, it moves it: a number still has
    to come from somewhere, and now "somewhere" includes a row that records who
    said it and when. A number from neither is still invented.
    """
    supported = supported or set()
    problems = []
    new_nums = _numbers(rewritten) - _numbers(original) - supported
    if new_nums:
        problems.append(f"invented number(s): {sorted(new_nums)}")
    # Names get the same treatment as numbers. They did not at first, and the
    # split was invisible: told "I used Amplitude daily", the store held the
    # skill, the number path worked, and the bullet was still reverted.
    new_names = _propers(rewritten) - _propers(original) - supported
    if new_names:
        problems.append(f"invented name(s): {sorted(new_names)}")
    return problems


#: A number in an instruction that is a **claim about the person**, not a
#: pointer at a line. "say I have 8 years" is a claim; "make the 2nd bullet
#: stronger" is a pointer, and flagging pointers would fire this on ordinary
#: edits — which is how a guard stops being read.
CLAIM = re.compile(
    r"\b(\d[\d,\.]*)\s*(%|x|k|m|bn|cr|lakh|lpa|crore|million|billion|percent|"
    r"years?|yrs?|months?|users?|customers?|downloads?|installs?)\b", re.I)


def resume_text(master: dict) -> str:
    """Everything a reader would actually see, as one string."""
    parts = [master.get("summary", "")]
    for job in master.get("experience", []):
        parts += list(job.get("bullets", []))
    return " ".join(parts)


def instruction_outcome(instruction: str, before: dict, after: dict,
                        notes: list[str]) -> list[str]:
    """What became of what they asked for, when nothing was reverted.

    `apply_patch` reports a rewrite it *caught*. It cannot report one that was
    never attempted. Asked to claim eight years, a model that quietly writes
    "4+ years" produces no patch to revert — so the preview showed a clean
    resume, said nothing, and the person went on believing the document said
    eight. Silent compliance and silent refusal looked identical.

    Two mechanical checks separate them. No second model call: the first asks
    whether a number they asked for is anywhere in the finished resume, the
    second whether the resume changed at all.
    """
    if not instruction.strip():
        return []

    out: list[str] = []
    text = resume_text(after)
    reported = " ".join(notes).lower()

    for num, unit in CLAIM.findall(instruction):
        if num.lower() in reported:
            continue                    # apply_patch already reverted and said so
        if re.search(rf"\b{re.escape(num)}\b", text):
            continue                    # it is in there — the ask landed
        # Names the remedy, because this is usually not a lie — it is a true
        # thing that is simply not in the uploaded PDF. It is their career and
        # the system only ever saw one document of it. A note that says "no"
        # and stops leaves them arguing with a wall; this one hands them the
        # door: say it plainly, it goes on the record, and the next build uses
        # it. The guard exists to stop the *model* inventing, never the person
        # describing their own work.
        out.append(
            f'you asked for "{num} {unit}" and the resume does not say it — '
            f'nothing on record supports it yet. If they are telling you this '
            f'is true, save it with remember_experience and build again.'
        )

    if not out and not notes and resume_text(before) == text:
        out.append("nothing in the resume changed — what you asked for would "
                   "have needed a fact the resume does not contain")

    return out


def apply_patch(master: dict, patch: dict,
                supported: set[str] | None = None) -> tuple[dict, list[str]]:
    """Returns (tailored_master, reverted_notes).

    `supported` comes from the career store — every number the person has
    stated, each traceable to the message they said it in. See check_bullet.

    A failing bullet is reverted, not rejected — one bad rewrite should cost
    that bullet's improvement, not the whole resume.
    """
    out = {**master, "experience": []}
    notes: list[str] = []

    # The summary is checked exactly like a bullet. It was not, and it is the
    # most inviting place to invent: "Growth Manager with 8 years of
    # experience" is one sentence, sits at the top of the page, and is the
    # first thing a recruiter reads. The gap did not matter while the model
    # only ever saw the JD; it matters now that the person's own instruction
    # reaches this call and may be "say I have 8 years".
    if patch.get("summary"):
        original = master.get("summary", "")
        text = patch["summary"]
        # **Numbers only, unlike a bullet.** What makes a summary dangerous is
        # a quantity: "8 years", "₹500 Cr", "100M users". A proper noun there
        # is nearly always the JD's own vocabulary, which is what tailoring is
        # *for* — checking those reverted the summary on every single run,
        # including runs with no instruction at all, over words like "DAU" and
        # "Proven". A guard that fires every time is one nobody reads, and it
        # would have buried the "8 years" revert it exists to surface.
        #
        # Employment claims are still covered: company and title are locked
        # fields, asserted byte-identical below, and bullets keep both checks.
        new_nums = (_numbers(text) - _numbers(original) - (supported or set())
                    if original else set())
        if new_nums:
            notes.append(
                f"summary: reverted (invented number(s): {sorted(new_nums)})")
            text = original
        elif original and slop.added(original, text):
            notes.append(f"summary: kept your original (the rewrite sounded "
                         f"AI-written: {', '.join(slop.added(original, text))})")
            text = original
        out["summary"] = slop.tidy(text) if text != original else text

    by_index = {int(p["index"]): p for p in patch.get("experience", [])
                if "index" in p}

    for i, job in enumerate(master.get("experience", [])):
        p = by_index.get(i)
        bullets = list(job.get("bullets", []))

        if p:
            keep = p.get("keep")
            rewrites = {int(k): v for k, v in (p.get("rewrites") or {}).items()}
            order = keep if isinstance(keep, list) and keep else range(len(bullets))

            chosen = []
            for idx in order:
                if not (0 <= idx < len(bullets)):
                    continue
                original = bullets[idx]
                text = rewrites.get(idx, original)
                if text != original:
                    problems = check_bullet(original, text, supported)
                    if problems:
                        notes.append(
                            f"{job.get('company','?')} bullet {idx}: reverted "
                            f"({'; '.join(problems)})"
                        )
                        text = original
                    elif slop.added(original, text):
                        notes.append(
                            f"{job.get('company','?')} bullet {idx}: kept the "
                            f"original (the rewrite sounded AI-written: "
                            f"{', '.join(slop.added(original, text))})")
                        text = original
                    else:
                        text = slop.tidy(text)
                chosen.append(text)
            bullets = chosen or bullets

        out["experience"].append({**job, "bullets": bullets})

    # Locked fields must be byte-identical. Nothing in the patch can reach
    # them, but assert it rather than trust it.
    for i, job in enumerate(master.get("experience", [])):
        for field in ("company", "title", "dates"):
            if out["experience"][i].get(field) != job.get(field):
                raise AssertionError(f"locked field changed: {field}")

    return out, notes
