#!/usr/bin/env python3
"""people-reconcile — nightly: scan fresh session transcripts for person names that
have no people/<slug> entity yet. Report-only (nightshift/Claude reviews; no
auto-create). Spec 012 living-brain FR-002a safety net."""
import json, glob, os, re, subprocess, sys, pathlib, datetime

HOME = os.path.expanduser("~")
GB = HOME + "/Library/Application Support/reflex/bun/bin/gbrain"
OUT = pathlib.Path(HOME + "/.claude/logs/people-reconcile.jsonl")
DAYS = 1
# known-name seed to cut noise: frequent non-person tokens
STOP_FIRST = {"Claude","Anton","Razalia","The","This","That","Your","Our","When","What",
        "Note","Notes","Step","Phase","Spec","Task","Test","Open","Run","Use","See","If",
        "Gemini","Google","Anthropic","OpenAI","Voyage","ZeroEntropy","Mac","Linux",
        "Docker","Git","Github","GitHub","Slack","Jira","Web","New","How","Why"}
STOP_SECOND = {"Report","Reports","Plan","Plans","Spec","Specs","Task","Tasks","Loop",
        "Code","Stack","Review","Summary","Digest","Audit","Overview","Guide","Rules",
        "Principles","Decision","Decisions","Cycle","Weekly","Nightly","Session","Sessions",
        "Update","Updates","Fix","Fixes","Check","Checks","Mode","Modes","Api","CLI"}
MIN_REPEAT = 20  # >=2: 14,607 hits; >=5: 3,672 — still phrases ("Product Marketing").
TOP_N = 20       # v0 ships a reviewable shortlist only; phrase noise needs an LLM
                # gate (v1, nightshift phase: "which of these are people?").

def existing_entities():
    cfg = json.loads(pathlib.Path(HOME + "/.gbrain/config.json").read_text())
    sql = "SELECT p.slug, p.title FROM pages p JOIN sources s ON s.id=p.source_id WHERE s.id='people'"
    r = subprocess.run(["psql", cfg["database_url"], "-tA", "-F", "|", "-c", sql],
                       capture_output=True, text=True, timeout=60)
    names = {}
    for line in r.stdout.splitlines():
        if "|" not in line: continue
        slug, title = line.split("|", 1)
        names[title.strip().lower()] = slug
    return names

def main():
    since = datetime.datetime.now() - datetime.timedelta(days=DAYS)
    cand = {}
    for f in glob.glob(HOME + "/.claude/projects/**/*.jsonl", recursive=True):
        if datetime.datetime.fromtimestamp(os.path.getmtime(f)) < since: continue
        try:
            for line in open(f, encoding="utf-8", errors="replace"):
                if '"role":"user"' not in line and '"type":"user"' not in line: continue
                for m in re.finditer(r"\b([A-Z][a-z]{2,})\s+([A-Z][a-z]{2,})\b", line):
                    full = (m.group(1) + " " + m.group(2))
                    if m.group(1) in STOP_FIRST or m.group(2) in STOP_SECOND: continue
                    cand[full] = cand.get(full, 0) + 1
        except Exception:
            pass
    known = existing_entities()
    unknown_full = {n: c for n, c in cand.items() if n.lower() not in known and c >= MIN_REPEAT}
    unknown = dict(sorted(unknown_full.items(), key=lambda x: -x[1])[:TOP_N])
    rec = {"ts": datetime.datetime.utcnow().isoformat() + "Z",
           "scanned_files": True, "unknown_repeated": unknown}
    OUT.parent.mkdir(exist_ok=True)
    with OUT.open("a") as fh: fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"people-reconcile: {len(unknown)} unknown repeated name(s) -> {OUT}")

if __name__ == "__main__":
    main()
