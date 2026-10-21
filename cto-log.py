#!/usr/bin/env python3
"""Record one CTO tool-evaluation run to cto-agent.jsonl — the fire-log for actual CTO work.

The gap (found 2026-07-19): cto-agent.jsonl was written ONLY by owner-digest.sh (the weekly
heartbeat). The real CTO work — cto-cycle.js head-to-head-ing a candidate tool vs our incumbent,
or a repo-harvest triage — logged NOTHING, and cto-cycle.js can't self-log (Workflow scripts have
no filesystem access). So a real evaluation left zero instrumentation. The main loop calls this
after an evaluation so section_cto in the weekly can count evaluations distinct from the heartbeat.

  cto-log.py --candidate mcp-builder --incumbent "hand-rolled MCP" \\
      --verdict adopt --task "build a new MCP server" [--note "cherry-picked skill only"]

verdict: adopt · reject · defer. Written with agent:"cto-cycle" so section_cto separates it from
the agent:"owner-digest" heartbeat lines."""
from __future__ import annotations
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

LOG = Path.home() / ".claude" / "logs" / "cto-agent.jsonl"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidate", required=True)
    ap.add_argument("--incumbent", default="")
    ap.add_argument("--verdict", required=True, choices=["adopt", "reject", "defer"])
    ap.add_argument("--task", default="")
    ap.add_argument("--note", default="")
    a = ap.parse_args()
    rec = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "agent": "cto-cycle",
        "candidate": a.candidate,
        "incumbent": a.incumbent,
        "verdict": a.verdict,
        "task": a.task,
        "note": a.note,
    }
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"logged cto-cycle eval: {a.candidate} → {a.verdict}")


if __name__ == "__main__":
    main()
