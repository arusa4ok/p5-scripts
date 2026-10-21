#!/usr/bin/env python3
"""What would a hard line budget cost the memory index — and what would it save?

Graft from VictorTaelin/OptMem (teardown 2026-08-07): `cover(T, budget)` tiles an
append-only log with aligned power-of-two blocks, keeping a block whole only when
its size is small relative to its age. Detail therefore decays logarithmically:
recent entries stay verbatim, ancient ones collapse into one line, and the output
is at most `budget` lines however long the log grows. Nothing is deleted — an old
block is FOLDED, and drilling back in restores it.

Why this matters here, measured 2026-08-07: the two MEMORY.md indexes are 94 and
45 entries, 19,777 + 16,329 bytes ~= 9,000 tokens, loaded on every single session.
That is comparable to the entire always-loaded rules stack (11,586 tokens), and
unlike the rules it grows strictly linearly forever with no exit. I called this
YAGNI when recommending; the numbers say otherwise.

REPORT ONLY. This never rewrites a memory file — folding an index is destructive
and belongs to Anton, not to a script. It prints the plan so the cost of doing
nothing is visible.

Usage:
  memory-budget.py [MEMORY.md ...] [--budget N]
"""
import os
import re
import sys

DEFAULT_BUDGET = 40
DEFAULT_FILES = [
    os.path.expanduser("~/.claude/projects/-Users-rusakovanton/memory/MEMORY.md"),
    os.path.expanduser(
        "~/.claude/projects/-Users-rusakovanton-projects-cc-stack-2026/memory/MEMORY.md"),
]
ENTRY = re.compile(r"^\s*-\s+(.*)$")


def _cover(total, alpha):
    """OptMem's `_cover`, ported verbatim in behaviour: tile [0,total) with aligned
    power-of-two blocks; keep a block whole iff its size is at most `alpha` times
    its age. Bigger alpha = coarser = fewer lines."""
    root = 1
    while root < total:
        root *= 2
    out, stack = [], [(0, root)]
    while stack:
        lo, hi = stack.pop()
        if lo >= total:
            continue
        size = hi - lo
        if size > 1 and (hi > total or size > alpha * (total - lo)):
            mid = (lo + hi) // 2
            stack.append((mid, hi))
            stack.append((lo, mid))
        else:
            out.append((lo, hi))
    out.sort()
    return out


def cover(total, budget):
    """At most `budget` blocks, finest near `total`. Binary-search alpha, then
    spend any leftover budget splitting the newest block — block sizes jump in
    powers of two, so alpha alone undershoots."""
    if total <= 0:
        return []
    if total <= budget:
        return [(i, i + 1) for i in range(total)]
    lo, hi = 0.0, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if len(_cover(total, mid)) > budget:
            lo = mid
        else:
            hi = mid
    out = _cover(total, hi)
    while len(out) < budget:
        i = max((i for i, b in enumerate(out) if b[1] - b[0] > 1), default=None)
        if i is None:
            break
        a, b = out[i]
        mid = (a + b) // 2
        out[i:i + 1] = [(a, mid), (mid, b)]
    return out


def entries(path):
    """MEMORY.md is written NEWEST-FIRST, but the decay is defined over an
    append-only log where index 0 is oldest. Reverse on the way in, or the plan
    folds today's memories and keeps last spring's."""
    out = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                m = ENTRY.match(line.rstrip("\n"))
                if m and m.group(1).strip():
                    out.append(m.group(1))
    except OSError:
        return []
    return list(reversed(out))


DATE = re.compile(r"(20\d\d)-(\d\d)(?:-(\d\d))?|\((\d\d)-(\d\d)\)")


def age_ordered(rows):
    """cover() decays by POSITION and calls that age. That is only true if the
    index is actually ordered by age. Measured 2026-08-07: the cc-stack index is
    newest-first, but the global one is grouped by TOPIC — its dates run
    05, 05, 06, 06 … 05, 05, 05, 07. Planning a fold there would collapse
    arbitrary subjects while confidently reporting a token saving.

    Returns (fraction_non_decreasing, dated_count). Only entries carrying a date
    are compared; an undated entry is skipped rather than guessed at."""
    seen = []
    for r in rows:
        m = DATE.search(r)
        if not m:
            continue
        if m.group(1):
            seen.append((int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)))
        else:
            seen.append((2026, int(m.group(4)), int(m.group(5))))
    if len(seen) < 2:
        return 1.0, len(seen)
    ok = sum(1 for a, b in zip(seen, seen[1:]) if a <= b)
    return ok / (len(seen) - 1), len(seen)


def main():
    args = sys.argv[1:]
    budget = next((int(args[i + 1]) for i, a in enumerate(args)
                   if a == "--budget" and i + 1 < len(args)), DEFAULT_BUDGET)
    paths = [a for a in args if not a.startswith("--")
             and not a.isdigit()] or DEFAULT_FILES

    for path in paths:
        rows = entries(path)
        if not rows:
            print(f"{path}: no entries found")
            continue
        chars = sum(len(r) for r in rows)
        frac, dated = age_ordered(rows)
        if frac < 0.75:
            print(f"\n{os.path.basename(os.path.dirname(os.path.dirname(path)))}"
                  f"  ({len(rows)} entries, {chars:,} chars ~ {chars // 4:,} tokens)")
            print(f"  NOT age-ordered — only {frac:.0%} of {dated} dated entries run in order.")
            print("  This index is grouped by topic, so folding by position would collapse")
            print("  arbitrary subjects, not old ones. Re-sort it by date first, or fold by")
            print("  hand. No plan printed: a wrong plan with a token saving attached is worse")
            print("  than none.")
            continue
        blocks = cover(len(rows), budget)
        whole = [b for b in blocks if b[1] - b[0] == 1]
        folded = [b for b in blocks if b[1] - b[0] > 1]
        # A folded block costs one summary line. Assume it is as long as the median
        # entry — generous, since a summary of 8 entries is normally shorter.
        med = sorted(len(r) for r in rows)[len(rows) // 2]
        after = sum(len(rows[b[0]]) for b in whole) + len(folded) * med

        print(f"\n{os.path.basename(os.path.dirname(os.path.dirname(path)))}"
              f"  ({len(rows)} entries, {chars:,} chars ~ {chars // 4:,} tokens)")
        print(f"  budget {budget} lines -> {len(whole)} verbatim + {len(folded)} folded blocks")
        print(f"  ~{after:,} chars ~ {after // 4:,} tokens  "
              f"({100 - after * 100 // max(chars, 1)}% smaller)")
        if folded:
            print("  would fold (oldest first):")
            for lo, hi in folded:
                print(f"    [{lo:>3}..{hi:>3})  {hi - lo:>3} entries  e.g. {rows[lo][:66]}")
        else:
            print("  nothing folds — the index still fits its budget")
    print("\nreport only; no memory file was modified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
