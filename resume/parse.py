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

import paths

HERE = Path(__file__).resolve().parent
#: Per-profile: the parsed resume is the user's, not the code's.
MASTER = paths.MASTER

SYSTEM = """Extract this resume into JSON. Return ONLY JSON, this shape:

{
  "name": "...",
  "contact": {"email": "...", "phone": "...", "location": "...", "linkedin": "...",
              "github": "...", "x": "...", "website": "..."},
  "summary": "...",
  "experience": [
    {"company": "...", "title": "...", "dates": "Jan 2023 - Present",
     "bullets": ["...", "..."]}
  ],
  "education": [{"school": "...", "degree": "...", "dates": "...", "score": "..."}],
  "skills": {"Category": ["skill", "skill"]},
  "projects": [{"name": "...", "text": "...", "dates": "..."}],
  "positions": [{"name": "...", "text": "...", "dates": "..."}],
  "achievements": [{"name": "...", "text": "...", "dates": "..."}],
  "certifications": [{"name": "...", "text": "...", "dates": "..."}]
}

Rules:
- Copy text exactly. Do not improve, shorten, or rewrite anything.
- Every section in the resume goes somewhere. Nothing is dropped.
- `experience` is paid work and internships only. A campus or volunteer role
  (placement coordinator, club lead, class representative, NSS/NCC) goes in
  `positions`.
- Awards, ranks, scholarships and competition wins go in `achievements`.
- Languages spoken go in `skills` under the category "Languages".
- A section the resume does not have is an empty list.
- Experience in reverse chronological order, most recent first.
- Keep every bullet. Selection happens later, per job.
- One consistent date format throughout.
- If a field is genuinely absent, use null. Never invent one.
- `linkedin`, `github`, `x` (Twitter/X) and `website` (portfolio or personal
  site) must each be a full https:// URL, or null. A resume usually shows the
  word "LinkedIn" as a hyperlink, so the visible text is not the address — take
  the address from the LINKS list below the resume text."""

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


#: A profile, not a page on the site: github.com/name but not github.com/name/repo.
#: A repo link is a project, and the repo may not even be theirs.
_PROFILES = {
    "linkedin": re.compile(r"linkedin\.com/in/[\w%-]+", re.I),
    "github": re.compile(r"//(?:www\.)?github\.com/[\w-]+/?(?:[?#]|$)", re.I),
    "x": re.compile(r"//(?:www\.)?(?:x|twitter)\.com/\w+/?(?:[?#]|$)", re.I),
}
#: The same profiles written out as plain text, which is how a resume that
#: already shows its URLs carries them. No scheme needed in the text.
_IN_TEXT = re.compile(
    r"(?<![\w.-])(?:https?://)?(?:www\.)?(?:linkedin\.com/in/[\w%-]+|github\.com/[\w-]+"
    r"|(?:x|twitter)\.com/\w+)(?![\w/.-]*\w)", re.I)
_OWNED = re.compile(r"linkedin\.com|github\.com|(?:^|[/.])(?:x|twitter)\.com", re.I)


def text_links(text: str) -> list[str]:
    """Profile URLs typed out in the resume's text, as full https:// addresses."""
    seen: dict[str, None] = {}
    for m in _IN_TEXT.finditer(text or ""):
        url = re.sub(r"^(?:https?://)?(?:www\.)?", "https://", m.group(0))
        seen.setdefault(url.rstrip("/"), None)
    return list(seen)


def _fix_links(master: dict, links: list[str]) -> dict:
    """Overwrite each profile link with the real URL when the file has one.

    Deterministic, and it runs after the model: the address is a fact sitting
    in the file, so it should not depend on the model having copied it right.
    """
    contact = master.setdefault("contact", {}) or {}
    master["contact"] = contact
    # Older spellings the model sometimes uses; one key per link from here on.
    for old, new in (("twitter", "x"), ("portfolio", "website")):
        if contact.get(old) and not contact.get(new):
            contact[new] = contact[old]
        contact.pop(old, None)
    for key, pattern in _PROFILES.items():
        real = next((u for u in links if pattern.search(u)), None)
        if real:
            contact[key] = real
        elif not str(contact.get(key) or "").startswith("http"):
            # Anchor text like "Portfolio | LinkedIn" is worse than nothing: it
            # would be typed into a URL field verbatim.
            contact[key] = None
    # The portfolio is whatever link is left over — anything not a profile on
    # one of the sites above, and not a mail link.
    other = next((u for u in links if u.lower().startswith("http")
                  and not _OWNED.search(u)), None)
    if other and not str(contact.get("website") or "").startswith("http"):
        contact["website"] = other
    elif not str(contact.get("website") or "").startswith("http"):
        contact["website"] = None
    return master


def parse(pdf: Path, out: Path = MASTER) -> dict:
    text = pdf_text(pdf)
    # Hyperlinks first, then addresses typed out as text: a resume that
    # already prints "github.com/name" may have no link annotation at all.
    links = list(dict.fromkeys(pdf_links(pdf) + text_links(text)))
    if links:
        text += "\n\nLINKS (real addresses behind this resume's hyperlinks):\n"
        text += "\n".join(links)
    # 60 s a try: a normal read is 6-18 s, so a reply slower than that is
    # stuck, and the client's retry gets a fresh one sooner.
    import usage
    with usage.purpose("read_resume"):
        master = llm.complete_json(SYSTEM, text, max_tokens=8192, timeout=60)
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
        "github": (c.get("github")
                   if str(c.get("github") or "").startswith("http") else None),
        "x": (c.get("x") if str(c.get("x") or "").startswith("http") else None),
    }
    if jobs:
        rows["current_company"] = jobs[0].get("company")
        rows["current_title"] = jobs[0].get("title")
    if rows.get("full_name"):
        parts = rows["full_name"].split()
        rows["first_name"] = parts[0]
        rows["last_name"] = parts[-1] if len(parts) > 1 else None
    return {k: v for k, v in rows.items() if v}
