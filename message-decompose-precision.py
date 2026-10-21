#!/usr/bin/env python3
"""message-decompose-precision — the US1 pilot readout (spec 004).

Replaces the misleading raw nudge-count ("1.7%" — a number that measured firing, not helping) with
the preflight hook's real PRECISION: of the nudges that led into the message-decomposer, how often was
it genuine multi-intent (`acted`) vs a false-positive on one ask (`hard-skipped`).

Joins message-decompose-preflight.jsonl (every nudge, with session_id) against
message-decompose-preflight-outcome.jsonl (the outcome lines the skill now logs). The gap between them
— nudges with no logged outcome — is the pre-instrumentation / un-scored tail, reported honestly rather
than hidden. Deterministic, $0.
"""
from __future__ import annotations
import json
from pathlib import Path

LOGS = Path.home() / ".claude" / "logs"
PREFLIGHT = LOGS / "message-decompose-preflight.jsonl"
OUTCOME = LOGS / "message-decompose-preflight-outcome.jsonl"


def _load(path):
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def main():
    nudges = _load(PREFLIGHT)
    outcomes = _load(OUTCOME)
    acted = sum(1 for r in outcomes if r.get("outcome") == "acted")
    skipped = sum(1 for r in outcomes if r.get("outcome") == "hard-skipped")
    scored = acted + skipped
    unlogged = len(nudges) - len(outcomes)

    print(f"message-decompose-preflight — precision readout")
    print(f"  nudges fired (all time):     {len(nudges)}")
    print(f"  outcomes logged:             {len(outcomes)}  (acted {acted} · hard-skipped {skipped})")
    print(f"  un-scored tail (no outcome): {max(unlogged, 0)}  ← pre-instrumentation / not engaged")
    MIN_FIRES = 5   # same thin-data gate as tool-verdict.py — never a verdict on <5 (this spec's own rule)
    if scored == 0:
        print(f"  PRECISION: — (no scored fires yet; verdict 👀 collecting, need ≥{MIN_FIRES})")
        print(f"  the loop is wired; this number becomes real as the skill logs outcomes going forward")
        return
    precision = acted / scored
    if scored < MIN_FIRES:
        verdict = "👀"   # thin data — report the %, but DO NOT crown it (the overclaim this spec fights)
    else:
        verdict = "✅" if precision >= 0.6 else ("⚠️" if precision < 0.3 else "👀")
    thin = f"  (thin — {scored}/{MIN_FIRES} fires, verdict held at 👀 until enough data)" if scored < MIN_FIRES else ""
    print(f"  PRECISION (acted / scored):  {precision:.0%}   verdict {verdict}{thin}")
    print(f"  vs the old raw metric: firing said nothing about worth; this says how often the nudge was right")


if __name__ == "__main__":
    main()
