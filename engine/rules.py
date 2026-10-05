"""Free labels from the title, the location and Adzuna's category. No model.

The LLM labeller (labels.py) reads each job once and is the authority. But
most of the table is never read by it: labels are off for now, and 5,800
Adzuna rows arrived in two days. A job with no labels still matches every
search, so nothing is lost; but a search by kind of work ("design jobs")
cannot find it by label, and a search in India cannot tell it is in India.

These rules fill that gap for what is certain from the text alone:

- **Only empty columns, only on rows the LLM has not read** (`label_hash IS
  NULL`). An LLM label is never overwritten. When the LLM does read the row
  later, its answer replaces these.
- **Every family the title names.** "Sales & Marketing Manager" gets
  "sales,marketing", so a search for either finds it (2026-10-02; it used to
  get none, which hid it from both label searches).
- **Growth and CRM are never guessed from a title.** The label prompt tells
  them apart by the work, and half of "Growth Manager" roles in India are CRM.
- **Adzuna's category only when the title says nothing,** and only for the
  categories that name one family ("Accounting & Finance Jobs"). "IT Jobs"
  names a dozen.
- **Country only when exactly one is named.** "Bangalore; New York" stays
  NULL, so it passes a search in either.
"""

from __future__ import annotations

import re
import sqlite3

from .adapters import CITY_ALIASES

W = lambda p: re.compile(p, re.I)   # noqa: E731

#: Title patterns per family. All that match are collected; more than one
#: family means no label.
FAMILY_RULES: list[tuple[str, re.Pattern]] = [
    ("design", W(r"\bdesigners?\b|\bux\b|user experience|\bui\s*/\s*ux\b")),
    ("product", W(r"\bproduct (managers?|owner|lead|head|director|management)\b"
                  r"|\bhead of product\b|\bapm\b|\bchief product officer\b"
                  r"|\bvp,? product\b")),
    ("marketing", W(r"\bmarketing\b|\bbrand manager\b|\bseo\b|\bsem\b"
                    r"|\bsocial media\b|\bpublic relations\b")),
    ("data", W(r"\bdata (analyst|scientist|science)\b|\banalytics\b"
               r"|\bbusiness intelligence\b|\bbi (analyst|developer)\b"
               r"|\bproduct analyst\b")),
    ("engineering", W(r"\bengineer(s|ing)?\b|\bdevelopers?\b|\bsde\b|\bdevops\b"
                      r"|\bsre\b|\bprogrammer\b|\bfull[\s-]?stack\b"
                      r"|\bback[\s-]?end\b|\bfront[\s-]?end\b")),
    ("business_development", W(r"\bbusiness development\b|\bbdm?\b"
                               r"|\bpartnerships?\b")),
    ("sales", W(r"\bsales\b|\baccount executive\b|\bkey account manager\b"
                r"|\binside sales\b")),
    ("customer_success", W(r"\bcustomer success\b")),
    ("support", W(r"\bcustomer (support|service|care)\b|\btechnical support\b"
                  r"|\bhelp ?desk\b|\bservice desk\b|\bvoice process\b")),
    ("hr", W(r"\bhr\b|\bhuman resources?\b|\brecruiter\b|\btalent acquisition\b"
             r"|\bhrbp\b|\bpeople partner\b")),
    ("finance", W(r"\baccountant\b|\baccounting\b|\baccounts (executive|payable|receivable)\b"
                  r"|\bfinance (manager|executive|analyst|lead)\b|\bfp&a\b"
                  r"|\bchartered accountant\b|\btreasury\b|\btaxation\b")),
    ("legal", W(r"\blegal\b|\bcounsel\b|\blawyer\b|\badvocate\b")),
    ("supply_chain", W(r"\bsupply chain\b|\blogistics\b|\bwarehouse\b"
                       r"|\bprocurement\b|\bpurchase (executive|manager)\b")),
    ("content", W(r"\bcontent writer\b|\bcopywriter\b|\bcontent (strategist|editor)\b")),
    ("healthcare", W(r"\bnurse\b|\bnursing\b|\bphysician\b|\bpharmacist\b"
                     r"|\bmedical officer\b|\bdentist\b|\bphysiotherapist\b")),
    ("education", W(r"\bteacher\b|\btutor\b|\bfaculty\b|\bprofessor\b|\blecturer\b")),
    ("admin", W(r"\breceptionist\b|\bfront office\b|\bexecutive assistant\b"
                r"|\bdata entry\b|\boffice (assistant|administrator)\b")),
]

#: Titles that trip a family pattern while meaning another thing.
NOT_A_MATCH = {
    "product": W(r"\bproduct marketing\b"),
    "engineering": W(r"\bsales engineer\b|\bpre-?sales\b"),
}

#: Adzuna categories that name exactly one family. The rest ("IT Jobs",
#: "Engineering Jobs", "PR, Advertising & Marketing Jobs") are left alone.
CATEGORY_FAMILY = {
    "Accounting & Finance Jobs": "finance",
    "HR & Recruitment Jobs": "hr",
    "Sales Jobs": "sales",
    "Teaching Jobs": "education",
    "Healthcare & Nursing Jobs": "healthcare",
    "Legal Jobs": "legal",
    "Admin Jobs": "admin",
    "Logistics & Warehouse Jobs": "supply_chain",
    "Customer Services Jobs": "support",
}

LEVEL_RULES: list[tuple[str, re.Pattern]] = [
    ("intern", W(r"\bintern(ship)?\b")),
    ("leadership", W(r"\bhead of\b|\bavp\b|\bdirector\b|\bvp\b|\bvice president\b"
                     r"|\bchief\b|\bc[etfmop]o\b")),
    ("lead", W(r"\b(principal|staff|team lead|tech lead)\b"
               r"|\blead\b(?! generation)")),
    ("senior", W(r"\bsenior\b|\bsr\.?\b")),
    ("junior", W(r"\bjunior\b|\bjr\.?\b|\bassociate\b|\bentry[ -]level\b"
                 r"|\bfresher\b|\btrainee\b")),
]

INDIA = W(r"\bindia\b|" + "|".join(rf"\b{re.escape(c)}\b" for c in CITY_ALIASES)
          + r"|\bghaziabad\b|\bfaridabad\b|\bkochi\b|\bcoimbatore\b|\bindore\b")
#: A named country or an unmistakable city in one. Deliberately short: a
#: country we fail to spot stays NULL, which passes every filter.
ABROAD: list[tuple[str, re.Pattern]] = [
    ("United States", W(r"\bunited states\b|\busa\b|\bu\.s\.|\bnew york\b"
                        r"|\bsan francisco\b|\bseattle\b|\baustin\b|\bboston\b"
                        r"|\bchicago\b|\blos angeles\b|\bsanta clara\b"
                        r"|\bsan mateo\b|\bpalo alto\b|\bmountain view\b")),
    ("United Kingdom", W(r"\bunited kingdom\b|\buk\b|\blondon\b")),
    ("Canada", W(r"\bcanada\b|\btoronto\b|\bvancouver\b")),
    ("Germany", W(r"\bgermany\b|\bberlin\b|\bmunich\b")),
    ("Singapore", W(r"\bsingapore\b")),
    ("South Korea", W(r"\bkorea\b|\bseoul\b")),
    ("Japan", W(r"\bjapan\b|\btokyo\b")),
    ("Sweden", W(r"\bsweden\b|\bstockholm\b")),
    ("United Arab Emirates", W(r"\buae\b|\bunited arab emirates\b|\bdubai\b")),
    ("Australia", W(r"\baustralia\b|\bsydney\b|\bmelbourne\b")),
    ("Netherlands", W(r"\bnetherlands\b|\bamsterdam\b")),
    ("France", W(r"\bfrance\b|\bparis\b")),
    ("Ireland", W(r"\bireland\b|\bdublin\b")),
    ("Philippines", W(r"\bphilippines\b|\bmanila\b")),
]
REMOTE = W(r"\bremote\b|\bwork from home\b|\bwfh\b")
HYBRID = W(r"\bhybrid\b")


#: Titles in the growth / CRM / marketing tangle. Any family a rule gives
#: them could be the wrong one of the three, so they get none.
GROWTHISH = W(r"\bgrowth\b|\bretention\b|\blifecycle\b|\bcrm\b|\bengagement\b")


def family(title: str, category: str | None = None) -> str | None:
    """Every family the title names, in the order it names them: "Sales &
    Marketing Manager" -> "sales,marketing" (see db.family_match)."""
    if GROWTHISH.search(title or ""):
        return None
    hits = sorted((m.start(), fam) for fam, rx in FAMILY_RULES
                  if (m := rx.search(title or ""))
                  and not (fam in NOT_A_MATCH and NOT_A_MATCH[fam].search(title)))
    if hits:
        return ",".join(fam for _, fam in hits)
    if category:
        return CATEGORY_FAMILY.get(category)
    return None


def level(title: str) -> str | None:
    for name, rx in LEVEL_RULES:
        if rx.search(title or ""):
            return name
    return None


def country(location: str | None, source: str) -> str | None:
    if source == "adzuna":
        return "India"      # every Adzuna query here is the India endpoint
    text = location or ""
    found = {"India"} if INDIA.search(text) else set()
    found |= {name for name, rx in ABROAD if rx.search(text)}
    return found.pop() if len(found) == 1 else None


def work_mode(title: str, location: str | None) -> str | None:
    text = f"{title or ''} | {location or ''}"
    if HYBRID.search(text):
        return "hybrid"
    if REMOTE.search(text):
        return "remote"
    return None


def labels_for(row: dict) -> dict:
    cat = row.get("department") if row.get("source") == "adzuna" else None
    return {"role_family": family(row["title"], cat),
            "level": level(row["title"]),
            "country": country(row.get("location"), row["source"]),
            "work_mode": work_mode(row["title"], row.get("location"))}


#: Rows per transaction. Another process (the evals) reads this file while
#: sync runs, so writes are kept short.
CHUNK = 500


def run(conn: sqlite3.Connection) -> dict:
    """Fill empty label columns on open rows the LLM has not read."""
    rows = [dict(r) for r in conn.execute(
        "SELECT source, source_id, title, location, department FROM jobs "
        "WHERE closed_at IS NULL AND label_hash IS NULL "
        "AND (role_family IS NULL OR level IS NULL OR country IS NULL "
        "     OR work_mode IS NULL)")]
    report = {"read": len(rows), "role_family": 0, "level": 0, "country": 0,
              "work_mode": 0}
    for i in range(0, len(rows), CHUNK):
        for row in rows[i:i + CHUNK]:
            lab = labels_for(row)
            for k, v in lab.items():
                report[k] += v is not None
            # COALESCE: a column someone already filled keeps its value.
            conn.execute(
                """UPDATE jobs SET
                     role_family = COALESCE(role_family, ?),
                     level       = COALESCE(level, ?),
                     country     = COALESCE(country, ?),
                     work_mode   = COALESCE(work_mode, ?),
                     remote      = CASE WHEN work_mode IS NULL AND ? = 'remote'
                                        THEN 1 ELSE remote END
                   WHERE source = ? AND source_id = ? AND label_hash IS NULL""",
                (lab["role_family"], lab["level"], lab["country"],
                 lab["work_mode"], lab["work_mode"], row["source"],
                 row["source_id"]))
        conn.commit()
    return report
