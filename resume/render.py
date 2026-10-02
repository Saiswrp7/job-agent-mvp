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
import unicodedata
from pathlib import Path

import paths

from resume import layouts as L

HERE = Path(__file__).resolve().parent
#: The one template every layout fills (`resume/layouts/base.tex`). It ships
#: with the code; the PDFs it produces belong to the user.
TEMPLATE = paths.TEMPLATE
OUT_DIR = paths.RESUMES

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
    out = out.replace("–", "--").replace("—", "---").replace("’", "'")
    # Straight double quotes print as two closing quotes in TeX ("CRM Second
    # Brain" came out as ”CRM Second Brain”). A matched pair becomes `` ''.
    return re.sub(r'"([^"]*)"', r"``\1''", out)


#: Contact keys that hold a profile link, in the order they are shown.
#: `portfolio` and `twitter` are older spellings of `website` and `x`.
LINK_KEYS = (("website", "portfolio"), ("linkedin",), ("github",), ("x", "twitter"))


def links(m: dict) -> list[str]:
    """Every profile URL the person has, in display order. Only real
    addresses: anchor text like "LinkedIn" in a URL slot is skipped, not shown."""
    c = m.get("contact", {}) or {}
    out = []
    for keys in LINK_KEYS:
        url = next((c.get(k) for k in keys
                    if str(c.get(k) or "").startswith("http")), None)
        if url:
            out.append(url)
    return out


def shown(url: str) -> str:
    """The address as a person reads it: linkedin.com/in/x, not https://www…"""
    return re.sub(r"^https?://(www\.)?", "", url).rstrip("/")


def _href(url: str) -> str:
    # Inside \href, % and # still end or break the argument.
    return url.replace("%", r"\%").replace("#", r"\#")


def _contact(m: dict) -> str:
    c = m.get("contact", {})
    parts = [c.get("location"), c.get("email"), c.get("phone")]
    line = "~|~".join(tex_escape(p) for p in parts if p)
    # The address itself is printed, not the word "LinkedIn" behind a link.
    # Parsers keep the visible text and often drop the link target, so a
    # resume that says "LinkedIn" hands the ATS a word instead of an address.
    # Links get their own line: four URLs after an email and phone overflow it.
    shown_links = [r"\href{" + _href(u) + "}{" + tex_escape(shown(u)) + "}"
                   for u in links(m)]
    if shown_links:
        line += r"\\[1pt]" + "~|~".join(shown_links)
    return line


def _experience(m: dict) -> str:
    blocks = []
    for job in m.get("experience", []):
        bullets = job.get("bullets", [])
        # A role with no bullets still prints its heading line. Skipping it
        # hid a real job from the page, and the gate, which checks every
        # company made it out, then blocked the whole PDF (every fresher
        # resume, 2026-09-28): one rule deleted the line, the next one punished
        # its absence.
        role = (rf"\role{{{tex_escape(job.get('company',''))}}}"
                rf"{{{tex_escape(job.get('dates',''))}}}"
                rf"{{{tex_escape(job.get('title',''))}}}")
        if not bullets:
            blocks.append(role + "\n" r"\vspace{6pt}")
            continue
        items = "\n".join(rf"  \item {tex_escape(b)}" for b in bullets)
        blocks.append(
            role + "\n" rf"\begin{{itemize}}" "\n" f"{items}\n" rf"\end{{itemize}}"
            "\n" r"\vspace{6pt}"
        )
    return "\n".join(blocks)


#: Keys a score may be stored under. Printed exactly as given: IIT-M's rule is
#: that 7.99 is not 8, so nothing here rounds.
_SCORE_KEYS = ("cgpa", "gpa", "score", "percentage")


def _education(m: dict) -> str:
    rows = []
    for e in m.get("education", []):
        score = next((str(e[k]) for k in _SCORE_KEYS if e.get(k)), "")
        degree = tex_escape(e.get("degree", ""))
        if score:
            degree += f" \\textperiodcentered\\ {tex_escape(score)}"
        rows.append(
            rf"\textbf{{{tex_escape(e.get('school',''))}}} \hfill "
            rf"\datestyle{{{tex_escape(e.get('dates',''))}}}\\[1pt]"
            # \par, not only \vspace: a \vspace inside a paragraph waits for the
            # line to end, so without it every school ran into the one before.
            rf"\textit{{{degree}}}\par\vspace{{4pt}}"
        )
    return "\n".join(rows)


def _items(entries) -> str:
    """Projects, positions, achievements, certifications: optional sections
    that print only when the person has them. A plain line, or a name plus a
    line, each a bullet."""
    if not entries:
        return ""
    lines = []
    for e in entries:
        if isinstance(e, dict):
            head = e.get("name") or e.get("title") or e.get("role") or ""
            body = e.get("text") or e.get("description") or e.get("detail") or ""
            when = e.get("dates") or e.get("year") or ""
            line = (rf"\textbf{{{tex_escape(str(head))}}}" if head else "")
            if body:
                line += (": " if head else "") + tex_escape(str(body))
            if when:
                line += rf" \hfill {tex_escape(str(when))}"
        else:
            line = tex_escape(str(e))
        if line:
            lines.append(rf"  \item {line}")
    if not lines:
        return ""
    return "\\begin{itemize}\n" + "\n".join(lines) + "\n\\end{itemize}"


def _skills(m: dict) -> str:
    skills = m.get("skills", {})
    if isinstance(skills, list):
        return tex_escape(", ".join(skills))
    rows = []
    for category, items in skills.items():
        joined = ", ".join(items) if isinstance(items, list) else str(items)
        rows.append(rf"\textbf{{{tex_escape(category)}:}} {tex_escape(joined)}")
    # Between rows, never after the last: a trailing \\ ends an empty line,
    # and at the bottom of a full page that empty line is a blank page 2.
    return "\\\\[4pt]\n".join(rows)


def _name_block(master: dict, spec: dict) -> str:
    name = tex_escape(master.get("name", ""))
    contact = _contact(master)
    if master.get("headline"):
        # One line under the name, above the contact line: "Growth Manager |
        # CRM & Revenue". Plain text, so an ATS reads it as words.
        contact = (r"\normalsize " + tex_escape(master["headline"])
                   + r"}\\[2pt]{\small " + contact)
    style = spec["name"]
    if style == "center_smallcaps":
        # Small caps read the letters' case, so the name goes in as written in
        # title case, not in capitals (which small caps would print full size).
        return (r"\begin{center}{\huge\scshape " + name.title() + r"}\\[3pt]"
                r"{\small " + contact + r"}\end{center}\vspace{-8pt}")
    if style == "center":
        return (r"\begin{center}{\LARGE\bfseries " + name.upper() + r"}\\[3pt]"
                r"{\small " + contact + r"}\end{center}\vspace{-8pt}")
    color = r"\color{accent}" if style == "left_accent" else ""
    return (r"{\Large\bfseries" + color + " " + name.upper() + r"}\\[2pt]"
            r"{\small " + contact + r"}\vspace{2pt}")


def _body(master: dict, spec: dict) -> str:
    build = {
        "summary": lambda: tex_escape(master.get("summary", "")),
        "experience": lambda: _experience(master),
        "education": lambda: _education(master),
        "skills": lambda: _skills(master),
        "projects": lambda: _items(master.get("projects")),
        "positions": lambda: _items(master.get("positions")),
        "achievements": lambda: _items(master.get("achievements")),
        "certifications": lambda: _items(master.get("certifications")),
    }
    # Their own order and headings, when they set them (resume/structure.py),
    # win over the layout's.
    titles = {**L.TITLES, **spec.get("titles", {}), **(master.get("_titles") or {})}
    out = []
    for key in master.get("_order") or spec.get("sections") or L.STANDARD_ORDER:
        content = build[key]()
        if content and content.strip():
            # No \vspace between a heading and its content: a vertical space is a
            # legal page break, and it left "Skills" alone at the foot of page 1
            # with its lines on page 2. The gap lives in \titlespacing instead.
            # \needspace: a heading with fewer than four lines of room goes to
            # the next page with its content, instead of ending this one alone.
            out.append(r"\needspace{4\baselineskip}" "\n"
                       rf"\section*{{{titles[key]}}}" "\n" + content)
    return "\n\n".join(out)


def to_tex(master: dict, layout: str | None = None, paper: str = "a4") -> str:
    """The resume in a layout. A4 unless asked: India first, and Letter is
    only for jobs in the US."""
    spec = L.get(layout)
    font, font_opts = L.FONTS[spec["font"]]
    size = float(spec["size"])
    company, dates = L.ROLES[spec["role"]]
    heading = L.HEADINGS[spec["heading"]] + "\n" + L.SPACING.replace(
        "((SECTION_GAP))", spec.get("section_gap", "6pt"))
    lender = L.RUPEE_FROM.get(spec["font"])
    rupee = ""
    if lender:
        name, opts = L.FONTS[lender]
        rupee = ("\\usepackage{newunicodechar}\n"
                 f"\\newfontfamily\\rupeefont{{{name}}}[{opts}]\n"
                 "\\newunicodechar{₹}{{\\rupeefont ₹}}")
    fills = {
        "((CLASS_SIZE))": "11pt" if size >= 11 else "10pt",
        "((PAPER))": "letterpaper" if paper == "letter" else "a4paper",
        "((MARGIN))": spec["margin"],
        "((ACCENT))": spec["accent"],
        "((FONT))": font, "((FONT_OPTS))": font_opts, "((RUPEE))": rupee,
        "((SIZE))": f"{size:g}pt", "((LEAD))": f"{size * float(spec.get('leading', 1.15)):.1f}pt",
        "((HEADING))": heading,
        "((ITEMSEP))": spec["itemsep"],
        "((ROLE_COMPANY))": company, "((ROLE_DATES))": dates,
        "((NAME_BLOCK))": _name_block(master, spec),
        "((BODY))": _body(master, spec),
    }
    tex = TEMPLATE.read_text()
    for key, value in fills.items():
        tex = tex.replace(key, value)
    return tex


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


#: Characters the typesetting itself turns into something else on purpose:
#: dashes are rewritten to TeX ligatures, and quotes may come back straight.
_TYPESET = set("–—‘’“”")


def _all_text(master: dict) -> str:
    """Everything the person wrote that goes on the page."""
    import json
    return json.dumps(master, ensure_ascii=False)


def _role_at(flat: str, company: str | None, dates: str | None,
             near: int = 40) -> int:
    """Where a job's role line sits: the first mention of the company with its
    dates right after it (base.tex prints "Company \\hfill Dates" on every
    layout). No dates, or none that close, falls back to the first mention."""
    if not company:
        return -1
    first = flat.find(company)
    d = (dates or "").strip().replace("–", "-")
    if not d:
        return first
    text = flat.replace("–", "-")
    i = first
    while i != -1:
        end = i + len(company)
        if d in text[end: end + len(d) + near]:
            return i
        i = flat.find(company, i + 1)
    return first


def gate(pdf: Path, master: dict) -> list[str]:
    """What the ATS sees. Returns a list of problems; empty means it passed."""
    text = extract_text(pdf)
    if not text.strip():
        return ["pdftotext extracted nothing — the PDF has no text layer"]

    problems = []
    flat = re.sub(r"\s+", " ", text)

    # Characters a recruiter's search can never match: merged letters (ﬁ ﬂ ﬃ),
    # icon-font glyphs in the Private Use Area, and the replacement character
    # a broken font map leaves behind. Any one of them inside a word makes that
    # word unfindable, which is exactly how AppsFlyer went missing.
    junk = sorted({ch for ch in text if "\ufb00" <= ch <= "\ufb06"
                   or "\ue000" <= ch <= "\uf8ff" or ch == "\ufffd"})
    if junk:
        problems.append("unsearchable characters in extracted text: "
                        + ", ".join(f"U+{ord(ch):04X}" for ch in junk))

    # A token that only matches once the text is NFKC-normalised is the same
    # leak seen from the other side: it looks present, and a literal search
    # still misses it. The name is compared case-blind because it prints in
    # capitals — with lower(), not casefold(): casefold() itself expands ﬁ to
    # "fi" and would hide the very leak this looks for.
    norm = unicodedata.normalize("NFKC", flat)
    c = master.get("contact", {}) or {}
    tokens = [master.get("name"), c.get("email")]
    tokens += [j.get("company") for j in master.get("experience", [])]
    for tok in filter(None, (str(t).strip() for t in tokens)):
        if tok.lower() not in flat.lower() and tok.lower() in norm.lower():
            problems.append(f"{tok!r} only matches after normalising — a "
                            f"ligature or compatibility character leaked")

    # Every non-ASCII character they wrote must come back out. A font with no
    # glyph for one either drops it silently (XCharter printed "₹200 Cr" as
    # "200 Cr") or prints U+FFFD; the first is invisible to every other check.
    written = unicodedata.normalize("NFC", _all_text(master))
    got = unicodedata.normalize("NFC", text)
    lost = sorted({ch for ch in written if ord(ch) > 127 and ch not in got
                   and ch not in _TYPESET})
    if lost:
        problems.append("characters lost in the PDF (font has no glyph): "
                        + ", ".join(f"{ch} U+{ord(ch):04X}" for ch in lost))

    for url in links(master):
        if shown(url) not in flat:
            problems.append(f"link not visible as text: {shown(url)!r}")

    for job in master.get("experience", []):
        for field in ("company", "dates"):
            val = (job.get(field) or "").strip()
            if val and val.replace("–", "-") not in flat.replace("–", "-"):
                problems.append(f"missing from extracted text: {field} {val!r}")

    email = (master.get("contact", {}) or {}).get("email")
    if email and email not in flat:
        problems.append(f"contact email {email!r} did not survive extraction")

    # Order: experience must come out in the order it went in, or the parser
    # will attach bullets to the wrong employer. Each company is located at
    # its role line (the name with its dates close by), not at its first
    # mention: a summary that names an older employer first read as columns
    # and failed every tailor of a real resume.
    positions = [p for p in (_role_at(flat, j.get("company"), j.get("dates"))
                             for j in master.get("experience", [])) if p != -1]
    if positions != sorted(positions):
        problems.append("experience extracted out of order — check for columns")

    return problems


def upload_name(master: dict) -> str:
    """The name an employer sees: Firstname_Lastname_Resume.pdf.

    The stored file is named per job ("…_Paytm_Growth_Manager.pdf") so the
    person's copies stay apart, but that name travels with the upload and
    tells the company which other companies got a version. Name parts keep
    the order the person wrote them in; which part is the surname is not ours
    to guess.
    """
    words = re.findall(r"[^\W\d_]+", master.get("name") or "")
    return "_".join(w.capitalize() for w in words + ["Resume"]) + ".pdf"


def first_name(master: dict) -> str:
    """The first word of the name as they wrote it. For a name written surname
    first ("BUGATA SAI SWAROOP") that is the surname, which is why the vault's
    `first_name` wins over this when the person has set it."""
    words = re.findall(r"[^\W\d_]+", master.get("name") or "")
    return words[0].capitalize() if words else "Resume"


def company_part(company: str) -> str:
    """"Paytm First Games" -> "PaytmFirstGames", "CRED" stays "CRED": one
    token, so the file name reads as exactly two parts."""
    words = re.findall(r"[^\W_]+", company or "")
    return "".join(w if w.isupper() else w.capitalize() for w in words)


def named_for(src: Path, first: str, company: str | None) -> Path:
    """A copy of `src` named Firstname_Company.pdf, the name the company sees.

    A copy, not a rename: the stored file keeps its own name, and for their own
    upload the bytes stay exactly theirs, only the file name changes. One
    folder per source file, since three applications can run at once.
    """
    part = company_part(company or "")
    if not part or not src.exists():
        return src
    dest = src.parent / "upload" / src.stem / "to" / f"{first}_{part}.pdf"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(src, dest)
    return dest


def upload_path(pdf: Path) -> Path:
    """Where `render` left the upload-named copy of `pdf`.

    One folder per stored PDF, because every copy has the same file name and
    up to three applications run at once."""
    folder = pdf.parent / "upload" / pdf.stem
    found = sorted(folder.glob("*.pdf"))
    if not found:
        raise FileNotFoundError(f"no upload copy for {pdf.name} — render it again")
    return found[0]


def pages(pdf: Path) -> int:
    out = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True)
    m = re.search(r"^Pages:\s+(\d+)", out.stdout, re.M)
    return int(m.group(1)) if m else 1


def preview_png(pdf: Path, out: Path | None = None, dpi: int = 110) -> Path:
    """Page 1 as a PNG, so a person picks a layout by looking at their own
    resume in it. Asking someone to choose between seven names they cannot
    see is a bad question. About a second with pdftoppm."""
    if not shutil.which("pdftoppm"):
        raise RuntimeError("pdftoppm not found — brew install poppler")
    out = out or pdf.with_suffix(".png")
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["pdftoppm", "-png", "-r", str(dpi), "-f", "1", "-l", "1",
                    "-singlefile", str(pdf), str(out.with_suffix(""))],
                   check=True, capture_output=True)
    return out


def render(master: dict, out_name: str, *, skip_gate: bool = False,
           layout: str | None = None) -> Path:
    out = OUT_DIR / out_name
    # Last build's upload copy goes first, so a rebuild that fails the gate
    # cannot leave the old PDF behind to be sent in its place.
    folder = out.parent / "upload" / out.stem
    for old in folder.glob("*.pdf"):
        old.unlink()
    compile_pdf(to_tex(master, layout), out)
    if not skip_gate:
        problems = gate(out, master)
        if problems:
            out.unlink(missing_ok=True)
            raise RuntimeError("ATS extraction gate failed:\n  - "
                               + "\n  - ".join(problems))
    folder.mkdir(parents=True, exist_ok=True)
    shutil.copy(out, folder / upload_name(master))
    return out
