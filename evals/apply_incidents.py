"""Replay the three live runs of 2026-09-29 through the form eval's checks.
Never submits. Swiggy runs in Browserbase (its bot check blocks a local Chrome)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import evals.apply_forms as ev                     # noqa: E402

JOBS = [
    {"source": "lever", "company": "Paytm", "title": "Growth Management & Analytics",
     "apply_url": "https://jobs.lever.co/paytm/36be80ec-3701-4d0f-8048-7571e3bd3a56/apply"},
    {"source": "smartrecruiters", "company": "Swiggy", "title": "Senior Manager- Growth",
     "apply_url": "https://jobs.smartrecruiters.com/SWIGGY/6000000001434065-senior-manager-growth?oga=true"},
    {"source": "keka", "company": "Clickpost", "title": "Associate Product Manager",
     "apply_url": "https://clickpost.keka.com/careers/jobdetails/143429"},
]

if __name__ == "__main__":
    res = ev.run(JOBS, {"smartrecruiters"})
    out = ev.OUT / "apply_forms" / "incidents.json"
    out.write_text(json.dumps(res, indent=1))
    for r in res:
        print(r["company"], "|", r["verdict"], "| opened:", r.get("opened"), "| read", r.get("read"),
              "| submit", r.get("submit_button"), "| next", r.get("next_button"))
