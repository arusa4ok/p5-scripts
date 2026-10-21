#!/usr/bin/env python3
"""Append one finder-agent finding to finder-findings.jsonl — the source must-act.py consumes.

Finder-agents (performance-engineer, incident-responder, …) are read-only RECOMMENDERS: they find
real problems, but their findings live only in the sub-agent's return text and vanish when that
context is discarded. The main loop calls this after a finder returns an actionable finding, so the
finding survives into the fix-one-per-cycle consumer loop instead of evaporating.

  finder-log.py --agent performance-engineer --project career-os --severity idle \\
      --what "jobs(geo_class,location) missing covering index — 7.77s of 8.10s in test X" \\
      --path "~/projects/career-os/..."

Severity: broken (a real breakage/outage) · dead (a stopped/absent thing) · idle (actionable but not
breaking — most perf findings). id is derived from agent+project+what so the same finding re-logged
across runs collapses to one item, and fixing it (id → must-act-applied.txt) clears it like any other.
"""
from __future__ import annotations
import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

LOG = Path.home() / ".claude" / "logs" / "finder-findings.jsonl"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", required=True)
    ap.add_argument("--project", default="")
    ap.add_argument("--severity", default="idle", choices=["broken", "dead", "idle"])
    ap.add_argument("--what", required=True)
    ap.add_argument("--path", default="—")
    a = ap.parse_args()
    slug = re.sub(r"[^a-z0-9]+", "-", f"{a.agent}-{a.project}-{a.what}".lower())[:56].strip("-")
    rec = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "id": f"finder:{slug}",
        "agent": a.agent,
        "project": a.project,
        "severity": a.severity,
        "what": a.what,
        "path": a.path,
    }
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"logged {rec['id']}")


if __name__ == "__main__":
    main()
