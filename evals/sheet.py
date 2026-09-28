"""Turn evals/out/*.json into the rows of the "Job Agent: Launch Evals" sheet.

    python3 evals/sheet.py        # writes evals/out/sheet.json, one list of rows per tab

Plain words only, no em dashes: Sai reads this, not a programmer.
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"


def load(name):
    p = OUT / name
    return json.loads(p.read_text()) if p.exists() else {"rows": []}


def nodash(s: str) -> str:
    import re
    s = re.sub(r"\s*—\s*", ", ", str(s))
    return s.replace("–", "-")


def guard_note(x: str) -> str:
    """'Paytm bullet 11: reverted (invented name(s): ['Collaborated'])' ->
    "read 'Collaborated' as a made-up name" """
    import re
    words = re.findall(r"'([^']+)'", x)
    if "number" in x:
        return "saw new number(s) " + ", ".join(words)
    return "read " + ", ".join(f"'{w}'" for w in words) + " as a made-up name"


# --- Search ----------------------------------------------------------------

def should(c: dict) -> str:
    e = c.get("expect") or {}
    parts = []
    for g in c.get("expect_role") or []:
        parts.append("role: " + " or ".join(g))
    for k, needle in (c.get("expect_contains") or {}).items():
        if k == "title_keywords":
            parts.append(f"role: {needle}")
        elif k == "companies":
            parts.append(f"company: {needle}")
        elif k == "level":
            parts.append(f"level: {needle}")
    for k, v in (c.get("expect_like") or {}).items():
        parts.append(f"{k}: {v}")
    if "count" in e:
        parts.append(f"{e['count']} jobs")
    if "city" in e:
        parts.append(f"city: {e['city']}" if e["city"] else "any city (none named)")
    for k, opts in (c.get("expect_one_of") or {}).items():
        label = "posted within" if k == "posted_within_days" else k
        parts.append(f"{label}: " + " or ".join(map(str, opts))
                     + (" days" if k == "posted_within_days" else ""))
    if "posted_within_days" in e:
        v = e["posted_within_days"]
        parts.append(f"posted within {v} day{'s' if v != 1 else ''}"
                     if v else "no date limit")
    if "remote" in e:
        parts.append("remote only" if e["remote"] else "not remote-only")
    if "company_type" in e:
        parts.append(f"{e['company_type'].upper()} companies" if e["company_type"]
                     else "any company type")
    for k in ("seniority", "country", "industry"):
        if k in e and e[k] is None:
            parts.append(f"no {k} added")
    if "title_keywords" in e and e["title_keywords"] is None:
        parts.append("no role guessed")
    if c.get("expect_country"):
        parts.append(f"country: {c['expect_country']}")
    if c.get("expect_nonempty"):
        parts.append("keeps your preference (\"nothing CRM-heavy\")")
    parts.append("nothing else added")
    return "; ".join(parts)


def search_rows():
    f = load("filters.json")
    chat = load("chat.json")
    rows = [["#", "What the user typed", "What the bot should understand",
             "What it understood", "Pass?", "Why it failed (plain words)",
             "Where the sentence came from"]]
    n = 0
    for r in f["rows"]:
        n += 1
        tries = r.get("tries", 1)
        ok = r.get("passed", tries) == tries
        passed = "Pass" if ok else (f"Fail ({r['passed']} of {tries} tries passed)"
                                    if tries > 1 else "Fail")
        why = "; ".join(p[1] for p in r.get("problems") or []) if not ok else ""
        if r.get("means") and not ok:
            why += f". What it costs you: {r['means']}"
        said = r["say"] + (f"   [bot knows: {r['profile']}]" if r.get("profile") else "")
        rows.append([str(n), said, should(r), r.get("understood", ""), passed,
                     nodash(why), r.get("source", "written (original 17)")])
    for r in chat["rows"]:
        n += 1
        ok = not r["problems"]
        if r["check"] == "no_city":
            got = " | ".join(f'searched "{x["query"]}"' + (
                f', city {x["filter"].get("city")}' if (x["filter"] or {}).get("city")
                else ", any city") for x in r["funnels"]) or "did not search"
        else:
            got = "Bot replied: " + r["replies"][-1][:220].replace("\n", " ")
        why = "; ".join(r["problems"])
        verdict = "Pass" if ok else "Fail"
        if r["id"] == "chat-blocked-not-applied" and ok:
            verdict = "Pass (partly)"
            why = ("Did not say 'already applied'. But it said nothing about "
                   "the CRED form that was filled and not sent. The test's "
                   "application row was written by the eval, so it may not "
                   "match a real run exactly.")
        rows.append([str(n), "WHOLE CHAT TURN: " + " / then: ".join(r["turns"]),
                     r["what"], nodash(got), verdict, nodash(why),
                     f"Known bug {r['bug']} from your Telegram sheet"])
    return rows, f, chat


# --- Rank ------------------------------------------------------------------

def rank_rows():
    d = load("rank.json")
    rows = [["#", "What the user asked", "Jobs shown (title, company, city)",
             "Any job that breaks the ask?", "Pass?", "Note"]]
    for i, r in enumerate(d["rows"], 1):
        shown = "\n".join(
            f"{j+1}. {p['title'].strip()}, {p['company']}, "
            f"{'Remote' if p['remote'] and not p['city'] else p['city'] or 'no city'}"
            + (f" (posted {p['posted_at']})" if p.get("posted_at") else "")
            for j, p in enumerate(r["picks"])) or "No jobs shown"
        bad = "\n".join(r["problems"]) or "No"
        note = (f"{r.get('matched', '?')} jobs matched before ranking, "
                f"{len(r['picks'])} shown. {r['seconds']} s, "
                f"{r['tokens']['total']:,} tokens.")
        if r.get("notes"):
            note += " Bot's own notes: " + " | ".join(r["notes"])[:300]
        rows.append([str(i), r["say"], nodash(shown), nodash(bad),
                     "Pass" if not r["problems"] else "Fail", nodash(note)])
    return rows, d


# --- Resume ----------------------------------------------------------------

JOB_SHORT = {"product": "Senior PM, Truecaller",
             "growth": "Manager DAU Growth, Meesho",
             "business development": "BD & Partnerships, CRED"}

SAI_PICKS: list[str] = []      # filled by pick_five


def pick_five(res) -> set[str]:
    """Five good-looking pairs for Sai to judge by hand: ATS pass, nothing
    made up, one page where expected, spread over resumes and jobs."""
    cands = []
    for r in res["rows"]:
        for t in r["tailors"]:
            if t.get("error") or t.get("ats") != "pass" or t.get("new_numbers"):
                continue
            score = (len(t.get("slop") or []), len(t.get("lost_in_output") or []))
            cands.append((score, r["slug"], t["family"], t["name"]))
    cands.sort()
    chosen, seen_r, seen_j = [], set(), {}
    for _, slug, fam, name in cands:
        if slug in seen_r or seen_j.get(fam, 0) >= 2:
            continue
        chosen.append(name)
        seen_r.add(slug)
        seen_j[fam] = seen_j.get(fam, 0) + 1
        if len(chosen) == 5:
            break
    return set(chosen)


#: Chosen by hand after the run: ATS pass, nothing made up, and a resume that
#: would plausibly be sent to that job (a sales CV to a DAU growth job would
#: not be). pick_five is the automatic fallback.
HAND_PICKS = {"sai_growth__growth", "sai_product__product",
              "synthetic_senior__product", "synthetic_sales__business_development",
              "synthetic_two_column__growth"}


def resume_rows():
    res = load("resume.json")
    names = {t["name"] for r in res["rows"] for t in r["tailors"]
             if t.get("ats") == "pass"}
    picks = HAND_PICKS & names if len(HAND_PICKS & names) == 5 else pick_five(res)
    rows = [["#", "Resume (real/synthetic + shape)", "Job", "Made anything up?",
             "ATS check", "Top-5 JD keywords in top half", "Lost sections",
             "Time (s)", "PDF path", "Would you send this? (Sai fills)", "Note"]]
    n = 0
    for r in res["rows"]:
        for t in r["tailors"]:
            n += 1
            if t.get("error"):
                rows.append([str(n), r["label"], JOB_SHORT[t["family"]], "", "",
                             "", "", str(t["seconds"]), "", "",
                             "Crashed: " + nodash(t["error"])])
                continue
            rev = t.get("reverted") or []
            made = "No" if not t.get("new_numbers") else \
                "YES: numbers not in your file: " + ", ".join(t["new_numbers"])
            if rev:
                made += (f". The guard undid {len(rev)} rewritten line"
                         f"{'s' if len(rev) > 1 else ''} because it "
                         + "; ".join(guard_note(x) for x in rev))
            ats = "Pass" if t["ats"] == "pass" else (
                "Fail: " + "; ".join(p.strip(" -").replace(
                    "missing from extracted text: ", "missing: ")
                    for p in t["ats"][6:].split("   - ")))
            top = t.get("top5") or {}
            found = {k: v for k, v in top.items() if v != "missing"}
            high = [k for k, v in found.items() if v == "top half"]
            low = [k for k, v in found.items() if v == "lower"]
            miss = [k for k, v in top.items() if v == "missing"]
            top_s = (f"{len(high)} of {len(found)} that are on the resume sit in "
                     f"the top half" if found else
                     "None of the job's top 5 are on this resume")
            if low:
                top_s += ". Too low: " + "; ".join(low)
            if miss:
                top_s += f". Not on the resume at all ({len(miss)}): " + "; ".join(miss)
            lost = []
            if r.get("lost_in_parse"):
                lost.append("lost when reading your file: " + ", ".join(r["lost_in_parse"]))
            if r.get("filed_as_job"):
                lost.append("filed as if it were a job: " + ", ".join(r["filed_as_job"]))
            only_out = [s for s in t.get("lost_in_output") or []
                        if s not in (r.get("lost_in_parse") or [])]
            if only_out:
                lost.append("dropped when building: " + ", ".join(only_out))
            lost.append(f"skills list: {t.get('skills_kept')} of your skills printed "
                        f"(the Skills line reads \"{t.get('skills_line', '')}\")")
            note = []
            if t.get("slop"):
                note.append(f"AI-sounding in {len(t['slop'])} of "
                            f"{t.get('model_lines')} rewritten lines: "
                            + " | ".join(s.split(':')[0] for s in t["slop"]))
            if t.get("pages", 1) > 1 and r["slug"] != "synthetic_senior":
                note.append(f"runs to {t['pages']} pages")
            if t["ats"] != "pass":
                note.append("PDF kept anyway (marked GATE_FAILED) so you can see it")
            note.append(f"{t['tokens']['total']:,} tokens")
            rows.append([str(n), r["label"], JOB_SHORT[t["family"]], nodash(made),
                         nodash(ats), nodash(top_s), nodash("; ".join(lost)),
                         str(t["seconds"]), t["pdf"],
                         "" if t["name"] in picks else "(not picked for hand check)",
                         nodash(". ".join(note))])
    return rows, res, picks


# --- Cost ------------------------------------------------------------------

def _avg(xs):
    xs = list(xs)
    return round(sum(xs) / len(xs)) if xs else 0


def cost_rows():
    f, rank, chat, res = (load("filters.json"), load("rank.json"),
                          load("chat.json"), load("resume.json"))
    tries = [t for r in f["rows"] for t in [r["tokens"]]]
    tailors = [t["tokens"] for r in res["rows"] for t in r["tailors"] if "tokens" in t]
    parses = [r["parse_tokens"] for r in res["rows"] if r.get("parse_tokens", {}).get("calls")]
    ranks = [r["tokens"] for r in rank["rows"] if r["tokens"]["calls"] > 1]
    chat_search = [r["tokens"] for r in chat["rows"] if r["check"] == "no_city"]
    chat_plain = [r["tokens"] for r in chat["rows"] if r["id"] == "chat-hi-attach"]

    def line(step, what, xs, n_label):
        return [step, what, str(len(xs)),
                f"{_avg(x['calls'] for x in xs)}",
                f"{_avg(x['input'] + x['cache_read'] for x in xs):,}",
                f"{_avg(x['output'] for x in xs):,}",
                f"{_avg(x['total'] for x in xs):,}", n_label]

    money = "Not known"
    rows = [["Step", "What it is", "Times measured", "Model calls each",
             "Tokens read (avg)", "Tokens written (avg)", "Total tokens (avg)",
             "Money"],
            line("Understand the ask", "Turns your sentence into search settings",
                 tries, money),
            line("Search + rank", "Settings, database, then the AI reads ~20 jobs "
                 "and picks the best (only searches that reached ranking)", ranks, money),
            line("Chat turn with a search", "One message through the chat agent that "
                 "searches (includes the search above)", chat_search, money),
            line("Chat turn, no search", 'A plain message like "Hi"', chat_plain, money),
            line("Read a resume (once)", "Turns an uploaded PDF into the record",
                 parses, money),
            line("Tailor one resume", "Rewrites for one job, then the checks "
                 "(no model) and the PDF", tailors, money)]
    avg = lambda xs: _avg(x["total"] for x in xs)  # noqa: E731
    week = (10 * avg(chat_search) + 20 * avg(chat_plain)
            + 5 * (avg(tailors) + avg(chat_plain)))
    rows += [[], ["Estimate per user per week", "", "", "", "", "",
                  f"{week:,}", money],
             ["Assumptions", "10 search messages, 20 other messages, 5 tailored "
              "resumes a week. Resume reading is once per user, not weekly "
              f"(~{avg(parses):,} tokens). Measured on fresh conversations: a long "
              "chat re-reads its history every turn, so real turns cost more."],
             ["Why no money column", "There is no GLM price anywhere in the code, and "
              "GLM's reply carries token counts only. Multiply the tokens by the "
              "price on your bigmodel.cn billing page to get rupees."],
             ["Not counted", "Picking the job's top 5 for the resume check (an eval "
              "step, not a product step): one small call per job, cached."]]
    return rows


if __name__ == "__main__":
    s, f, chat = search_rows()
    r, rank = rank_rows()
    rr, res, picks = resume_rows()
    c = cost_rows()
    (OUT / "sheet.json").write_text(json.dumps(
        {"Search": s, "Rank": r, "Resume": rr, "Cost": c, "picks": sorted(picks)},
        indent=1, ensure_ascii=False))
    print(len(s), len(r), len(rr), sorted(picks))
