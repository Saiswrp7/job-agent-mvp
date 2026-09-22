"""JD + master.json -> a tailored PDF. One LLM call, then mechanical checks."""

from __future__ import annotations

import json
import re
from pathlib import Path

import llm
from resume import render, verify

HERE = Path(__file__).resolve().parent
MASTER = HERE / "master.json"
JD_CHARS = 6000


def load_master(path: Path = MASTER) -> dict:
    if not path.exists():
        raise FileNotFoundError(
            f"{path} missing — run `python cli.py parse-resume <pdf>` first"
        )
    return json.loads(path.read_text())


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")[:40]


def tailor(job: dict, master: dict | None = None) -> dict:
    """Returns {master, patch, notes} — the tailored data, not a file."""
    master = master or load_master()

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

    patch = llm.complete_json(llm.prompt("tailor"), user, max_tokens=4096)
    tailored, notes = verify.apply_patch(master, patch)
    return {"master": tailored, "patch": patch, "notes": notes}


def build(job: dict, master: dict | None = None) -> dict:
    """Tailor and render. Returns the result plus the PDF path."""
    result = tailor(job, master)
    name = (f"{_slug(result['master'].get('name','resume'))}"
            f"_{_slug(job['company'])}_{_slug(job['title'])}.pdf")
    result["pdf"] = render.render(result["master"], name)
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
    if result.get("notes"):
        lines.append("Reverted (would have invented something):")
        lines += [f"  ! {n}" for n in result["notes"]]
    return "\n".join(lines)
