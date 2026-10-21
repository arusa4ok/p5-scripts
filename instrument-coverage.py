#!/usr/bin/env python3
"""Instrument-coverage: the measurement system measuring its own coverage.

Problem it solves (measured 2026-07-16, not assumed): measure-what-you-ship says
log -> yardstick -> verdict in the weekly. Steps 1-2 happen reliably; step 3 dies
silently, because "👀 pending" is a LEGAL value in feature-tracking.md — a feature
can be marked shipped while no consumer renders it. Result at the time of writing:
8 of 47 registered features had no consumer (oldest 16 days), and 7 of 54 log
streams were read by nobody. (The first draft of this check said 10 — it matched
the marker anywhere in the row, so it counted rows that merely *describe* it. The
number a broken instrument reports is a claim, not a fact.) The rule knows this
failure mode about itself
("rules-in-context fail silently without a feedback signal") and had no signal.

This is that signal: pending rows — feature-tracking.md rows still marked "👀 pending",
with age. Deterministic, $0, no LLM.

It also used to list "orphan log streams" (a log nobody reads). That check is GONE, on
purpose: signals.py now renders every unclaimed stream by default, so an invisible
stream cannot exist and the check could only ever print zero. A check that cannot fire
is not reassurance, it is noise — the visibility half of this problem is solved by
construction, and only the verdict half needs a watcher.

Renders ONLY when something is wrong (silent when green, like stack-cron), so the
weekly doesn't carry a table nobody reads.

Yardstick: pending rows trending DOWN week over week (8 on 2026-07-16, 2 of them
older than a fortnight).
Kill-threshold: if by 2026-08-16 the count has not fallen, the mirror does not change
behaviour — remove it, do not tune it.
"""
import datetime
import pathlib
import re
import sys

P5 = pathlib.Path(__file__).parent
TRACKING = P5.parent.parent / "p5-loop" / "feature-tracking.md"
SELF = pathlib.Path(__file__).name


def pending_rows(today):
    """feature-tracking rows registered with no consumer, oldest first."""
    if not TRACKING.exists():
        return []
    out = []
    for line in TRACKING.read_text(errors="ignore").splitlines():
        if not line.startswith("| **"):
            continue
        # Split on UNESCAPED pipes only. Several rows quote a log schema inside a cell
        # — {"action":"pass\|block\|override"} — and a plain split("|") chops there too,
        # shifting every later column. That silently read the wrong cell for 7 of 49 rows
        # and hid two real gaps (operating-loop-guard, test-on-commit-guard). Anton spotted
        # the mismatch from the outside: the statuses did not line up.
        cells = re.split(r"(?<!\\)\|", line)
        # Match the CONSUMER cell only, never the whole row: a row may legitimately
        # quote "👀 pending" while describing it, and a raw substring match then
        # flags that row as its own finding (this check did exactly that to itself
        # on first run, 2026-07-16 — same class of bug as block-no-verify blocking
        # its own documentation commit).
        # 👀 = no consumer yet · ⚠️ = renders but the number misleads (worse than absent:
        # a dashboard that lies is trusted). ⛔ = deliberately not wired WITH a stated reason,
        # which is a closed decision, not a gap.
        # Counting ⚠️ too is deliberate: on 2026-07-16 the 👀 count hit zero partly because
        # rows were re-labelled ⚠️/⛔, and a checker that only counts one marker can be
        # emptied by renaming rather than by fixing. Don't let the loophole re-form.
        # Match the marker the cell STARTS with — its status — never a marker anywhere
        # inside it: most cells also spell out a verdict SCALE ("✅ <10% / 👀 10-20% /
        # ⚠️ >20%"), and a substring match reads those as findings. This bit me three
        # times in one day (2026-07-16); position is the discriminator, not presence.
        if len(cells) < 4:
            continue
        status = cells[3].strip()
        if not status.startswith(("👀", "⚠️")):
            continue
        name = re.sub(r"\*\*", "", cells[1]).split("(")[0].strip()
        shipped = cells[-2].strip()
        age = ""
        m = re.search(r"\d{4}-\d{2}-\d{2}", shipped)
        if m:
            try:
                d = datetime.date.fromisoformat(m.group())
                age = (today - d).days
            except ValueError:
                pass
        out.append((age if age != "" else -1, name[:44]))
    out.sort(key=lambda r: -(r[0] or 0))
    return out


def main():
    today = datetime.date.today()
    pending = pending_rows(today)
    if not pending:
        return 0  # green -> say nothing

    print("## 📏 Покрытие измерений")
    print()
    print("_Фича, которую никто не читает, не измеряется — она просто пишет в стол._")
    print()
    if pending:
        stale = [p for p in pending if p[0] >= 14]
        print(f"**Без потребителя: {len(pending)} фич**"
              + (f" · из них ≥2 недель: {len(stale)} 🔴" if stale else ""))
        for age, name in pending[:8]:
            mark = "🔴" if age >= 14 else "👀"
            days = f"{age} дн." if age >= 0 else "дата?"
            print(f"- {mark} `{name}` — висит {days}")
        if len(pending) > 8:
            print(f"- _…ещё {len(pending) - 8}_")
        print()
    return 0


if __name__ == "__main__":
    if "--md" not in sys.argv:
        print("usage: instrument-coverage.py --md", file=sys.stderr)
        sys.exit(2)
    sys.exit(main())
