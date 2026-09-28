"""A resume from the career store, with no job attached.

Every PDF this project made until now came out of `tailor.build(job, ...)`, so
there was no way to ask for a resume — only for a resume *for something*. This
is the other path: the uploaded document as the skeleton, everything they have
told us since as the additions.

**Nothing here calls a model.** Selection is by company and recency, which are
columns. That keeps a plain "build my resume" free, and it keeps the output
predictable: the same store produces the same document twice.
"""

from __future__ import annotations

import re
import sqlite3
import subprocess
from pathlib import Path

import career
from resume import render, tailor


def from_career(master: dict | None = None,
                conn: sqlite3.Connection | None = None,
                ) -> tuple[dict, list[dict]]:
    """Returns (master_with_additions, unplaced).

    `unplaced` is every accomplishment they stated without naming a company.
    It is handed back rather than filed somewhere plausible, because guessing
    which job a thing belongs to is inventing employment history — the one
    class of error this system has never been willing to make. The caller asks
    them where it goes.
    """
    master = master or tailor.load_master()
    rows = career.all(conn=conn)

    said = [r for r in rows if r["source"] != "resume"]
    by_company: dict[str, list[dict]] = {}
    unplaced: list[dict] = []
    for r in said:
        if r["kind"] not in ("accomplishment", "metric"):
            continue
        if r.get("company"):
            by_company.setdefault(r["company"].strip().lower(), []).append(r)
        else:
            unplaced.append(r)

    out = {**master, "experience": []}
    for job in master.get("experience", []) or []:
        extra = by_company.get((job.get("company") or "").strip().lower(), [])
        bullets = list(job.get("bullets", []) or [])
        # Appended, never interleaved. A bullet from the parsed PDF and one
        # from Tuesday's conversation are both theirs, but only the first has
        # been read back by a human, so the older material keeps the top of
        # the list where it is actually read.
        bullets += [r["text"] for r in extra if r["text"] not in bullets]
        out["experience"].append({**job, "bullets": bullets})

    # Skills are grouped ({"Growth": ["SEO", "CRM"]}) when parsed from a PDF.
    # `list()` of that dict kept only the group names, so every tailored
    # resume printed "Growth, AI, Tools" and no skills (18 of 18, 2026-09-28).
    # Groups stay groups; skills said in chat go under "Other".
    skills = master.get("skills") or []
    new = [r["text"] for r in said if r["kind"] == "skill"
           and r["text"] not in _skill_list(skills)]
    new = list(dict.fromkeys(new))
    if isinstance(skills, dict):
        skills = {k: list(v) if isinstance(v, list) else v
                  for k, v in skills.items()}
        if new:
            skills["Other"] = list(skills.get("Other") or []) + new
    else:
        skills = list(skills) + new
    if skills:
        out["skills"] = skills

    return out, unplaced


def _skill_list(skills) -> list[str]:
    """Every skill as one flat list, whether grouped or not."""
    if isinstance(skills, dict):
        return [s for v in skills.values()
                for s in (v if isinstance(v, list) else [v])]
    return list(skills or [])


def additions(master: dict | None = None,
              conn: sqlite3.Connection | None = None) -> int:
    """How many lines the record adds to the file they uploaded.

    Zero means "updated" and "your file" say the same thing, so the apply
    question offers two choices, not three. Asking someone to pick between two
    identical documents is a question with no answer.
    """
    master = master or tailor.load_master()
    out, _ = from_career(master, conn)
    before = sum(len(j.get("bullets") or []) for j in master.get("experience") or [])
    after = sum(len(j.get("bullets") or []) for j in out.get("experience") or [])
    return (after - before) + (len(_skill_list(out.get("skills")))
                               - len(_skill_list(master.get("skills"))))


def page_limit(years: int | None) -> int:
    """RESUME_PLAN §7: one page under 8 years, two after. Unknown years get one
    page, because a second page nobody needed costs more than a line cut."""
    return 2 if years is not None and years >= 8 else 1


def pages(pdf: Path) -> int:
    out = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True)
    m = re.search(r"^Pages:\s+(\d+)", out.stdout, re.M)
    return int(m.group(1)) if m else 1


def _weakest(master: dict) -> tuple[int, int] | None:
    """(role, bullet) to cut next: the oldest role first, a line with no number
    before one with a number, the lowest on the list before the higher ones.
    Every role keeps at least one line, so cutting never deletes a job."""
    for i in range(len(master.get("experience") or []) - 1, -1, -1):
        bullets = master["experience"][i].get("bullets") or []
        if len(bullets) < 2:
            continue
        plain = [k for k, b in enumerate(bullets) if not re.search(r"\d", b)]
        return i, (plain or list(range(len(bullets))))[-1]
    return None


def fit(master: dict, limit: int, render_fn, name: str,
        count=pages, projects_first: bool = False) -> tuple[dict, Path, list[str]]:
    """Render, and while it runs past `limit` pages cut the weakest line and
    render again. Returns what it cut, so the person is told rather than
    finding out from a recruiter. The font is never shrunk to make room: a
    smaller font is a cut nobody gets told about.

    `projects_first`: with two years or more at work, a side project goes
    before a line of real work does. Reading Projects back in (2026-09-28)
    pushed a 4-year resume to two pages."""
    master = {**master, "experience": [
        {**j, "bullets": list(j.get("bullets") or [])}
        for j in master.get("experience") or []],
        "projects": list(master.get("projects") or [])}
    cut: list[str] = []
    pdf = render_fn(master, name)
    while count(pdf) > limit:
        if projects_first and master["projects"]:
            p = master["projects"].pop()
            cut.append(p.get("name") or p.get("text") or str(p)
                       if isinstance(p, dict) else str(p))
            pdf = render_fn(master, name)
            continue
        where = _weakest(master)
        if where is None:
            break
        i, k = where
        cut.append(master["experience"][i]["bullets"].pop(k))
        pdf = render_fn(master, name)
    return master, pdf, cut


def build(master: dict | None = None, conn: sqlite3.Connection | None = None,
          out_name: str | None = None, layout: str | None = None) -> dict:
    """Their updated general resume: the upload plus everything they have told
    us since, fitted to the page limit. Same result shape as `tailor.build`.

    Not "everything on record". The record stays whole; what gets sent is the
    part of it that fits, because a resume that runs to a third page to hold
    every line reads as unedited, whoever wrote it.

    `notes` carries the unplaced accomplishments and anything cut to fit, so it
    prints through the same `tailor.preview` the tailored path uses.
    """
    built, unplaced = from_career(master, conn)
    name = out_name or f"{tailor._slug(built.get('name', 'resume'))}.pdf"
    from search.run import experience_years
    from resume import layouts
    # A two-page layout they chose is two pages even under 8 years: they were
    # told once why most recruiters expect one (layouts.fit_warning), and the
    # choice is theirs. A one-page layout keeps the years rule.
    limit = max(page_limit(experience_years(conn)),
                layouts.get(layout)["max_pages"])
    years = experience_years(conn)
    built, pdf, cut = fit(built, limit,
                          lambda m, n: render.render(m, n, layout=layout), name,
                          projects_first=(years or 0) >= 2)
    notes = [
        f'"{r["text"]}" — not on the resume yet: say which role it belongs to'
        for r in unplaced
    ] + [f'"{b}" — cut to fit the page; still on your record' for b in cut]
    return {"master": built, "patch": {}, "notes": notes,
            "unplaced": unplaced, "pdf": pdf}


def preview_layouts(keys: list[str], conn: sqlite3.Connection | None = None,
                    master: dict | None = None) -> list[dict]:
    """Page 1 of their own resume in each layout, as PNGs.

    Their resume, not a sample person: choosing between names like "Harvard
    Classic" and "Modern Accent" is guessing until you see your own lines in
    them. No fitting here, so a layout that runs long shows that it does.
    """
    from resume import layouts
    built, _ = from_career(master, conn)
    out = []
    for key in keys:
        spec = layouts.get(key)
        pdf = render.OUT_DIR / "previews" / f"{spec['key']}.pdf"
        render.compile_pdf(render.to_tex(built, spec["key"]), pdf)
        out.append({"layout": spec["key"], "label": spec["label"],
                    "png": str(render.preview_png(pdf)),
                    "pages": render.pages(pdf)})
    return out
