#!/usr/bin/env python3
"""queue-from-must-act — the wire between what the stack FINDS and what it FIXES.

The gap this closes, measured 2026-08-14: the night-desk orchestrator fired 43 times
between 2026-07-04 and 2026-08-13, and **42 of those runs had queue_chars: 0**. It woke
at 02:00, read an empty QUEUE.md, and went back to sleep. Meanwhile must-act.json held
13 open findings. Both halves worked; nothing connected them.

So this is a wire, not a system: read must-act, pick what is safe to hand an unattended
worker, append it to QUEUE.md. night-desk-launch.sh calls it as step 0, which means the
wire runs at exactly the moment the consumer runs -- no second cron, no clock race.

WHAT IS ELIGIBLE, and why the filter is narrow on purpose:
  - stack-scoped only (~/.claude or cc-stack-2026). A finding about another project goes
    nowhere near an unattended worker; those stay for a session with Anton.
  - already-applied ids are dropped (must-act-applied.txt, same convention as everywhere).
  - an id is queued at most once per COOLDOWN_DAYS, so a finding the night could not fix
    gets retried later instead of being re-sent every single night.
  - MAX_TASKS is deliberately small. The bottleneck was never throughput -- it was that
    nothing arrived at all. Two a night is observable; five is a wall of PRs.

The worker never merges: it opens a DRAFT PR and a separate reviewer votes. That is the
night-desk contract, restated in every task so it survives decomposition.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MUST_ACT = ROOT / "scripts" / "p5" / "must-act.py"
MUST_ACT_JSON = ROOT / "p5-loop" / "must-act.json"
APPLIED = ROOT / "p5-loop" / "must-act-applied.txt"
QUEUE = ROOT / "p5-loop" / "omni-pilot" / "night-desk" / "QUEUE.md"
QUEUED = ROOT / "p5-loop" / "night-desk-queued.txt"   # id<TAB>YYYY-MM-DD, one per line

MAX_TASKS = 2
COOLDOWN_DAYS = 7
SEV_ORDER = {"broken": 0, "dead": 1, "idle": 2}

# A finding's `what` starts with "[project] " when it came from finder-log. No bracket
# means it is stack-level (a dead log stream, a drift warning) and is in scope by default.
STACK_PROJECTS = {"cc-stack", "cc-stack-2026"}

# Aggregates, not defects. `warn:finder` says "9 findings are piling up"; `advisor:` says
# "0 critical / 1 warn -- go read the log". Both are instructions to a thinking session,
# and neither names a place a worker could change. Caught on the first dry run, which
# picked `warn:finder` as one of two tasks -- a whole night spent on a queue-depth notice.
META_PREFIXES = ("warn:", "advisor:")


def applied_ids() -> set[str]:
    if not APPLIED.exists():
        return set()
    return {ln.split("\t")[0].strip()
            for ln in APPLIED.read_text(encoding="utf-8", errors="replace").splitlines()
            if ln.strip() and not ln.startswith("#")}


def queued_recently() -> set[str]:
    """Ids queued inside the cooldown. Older entries are kept as history, not as a block."""
    if not QUEUED.exists():
        return set()
    cutoff = date.today() - timedelta(days=COOLDOWN_DAYS)
    out = set()
    for ln in QUEUED.read_text(encoding="utf-8", errors="replace").splitlines():
        if not ln.strip() or ln.startswith("#"):
            continue
        parts = ln.split("\t")
        try:
            when = datetime.strptime(parts[1].strip(), "%Y-%m-%d").date()
        except (IndexError, ValueError):
            continue          # a malformed row blocks nothing; it is history, not a gate
        if when > cutoff:
            out.add(parts[0].strip())
    return out


def actionable_by_a_worker(item: dict) -> bool:
    if (item.get("id") or "").startswith(META_PREFIXES):
        return False
    what = (item.get("what") or "").strip()
    if what.startswith("["):
        project = what[1:what.index("]")] if "]" in what else ""
        return project in STACK_PROJECTS
    return True


def render(item: dict) -> str:
    return (f"- **{item['id']}** ({item['severity']}) — {item['what']}\n"
            f"  Where: `{item.get('path', '—')}`. Open since {item.get('since', '—')}.\n"
            f"  Stay inside `~/.claude` and `cc-stack-2026`; reversible changes only. Open a\n"
            f"  DRAFT PR and stop — do not merge, do not touch another project.\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print the plan, write nothing")
    ap.add_argument("--no-refresh", action="store_true", help="use must-act.json as-is")
    args = ap.parse_args()

    if not args.no_refresh:
        # $0 and read-only against its sources -- it recomputes the finding list from the
        # audit, the weekly verdicts and the finder log. Queuing a stale list would hand
        # the night work that was already done.
        #
        # The first version of this call discarded the result entirely -- no returncode
        # check, no timeout handling -- so the very failure the comment above promises to
        # prevent would have happened silently, and a TimeoutExpired would have crashed
        # step 0 of the whole night with a traceback. Caught by a polly review, 2026-08-14.
        # Both cases degrade the same way: keep going with the list we have, and SAY SO in
        # the receipt, because a fail-open nobody can see is the defect we keep paying for.
        stale_reason = None
        try:
            proc = subprocess.run([sys.executable, str(MUST_ACT)], capture_output=True, timeout=300)
            if proc.returncode != 0:
                stale_reason = f"must-act exited {proc.returncode}"
        except subprocess.TimeoutExpired:
            stale_reason = "must-act timed out after 300s"
    else:
        stale_reason = None

    if not MUST_ACT_JSON.exists():
        print(json.dumps({"queued": 0, "reason": "no must-act.json"}))
        return 0

    # One block per day, full stop. The cooldown alone is not enough: it stops the SAME
    # finding coming back, but a second invocation just picks the next two and appends
    # them, so N calls put 2N tasks in one night. Caught by the idempotency test on the
    # first try -- run 2 was supposed to be a no-op and queued two more.
    today = date.today().isoformat()
    marker = f"## From must-act ({today})"
    if QUEUE.exists() and marker in QUEUE.read_text(encoding="utf-8"):
        out = {"queued": 0, "reason": "already queued today", "dry_run": args.dry_run}
        if stale_reason:
            out["stale"] = stale_reason      # a refresh failure is news on every path
        print(json.dumps(out))
        return 0

    items = json.loads(MUST_ACT_JSON.read_text(encoding="utf-8")).get("items", [])
    skip = applied_ids() | queued_recently()
    eligible = [i for i in items if i.get("id") not in skip and actionable_by_a_worker(i)]
    eligible.sort(key=lambda i: SEV_ORDER.get(i.get("severity"), 9))
    picked = eligible[:MAX_TASKS]

    receipt = {"considered": len(items), "eligible": len(eligible),
               "queued": len(picked), "ids": [i["id"] for i in picked],
               "dry_run": args.dry_run}
    if stale_reason:
        # Goes in the receipt, not to stderr: the launcher logs stdout into
        # night-desk.jsonl and sends stderr to /dev/null, so stderr is the one place
        # this warning could not be found the morning after.
        receipt["stale"] = stale_reason

    if not picked or args.dry_run:
        print(json.dumps(receipt, ensure_ascii=False))
        return 0

    block = (f"\n{marker} — found by the stack, not by Anton\n\n"
             + "".join(render(i) for i in picked))
    with QUEUE.open("a", encoding="utf-8") as fh:
        fh.write(block)
    with QUEUED.open("a", encoding="utf-8") as fh:
        for i in picked:
            fh.write(f"{i['id']}\t{today}\n")

    print(json.dumps(receipt, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
