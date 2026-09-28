"""Turn a scenario run into a transcript a person can read.

    python3 scenario-runs/report.py                  # newest run
    python3 scenario-runs/report.py runs/run-X.json

Writes `transcript-<stamp>.md` next to the run. Every conversation in full,
with what ran underneath each turn, so a claim in the analysis can be checked
against the thing it is a claim about.
"""

from __future__ import annotations

import glob
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def fmt_filter(f: dict | None) -> str:
    if not f:
        return "—"
    keep = {k: v for k, v in f.items() if v not in (None, "", [], {}) and k != "soft_criteria"}
    soft = f.get("soft_criteria")
    s = json.dumps(keep, ensure_ascii=False)
    if soft:
        s += f'   + judged loosely: "{soft}"'
    return s


def main() -> None:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(
        sorted(glob.glob(str(HERE / "runs" / "run-*.json")))[-1])
    if not src.is_absolute():
        src = HERE / src
    data = json.loads(src.read_text())
    out = [f"# Scenario transcripts — `{src.name}`", ""]

    # --- index ---
    out += ["| # | Scenario | Profile | Turns | Promise kept? |", "|---|---|---|---|---|"]
    for s in data:
        if s.get("crashed"):
            out.append(f"| {s['id']} | {s['name']} | — | — | **CRASHED** |")
            continue
        flags = []
        for t in s["turns"]:
            p = t.get("promise") or {}
            if p.get("city_misses"):
                flags.append(f"{len(p['city_misses'])} wrong city")
            if p.get("role_misses"):
                flags.append(f"{len(p['role_misses'])} wrong role")
        verdict = "ok" if not flags else "**" + ", ".join(flags) + "**"
        out.append(f"| {s['id']} | {s['name']} | {s['profile']} | "
                   f"{len(s['turns'])} | {verdict} |")
    out += ["", "---", ""]

    # --- full conversations ---
    for s in data:
        out += [f"## {s['id']} — {s['name']}", ""]
        if s.get("crashed"):
            out += [f"**CRASHED:** `{s['crashed']}`", "", "---", ""]
            continue
        out += [f"*Profile:* `{s['profile']}`  ·  *Looking for:* {s['why']}", ""]

        for t in s["turns"]:
            out += [f"### Turn {t['n']}", "", f"**User:**", "", f"> {t['user']}", ""]

            for st in t.get("steps", []):
                tool, inp = st.get("tool"), json.dumps(st.get("input"), ensure_ascii=False)
                out += [f"**→ ran `{tool}`** with `{inp}`  "
                        f"({'ok' if st.get('ok') else 'ERROR'}, {st.get('ms')}ms)", ""]
                fn = st.get("funnel") or {}
                if fn:
                    out += [f"- filter: `{fmt_filter(fn.get('filter'))}`",
                            f"- in words: {'; '.join(fn.get('looked_for') or []) or '—'}",
                            f"- funnel: {fn.get('open')} open → {fn.get('matched')} matched "
                            f"→ {fn.get('shown')} shown"]
                    if fn.get("relaxed"):
                        out.append(f"- **relaxed:** {'; '.join(fn['relaxed'])}")
                    if fn.get("dropped"):
                        out.append("- rejected by the ranker:")
                        out += [f"    - {d['title'][:58]} — *{d.get('why','')}*"
                                for d in fn["dropped"][:6]]
                    out.append("")
                res = (st.get("result") or "").strip()
                if res and not fn:
                    out += ["```", res[:700], "```", ""]

            if t.get("error"):
                out += [f"**ERROR:** `{t['error']}`", ""]

            out += ["**Agent:**", ""]
            out += ["> " + l if l.strip() else ">"
                    for l in (t["assistant"] or "(empty)").split("\n")]
            out += [""]

            if t.get("picks_after"):
                out += ["**What `apply to N` resolves to right now:**", ""]
                out += [f"{p['i']}. {p['title'][:60]} — {p['company']}, {p.get('city')}"
                        for p in t["picks_after"]]
                out += [""]

            p = t.get("promise") or {}
            if p.get("city_misses") or p.get("role_misses"):
                out += ["**Promise check:**", ""]
                if p.get("city_misses"):
                    out.append(f"- asked for **{', '.join(p['city_asked'])}**, "
                               f"these are not there:")
                    out += [f"    - {m}" for m in p["city_misses"]]
                if p.get("role_misses"):
                    out.append(f"- asked for **{', '.join(p['role_asked'])}**, "
                               f"these titles do not say it:")
                    out += [f"    - {m}" for m in p["role_misses"]]
                out += [""]

            out += [f"*{t['seconds']}s · {t.get('model_calls')} model calls*", ""]

        out += ["---", ""]

    dest = HERE / f"transcript-{src.stem.replace('run-','')}.md"
    dest.write_text("\n".join(out))
    print(f"{len(data)} scenarios -> {dest}")


if __name__ == "__main__":
    main()
