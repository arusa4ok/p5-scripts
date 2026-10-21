#!/usr/bin/env python3
"""spec-drift — flag projects whose specs went cold while the repo kept shipping.

Audit fix #3 (cc-stack 2026-07-05): connectpro and cc-stack silently abandoned their
speckit pipeline (specs untouched 5+ weeks) while the repos kept getting daily commits —
nobody noticed for over a month. This is report-only drift detection, surfaced weekly.

For each git project with a specs/ or .specify/ dir, compares the last commit that touched
specs against the last commit anywhere in the repo:
  - specs present but NEVER committed        -> "installed-unused" (like razalia-pmm)
  - repo active (<=21d) but specs cold (>28d) -> "drift" (the connectpro/cc-stack case)
Writes p5-loop/spec-drift.json (consumed by the weekly System Overview) and logs one line
per run to ~/.claude/logs/spec-drift.jsonl.

Kill-threshold: 3 consecutive weeks of all-false-positives (flagging projects on a
legitimate pause, not real drift) -> remove the check, don't just silence it.
"""
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "p5-loop" / "spec-drift.json"
LOG = Path.home() / ".claude" / "logs" / "spec-drift.jsonl"

ACTIVE_DAYS = 21   # repo counts as "active" if committed within this window
STALE_DAYS = 28    # specs count as "cold" if untouched longer than this

# Where projects live (globbed); each candidate must be a git repo with a spec dir.
SCAN_GLOBS = [Path.home() / "projects", Path.home() / "connectpro"]
SPEC_DIRS = ["specs", ".specify"]


def _git_epoch(repo, paths=None):
    cmd = ["git", "-C", str(repo), "log", "-1", "--format=%ct"]
    if paths:
        cmd += ["--"] + paths
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        out = r.stdout.strip()
        return int(out) if out else None
    except Exception:
        return None


def _candidate_repos():
    repos = []
    for g in SCAN_GLOBS:
        if not g.exists():
            continue
        cands = [g] if (g / ".git").exists() else [p for p in g.iterdir() if p.is_dir()]
        for p in cands:
            if (p / ".git").exists() and any((p / d).is_dir() for d in SPEC_DIRS):
                repos.append(p)
    return repos


def main(now=None):
    now = now or datetime.now(timezone.utc).timestamp()
    findings = []
    for repo in _candidate_repos():
        present = [d for d in SPEC_DIRS if (repo / d).is_dir()]
        repo_last = _git_epoch(repo)
        spec_last = _git_epoch(repo, present)
        name = repo.name
        if repo_last is None:
            continue
        if spec_last is None:
            findings.append({"project": name, "kind": "installed-unused",
                             "detail": f"{'/'.join(present)} present but never committed"})
            continue
        repo_age = (now - repo_last) / 86400
        spec_age = (now - spec_last) / 86400
        if repo_age <= ACTIVE_DAYS and spec_age > STALE_DAYS:
            findings.append({
                "project": name, "kind": "drift",
                "detail": f"repo active ({repo_age:.0f}d) but specs cold ({spec_age:.0f}d)",
                "repo_age_days": round(repo_age, 1), "spec_age_days": round(spec_age, 1),
            })

    result = {"ts": datetime.now(timezone.utc).isoformat(),
              "scanned": len(_candidate_repos()), "findings": findings}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as f:
            f.write(json.dumps({"ts": result["ts"], "scanned": result["scanned"],
                                "findings": len(findings)}, ensure_ascii=False) + "\n")
    except Exception:
        pass

    if "--print" in sys.argv:
        if not findings:
            print(f"spec-drift: {result['scanned']} projects scanned, no drift.")
        for f in findings:
            print(f"  [{f['kind']}] {f['project']}: {f['detail']}")
    return result


if __name__ == "__main__":
    main()
