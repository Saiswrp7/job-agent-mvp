"""Resume PDF -> master.json. Run once, then fix it by hand.

This file is the source of truth for every resume the system ever generates,
so an error here propagates forever. The CLI prints the result and tells you to
read it.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import llm

HERE = Path(__file__).resolve().parent
MASTER = HERE / "master.json"

SYSTEM = """Extract this resume into JSON. Return ONLY JSON, this shape:

{
  "name": "...",
  "contact": {"email": "...", "phone": "...", "location": "...", "linkedin": "..."},
  "summary": "...",
  "experience": [
    {"company": "...", "title": "...", "dates": "Jan 2023 - Present",
     "bullets": ["...", "..."]}
  ],
  "education": [{"school": "...", "degree": "...", "dates": "..."}],
  "skills": {"Category": ["skill", "skill"]}
}

Rules:
- Copy text exactly. Do not improve, shorten, or rewrite anything.
- Experience in reverse chronological order, most recent first.
- Keep every bullet. Selection happens later, per job.
- One consistent date format throughout.
- If a field is genuinely absent, use null. Never invent one.
- `linkedin` must be a full https:// URL. A resume usually shows the word
  "LinkedIn" as a hyperlink, so the visible text is not the address — take the
  address from the LINKS list below the resume text."""

#: Anchor text is not an address. A resume that renders its LinkedIn as the
#: word "LinkedIn" leaves nothing useful in the extracted text, and whatever
#: the model guesses from that gets typed into a real application's URL field.
_HREF = re.compile(r'href="(https?://[^"]+)"')


def pdf_links(path: Path) -> list[str]:
    """Real URLs behind the hyperlinks, in document order, deduplicated.

    `pdftotext` drops link targets entirely — they live in PDF annotations, not
    in the text layer — so this shells out to `pdftohtml` instead. Missing
    poppler or a PDF with no links is not an error; it just means no links.
    """
    try:
        out = subprocess.run(
            ["pdftohtml", "-i", "-stdout", "-noframes", "-q", str(path)],
            capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return []
    seen: dict[str, None] = {}
    for url in _HREF.findall(out.stdout or ""):
        seen.setdefault(url.rstrip("/"), None)
    return list(seen)


def pdf_text(path: Path) -> str:
    out = subprocess.run(["pdftotext", "-layout", str(path), "-"],
                         capture_output=True, text=True)
    if out.returncode != 0 or not out.stdout.strip():
        raise RuntimeError(f"could not extract text from {path}")
    return out.stdout


def _fix_links(master: dict, links: list[str]) -> dict:
    """Overwrite `contact.linkedin` with a real LinkedIn URL when one exists.

    Deterministic, and it runs after the model: the address is a fact sitting
    in the file, so it should not depend on the model having copied it right.
    """
    contact = master.setdefault("contact", {}) or {}
    master["contact"] = contact
    real = next((u for u in links if "linkedin.com/in/" in u.lower()), None)
    if real:
        contact["linkedin"] = real
    elif not str(contact.get("linkedin") or "").startswith("http"):
        # Anchor text like "Portfolio | LinkedIn" is worse than nothing: it
        # would be typed into a URL field verbatim.
        contact["linkedin"] = None
    other = next((u for u in links
                  if "linkedin.com" not in u.lower()), None)
    if other and not contact.get("website"):
        contact["website"] = other
    return master


def parse(pdf: Path, out: Path = MASTER) -> dict:
    links = pdf_links(pdf)
    text = pdf_text(pdf)
    if links:
        text += "\n\nLINKS (real addresses behind this resume's hyperlinks):\n"
        text += "\n".join(links)
    master = llm.complete_json(SYSTEM, text, max_tokens=8192)
    master = _fix_links(master, links)
    out.write_text(json.dumps(master, indent=2, ensure_ascii=False))
    return master


def vault_rows(master: dict) -> dict:
    """The typed facts a resume can fill. The rest a form asks for has to come
    from the user, which is what the pre-flight question exists for."""
    c = master.get("contact", {}) or {}
    jobs = master.get("experience", []) or []
    rows = {
        "full_name": master.get("name"),
        "email": c.get("email"),
        "phone": c.get("phone"),
        "location": c.get("location"),
        # Only a real address. Anchor text in a URL field is worse than blank:
        # blank makes the agent ask, junk makes it confidently type junk.
        "linkedin": (c.get("linkedin")
                     if str(c.get("linkedin") or "").startswith("http") else None),
        "website": (c.get("website")
                    if str(c.get("website") or "").startswith("http") else None),
    }
    if jobs:
        rows["current_company"] = jobs[0].get("company")
        rows["current_title"] = jobs[0].get("title")
    if rows.get("full_name"):
        parts = rows["full_name"].split()
        rows["first_name"] = parts[0]
        rows["last_name"] = parts[-1] if len(parts) > 1 else None
    return {k: v for k, v in rows.items() if v}
