"""Resume layouts: each one a handful of values over one shared template.

Why values and not templates: every layout goes through `base.tex` and the
same `render.gate`, so a layout cannot ship a resume the ATS reads wrong
without the build failing. Seven .tex files would each need their own
checking, and would drift. This is RenderCV's design (Research B §4): "all
themes are identical except for their default values".

What a layout may change: font, body size, margins, paper, heading style,
where the name sits, one accent colour, and which sections come first.
What it may not: columns, tables, icons, anything in a header or footer.
Those are on Greenhouse's own failed-parse list, which is why Deedy, AltaCV
and the real IIM table grid are not here.

Fonts are the ones tectonic ships and that were test-compiled with
`Ligatures=NoCommon` and checked for merged letters in the extracted text.
Substitutions, each found by compiling and reading the extracted text back:
- Modern Accent was specced as Source Sans 3. It is not in tectonic's bundle,
  and Source Sans Pro (its older name) maps "-" to U+2011, so "sai-swaroop"
  extracts as a different word; Lato does the same with U+2010. Fira Sans
  extracts a plain hyphen and has ₹, so Modern Accent uses it.
- Calibri is proprietary; Indian Placement uses Carlito, metric-compatible.
"""

from __future__ import annotations

import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE = HERE / "base.tex"
DEFAULT = "charter"


def _files(name: str, up: str, bold: str, it: str, bi: str, ext: str,
           extra: str = "") -> tuple[str, str]:
    opts = (f"Extension={ext}, UprightFont=*-{up}, BoldFont=*-{bold}, "
            f"ItalicFont=*-{it}, BoldItalicFont=*-{bi}")
    return name, opts + (f", {extra}" if extra else "")


FONTS = {
    "xcharter": _files("XCharter", "Roman", "Bold", "Italic", "BoldItalic", ".otf"),
    # Latin Modern's real small caps live in their own file. Without naming it
    # fontspec fakes them, and faked small caps extract as "EXPERiENCE".
    "latin_modern": _files("lmroman10", "regular", "bold", "italic", "bolditalic",
                           ".otf", "SmallCapsFont=lmromancaps10-regular"),
    "eb_garamond": _files("EBGaramond", "Regular", "Bold", "Italic", "BoldItalic", ".otf"),
    "carlito": _files("Carlito", "Regular", "Bold", "Italic", "BoldItalic", ".ttf"),
    "termes": _files("texgyretermes", "regular", "bold", "italic", "bolditalic", ".otf"),
    "heros": _files("texgyreheros", "regular", "bold", "italic", "bolditalic", ".otf"),
    "fira_sans": _files("FiraSans", "Regular", "Bold", "Italic", "BoldItalic", ".otf"),
}

#: Fonts with no ₹ glyph, and the font that lends them one. XCharter drops a
#: missing glyph without a trace ("from 200 Cr"), Latin Modern and TeX Gyre
#: print U+FFFD; either way the amount loses its currency. A serif lends to
#: serifs and a sans to sans, so the borrowed sign does not stand out.
RUPEE_FROM = {"xcharter": "eb_garamond", "latin_modern": "eb_garamond",
              "termes": "eb_garamond", "heros": "carlito"}

#: Section headings. All are on the ATS allow-list (Research B, rule 9):
#: "Positions of Responsibility" included, because Indian campus parsers and
#: recruiters both expect it by that name.
STANDARD_ORDER = ["summary", "experience", "projects", "education",
                  "positions", "achievements", "certifications", "skills"]

LAYOUTS: dict[str, dict] = {
    "charter": {
        "label": "Charter Professional",
        "line": "Charter serif, bold company, italic title. The safe default "
                "for experienced people in any function.",
        "for": "experienced, any function",
        "font": "xcharter", "size": 10.5, "margin": "0.5in",
        "heading": "caps_rule", "name": "left", "accent": "000000",
        "role": "bold_dates", "itemsep": "1.5pt", "max_pages": 1,
    },
    "tech_compact": {
        "label": "Tech Compact",
        "line": "Jake's-resume style: Latin Modern, small-caps headings, "
                "dense. Engineering, data, tech PM, early career.",
        "for": "engineering, data, tech PM, early career",
        # 10pt: Latin Modern sets wider than Charter, and a dense page is the
        # point of this layout. Still at the 10pt floor, not the 9.5 allowed.
        "font": "latin_modern", "size": 10, "margin": "0.5in",
        "heading": "smallcaps_rule", "name": "center_smallcaps",
        "accent": "000000", "role": "plain_dates", "itemsep": "1pt",
        "max_pages": 1,
    },
    "harvard": {
        "label": "Harvard Classic",
        "line": "Garamond serif, name centred, all-caps headings. "
                "Consulting, finance, business, MBA.",
        "for": "consulting, finance, business, MBA",
        "font": "eb_garamond", "size": 11, "margin": "0.75in",
        "heading": "caps_rule", "name": "center", "accent": "000000",
        "role": "bold_dates", "itemsep": "1.5pt", "max_pages": 1,
    },
    "modern_accent": {
        "label": "Modern Accent",
        "line": "Clean sans serif, one blue accent on the name and headings, "
                "no icons. Product, growth, marketing, design-adjacent.",
        "for": "product, growth, marketing, design-adjacent",
        "font": "fira_sans", "size": 10.5, "margin": "0.6in",
        "heading": "accent_rule", "name": "left_accent", "accent": "1F4E79",
        "role": "bold_dates", "itemsep": "1.5pt", "max_pages": 1,
    },
    "indian_placement": {
        "label": "Indian Placement",
        "line": "IIM-style campus CV as plain text: education first, Positions "
                "of Responsibility, no tables. Freshers, MBA campus, BFSI/FMCG.",
        "for": "freshers, MBA campus, Indian BFSI/FMCG/consulting",
        "font": "carlito", "size": 10.5, "margin": "0.6in",
        "heading": "caps_rule", "name": "center", "accent": "000000",
        "role": "bold_dates", "itemsep": "1pt", "max_pages": 1,
        # Education first is what makes a placement CV; no summary, because
        # campus CVs do not carry one and the space goes to scores and PoRs.
        "sections": ["education", "experience", "projects", "positions",
                     "achievements", "certifications", "skills"],
        "titles": {"experience": "Work Experience"},
    },
    "executive": {
        "label": "Executive Two-Page",
        "line": "Times-style serif at 11pt, wider margins, up to two pages. "
                "12+ years, director and VP.",
        "for": "12+ years, director/VP",
        "font": "termes", "size": 11, "margin": "0.75in",
        "heading": "caps_rule", "name": "left", "accent": "000000",
        "role": "bold_dates", "itemsep": "2pt", "max_pages": 2,
        "titles": {"summary": "Professional Summary",
                   "experience": "Professional Experience"},
    },
    "engineering_classic": {
        "label": "Engineering Classic",
        "line": "Tech Compact in a Helvetica-style sans. Core engineering "
                "(mechanical, electrical), freshers.",
        "for": "core engineering, freshers",
        "font": "heros", "size": 10, "margin": "0.5in",
        "heading": "caps_rule", "name": "left", "accent": "000000",
        "role": "plain_dates", "itemsep": "1pt", "max_pages": 1,
    },
}

TITLES = {"summary": "Summary", "experience": "Experience",
          "projects": "Projects", "education": "Education",
          "positions": "Positions of Responsibility",
          "achievements": "Achievements", "certifications": "Certifications",
          "skills": "Skills"}

HEADINGS = {
    "caps_rule": r"\titleformat{\section}{\normalsize\bfseries}{}{0em}"
                 r"{\MakeUppercase}[\titlerule]",
    # Real small caps from lmromancaps10: they extract as ordinary letters.
    "smallcaps_rule": r"\titleformat{\section}{\large\scshape\raggedright}{}{0em}"
                      r"{}[\titlerule]",
    # The colour goes inside the title, and the coloured rule ends in \nobreak:
    # \color in vertical mode leaves a marker, the glue after a marker is a
    # legal page break, and "Skills" was left alone at the foot of page 1.
    "accent_rule": r"\titleformat{\section}{\large\bfseries}{}{0em}"
                   r"{\color{accent}}[{\color{accent}\titlerule}\nobreak]",
}
SPACING = r"\titlespacing*{\section}{0pt}{((SECTION_GAP))}{6pt}"

ROLES = {"bold_dates": (r"\textbf", r"\textbf"),
         "plain_dates": (r"\textbf", r"")}


def get(name: str | None) -> dict:
    """A layout by key, falling back to the default rather than failing: a
    stale vault value must never stop a resume from building."""
    key = (name or DEFAULT).strip().lower()
    return {"key": key, **LAYOUTS[key]} if key in LAYOUTS else \
        {"key": DEFAULT, **LAYOUTS[DEFAULT]}


#: Words people use for a look, mapped to the layout that answers them.
_SAID = [
    ("harvard", r"harvard|classic|conservative|traditional|garamond"),
    ("tech_compact", r"tech|jake|compact|dense|latex|engineer(ing)? ?resume|swe|developer"),
    ("modern_accent", r"modern|colou?r|accent|blue|clean|sans|fresh|design"),
    ("indian_placement", r"indian|iim|placement|campus|fresher|education first|b-?school"),
    ("executive", r"executive|two[- ]page|2[- ]page|senior|director|vp\b"),
    ("engineering_classic", r"core engineering|mechanical|electrical|civil|helvetica|arial"),
    ("charter", r"charter|default|original|current|same as before|professional"),
]


def resolve(said: str) -> str | None:
    """A layout key from a key, a label or a description. None when nothing
    matches, so the caller asks instead of guessing a look for them."""
    s = (said or "").strip().lower().replace("-", "_")
    if s in LAYOUTS:
        return s
    for key, spec in LAYOUTS.items():
        if s and s in spec["label"].lower():
            return key
    for key, rx in _SAID:
        if re.search(rx, s.replace("_", " "), re.I):
            return key
    return None


def suggest(role: str = "", years: int | None = None) -> list[str]:
    """Two layouts for this person: the one that fits their stage and field,
    and a second that is different enough to be a real choice."""
    r = (role or "").lower()
    if years is not None and years >= 12:
        return ["executive", "charter"]
    if years is not None and years < 2:
        return (["indian_placement", "tech_compact"]
                if not re.search(r"engineer|developer|data|sde", r)
                else ["tech_compact", "indian_placement"])
    if re.search(r"engineer|developer|sde|data|ml|analyst", r):
        return ["tech_compact", "charter"]
    if re.search(r"consult|financ|bank|mba|strategy", r):
        return ["harvard", "charter"]
    if re.search(r"product|growth|marketing|design|brand", r):
        return ["charter", "modern_accent"]
    return ["charter", "modern_accent"]


def fit_warning(key: str, years: int | None) -> str | None:
    """One sentence when a layout works against them, per the agreed rule:
    say it once with the reason, then build what they chose."""
    spec = get(key)
    if spec["max_pages"] == 2 and years is not None and years < 8:
        return (f"{spec['label']} allows two pages, and at {years} years most "
                "recruiters expect one; a second page reads as padding.")
    if key == "indian_placement" and years is not None and years >= 3:
        return ("Indian Placement puts education first and drops the summary; "
                f"at {years} years your experience is the stronger opening.")
    return None


def catalogue() -> str:
    """Every layout, one line each, for the agent to show."""
    return "\n".join(f"- {k} · {v['label']}: {v['line']}"
                     for k, v in LAYOUTS.items())
