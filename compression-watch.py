#!/usr/bin/env python3
"""Spec 021 T040 / SC-005: did compressing the always-loaded layer cost behaviour?

For the N days before and after the promotion day D, prints
  - Read counts per moved rule (Read tool_use whose file_path names on-demand-rules/<file>)
  - correction candidates per session (~/.claude/logs/correction-candidates.jsonl, the
    correction-detector hook's log) — the R6 §4 step-5 production tail.
Sessions = top-level transcripts under ~/.claude/projects/*/ by mtime. A/B replay runs never land
there: their CLAUDE_CONFIG_DIR is ~/.claude-a? with a real projects/ dir (ab-profile.sh), which is
how AB_RUN sessions are excluded.

Usage: compression-watch.py --d 2026-10-22 [--days 14]
"""
import argparse
import json
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

HOME = Path.home()
STAGED = HOME / "projects/cc-stack-2026/.specify/specs/021-context-compression/evidence/staging/on-demand-rules"
LIVE_OD = HOME / ".claude/on-demand-rules"


def window(d, days, after):
    return (d, d + timedelta(days=days)) if after else (d - timedelta(days=days), d)


def scan(lo, hi, names):
    reads, sessions = Counter(), 0
    for f in (HOME / ".claude/projects").glob("*/*.jsonl"):
        if not lo <= date.fromtimestamp(f.stat().st_mtime) < hi:
            continue
        sessions += 1
        with open(f, errors="replace") as fh:
            for line in fh:
                if "on-demand-rules/" not in line or '"Read"' not in line:
                    continue
                try:
                    content = json.loads(line).get("message", {}).get("content", [])
                except ValueError:
                    continue
                for c in content if isinstance(content, list) else []:
                    if c.get("type") == "tool_use" and c.get("name") == "Read":
                        name = Path(c.get("input", {}).get("file_path", "")).name
                        if "on-demand-rules/" in c["input"].get("file_path", "") and name in names:
                            reads[name] += 1
    return reads, sessions


def corrections(lo, hi):
    n = 0
    for line in open(HOME / ".claude/logs/correction-candidates.jsonl", errors="replace"):
        try:
            ts = datetime.fromisoformat(json.loads(line)["ts"]).date()
        except (ValueError, KeyError):
            continue
        n += lo <= ts < hi
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--d", required=True, help="promotion day, YYYY-MM-DD")
    ap.add_argument("--days", type=int, default=14)
    a = ap.parse_args()
    d = date.fromisoformat(a.d)
    names = sorted({p.name for src in (STAGED, LIVE_OD) if src.exists() for p in src.glob("*.md")})
    rows = {}
    for label, after in (("before", False), ("after", True)):
        lo, hi = window(d, a.days, after)
        hi = min(hi, date.today() + timedelta(days=1))
        reads, sessions = scan(lo, hi, set(names))
        corr = corrections(lo, hi)
        rows[label] = reads
        print(f"{label:6} {lo}..{hi - timedelta(days=1)}: {sessions} sessions · {corr} correction candidates · "
              f"{corr / sessions if sessions else 0:.3f}/session · {sum(reads.values())} on-demand Reads")
    print("\nrule reads (before → after):")
    for n in names:
        print(f"  {n:38} {rows['before'][n]:4} → {rows['after'][n]:4}")


if __name__ == "__main__":
    main()
