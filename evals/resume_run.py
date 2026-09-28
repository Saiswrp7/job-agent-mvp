"""Resume evals: parse each resume, tailor it for each job, check the result.

    python3 evals/resume_run.py                 # every resume x every job
    python3 evals/resume_run.py sai_growth      # one resume

Runs the product's own path, in the order a person hits it:
parse-resume (resume/parse.py) -> career store seeded -> tailor.tailor ->
render.render with the ATS gate. Same as `tailor.build`, split in two only so
a PDF that fails the gate can still be kept for Sai to look at.

Checks, all mechanical:
- made anything up: the invention guard's reverts, plus any number in the
  tailored PDF that is nowhere in the source PDF
- ATS gate: render.gate, pass or the reason
- top 5: the job's five most important asks (one model call per job, cached);
  each one that is on the resume at all should sit in the top half of page 1
- slop: banned words, filler, pronouns and em dashes, only in lines the
  model wrote (RESUME_PLAN §9; resume/slop.py is not built yet)
- lost sections: headings in the source PDF, then in the parsed record, then
  in the tailored PDF

Safety: each resume gets its own scratch profile under
profiles/eval-launch/resumes/<name>. Nothing here applies to anything.
Sai's real resumes are copied in, never read from his profile in place.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
OUT = HERE / "out" / "resume"
FIX = HERE / "fixtures"
SCRATCH = "profiles/eval-launch/resumes"

RESUMES = {
    "sai_growth": {"pdf": ROOT.parent / "Interview Prep" / "Sai Swaroop Growth.pdf",
                   "label": "Real: Sai, growth (the file he sent the Telegram bot)"},
    "sai_product": {"pdf": ROOT.parent / "Interview Prep" / "Sai Swaroop Product - Resume.pdf",
                    "label": "Real: Sai, product (Chrome-made PDF)"},
    "synthetic_fresher": {"pdf": FIX / "resumes" / "synthetic_fresher.pdf",
                          "label": "Synthetic: fresher, Indian placement style"},
    "synthetic_two_column": {"pdf": FIX / "resumes" / "synthetic_two_column.pdf",
                             "label": "Synthetic: two-column, Skills + Education in sidebar"},
    "synthetic_senior": {"pdf": FIX / "resumes" / "synthetic_senior.pdf",
                         "label": "Synthetic: 11-year director, 2 pages"},
    "synthetic_sales": {"pdf": FIX / "resumes" / "synthetic_sales.pdf",
                        "label": "Synthetic: non-tech, FMCG sales"},
}

#: Three real board jobs, different role families. Snapshotted into
#: fixtures/jds.json on first run so a re-run uses the same text.
JOBS = [("greenhouse", "8158862", "product"),
        ("lever", "d4c4d777-d41b-4940-9407-e2100fb5dcd1", "growth"),
        ("lever", "09022710-e79a-4cf0-8060-df0e1cd64263", "business development")]


# --- job fixtures ----------------------------------------------------------

def jds() -> list[dict]:
    path = FIX / "jds.json"
    if path.exists():
        return json.loads(path.read_text())
    import sqlite3
    c = sqlite3.connect(f"file:{ROOT / 'jobs.db'}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    out = []
    for src, sid, fam in JOBS:
        r = c.execute("SELECT * FROM jobs WHERE source=? AND source_id=?",
                      (src, sid)).fetchone()
        out.append({**dict(r), "family": fam})
    c.close()
    path.write_text(json.dumps(out, indent=1, ensure_ascii=False))
    return out


TOP5_PROMPT = """From this job description, pick the 5 things a recruiter will
check first. Score: in the title or first two bullets +3; under required /
must-have / minimum, or a years/degree item +2; repeated across sections +2
each; a hard skill or tool +1; a soft skill 0; under nice-to-have -2.

Return ONLY JSON: [{"item": "short name", "match": ["2 to 5 lowercase
phrases a resume would literally contain if the person has this"]}], exactly
5 items, most important first. Match phrases are short (1-3 words), e.g.
"sql", "a/b test", "experimentation"."""


def top5(job: dict) -> list[dict]:
    path = FIX / "jd_top5.json"
    cache = json.loads(path.read_text()) if path.exists() else {}
    key = f"{job['source']}:{job['source_id']}"
    if key not in cache:
        import llm
        cache[key] = llm.complete_json(
            TOP5_PROMPT, f"{job['title']} at {job['company']}\n\n"
            f"{job['description'][:6000]}", effort="low", max_tokens=1500)
        path.write_text(json.dumps(cache, indent=1, ensure_ascii=False))
    return cache[key]


# --- checks ----------------------------------------------------------------

def pdf_text(pdf: Path, layout: bool = True) -> str:
    args = ["pdftotext"] + (["-layout"] if layout else []) + [str(pdf), "-"]
    # A page break comes out as a form feed glued to the next line; make it a
    # line break so a heading at the top of page 2 still starts a line.
    return subprocess.run(args, capture_output=True, text=True).stdout.replace("\f", "\n")


def pages(pdf: Path) -> int:
    out = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True).stdout
    m = re.search(r"^Pages:\s+(\d+)", out, re.M)
    return int(m.group(1)) if m else 0


_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")


def numbers(text: str) -> set[str]:
    return {m.group(0).replace(",", "") for m in _NUM.finditer(text)}


SECTIONS = {
    "summary": r"SUMMARY|PROFILE|OBJECTIVE",
    "experience": r"(?:PROFESSIONAL |WORK )?EXPERIENCE|INTERNSHIPS",
    "education": r"EDUCATION",
    "skills": r"(?:CORE |TECHNICAL )?SKILLS",
    "projects": r"(?:ACADEMIC |AI )?PROJECTS",
    "positions": r"POSITIONS OF RESPONSIBILITY|BOARD AND ADVISORY|LEADERSHIP",
    "achievements": r"ACHIEVEMENTS|AWARDS",
    "certifications": r"CERTIFICATIONS?",
    "languages": r"LANGUAGES",
}


def headings(text: str, case_blind: bool = False) -> set[str]:
    """Section names whose heading appears at the start of a line or of a
    column (after a wide gap, as pdftotext -layout prints a sidebar)."""
    found = set()
    for key, pat in SECTIONS.items():
        rx = re.compile(rf"(?:^|\s{{3,}})(?:{pat})\b", re.M | (re.I if case_blind else 0))
        if rx.search(text):
            found.add(key)
    return found


def parsed_sections(master: dict) -> set[str]:
    out = set()
    for key in SECTIONS:
        v = master.get(key)
        if key == "experience":
            v = [j for j in (v or []) if j.get("bullets")]
        if v:
            out.add(key)
    return out


def skills_block(text: str) -> str:
    """The printed Skills section: from its heading to the next heading."""
    m = re.search(r"(?im)^\s*SKILLS\b.*$", text)
    if not m:
        return ""
    rest = text[m.end():]
    nxt = re.search(r"(?m)^[A-Z][A-Z &]{3,}$", rest)
    return rest[:nxt.start()] if nxt else rest


def skills_in_section(text: str, skills: list[str]) -> str:
    block = skills_block(text).lower()
    return f"{sum(1 for s in skills if s.lower() in block)}/{len(skills)}"


def skill_items(master: dict) -> list[str]:
    s = master.get("skills") or []
    if isinstance(s, dict):
        return [x for v in s.values() for x in (v if isinstance(v, list) else [v])]
    return [str(x) for x in s]


BANNED = ["spearheaded", "leveraged", "leverage", "orchestrated", "orchestrating",
          "utilize", "utilized", "synergy", "results-driven", "passionate",
          "dynamic", "delve", "robust", "tapestry", "testament", "go-getter",
          "team player", "detail-oriented", "self-starter", "proven track record",
          "think outside the box", "seamless", "cutting-edge", "data-driven"]
FILLER = ["responsible for", "helped", "various", "multiple", "very ", "extremely"]
PRONOUN = re.compile(r"\b(I|me|my|we)\b")


def slop(lines: list[str]) -> list[str]:
    hits = []
    for ln in lines:
        low = ln.lower()
        found = [w for w in BANNED if re.search(rf"\b{re.escape(w)}\b", low)]
        found += [w.strip() for w in FILLER
                  if re.search(rf"\b{re.escape(w.strip())}\b", low)]
        found += [f'"{m}"' for m in PRONOUN.findall(ln)]
        if "—" in ln:
            found.append("em dash")
        if found:
            hits.append(f"{', '.join(found)}: {ln[:90]}")
    return hits


def model_lines(before: dict, after: dict) -> list[str]:
    """Lines the model wrote: a changed summary, and any bullet that is not
    word for word one of the originals."""
    lines = []
    if after.get("summary") and after.get("summary") != before.get("summary"):
        lines.append(after["summary"])
    originals = {b for j in before.get("experience") or [] for b in j.get("bullets") or []}
    for j in after.get("experience") or []:
        lines += [b for b in j.get("bullets") or [] if b not in originals]
    return lines


def top_half(pdf: Path, items: list[dict]) -> dict:
    """Where each of the job's top 5 lands: top half of page 1, lower, or
    nowhere. Lines are rebuilt from pdfplumber words with their y position."""
    import pdfplumber
    placed = {}
    with pdfplumber.open(str(pdf)) as doc:
        lines_all = []
        for pno, page in enumerate(doc.pages):
            rows: dict[int, list] = {}
            for w in page.extract_words():
                rows.setdefault(round(w["top"] / 3), []).append(w)
            for k in sorted(rows):
                ws = sorted(rows[k], key=lambda w: w["x0"])
                text = " ".join(w["text"] for w in ws).lower()
                lines_all.append((pno, ws[0]["top"], page.height, text))
    for it in items:
        where = "missing"
        for pno, top, height, text in lines_all:
            if any(m.lower() in text for m in it.get("match") or []):
                where = "top half" if (pno == 0 and top < height / 2) else "lower"
                break
        placed[it["item"]] = where
    return placed


# --- one resume, in its own profile (own process) ---------------------------

def run_one(slug: str) -> dict:
    home = f"{SCRATCH}/{slug}"
    os.environ["JOB_AGENT_HOME"] = home
    os.environ["ALLOW_SUBMIT"] = "0"
    os.environ["JOB_AGENT_LIVE"] = "0"
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(HERE))
    import tokens
    tokens.install()
    import career
    import paths
    import vault as v
    from engine import db
    from resume import parse, render, tailor as t

    assert not paths.IS_DEFAULT and "tg-" not in str(paths.HOME), paths.HOME
    paths.ensure()
    spec = RESUMES[slug]
    src = paths.UPLOADS / spec["pdf"].name
    shutil.copy(spec["pdf"], src)
    source_text = pdf_text(src)

    conn = db.connect()
    rec = {"slug": slug, "label": spec["label"], "source_pdf": str(spec["pdf"]),
           "source_pages": pages(src), "tailors": []}

    # parse, once per resume (what `cli.py parse-resume` and the upload do)
    m0 = tokens.mark()
    t0 = time.monotonic()
    if paths.MASTER.exists():
        master = json.loads(paths.MASTER.read_text())
        rec["parse_seconds"] = None
    else:
        master = parse.parse(src)
        rec["parse_seconds"] = round(time.monotonic() - t0, 1)
        for k, val in parse.vault_rows(master).items():
            v.put(k, val, source="resume", conn=conn)
        v.put("master_resume_path", str(src), source="resume", conn=conn)
        career.seed_from_master(master, conn=conn)
    rec["parse_tokens"] = tokens.since(m0)
    in_source = headings(source_text)
    in_parsed = parsed_sections(master)
    rec["sections_source"] = sorted(in_source)
    rec["sections_parsed"] = sorted(in_parsed)
    # The parse schema has no projects/positions/achievements keys, so the
    # model sometimes files a section as if it were an employer.
    fake = [j.get("company") for j in master.get("experience") or []
            if re.search(r"project|responsibilit|achievement|award|certif",
                         str(j.get("company") or ""), re.I)]
    rec["filed_as_job"] = fake
    for f in fake:
        for key in ("projects", "positions", "achievements", "certifications"):
            if re.search(SECTIONS[key], f.upper()):
                in_parsed.add(key)
    rec["lost_in_parse"] = sorted(in_source - in_parsed)
    skills = skill_items(master)
    rec["skills_parsed"] = len(skills)

    for job in jds():
        name = f"{slug}__{job['family'].replace(' ', '_')}"
        row = {"job": f"{job['title']} at {job['company']}", "family": job["family"],
               "name": name}
        m = tokens.mark()
        t0 = time.monotonic()
        try:
            result = t.tailor(job, conn=conn)        # master=None: the chat path
            tailored = result["master"]
            row["reverted"] = [n for n in result["notes"] if "reverted" in n]
            row["notes"] = result["notes"]
            pdf_name = f"{name}.pdf"
            try:
                # The product's page fit (tailor.build), so page counts here
                # are the ones a recruiter would get.
                from resume import generate, layouts
                from search.run import experience_years
                yrs = experience_years(conn)
                limit = max(generate.page_limit(yrs), layouts.get(None)["max_pages"])
                tailored, pdf, cut = generate.fit(
                    tailored, limit, lambda m_, n_: render.render(m_, n_),
                    pdf_name, projects_first=(yrs or 0) >= 2)
                row["notes"] = row["notes"] + [f'"{b}" cut to fit' for b in cut]
                row["ats"] = "pass"
            except RuntimeError as exc:
                msg = str(exc)
                if "ATS extraction gate failed" not in msg:
                    raise
                row["ats"] = "fail: " + msg.split(":\n", 1)[-1].replace("\n", " ").strip(" -")
                pdf = render.render(tailored, f"{name}__GATE_FAILED.pdf",
                                    skip_gate=True)
            row["seconds"] = round(time.monotonic() - t0, 1)
            row["tokens"] = tokens.since(m)
            OUT.mkdir(parents=True, exist_ok=True)
            dest = OUT / pdf.name
            shutil.copy(pdf, dest)
            row["pdf"] = str(dest.relative_to(ROOT))
            row["pages"] = pages(dest)
            out_text = pdf_text(dest)

            new_nums = sorted(numbers(out_text) - numbers(source_text))
            row["new_numbers"] = new_nums

            row["top5"] = top_half(dest, top5(job))
            before, _ = __import__("resume.generate", fromlist=["x"]).from_career(conn=conn)
            row["model_lines"] = len(model_lines(before, tailored))
            row["slop"] = slop(model_lines(before, tailored))

            in_output = headings(out_text, case_blind=True)
            row["lost_in_output"] = sorted(in_source - in_output)
            low = out_text.lower()
            # In the Skills section itself, not anywhere on the page: a skill
            # word inside a bullet does not mean the skills list survived.
            row["skills_kept"] = skills_in_section(out_text, skills)
            row["skills_line"] = " ".join(skills_block(out_text).split())[:160]
        except Exception as exc:                          # noqa: BLE001
            row["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
            row["seconds"] = round(time.monotonic() - t0, 1)
            row["tokens"] = tokens.since(m)
        rec["tailors"].append(row)
        print(f"  {name}: {row.get('ats', row.get('error'))} "
              f"{row['seconds']}s reverted={len(row.get('reverted') or [])}",
              flush=True)
    conn.close()
    return rec


def recheck() -> None:
    """Re-score the skills check on PDFs already built. No model calls."""
    import sys as _s
    _s.path.insert(0, str(ROOT))
    for slug in RESUMES:
        f = OUT / f"{slug}.json"
        if not f.exists():
            continue
        rec = json.loads(f.read_text())
        master = json.loads((ROOT / SCRATCH / slug / "resume" / "master.json").read_text())
        skills = skill_items(master)
        for t in rec["tailors"]:
            if t.get("pdf"):
                text = pdf_text(ROOT / t["pdf"])
                t["skills_kept"] = skills_in_section(text, skills)
                t["skills_line"] = " ".join(skills_block(text).split())[:160]
        f.write_text(json.dumps(rec, indent=1, ensure_ascii=False, default=str))


def main(argv: list[str]) -> int:
    if argv and argv[0] == "--recheck":
        recheck()
        rows = [json.loads((OUT / f"{s}.json").read_text()) for s in RESUMES
                if (OUT / f"{s}.json").exists()]
        d = json.loads((HERE / "out" / "resume.json").read_text())
        d["rows"] = rows
        (HERE / "out" / "resume.json").write_text(json.dumps(d, indent=1, ensure_ascii=False))
        return 0
    if argv and argv[0] == "--one":
        rec = run_one(argv[1])
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / f"{argv[1]}.json").write_text(
            json.dumps(rec, indent=1, ensure_ascii=False, default=str))
        return 0

    sys.path.insert(0, str(ROOT))
    jds()                                   # snapshot before the workers start
    for j in jds():                         # cache top-5 once, not per worker
        top5(j)
    slugs = argv or list(RESUMES)

    def go(slug):
        log = OUT / f"{slug}.log"
        OUT.mkdir(parents=True, exist_ok=True)
        with open(log, "w") as f:
            subprocess.run([sys.executable, __file__, "--one", slug], cwd=ROOT,
                           stdout=f, stderr=subprocess.STDOUT)
        return slug

    with ThreadPoolExecutor(3) as pool:
        for slug in pool.map(go, slugs):
            print("done", slug, flush=True)

    rows = []
    for slug in list(RESUMES):
        f = OUT / f"{slug}.json"
        if f.exists():
            rows.append(json.loads(f.read_text()))
    (HERE / "out" / "resume.json").write_text(json.dumps(
        {"ran_at": datetime.now().isoformat(timespec="seconds"), "rows": rows},
        indent=1, ensure_ascii=False, default=str))
    print("wrote", HERE / "out" / "resume.json")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
