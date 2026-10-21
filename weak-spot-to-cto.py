#!/usr/bin/env python3
"""weak-spot-to-cto — the missing wire (spec 004 US4): internal weakness → outward market scan.

The two loops were decoupled: cto-cycle.js (the outward head-to-head) took its `candidate`/`incumbent`
from the CALLER's args, and nothing fed our own weak spots in. So the market scan couldn't target what
is actually weak in our stack — it evaluated tools in the abstract. This closes that: it reads the
must-act weak-spots (⚠️ computed tool-verdicts + idle/broken items that name a replaceable tool) and
emits a cto-cycle arg template per weak spot, with `incumbent` = the weak tool (the thing to beat) and
a task derived from the weakness. `candidate` is left null — that is the market's job to fill; this only
points the scan at a real weakness instead of a guess.

Manual-trigger by design (no cron — evaluating a tool is an event, not a schedule; per DEC event-driven).
Deterministic, $0, no LLM. Writes p5-loop/cto-inward/weak-spots-to-scan.json for the CTO cycle to pick up.
"""
from __future__ import annotations
import argparse, json, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MUST_ACT = ROOT / "p5-loop" / "must-act.json"
MUST_ACT_SCRIPT = ROOT / "scripts" / "p5" / "must-act.py"
OUT = ROOT / "p5-loop" / "cto-inward" / "weak-spots-to-scan.json"

# Which must-act ids name a REPLACEABLE tool (a thing a market alternative could beat). A stale
# producer or a one-off warning is NOT a tool to replace — only these prefixes are.
REPLACEABLE_PREFIXES = ("toolverdict:", "unrouted:", "kill:")


def _tool_from(item: dict) -> str | None:
    iid = item.get("id", "")
    for p in REPLACEABLE_PREFIXES:
        if iid.startswith(p):
            return iid[len(p):]
    return None


def load_must_act(refresh: bool) -> dict:
    if refresh or not MUST_ACT.exists():
        try:
            subprocess.run([sys.executable, str(MUST_ACT_SCRIPT)],
                           capture_output=True, timeout=60)
        except subprocess.SubprocessError:
            pass
    if not MUST_ACT.exists():
        return {"items": []}
    try:
        return json.loads(MUST_ACT.read_text())
    except ValueError:
        return {"items": []}


def build(refresh: bool) -> list[dict]:
    d = load_must_act(refresh)
    scans = []
    for it in d.get("items", []):
        tool = _tool_from(it)
        if not tool:
            continue
        scans.append({
            "incumbent": tool,                       # what to beat = our weak tool
            "candidate": None,                       # market fills this — the point of the scan
            "task": f"Find and head-to-head a replacement for `{tool}`. Deciding metric: "
                    f"it must fix the weakness — {it.get('what', tool)}",
            "groundTruth": "Establish deterministically, external to the tester; else score only "
                           "objective metrics (reachability, token count, wall-clock).",
            "sandbox": f"/tmp/cto-scan-{tool.replace('/', '-')}",
            "source_weakspot": it.get("id"),
            "severity": it.get("severity", "idle"),
            "since": it.get("since", "—"),
        })
    return scans


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="regenerate must-act.json first")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    scans = build(args.refresh)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(
        {"generated": datetime.now(timezone.utc).isoformat(), "scans": scans}, indent=2))
    if args.quiet:
        return
    if not scans:
        print("weak-spot-to-cto: no replaceable weak-spots in must-act — nothing to scan (good).")
        print("(A ⚠️ tool-verdict, an unrouted capability, or a KILL row would produce one.)")
        return
    print(f"weak-spot-to-cto: {len(scans)} weak-spot(s) ready to hand the CTO scan → {OUT}")
    top = scans[0]
    print(f"\nTop weak-spot: {top['source_weakspot']} (incumbent to beat: {top['incumbent']})")
    print("Run the head-to-head once the market scan names a candidate:")
    print(f"  Workflow(name='cto-cycle', args={{")
    print(f"    'candidate': '<tool the scan found>',")
    print(f"    'incumbent': {top['incumbent']!r},")
    print(f"    'task': {top['task']!r},")
    print(f"    'sandbox': {top['sandbox']!r} }})")


if __name__ == "__main__":
    main()
