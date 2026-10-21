#!/usr/bin/env python3
"""tool-verdict — turn per-tool OUTCOME signals into a computed verdict, never an asserted one.

The disease (measured 2026-07-19): the stack logs THAT a tool ran everywhere; almost nowhere logs
WHETHER it helped. feature-tracking.md then fills the gap with prose ("✅ working") that no code
computes — ~62% overclaim on the OUTCOME layer. This script is the aggregator that replaces the
prose with arithmetic.

It reads the uniform outcome schema (p5-loop/tool-tracking/OUTCOME-CONVENTION.md) from every
`*-outcome.jsonl` under ~/.claude/logs, groups by `tool`, and computes:

    precision = helped / (helped + wasted)     # over classified fires only

Verdict per tool over a trailing window (default 7d):
    ✅ working      precision >= HELPED_HI
    ⚠️ ineffective  precision <  HELPED_LO  AND fires >= MIN_FIRES
    👀 watch        everything else (incl. too-few-fires — never condemn on thin data)

--items emits the ⚠️ tools as must-act items (same shape as must-act.py finder_items()), so the
weakest tool gets addressed one-per-cycle. --json emits the full per-tool table for the render.

Deterministic, $0, no LLM (Constitution I).
"""
from __future__ import annotations
import argparse, json, sys
from datetime import datetime, timezone
from pathlib import Path

LOGS = Path.home() / ".claude" / "logs"

# One token per class → helped / wasted. Anything not here is a fire but neither helped nor wasted
# (so an unknown token can never silently inflate precision). See OUTCOME-CONVENTION.md.
OUTCOME_MAP = {
    # skill
    "used": "helped", "redone": "wasted", "abandoned": "wasted",
    # hook (nudge/guard)
    "acted": "helped", "hard-skipped": "wasted", "ignored": "wasted",
    # A GUARD is not a nudge, and the difference showed up as a false verdict (2026-07-29).
    # `hard-skipped` means "the nudge fired and the skill declined" — a real false positive.
    # test-on-commit-guard was writing the same token when someone had switched it OFF, so
    # 39 deliberate bypasses were scored as 39 failures of the tool and it read "helped 19%
    # of 48". It never got to judge anything on those runs. A disabled tool is neither right
    # nor wrong, so `disabled` maps nowhere and drops out of the precision denominator.
    # `overruled` is the honest wasted signal for a guard: it blocked, and the block did not
    # stand. Measured over 30d: 49 blocks, 19 stood (39%); over the last 7d, 6 of 10 (60%).
    "disabled": None, "overruled": "wasted",
    # A guard's precision answers "when it INTERVENED, was it right?" — so neither of these
    # belongs in the ratio, and both belong in `fires` (2026-08-07). `passed` = it ran the
    # real suite and cleared the commit; that is the guard working, but it is not an
    # intervention, and scoring it `helped` would drown the overruled signal in green runs.
    # `fail-open` = timeout / runner error / non-1 exit: it judged NOTHING and let the commit
    # through. Not wasted (it cost no one anything) but a coverage hole, and until today it
    # was written nowhere at all — 186 of them, silent.
    "passed": None, "fail-open": None,
    # rule (before/after recurrence)
    "clean": "helped", "recurred": "wasted",
}

# Thresholds — ONE place, so a single edit re-tunes every class.
HELPED_HI = 0.60   # >= → ✅
HELPED_LO = 0.30   # <  → ⚠️ (only once MIN_FIRES is met)
MIN_FIRES = 5      # below this: 👀 watch, never a verdict on thin data


def _within_window(ts: str, window_days: int) -> bool:
    if window_days <= 0:
        return True
    try:
        t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return True   # undated rows count — a missing ts is a logging bug, not a reason to drop data
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - t).total_seconds() <= window_days * 86400


def collect(logs_dir: Path, window_days: int) -> list[dict]:
    """One row per tool with fires/helped/wasted/precision/verdict."""
    agg: dict[str, dict] = {}
    for f in sorted(logs_dir.glob("*-outcome.jsonl")):
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            tool = d.get("tool")
            outcome = d.get("outcome")
            if not tool or outcome is None:
                continue
            if not _within_window(d.get("ts", ""), window_days):
                continue
            a = agg.setdefault(tool, {"tool": tool, "tool_class": d.get("tool_class", "?"),
                                      "fires": 0, "helped": 0, "wasted": 0, "last": ""})
            a["fires"] += 1
            bucket = OUTCOME_MAP.get(outcome)
            if bucket == "helped":
                a["helped"] += 1
            elif bucket == "wasted":
                a["wasted"] += 1
            ts = d.get("ts", "")
            if ts > a["last"]:
                a["last"] = ts
    rows = []
    for a in agg.values():
        classified = a["helped"] + a["wasted"]
        precision = (a["helped"] / classified) if classified else None
        rows.append({**a, "classified": classified, "precision": precision,
                     "verdict": _verdict(precision, a["fires"])})
    rows.sort(key=lambda r: (r["precision"] if r["precision"] is not None else 1.0, -r["fires"]))
    return rows


def _verdict(precision, fires: int) -> str:
    if precision is None or fires < MIN_FIRES:
        return "👀"
    if precision >= HELPED_HI:
        return "✅"
    if precision < HELPED_LO:
        return "⚠️"
    return "👀"


def verdict_items(logs_dir: Path, window_days: int) -> list[dict]:
    """⚠️ tools as must-act items — mirrors must-act.py finder_items() shape exactly, so main()
    there dedupes/clears them the same way (applied marker: `toolverdict:<tool>`)."""
    items = []
    for r in collect(logs_dir, window_days):
        if r["verdict"] != "⚠️":
            continue
        pct = int(round((r["precision"] or 0) * 100))
        items.append({
            "id": f"toolverdict:{r['tool']}",
            "what": f"{r['tool']} ({r['tool_class']}): помог в {pct}% из {r['fires']} срабатываний "
                    f"— почини/подрежь/перепиши или /forwhat-ретайр",
            "path": "p5-loop/tool-tracking/OUTCOME-CONVENTION.md",
            "since": (r["last"] or "—")[:10],
            "severity": "idle",
        })
    return items


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--logs", default=str(LOGS), help="dir of *-outcome.jsonl")
    ap.add_argument("--window-days", type=int, default=7, help="0 = all time")
    ap.add_argument("--json", action="store_true", help="full per-tool table")
    ap.add_argument("--items", action="store_true", help="⚠️ tools as must-act items")
    args = ap.parse_args()
    logs_dir = Path(args.logs).expanduser()
    if not logs_dir.exists():
        print(json.dumps({"tools": []}) if args.json else "[]" if args.items
              else "tool-verdict: no logs dir")
        return
    if args.items:
        print(json.dumps(verdict_items(logs_dir, args.window_days)))
        return
    rows = collect(logs_dir, args.window_days)
    if args.json:
        print(json.dumps({"generated": datetime.now(timezone.utc).isoformat(),
                          "window_days": args.window_days, "tools": rows}, indent=2))
        return
    for r in rows:
        p = "—" if r["precision"] is None else f"{r['precision']:.0%}"
        print(f"{r['verdict']} {r['tool']:<28} {r['tool_class']:<6} "
              f"{p:>4} ({r['helped']}✓/{r['wasted']}✗ of {r['fires']})")


if __name__ == "__main__":
    main()
