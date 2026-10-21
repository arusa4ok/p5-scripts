#!/usr/bin/env python3
"""tool-outcome-log — append ONE outcome line for the tool-effectiveness loop (spec 004).

The whole loop turns on this single call: a tool, at the moment it finishes, records not THAT it ran
(already logged everywhere) but WHETHER it helped — one token from its class vocab. tool-verdict.py
reads these and computes ✅/👀/⚠️; must-act surfaces the ⚠️ ones. One reusable writer so every class
(skill/hook/rule) instruments with the same call instead of hand-rolling JSONL.

Usage:
  tool-outcome-log.py --tool message-decompose-preflight --class hook --outcome acted --session $SID
  tool-outcome-log.py --tool swarm-cycle --class skill --outcome used

Writes {ts, tool, tool_class, outcome[, session_id]} to ~/.claude/logs/<tool>-outcome.jsonl.
Fail-soft by design: never raises, never blocks the caller (Constitution V) — a lost outcome line is
a measurement gap, never a broken tool. Vocab is validated against OUTCOME-CONVENTION.md; an unknown
token is still written (tool-verdict.py counts it as a fire but neither helped nor wasted) but warns.
"""
from __future__ import annotations
import argparse, json, sys
from datetime import datetime, timezone
from pathlib import Path

LOGS = Path.home() / ".claude" / "logs"
VOCAB = {
    "skill": {"used", "redone", "abandoned"},
    # A guard is a hook, but it has outcomes a nudge does not: it can be switched off, be
    # overruled, clear a commit on a green suite, or judge nothing at all. These four have
    # been in tool-verdict's OUTCOME_MAP since 2026-07-29 / 2026-08-07 and were simply never
    # mirrored here, so 181 correctly-handled rows each printed "invisible to precision" —
    # a warning that was itself wrong, which is worse than no warning.
    "hook": {"acted", "hard-skipped", "ignored",
             "disabled", "overruled", "passed", "fail-open"},
    "rule": {"recurred", "clean"},
    "agent": None,  # agent uses finder-log.py; accept anything
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tool", required=True)
    ap.add_argument("--class", dest="tool_class", required=True,
                    choices=["skill", "hook", "rule", "agent"])
    ap.add_argument("--outcome", required=True)
    ap.add_argument("--session", default=None)
    ap.add_argument("--logs", default=str(LOGS))
    args = ap.parse_args()
    allowed = VOCAB.get(args.tool_class)
    if allowed is not None and args.outcome not in allowed:
        print(f"tool-outcome-log: WARN '{args.outcome}' not in {args.tool_class} vocab {sorted(allowed)} "
              f"— written but invisible to precision until added to OUTCOME_MAP", file=sys.stderr)
    row = {"ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
           "tool": args.tool, "tool_class": args.tool_class, "outcome": args.outcome}
    if args.session:
        row["session_id"] = args.session
    try:
        logs = Path(args.logs).expanduser()
        logs.mkdir(parents=True, exist_ok=True)
        with (logs / f"{args.tool}-outcome.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except Exception as e:            # never block the caller on a logging failure
        print(f"tool-outcome-log: soft-fail {e}", file=sys.stderr)
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
