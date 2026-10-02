"""Read-only probe (research/apply/browserbase_login_and_forms.md, 2026-10-01): what does each account-ATS ask for at its apply gate?

Opens each job in a Browserbase session (same region/settings as the bot),
presses only navigation buttons (Apply / Apply Now / Apply Manually), never
types, never presses sign-in, social or submit buttons. Records what the
gate page shows, then releases the session.
"""
import json, os, re, sys, time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

os.environ["CLOUD_KEEP_SECONDS"] = "0"            # no keep-alive for a probe
PROJECT = "/Users/cuoreji/Desktop/Claude Folders/Job Agent MVP"
OUT = Path(PROJECT) / "evals" / "out" / "gate_probe"    # gitignored
OUT.mkdir(parents=True, exist_ok=True)

JOBS = [
    ("workday", "Alation", "https://alation.wd503.myworkdayjobs.com/ExternalSite/job/IND-CHENNAI/Manager--General-Accounting_R10000793/apply"),
    ("workday", "Huron", "https://huron.wd1.myworkdayjobs.com/huroncareers/job/Bangalore-India---Outer-Ring-Road/Product-Support-Engineer_JR-0016622/apply"),
    ("workday", "Gap", "https://gapinc.wd1.myworkdayjobs.com/gapinc/job/Spoke---Hyderabad/Manager--Total-Rewards---HR-Shared-Services_R219464/apply"),
    ("workday", "Murex", "https://murex.wd3.myworkdayjobs.com/murexcareerpage1/job/Mumbai/Trading-Manager--Trading-Senior-Consultant_JR102165/apply"),
    ("workday", "ERM", "https://erm.wd3.myworkdayjobs.com/erm_experiencedprofessionals/job/New-Delhi-India/Manager---People-Services-Operations_R00031857/apply"),
    ("oracle", "Cyncly", "https://fa-ewdg-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CynclyJobs/job/2691"),
    ("oracle", "Legrand", "https://iadugs.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/865"),
    ("oracle", "Emerson", "https://hdjq.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/26005486"),
    ("oracle", "Lucy Electric", "https://hdga.fa.em3.oraclecloud.com/hcmUI/CandidateExperience/en/sites/Lucyelectric/job/26206"),
    ("oracle", "DTCC", "https://ebxr.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/214685"),
    ("phenom", "WEX", "https://careers.wexinc.com/us/en/job/R22992"),
    ("phenom", "Fiserv", "https://careers.fiserv.com/us/en/job/R-10399140"),
    ("phenom", "TJX", "https://jobs.tjx.com/global/en/job/REQ153251"),
    ("phenom", "Merck", "https://careers.merckgroup.com/global/en/job/303703"),
    ("successfactors", "Lupin", "https://careers.lupin.com/job/IRF-Delhi-Area-Manager-DL/1435215400/"),
    ("successfactors", "Ferrero", "https://jobs.ferrero.com/job/Pune-National-Key-Account-Manager-Omni-Channel-MH/1442607133/"),
    ("successfactors", "Dynatrace", "https://career41.sapsf.com/sfcareer/jobreqcareer?jobId=5997&company=dynatracel"),
    ("icims", "Linesight", "https://careers-linesight.icims.com/jobs/5603/assistant-cost-manager-csa-%28india-projects%29/job?in_iframe=1"),
    ("icims", "IDC", "https://idccareers-apac-idg.icims.com/jobs/7189/associate-production-specialist/job?in_iframe=1"),
]

# Only these move us forward; anything that signs in, uses a social account
# or could send is never pressed.
GO = re.compile(r"^\s*(apply|apply now|apply now\s*»|apply to (this )?job|apply for this (job|position|role)|apply online|apply manually|"
                r"apply for this job online|start (your )?application|i'?m interested)\s*$", re.I)
NEVER = re.compile(r"google|linkedin|indeed|facebook|apple|sign ?in|log ?in|submit|send|create account|"
                   r"register|resume|autofill|last application|seek|xing", re.I)

READ = r"""() => {
  const deepAll = (sel, root = document, out = []) => {
    for (const el of root.querySelectorAll('*')) {
      if (el.matches(sel)) out.push(el);
      if (el.shadowRoot) deepAll(sel, el.shadowRoot, out);
    }
    return out;
  };
  const shown = el => !!(el.offsetParent || el.getClientRects().length);
  const lab = el => {
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) return l.innerText.trim(); }
    const w = el.closest('label'); if (w) return w.innerText.trim();
    return (el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.getAttribute('data-automation-id') || el.name || el.id || '').trim();
  };
  const inputs = deepAll('input,select,textarea').filter(shown)
    .filter(e => !['hidden','submit','button'].includes((e.type||'').toLowerCase()))
    .map(e => ({type: (e.type || e.tagName).toLowerCase(), label: lab(e).slice(0, 80)}));
  const buttons = deepAll('button,a[role=button],[role=button],input[type=submit],a')
    .filter(shown).map(e => (e.innerText || e.value || e.getAttribute('aria-label') || '').trim().replace(/\s+/g,' '))
    .filter(t => t && t.length < 60);
  const frames = [...document.querySelectorAll('iframe')].map(f => f.src).filter(s => /captcha|hcaptcha|recaptcha|turnstile|challenges\.cloudflare/i.test(s));
  const text = (document.body ? document.body.innerText : '').replace(/\s+/g, ' ');
  return {url: location.href, title: document.title, inputs, buttons: [...new Set(buttons)].slice(0, 40),
          captcha_frames: frames, text: text.slice(0, 1500)};
}"""

HINTS = {
    "password_field": None,
    "create_account": re.compile(r"create (an )?account|create your account|register|sign up", re.I),
    "sign_in": re.compile(r"sign in|log ?in", re.I),
    "verify_email": re.compile(r"verif(y|ication)|confirm (your )?(email|identity)|one[- ]time|\bpin\b|\bcode\b", re.I),
    "social": re.compile(r"(sign in|apply|continue) with (google|linkedin|indeed|facebook|apple|seek)|linkedin|indeed", re.I),
    "autofill_resume": re.compile(r"autofill with resume|upload (your )?resume|parse|quick apply|easy apply|let'?s make this quick", re.I),
    "guest_apply": re.compile(r"apply (as )?(a )?guest|continue without|apply manually", re.I),
    "captcha_text": re.compile(r"captcha|i'?m not a robot|verify you are human|press (and|&) hold", re.I),
}


def summarize(snap):
    s = {"url": snap["url"], "title": snap["title"],
         "password_field": any(i["type"] == "password" for i in snap["inputs"]),
         "email_field": any(i["type"] == "email" or re.search(r"e-?mail", i["label"], re.I) for i in snap["inputs"]),
         "n_inputs": len(snap["inputs"]), "inputs": snap["inputs"][:15],
         "buttons": snap["buttons"][:25], "captcha_frames": snap["captcha_frames"]}
    blob = snap["text"] + " " + " ".join(snap["buttons"])
    for k, rx in HINTS.items():
        if rx is not None:
            m = rx.search(blob)
            s[k] = m.group(0) if m else None
    return s


def probe(job):
    ats, company, url = job
    sys.path.insert(0, PROJECT)
    os.chdir(PROJECT)
    from apply.cloud import Session
    from playwright.sync_api import sync_playwright
    rec = {"ats": ats, "company": company, "start": url, "steps": [], "error": None}
    t0 = time.time()
    sess = Session(app_id=0, url=url)
    rec["session"] = sess.id
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(sess.connect_url)
            ctx = browser.contexts[0]
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(12000)          # Workday pages render slowly
            for step in range(4):
                snap = page.evaluate(READ)
                for f in page.frames[1:]:              # iCIMS puts the job in an iframe
                    try:
                        sub = f.evaluate(READ)
                    except Exception:
                        continue
                    snap["inputs"] += sub["inputs"]; snap["buttons"] += sub["buttons"]; snap["text"] += " " + sub["text"][:600]
                rec["steps"].append(summarize(snap))
                if any(i["type"] == "password" for i in snap["inputs"]):
                    break
                # Oracle's first apply page = email + terms; stop there, never type.
                if rec["steps"][-1]["email_field"] and step > 0:
                    break
                target = None
                for loc in [f.get_by_role(r) for f in page.frames for r in ("button", "link")]:
                    for i in range(min(loc.count(), 80)):
                        el = loc.nth(i)
                        try:
                            t = (el.inner_text(timeout=500) or "").strip()
                        except Exception:
                            continue
                        if GO.match(t) and not NEVER.search(t) and el.is_visible():
                            target = (el, t)
                            break
                    if target:
                        break
                if not target:
                    break
                rec["steps"][-1]["clicked"] = target[1]
                pages_before = len(ctx.pages)
                target[0].click(timeout=8000, force=True)
                page.wait_for_timeout(6000)
                if len(ctx.pages) > pages_before:      # Apply opened a new tab
                    page = ctx.pages[-1]
                    page.wait_for_load_state("domcontentloaded", timeout=30000)
                    page.wait_for_timeout(4000)
            page.screenshot(path=str(OUT / f"{ats}_{company.replace(' ', '_')}.png"))
            browser.close()
    except Exception as exc:
        rec["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
    finally:
        sess.release()
    rec["seconds"] = round(time.time() - t0, 1)
    return rec


if __name__ == "__main__":
    with ProcessPoolExecutor(max_workers=5) as ex:
        results = list(ex.map(probe, JOBS))
    (OUT / "results.json").write_text(json.dumps(results, indent=1))
    for r in results:
        last = r["steps"][-1] if r["steps"] else {}
        print(f"{r['ats']:15} {r['company']:14} {r['seconds']:>5}s err={bool(r['error'])} "
              f"clicks={[s.get('clicked') for s in r['steps'] if s.get('clicked')]} "
              f"pw={last.get('password_field')} email={last.get('email_field')} "
              f"create={last.get('create_account')!r} verify={last.get('verify_email')!r} "
              f"social={last.get('social')!r} captcha={last.get('captcha_frames') or last.get('captcha_text')}")
