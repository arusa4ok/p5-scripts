#!/usr/bin/env python3
"""spec-handoff — the wire from "Anton said go" to the unattended spec build.

The missing link, found by looking for it on 2026-08-14: there was NO bridge from a
decision to a spec build. What existed was a PHRASE TRIGGER (`triggers.spec-kit.phrases`
in ~/.claude/hooks/skill-triggers.json) that routes a live session to the spec-kit
skills. That is a hint for Claude while Anton is at the keyboard, not a pipe — and
`log-decision.py`, which writes a resolved recommendation to four stores, does not
create a spec in any of them.

So this is the pipe, and its shape is set by the one thing Anton asked for
(2026-08-14): "мы идём обсуждаем и потом когда все понятно ты спрашиваешь — отдаю на
specify — я подтверждаю и все уезжает на автомат". The confirmation IS the trigger.
There is no watcher, no polling, no inference about whether a decision looks ready:
Claude asks, Anton says yes, Claude runs this. One gate, at the handoff, exactly where
he put it.

What it does: append a SPEC block to the nightshift QUEUE.md. nightshift already owns
everything after that -- worktree per task, cross-engine review, draft PR, kill switch,
run budget. Running here rather than on its own schedule means there is no second cron
and no clock race, same argument as queue-from-must-act.py.

Usage:
    spec-handoff.py --spec ~/projects/career-os/specs/033-linkedin-.../  \
                    [--note "one line of context from the conversation"] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
QUEUE = ROOT / "p5-loop" / "omni-pilot" / "nightshift" / "QUEUE.md"
LEDGER = ROOT / "p5-loop" / "spec-handoffs.txt"   # spec_path<TAB>YYYY-MM-DD, one per line


def already_handed_off(spec_dir: Path) -> str | None:
    """The date this spec was last handed off, or None.

    A second handoff of the same spec is almost always a mistake -- the first one is
    still in flight and the night would build it twice, in two worktrees, against two
    branches. Blocking is right; --force exists for the case where the first run failed
    and Anton wants it retried.
    """
    if not LEDGER.exists():
        return None
    for ln in LEDGER.read_text(encoding="utf-8", errors="replace").splitlines():
        if not ln.strip() or ln.startswith("#"):
            continue
        parts = ln.split("\t")
        if parts[0].strip() == str(spec_dir):
            return parts[1].strip() if len(parts) > 1 else "unknown date"
    return None


def render(spec_dir: Path, note: str | None) -> str:
    """The queue block. Absolute paths throughout, because the consumer runs at 02:00
    from launchd with a cwd nobody has checked, and a relative path there resolves
    somewhere nobody intended."""
    lines = [
        f"\n## SPEC BUILD — {spec_dir.name} (handed off {date.today().isoformat()})\n",
        f"\nSpec directory: `{spec_dir}`\n",
        f"Spec file: `{spec_dir / 'spec.md'}`\n",
    ]
    if note:
        lines.append(f"\nContext from the conversation: {note}\n")
    lines.append(
        "\nThis is a SPEC BUILD item, not a free-form brief — route it per rule 0e:\n"
        "dispatch `speckit_runner` (purpose=explore) to produce `tasks.md` beside that\n"
        "`spec.md`, then dispatch its atoms with the normal worker routing. Atoms marked\n"
        "`[ANTON]` are reported and never dispatched. Draft PR only; never merge.\n"
    )
    return "".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", required=True, help="path to the spec DIRECTORY (must contain spec.md)")
    ap.add_argument("--note", help="one line of context from the conversation that led to the go")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, write nothing")
    ap.add_argument("--force", action="store_true", help="hand off again even if already queued")
    args = ap.parse_args()

    spec_dir = Path(args.spec).expanduser().resolve()
    # Validate before writing, not after: a queue block pointing at a spec that isn't
    # there costs a whole night and reports as a worker failure, which is the wrong
    # place to discover a typo in a path.
    if not spec_dir.is_dir():
        print(json.dumps({"ok": False, "reason": f"not a directory: {spec_dir}"}))
        return 1
    if not (spec_dir / "spec.md").exists():
        print(json.dumps({"ok": False, "reason": f"no spec.md in {spec_dir} — needs brainstorm first"}))
        return 1

    prior = already_handed_off(spec_dir)
    if prior and not args.force:
        print(json.dumps({"ok": False, "reason": f"already handed off on {prior}; use --force to repeat"}))
        return 1

    block = render(spec_dir, args.note)
    receipt = {"ok": True, "spec": str(spec_dir), "dry_run": args.dry_run,
               "chars": len(block), "repeat": bool(prior)}
    if args.dry_run:
        print(json.dumps(receipt, ensure_ascii=False))
        print(block)
        return 0

    with QUEUE.open("a", encoding="utf-8") as fh:
        fh.write(block)
    with LEDGER.open("a", encoding="utf-8") as fh:
        fh.write(f"{spec_dir}\t{date.today().isoformat()}\n")

    print(json.dumps(receipt, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
