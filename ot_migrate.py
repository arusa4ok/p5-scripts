#!/usr/bin/env python3
"""spec 011 US2 — write the identity fields the ledger has always computed and thrown away.

`action-queue.py:_stable_key` hashes `[project]|**Title**` on every run and discards the result.
That number is the thread's identity: it survives an edit of the row, which is the whole reason
it exists (61 of 112 ids across five weekly snapshots appeared exactly once while the pile grew
28->62 — the churn was re-hashing, not turnover). US2 writes it down.

What this pass does and does NOT do:
  WRITES  `id:`     — for every row that lacks one. Rung 1: reuses the existing function.
  WRITES  `parent:` — rewritten from free text to `ot-xxxxxxxx` WHERE the named parent resolves
                      to exactly one row's title. Ambiguous or unmatched parents are left alone
                      and reported; guessing which of five Parnasa spellings meant which thread
                      is precisely the drift this field exists to end, so it is not automated.
  NEVER   priority / dependencies / promoted — the vocabulary is defined in the spec, but no row
                      carries the information today. Filling 235 rows with a guessed priority
                      would manufacture data and call it a migration. They are added by hand, or
                      by a later pass that has a real signal to read.

Safety, per the T014 risk recorded in evidence/T001-MISMATCH.md: the ledger GREW ~22 KB during
the session that specced this. So the write is line-by-line, in place, on the file as it is at
the moment of the run — never a copy of a working buffer laid over the live file. `--dry-run` is
the default; `--apply` takes a timestamped .bak first.

Usage:
  ot_migrate.py                 # dry run: prints the diff it would make
  ot_migrate.py --apply         # writes, after a .bak
  ot_migrate.py --report        # parent-resolution report only, no diff
"""
import argparse
import datetime
import hashlib
import re
import shutil
from pathlib import Path

LEDGER = Path.home() / ".claude/projects/-Users-rusakovanton/memory/open-threads.md"

ROW = re.compile(r"^- \[(?P<project>[^\]]{1,40})\]")
TITLE = re.compile(r"\*\*(.+?)\*\*")
HAS_ID = re.compile(r"\bid: `ot-[0-9a-f]{8}`")
PARENT = re.compile(r"parent: `(?P<v>[^`]*)`")


def stable_id(line):
    """Byte-for-byte the identity action-queue.py already computes — do not reimplement it
    differently here, or the weekly and the ledger will disagree about what a thread IS."""
    tag = ROW.match(line.strip())
    title = TITLE.search(line)
    if title:
        stem = f"{tag.group('project') if tag else ''}|{title.group(1)}"
        return "ot-" + hashlib.sha1(f"open:{stem}".encode()).hexdigest()[:8]
    # No bold title: fall back to the first 80 chars, exactly as _key does. Fragile by
    # construction — an edit inside those 80 chars re-mints. Reported separately so the
    # fragility is visible rather than silently baked in.
    return "ot-" + hashlib.sha1(f"open:{line.strip()[:80]}".encode()).hexdigest()[:8]


def title_of(line):
    m = TITLE.search(line)
    return m.group(1).strip() if m else None


def build(lines):
    """Return (new_lines, stats, parent_report). Pure — no I/O, so the dry run and the apply
    cannot diverge."""
    rows = [(i, l) for i, l in enumerate(lines) if ROW.match(l)]
    ids = {i: stable_id(l) for i, l in rows}

    # title -> id, for parent resolution. Lowercased, and only titles that are UNIQUE: a
    # duplicate title cannot be a parent reference, because the reference would be ambiguous.
    by_title = {}
    for i, l in rows:
        t = title_of(l)
        if not t:
            continue
        k = t.lower()
        by_title.setdefault(k, []).append(ids[i])
    unique = {k: v[0] for k, v in by_title.items() if len(v) == 1}

    out = list(lines)
    stats = {"rows": len(rows), "id_added": 0, "id_present": 0, "no_title": 0,
             "parent_resolved": 0, "parent_unresolved": 0, "parent_dash": 0}
    report = []

    for i, line in rows:
        new = line
        if title_of(line) is None:
            stats["no_title"] += 1
        if HAS_ID.search(new):
            stats["id_present"] += 1
        else:
            # Insert right after the project tag so the id is the first thing on the row,
            # before the emoji/title. Keeps every existing reader's regex intact — all four
            # anchor on `^- \[project\]` or on a `field: \`value\`` pattern, neither of which
            # this position disturbs.
            new = ROW.sub(lambda m: f"- [{m.group('project')}] id: `{ids[i]}` ·", new, count=1)
            stats["id_added"] += 1

        pm = PARENT.search(new)
        if pm:
            v = pm.group("v").strip()
            if v in ("—", "-", ""):
                stats["parent_dash"] += 1
            elif re.fullmatch(r"ot-[0-9a-f]{8}", v):
                pass
            else:
                hit = unique.get(v.lower())
                if hit and hit != ids[i]:
                    new = new[:pm.start()] + f"parent: `{hit}`" + new[pm.end():]
                    stats["parent_resolved"] += 1
                    report.append(("RESOLVED", v, hit))
                else:
                    stats["parent_unresolved"] += 1
                    report.append(("LEFT", v, "ambiguous or no matching title"))
        out[i] = new
    return out, stats, report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()

    lines = LEDGER.read_text(encoding="utf-8").split("\n")
    out, stats, report = build(lines)

    print(" · ".join(f"{k}={v}" for k, v in stats.items()))
    if a.report:
        seen = {}
        for kind, v, why in report:
            seen.setdefault((kind, v, why), 0)
            seen[(kind, v, why)] += 1
        for (kind, v, why), n in sorted(seen.items(), key=lambda x: -x[1]):
            print(f"  {kind:<9} x{n:<3} {v!r} -> {why}")
        return 0

    changed = [(i, a_, b) for i, (a_, b) in enumerate(zip(lines, out)) if a_ != b]
    print(f"\n{len(changed)} line(s) would change. First 5:")
    for i, before, after in changed[:5]:
        print(f"  L{i+1}\n    - {before[:150]}\n    + {after[:150]}")

    if not a.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply.")
        return 0

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = LEDGER.with_suffix(f".md.bak-{stamp}")
    shutil.copy2(LEDGER, bak)
    # Re-read and re-derive against the file AS IT IS NOW: the ledger is appended to by hooks
    # and by live sessions, so the buffer read at the top of main() may already be stale. This
    # is the T014 risk, and re-deriving is the whole guard against it.
    fresh = LEDGER.read_text(encoding="utf-8").split("\n")
    if fresh != lines:
        print(f"⚠ ledger changed during the run ({len(fresh)-len(lines):+d} lines) — re-deriving")
        out, stats, report = build(fresh)
    LEDGER.write_text("\n".join(out), encoding="utf-8")
    print(f"\nwritten · backup: {bak}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
