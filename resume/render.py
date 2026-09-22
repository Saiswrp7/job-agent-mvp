"""master.json (+ patch) -> LaTeX -> PDF, with an extraction gate.

Two things here are load-bearing:

- **Escaping.** Resume text is full of `&`, `%`, `$` and `#` — "R&D", "grew
  40%", "$2M". Unescaped, any one of them fails the compile. This is the tax
  LaTeX charges, and it is paid in exactly one function.

- **The gate.** Whatever `pdftotext` prints is what the ATS sees. If the
  extracted text loses a company, a date or the contact line, or comes back out
  of order, the render fails rather than shipping a resume that looks right and
  parses wrong.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "template.tex"
OUT_DIR = HERE.parent / "artifacts" / "resumes"

_ESCAPES = {
    "\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$",
    "#": r"\#", "_": r"\_", "{": r"\{", "}": r"\}",
    "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
}


def tex_escape(s: str) -> str:
    if not s:
        return ""
    # Backslash first, or it would double-escape the replacements that follow.
    out = s.replace("\\", _ESCAPES["\\"])
    for ch, rep in _ESCAPES.items():
        if ch == "\\":
            continue
        out = out.replace(ch, rep)
    return out.replace("–", "--").replace("—", "---").replace("’", "'")


def _contact(m: dict) -> str:
    c = m.get("contact", {})
    parts = [c.get("location"), c.get("email"), c.get("phone")]
    line = "~|~".join(tex_escape(p) for p in parts if p)
    if c.get("linkedin"):
        line += r"~|~\href{" + c["linkedin"] + r"}{LinkedIn}"
    return line


def _experience(m: dict) -> str:
    blocks = []
    for job in m.get("experience", []):
        bullets = job.get("bullets", [])
        if not bullets:
            continue
        items = "\n".join(rf"  \item {tex_escape(b)}" for b in bullets)
        blocks.append(
            rf"\role{{{tex_escape(job.get('company',''))}}}"
            rf"{{{tex_escape(job.get('dates',''))}}}"
            rf"{{{tex_escape(job.get('title',''))}}}"
            "\n" rf"\begin{{itemize}}" "\n" f"{items}\n" rf"\end{{itemize}}"
            "\n" r"\vspace{6pt}"
        )
    return "\n".join(blocks)


def _education(m: dict) -> str:
    rows = []
    for e in m.get("education", []):
        rows.append(
            rf"\textbf{{{tex_escape(e.get('school',''))}}} \hfill "
            rf"\textbf{{{tex_escape(e.get('dates',''))}}}\\[1pt]"
            rf"\textit{{{tex_escape(e.get('degree',''))}}}\vspace{{4pt}}"
        )
    return "\n".join(rows)


def _skills(m: dict) -> str:
    skills = m.get("skills", {})
    if isinstance(skills, list):
        return tex_escape(", ".join(skills))
    rows = []
    for category, items in skills.items():
        joined = ", ".join(items) if isinstance(items, list) else str(items)
        rows.append(rf"\textbf{{{tex_escape(category)}:}} {tex_escape(joined)}\\[4pt]")
    return "\n".join(rows)


def to_tex(master: dict) -> str:
    tex = TEMPLATE.read_text()
    return (tex
            .replace("((NAME))", tex_escape(master.get("name", "")).upper())
            .replace("((CONTACT))", _contact(master))
            .replace("((SUMMARY))", tex_escape(master.get("summary", "")))
            .replace("((EXPERIENCE))", _experience(master))
            .replace("((EDUCATION))", _education(master))
            .replace("((SKILLS))", _skills(master)))


def compile_pdf(tex: str, out_path: Path) -> Path:
    if not shutil.which("tectonic"):
        raise RuntimeError("tectonic not found — brew install tectonic")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "resume.tex"
        src.write_text(tex)
        proc = subprocess.run(
            ["tectonic", "--outdir", tmp, "--chatter", "minimal", str(src)],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout)[-1500:]
            raise RuntimeError(f"LaTeX compile failed:\n{tail}")
        shutil.copy(Path(tmp) / "resume.pdf", out_path)
    return out_path


def extract_text(pdf: Path) -> str:
    if not shutil.which("pdftotext"):
        return ""
    return subprocess.run(["pdftotext", "-layout", str(pdf), "-"],
                          capture_output=True, text=True).stdout


def gate(pdf: Path, master: dict) -> list[str]:
    """What the ATS sees. Returns a list of problems; empty means it passed."""
    text = extract_text(pdf)
    if not text.strip():
        return ["pdftotext extracted nothing — the PDF has no text layer"]

    problems = []
    flat = re.sub(r"\s+", " ", text)

    for job in master.get("experience", []):
        for field in ("company", "dates"):
            val = (job.get(field) or "").strip()
            if val and val.replace("–", "-") not in flat.replace("–", "-"):
                problems.append(f"missing from extracted text: {field} {val!r}")

    email = (master.get("contact", {}) or {}).get("email")
    if email and email not in flat:
        problems.append(f"contact email {email!r} did not survive extraction")

    # Order: experience must come out in the order it went in, or the parser
    # will attach bullets to the wrong employer.
    positions = [flat.find(j["company"]) for j in master.get("experience", [])
                 if j.get("company") and flat.find(j["company"]) != -1]
    if positions != sorted(positions):
        problems.append("experience extracted out of order — check for columns")

    return problems


def render(master: dict, out_name: str, *, skip_gate: bool = False) -> Path:
    out = OUT_DIR / out_name
    compile_pdf(to_tex(master), out)
    if not skip_gate:
        problems = gate(out, master)
        if problems:
            out.unlink(missing_ok=True)
            raise RuntimeError("ATS extraction gate failed:\n  - "
                               + "\n  - ".join(problems))
    return out
