#!/usr/bin/env python3
"""plan.py — keep p5-loop/PLAN.md honest against its four sources.

    python3 scripts/p5/plan.py            # report drift
    python3 scripts/p5/plan.py --quiet    # one line, for session-start

PLAN.md is hand-ordered on purpose — ranking ten items is judgment, and a scorer that
invents an order produces a list nobody trusts. What a script CAN do reliably is notice
when the list stops matching reality, which is the failure that killed the six backlogs
this file replaces: they did not go wrong, they went stale, and nothing said so.

So this checks two things and changes nothing:
  ARRIVED — a source raised something that is not in PLAN.md yet
  GONE    — a PLAN.md item's source no longer reports it (usually: it got fixed)

Sources:
  1. p5-loop/must-act.json                     — what the stack says is broken
  2. p5-loop/reports/<latest>.md               — "News synthesis" (external) + "Still open"
  3. p5-loop/reports/insights-*.md             — friction findings
  4. PLAN.md itself                            — the ordered truth
"""

import json
import glob
import os
import re
import sys
from datetime import date, datetime

P5 = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "p5-loop")
PLAN = os.path.join(P5, "PLAN.md")
MUST_ACT = os.path.join(P5, "must-act.json")

STALE_DAYS = 3   # a source older than this is not evidence, it is a memory — see below


def _age_days(path):
    try:
        return (datetime.now() - datetime.fromtimestamp(os.path.getmtime(path))).days
    except OSError:
        return None


def plan_keys():
    """Every `src:` tag written in PLAN.md, so an item can be traced back to its source."""
    try:
        with open(PLAN, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        return set(), ""
    return set(re.findall(r"`src:([^`]+)`", text)), text


def must_act_keys():
    try:
        with open(MUST_ACT, encoding="utf-8") as f:
            d = json.load(f)
    except (FileNotFoundError, ValueError):
        return {}, None
    age = _age_days(MUST_ACT)
    return {i["id"]: i.get("what", "")[:100] for i in d.get("items", [])}, age


def latest_weekly():
    reps = sorted(glob.glob(os.path.join(P5, "reports", "[0-9]*-W[0-9]*.md")))
    return reps[-1] if reps else None


def main():
    quiet = "--quiet" in sys.argv
    in_plan, plan_text = plan_keys()
    if not plan_text:
        print("PLAN.md missing — nothing to check.")
        return 1

    ma, ma_age = must_act_keys()

    # A stale producer is worse than a missing one: it answers confidently with last week's
    # world. On 2026-07-29 a 48h-old must-act reported "testgate: 0 fires, 70% overridden";
    # one rerun turned it into "34 fires, 20%". Three of its eight items evaporated. So the
    # age check comes BEFORE the contents are believed, not after.
    warnings = []
    if ma_age is not None and ma_age >= STALE_DAYS:
        warnings.append(f"must-act.json is {ma_age}d old — rerun scripts/p5/must-act.py "
                        f"before trusting anything below")

    # must-act ids embed their own numbers ("testgate-34-pass-32-..."), so they change on
    # every rerun — matching them exactly made this script report six phantom arrivals the
    # first time it ran. PLAN.md therefore carries a short STABLE slug and an id matches it
    # by containment. Fragile-by-design key, made robust at the comparison instead.
    slugs = {k.split("must-act:", 1)[1] for k in in_plan if k.startswith("must-act:")}
    arrived = [(k, v) for k, v in ma.items()
               if not any(s in k for s in slugs)]
    gone = [s for s in slugs if not any(s in k for k in ma)]

    weekly = latest_weekly()
    w_age = _age_days(weekly) if weekly else None
    if w_age is not None and w_age >= 8:
        warnings.append(f"weekly report is {w_age}d old — run scripts/p5/report.py")

    if quiet:
        bits = []
        if arrived:
            bits.append(f"{len(arrived)} new")
        if gone:
            bits.append(f"{len(gone)} likely done")
        if warnings:
            bits.append("stale source")
        print(f"📋 PLAN: {', '.join(bits)}" if bits else "📋 PLAN: in sync")
        return 0

    print(f"PLAN.md check — {date.today()}\n")
    for w in warnings:
        print(f"  ⚠️  {w}")
    if warnings:
        print()

    print(f"  tracked items with a source tag: {len(in_plan)}")
    print(f"  must-act items live now:         {len(ma)}\n")

    if arrived:
        print("ARRIVED — raised by a source, not in PLAN.md:")
        for k, v in arrived:
            print(f"  + [{k}] {v}")
        print()
    if gone:
        print("GONE — in PLAN.md, no longer reported by must-act (probably fixed):")
        for k in gone:
            print(f"  - {k}")
        print()
    if not arrived and not gone:
        print("  in sync — no drift between PLAN.md and its sources.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
