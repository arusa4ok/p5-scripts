#!/usr/bin/env python3
"""hook-health — dead-man's switch for instrumented hooks: EFFECT, not activity.

Why this exists (2026-09-16, Anton's callout): the gbrain-preflight banner was
silently dead for 17 days (2026-08-30 → 09-16, ugrep regex break) while every
weekly read its log as HEALTHY — because the loop counted FIRES (260/week,
"active") and never EFFECT (matched>=1 streaks). A hook that fires and produces
nothing is dead wearing a heartbeat. Activity metrics masked the failure; only
an effect metric could see it.

Rule per hook (the dead-man's switch):
  fires >= MIN_FIRES in the window AND zero fires with effect (matched>=1)
  => 🔴 DEAD-EFFECT, with the last live effect date from full history.
Plus a degrade check: error/timeout share of fires >= 20% => 🟡.
Silent when green (house style: a check that cannot fire is noise, and one that
always prints is a dashboard nobody reads).

Usage:
  python3 hook-health.py                 # human, last 7 days
  python3 hook-health.py --md            # markdown line for the weekly
  python3 hook-health.py --backtest 2026-08-31 2026-09-15   # prove on history
Yardstick: --backtest over the known-dead window MUST flag preflight (this is
the acceptance test, run in the commit that adds this script).
Kill-threshold: remove rather than tune if it never fires again by 2026-12-16.
"""
import datetime as dt
import json
import sys
from pathlib import Path

LOGS = Path.home() / ".claude/logs"
MIN_FIRES = 5          # enough fires in-window that "no effect" is signal, not quiet week
DEGRADE_PCT = 20       # error+timeout share of in-window fires

# log file -> (name, effect_field, degrade_tiers)
HOOKS = {
    "gbrain-preflight.jsonl": ("gbrain-preflight", ("tier", "semantic")),
    "gbrain-volunteer.jsonl": ("gbrain-volunteer", None),
}


def parse_ts(s):
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def rows(path):
    try:
        for line in open(path, errors="replace"):
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


def check(name, path, effect_tier, t0, t1):
    """Returns (findings, fires, effects, last_live_ts). Window is [t0, t1]."""
    fires = effects = errors = 0
    last_live = None
    for r in rows(path):
        ts = parse_ts(r.get("ts", ""))
        if ts is None:
            continue
        matched = int(r.get("matched") or 0)
        if matched > 0 and (last_live is None or ts > last_live):
            last_live = ts
        if not (t0 <= ts <= t1):
            continue
        fires += 1
        if matched > 0:
            effects += 1
        if effect_tier and r.get("tier") in ("error", "timeout"):
            errors += 1
    findings = []
    if fires >= MIN_FIRES and effects == 0:
        findings.append(
            f"🔴 {name}: {fires} fires in window, ZERO with effect — dead-man tripped. "
            f"Last live effect: {last_live.astimezone().strftime('%Y-%m-%d %H:%M') if last_live else 'NEVER'}")
    elif fires and errors * 100 >= fires * DEGRADE_PCT:
        findings.append(f"🟡 {name}: {errors}/{fires} fires error/timeout (≥{DEGRADE_PCT}%)")
    return findings, fires, effects, last_live


def main():
    args = sys.argv[1:]
    md = "--md" in args
    window = (7, dt.datetime.now(dt.timezone.utc))
    if "--backtest" in args:
        i = args.index("--backtest")
        t0 = dt.datetime.fromisoformat(args[i + 1]).replace(tzinfo=dt.timezone.utc)
        t1 = dt.datetime.fromisoformat(args[i + 2] + "T23:59:59").replace(tzinfo=dt.timezone.utc)
        window = (None, t1)
        t0w = t0
    else:
        t0w = window[1] - dt.timedelta(days=window[0])

    out = []
    for fname, (name, effect_tier) in HOOKS.items():
        f, fires, effects, _ = check(name, LOGS / fname, effect_tier, t0w, window[1])
        out.extend(f)
    if not out:
        print("hook-health: all green" + ("" if md else " (fires with effects present)"))
        return
    body = "\n".join(out)
    print(("**Hook health (dead-man's switch):**\n" if md else "") + body)
    # exit 0 always: a health check must never block the weekly render


if __name__ == "__main__":
    main()
