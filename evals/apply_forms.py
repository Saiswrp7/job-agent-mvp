"""Form eval: can the filler complete real application forms, site by site?

Opens real, live job forms from the job table, fills them with a made-up
applicant (example.com email, synthetic resume), and checks the page kept
every required value. **It never presses submit**: `submit()` is never called,
`ALLOW_SUBMIT` is removed from this process, and no approval is attached, so
the guard would refuse even if something called it.

What a form must do to pass:
  found     the page has a form, after pressing its own Apply button if needed
  no wall   no password field (sign-in) and no captcha on load
  filled    every required field reads back non-empty from the page
  resume    the resume uploaded, when the form has a file field
  sendable  a submit button is found and the form does not go on to a Next page

A site passes when at least 80% of its sampled forms pass. Passing sites are
the candidates for apply/sites.py; the report says why the rest fail.

    python3 evals/apply_forms.py                         # 5 forms per site, local Chrome
    python3 evals/apply_forms.py --per-site 3 --sites keka,lever
    python3 evals/apply_forms.py --cloud smartrecruiters  # those sites in Browserbase

Writes evals/out/apply_forms/<time>.json and evals/out/apply_forms_report.md.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.pop("ALLOW_SUBMIT", None)             # belt and braces: nothing is sent

from apply import pagecheck                       # noqa: E402
from apply.cloud import CloudBrowser, _match      # noqa: E402
from engine import db                             # noqa: E402

OUT = ROOT / "evals" / "out"
RESUME = ROOT / "evals" / "fixtures" / "resumes" / "synthetic_senior.pdf"
SITES = ["greenhouse", "lever", "smartrecruiters", "keka", "ashby", "workable",
         "recruitee", "teamtailor", "breezy"]
PASS_RATE = 0.8

#: The made-up applicant, matching the synthetic resume.
ME = [
    # Yes/no questions first: their wording often names a city or country.
    (r"authori[sz]ed|legally|eligible|right to work", "Yes"),
    (r"sponsor|visa|petition", "No"),
    (r"employed by .* before|worked (for|at) .* before|previously (worked|employed)", "No"),
    (r"relocat", "Yes"),
    (r"hear about|how did you (find|learn)|source", "LinkedIn"),
    (r"highest level of education|education level", "Bachelor"),
    (r"gender|hispanic|latino|veteran|disabilit|race|ethnic", "Decline"),
    (r"first name|given name", "Kavita"),
    (r"last name|surname|family name", "Iyer"),
    (r"middle name", ""),
    (r"full name|^name$|your name", "Kavita Iyer"),
    (r"e-?mail", "kavita.iyer@example.com"),
    (r"phone|mobile|contact number", "9000000003"),
    (r"linkedin", "https://www.linkedin.com/in/example-kavita"),
    (r"github|portfolio|website|url", "https://example.com"),
    (r"city|location|where.*(based|live)", "Bengaluru"),
    (r"country", "India"),
    (r"notice|available to join|join(ing)? in|start date", "30"),
    (r"current (ctc|salary|compensation)|current annual", "2500000"),
    (r"expected|desired|salary|pay|compensation|ctc|rate", "3000000"),
    (r"when .* (start|join)|start date|available from", "2026-11-02"),
    (r"years|experience", "11"),
    (r"current (company|employer|organi[sz]ation)|company name", "Example Payments"),
    (r"current (title|role|designation)|job title", "Director of Product"),
    (r"college|university|school|institute", "Example Institute of Technology"),
    (r"degree|qualification", "B.Tech"),
    (r"date of birth|dob|birth", "01/01/1990"),
    (r"cover letter|why|interest|tell us|about you|message|additional", "I am interested in this role. (Test entry.)"),
]


def value_for(label: str, ftype: str) -> str:
    low = label.lower()
    for pat, val in ME:
        if re.search(pat, low):
            if "birth" in low:
                return "1990-01-01"
            return val
    return {"textarea": "Test entry.", "number": "1", "date": "2026-10-15",
            "email": "kavita.iyer@example.com", "tel": "9000000003"}.get(ftype, "Test entry")


def sample(conn, sites: list[str], per_site: int, seed: int) -> list[dict]:
    """Live form jobs, one per company, spread across employers."""
    rnd = random.Random(seed)
    picked = []
    for site in sites:
        rows = [dict(r) for r in conn.execute(
            f"SELECT source, source_id, company, title, apply_url, url FROM jobs "
            f"WHERE {db.VISIBLE} AND apply_kind='form' AND source=?", (site,))]
        rnd.shuffle(rows)
        seen = set()
        for r in rows:
            if r["company"] in seen:
                continue
            seen.add(r["company"])
            picked.append(r)
            if len(seen) >= per_site:
                break
    return picked


def fill_all(b: CloudBrowser, fields: list[dict]) -> dict:
    """Fill every field a person would need to, the way the agent would."""
    notes, uploaded = [], None
    files = [f for f in fields if f["type"] == "file"]
    target = (next((f for f in files if re.search(r"resume|cv", f["label"], re.I)), None)
              or next((f for f in files if f["required"]), None)
              or (files[0] if files else None))
    if target:
        out = b.upload_file(target["name"], str(RESUME))
        uploaded = out.startswith("uploaded")
        if not uploaded:
            notes.append(f"{target['name']}: {out[:120]}")
    for f in fields:
        if f["type"] == "file":
            continue
        # Optional fields are left alone unless they look like identity: that
        # is what the agent does with a vault it has no value for.
        if not f["required"] and not re.search(r"name|mail|phone|linkedin", f["label"], re.I):
            continue
        if f["type"] in ("radio", "checkbox") and f.get("options"):
            want = value_for(f["label"], f["type"])
            opt = _match(f["options"], want) or f["options"][0]
            out = b.fill_field(f["name"], opt)
        elif f["type"] == "checkbox":
            out = b.fill_field(f["name"], "yes")
        elif f["type"] == "select" and f.get("options"):
            want = value_for(f["label"], f["type"])
            out = b.fill_field(f["name"], _match(f["options"], want) or f["options"][0])
        else:
            want = value_for(f["label"], f["type"])
            if not want:
                continue
            out = b.fill_field(f["name"], want)
            if out.startswith("ERROR") and b.offered:
                out = b.fill_field(f["name"], b.offered[0])
        if out.startswith("ERROR"):
            notes.append(f"{f['name']}: {out[:140]}")
    return {"uploaded": uploaded, "had_file": bool(files), "notes": notes}


def one(b: CloudBrowser, job: dict) -> dict:
    r = {k: job[k] for k in ("source", "company", "title")}
    r["url"] = job.get("apply_url") or job.get("url")
    t0 = time.time()
    try:
        b.page.wait_for_timeout(3000)
        r["opened"] = b.open_form()
        st = b.page_state()
        r.update(landed=st.get("url"), fields_on_page=st.get("fields", 0),
                 password=st.get("password"), captcha=st.get("captcha"))
        if re.search(r"\b404\b|page not found|not be found|no longer (available|exists)|"
                     r"no encontrada|job (has )?(closed|expired)", f"{st.get('title')} {(st.get('text') or '')[:1500]}", re.I) \
                and st.get("fields", 0) < 2:
            # The employer took the page down: a stale job, not a filler failure.
            return {**r, "verdict": "dead link (page not found)"}
        if st.get("password"):
            return {**r, "verdict": "sign-in wall"}
        if st.get("captcha") and st.get("fields", 0) < 2:
            return {**r, "verdict": "bot wall / captcha on load"}
        fields = b.read_form()
        req = [f for f in fields if f["required"]]
        r.update(read=len(fields), required=len(req),
                 labels=[f["name"] for f in fields][:40],
                 odd_labels=[f["name"] for f in fields if len(f["name"]) <= 3 or re.match(
                     r"(select|choose|please|back to|--)", f["name"], re.I)])
        if len(fields) < 2:
            return {**r, "verdict": "no form found"}
        # An autofill box (SmartRecruiters' "Easy Apply") goes first, as the
        # agent is told to: it fills much of the form from the resume.
        auto = next((f for f in fields if f["type"] == "file"
                     and re.search(r"easy apply|autofill|autocomplete", f["label"], re.I)), None)
        if auto:
            r["autofill"] = b.upload_file(auto["name"], str(RESUME))[:160]
            fields = b.read_form()
            req = [f for f in fields if f["required"]]
        filled = fill_all(b, fields)
        vals = b.current_values()
        empty = [f["name"] for f in req if f["type"] != "file" and not vals.get(f["name"])]
        st = b.page_state()
        labels = st.get("buttons", [])
        sub = pagecheck.submit_button(labels)
        nxt = pagecheck.next_button(labels)
        pages, any_req = 1, bool(req)
        while sub is None and nxt and not empty and pages < 4:
            out = b.next_page()
            if not out.startswith("on the next page"):
                r["next_page"] = out[:300]
                break
            pages += 1
            fields = b.read_form()
            req = [f for f in fields if f["required"]]
            any_req = any_req or bool(req)
            more = fill_all(b, fields)
            filled["notes"] += more["notes"]
            filled["had_file"] = filled["had_file"] or more["had_file"]
            filled["uploaded"] = filled["uploaded"] or more["uploaded"]
            vals = b.current_values()
            empty = [f["name"] for f in req if f["type"] != "file" and not vals.get(f["name"])]
            st = b.page_state()
            labels = st.get("buttons", [])
            sub = pagecheck.submit_button(labels)
            nxt = pagecheck.next_button(labels)
        r["pages"] = pages
        r.update(fill_errors=filled["notes"], required_empty=empty,
                 resume_uploaded=filled["uploaded"], had_file=filled["had_file"],
                 submit_button=labels[sub] if sub is not None else None, next_button=nxt,
                 captcha_after=st.get("captcha"))
        why = []
        if empty:
            why.append(f"{len(empty)} required field(s) would not keep a value")
        if filled["had_file"] and not filled["uploaded"]:
            why.append("resume did not upload")
        if sub is None and nxt:
            why.append(f"multi-page form ({nxt!r} button)")
        elif sub is None:
            why.append("no submit button found")
        if not any_req:
            why.append("no required fields detected (labels likely unread)")
        r["verdict"] = "pass" if not why else "; ".join(why)
        return r
    except Exception as exc:                          # noqa: BLE001
        return {**r, "verdict": f"crashed: {type(exc).__name__}: {str(exc)[:160]}"}
    finally:
        r["seconds"] = round(time.time() - t0, 1)


def run(jobs: list[dict], cloud_sites: set[str]) -> list[dict]:
    from playwright.sync_api import sync_playwright
    results = []
    with sync_playwright() as p:
        local = p.chromium.launch(headless=True)
        for i, job in enumerate(jobs, 1):
            url = job.get("apply_url") or job.get("url")
            print(f"[{i}/{len(jobs)}] {job['source']:15} {job['company'][:28]:28} ", end="", flush=True)
            b = None
            ctx = None
            sess = None
            try:
                if job["source"] in cloud_sites:
                    # One Playwright per process: the cloud session is reached
                    # through this one, not CloudBrowser's own.
                    from apply.cloud import Session
                    sess = Session(None)
                    remote = p.chromium.connect_over_cdp(sess.connect_url)
                    rctx = remote.contexts[0]
                    page = rctx.pages[0] if rctx.pages else rctx.new_page()
                    page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                    b = CloudBrowser(url, app_id=None, page=page)
                    b.session, b._browser = sess, remote
                else:
                    ctx = local.new_context(viewport={"width": 1280, "height": 900})
                    page = ctx.new_page()
                    page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                    b = CloudBrowser(url, app_id=None, page=page)
                b._guard_submit = _never                # this eval never sends
                r = one(b, job)
                r["where"] = "cloud" if job["source"] in cloud_sites else "local"
            except Exception as exc:                  # noqa: BLE001
                r = {"source": job["source"], "company": job["company"], "title": job["title"],
                     "url": url, "verdict": f"crashed opening: {type(exc).__name__}: {str(exc)[:160]}"}
            finally:
                try:
                    if sess is not None:
                        sess.release()
                    if ctx is not None:
                        ctx.close()
                except Exception:                     # noqa: BLE001
                    pass
            print(r["verdict"][:90])
            results.append(r)
        local.close()
    return results


def _never(fields):
    raise RuntimeError("the form eval never submits")


def report(results: list[dict], stamp: str) -> str:
    by = {}
    for r in results:
        by.setdefault(r["source"], []).append(r)
    lines = [f"# Form eval, {stamp}", "",
             "Real live forms, filled with a made-up applicant and a synthetic resume. "
             "Nothing was submitted: the submit step was never called.", "",
             f"A site passes at {int(PASS_RATE * 100)}% of its sampled forms or better.", "",
             "| Site | Forms | Pass | Rate | Site passes? | Main reason forms failed |",
             "|---|---|---|---|---|---|"]
    dead = [r for r in results if r["verdict"].startswith("dead link")]
    by = {k: [r for r in v if not r["verdict"].startswith("dead link")] for k, v in by.items()}
    by = {k: v for k, v in by.items() if v}
    for site, rs in sorted(by.items(), key=lambda kv: -sum(r["verdict"] == "pass" for r in kv[1]) / len(kv[1])):
        ok = sum(r["verdict"] == "pass" for r in rs)
        fails = {}
        for r in rs:
            if r["verdict"] != "pass":
                for part in r["verdict"].split("; "):
                    key = re.sub(r"\d+ required", "N required", re.sub(r"\(.*\)", "", part)).strip()
                    fails[key] = fails.get(key, 0) + 1
        main = ", ".join(f"{k} ({v})" for k, v in sorted(fails.items(), key=lambda kv: -kv[1])[:2]) or "none"
        rate = ok / len(rs)
        lines.append(f"| {site} | {len(rs)} | {ok} | {rate:.0%} | "
                     f"{'yes' if rate >= PASS_RATE else 'no'} | {main} |")
    if dead:
        lines += ["", f"Left out of the rates: {len(dead)} dead job link(s), where the employer's page "
                  f"no longer exists: " + ", ".join(f"{r['source']} {r['company']}" for r in dead) + "."]
    lines += ["", "## Every form", ""]
    for r in results:
        lines.append(f"### {r['source']}: {r['company']} ({r['title'][:60]})")
        lines.append(f"- Verdict: **{r['verdict']}**")
        lines.append(f"- Link: {r.get('url')}")
        if r.get("opened"):
            lines.append(f"- Opened by: {r['opened']} (landed on {r.get('landed')})")
        if "read" in r:
            lines.append(f"- Fields read: {r['read']}, required: {r['required']}, "
                         f"resume uploaded: {r.get('resume_uploaded')}, "
                         f"submit button: {r.get('submit_button')!r}, next button: {r.get('next_button')!r}")
        if r.get("required_empty"):
            lines.append(f"- Required fields left empty: {r['required_empty']}")
        if r.get("fill_errors"):
            lines.append(f"- Fill errors: {r['fill_errors'][:5]}")
        if r.get("odd_labels"):
            lines.append(f"- Odd field names: {r['odd_labels'][:8]}")
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sites", default=",".join(SITES))
    ap.add_argument("--per-site", type=int, default=5)
    ap.add_argument("--cloud", default="", help="sites to run in Browserbase")
    ap.add_argument("--seed", type=int, default=30)
    a = ap.parse_args()
    conn = db.connect()
    jobs = sample(conn, [s.strip() for s in a.sites.split(",") if s.strip()], a.per_site, a.seed)
    conn.close()
    results = run(jobs, {s.strip() for s in a.cloud.split(",") if s.strip()})
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    d = OUT / "apply_forms"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{datetime.now():%Y%m%d-%H%M}.json").write_text(json.dumps(results, indent=1))
    md = report(results, stamp)
    (OUT / "apply_forms_report.md").write_text(md)
    print("\n" + md.split("## Every form")[0])


if __name__ == "__main__":
    main()
