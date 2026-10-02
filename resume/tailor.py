"""JD + master.json -> a tailored PDF. One LLM call, then mechanical checks."""

from __future__ import annotations

import json
import re
from pathlib import Path

import llm
from resume import render, verify

import paths

HERE = Path(__file__).resolve().parent
#: Same file as resume/parse.MASTER. It is imported from one place now — two
#: constants naming one path is how a profile switch half-happens.
MASTER = paths.MASTER
JD_CHARS = 6000


def load_master(path: Path = MASTER) -> dict:
    if not path.exists():
        raise FileNotFoundError(
            f"{path} missing — run `python cli.py parse-resume <pdf>` first"
        )
    from resume.parse import tidy  # noqa: PLC0415
    return tidy(json.loads(path.read_text()))


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")[:40]


def tailor(job: dict, master: dict | None = None,
           instruction: str = "", conn=None, wants: str = "") -> dict:
    """Returns {master, patch, notes} — the tailored data, not a file.

    `instruction` is what the person asked for in their own words: "emphasise
    the CRM work", "make the second bullet stronger on revenue". It used to
    have nowhere to go — the tool took only a job number — so it was dropped
    in transit rather than refused. Asked to claim eight years of experience,
    the resume came back saying four and nobody was told the instruction had
    been ignored.

    Letting it through is safe because it lands in the same patch every other
    rewrite lands in, and `verify.apply_patch` reverts any bullet that gained a
    number or a proper noun the original did not have. So "say I have 8 years"
    is reverted *and reported* in `notes`, which is the part that was missing.
    """
    # Starts from everything on record, not the parsed PDF alone. From the PDF,
    # a fact they told us on Tuesday was only *permitted* — `supported` let its
    # number through a rewrite — but it could never be *selected*, because no
    # bullet carried it. The tailored resume was the one document that could
    # not use what they had said. `from_career` is what `build_resume` sends,
    # so tailoring now selects from the same pool the master resume shows.
    if master is None:
        from resume import generate    # generate imports this module
        master, _ = generate.from_career(conn=conn)

    resume_for_model = {
        "summary": master.get("summary", ""),
        "experience": [
            {"index": i, "company": j.get("company"), "title": j.get("title"),
             "bullets": j.get("bullets", [])}
            for i, j in enumerate(master.get("experience", []))
        ],
    }

    user = (
        f"JOB: {job['title']} at {job['company']}\n\n"
        f"{(job.get('description') or '')[:JD_CHARS]}\n\n"
        f"RESUME:\n{json.dumps(resume_for_model, indent=1)}"
    )
    # What they are after, in their words. The ranker has always had this
    # and the resume builder never did, so the search knew they wanted to own
    # a revenue line and the document written off the back of it did not. It
    # steers which bullets lead; it can never add one. A want is not a fact.
    if wants.strip():
        user += (f"\n\nWHAT THEY ARE LOOKING FOR:\n{wants.strip()[:1200]}\n"
                 f"Use this to decide which of their bullets lead and how they "
                 f"are worded. It describes what they want next, not what they "
                 f"have done — nothing in it is a fact about them and none of "
                 f"it may be added to the resume.")

    if instruction.strip():
        user += (f"\n\nWHAT THEY ASKED FOR:\n{instruction.strip()}\n"
                 f"Follow it as far as the resume honestly allows. Anything "
                 f"that would need a fact the resume does not contain, leave "
                 f"alone — it will be reverted and they will be told.")

    patch = llm.complete_json(llm.prompt("tailor"), user, max_tokens=4096)
    # Numbers they have stated since the PDF was parsed. Without this the guard
    # reverts the person's own words, because "not in the original bullet" is
    # the definition of invented only while the PDF is the only source.
    import career
    # What actually changed, written by code from what landed: a rewrite
    # that was reverted is not a change, whatever the model meant. Sai's eval
    # sheet row 33: "after building tell what you exactly did to align the
    # resume and job description".
    changes: list[str] = []
    tailored, notes = verify.apply_patch(master, patch, career.supported(conn),
                                         changes=changes)
    # A revert is only half the story. The other half is an instruction the
    # model quietly declined, which leaves nothing to revert and so said
    # nothing at all — see `instruction_outcome`.
    notes = notes + verify.instruction_outcome(instruction, master, tailored, notes)
    return {"master": tailored, "patch": patch, "notes": notes,
            "changes": changes}


def build(job: dict, master: dict | None = None,
          instruction: str = "", conn=None, wants: str = "",
          layout: str | None = None) -> dict:
    """Tailor and render. Returns the result plus the PDF path."""
    result = tailor(job, master, instruction=instruction, conn=conn,
                    wants=wants)
    name = (f"{_slug(result['master'].get('name','resume'))}"
            f"_{_slug(job['company'])}_{_slug(job['title'])}.pdf")
    # Fitted to the page like the updated resume. It was rendered as-is, and
    # once Projects came through the reader a 4-year resume ran to two pages.
    from resume import generate
    from search.run import experience_years
    years = experience_years(conn)
    limit = generate.page_limit()
    result["master"], result["pdf"], cut = generate.fit(
        result["master"], limit,
        lambda m, n: render.render(m, n, layout=layout), name,
        projects_first=(years or 0) >= 2)
    result["notes"] = result["notes"] + [
        f'"{b}" cut to fit the page; still on your record' for b in cut]
    return result


def preview(result: dict) -> str:
    """What the user approves — text, not a PDF.

    Someone spots a wrong number in three seconds reading text on a phone, and
    never by opening an attachment.
    """
    m = result["master"]
    lines = [f"Summary: {m.get('summary','')}", ""]
    for job in m.get("experience", []):
        if not job.get("bullets"):
            continue
        lines.append(f"{job.get('company')} — {job.get('title')}")
        lines += [f"  · {b}" for b in job["bullets"]]
        lines.append("")
    if result.get("changes") is not None:
        lines.append("What I changed for this job — tell them, short:")
        lines += [f"  + {c}" for c in result["changes"]] or [
            "  + nothing: it already fit this job"]
        lines.append("")
    if result.get("notes"):
        # Covers both kinds now: a rewrite that was caught and reverted, and an
        # instruction that was quietly never carried out. Both are news the
        # person is owed, and neither is visible in the bullets above.
        lines.append("What did not make it in — tell them:")
        lines += [f"  ! {n}" for n in result["notes"]]
    return "\n".join(lines)
