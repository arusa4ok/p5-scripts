#!/usr/bin/env python3
"""spec 011 T007+T008 — write `status:` onto field-less rows; rename the kanban headers.

Dry-run is the DEFAULT (011 FR-019). --apply writes. The pass APPENDS the field in place
(FR-003: no row moves; the only rewrite is the appended token) and collapses the five kanban
headers into the single legacy header (the only permitted structural edit, 011 T008). Rows in
a dropped kanban section still backfill with that section's token.

Section→token: five kanban sections by name; other headers by leading-emoji convention
(✅→done-unacted · ⏳→waiting · 🔧→in-work · 🔭→open · 📌→parked · else→open) — identical to
parse_ot_row()'s fallback, so classification is unchanged by the field's existence.

Usage:
  ot_backfill.py <ledger.md>                # dry-run report
  ot_backfill.py <ledger.md> --apply        # write in place (caller owns the .bak)
"""
import argparse
import re

KANBAN = (
    ("🔧 In work", "in-work"),
    ("⏳ Waiting on Anton", "waiting"),
    ("✅ Done, not yet acted-on", "done-unacted"),
    ("🔭 Next big threads", "open"),
    ("📌 Parked / not urgent", "parked"),
)
EMOJI = (
    ("✅", "done-unacted"),
    ("⏳", "waiting"),
    ("🔧", "in-work"),
    ("🔭", "open"),
    ("📌", "parked"),
)
LEGACY_HEADER = "## Legacy (pre-2026-08-07) — status lives in the field now"
_HAS_FIELD = re.compile(r"status: `[\w-]+`")
_ROW = re.compile(r"^- ")


def kanban_token(header: str):
    body = header.lstrip("# ").strip()
    for needle, st in KANBAN:
        if body == needle:          # exact: dated variants ("⏳ Waiting on Anton (2026-07-03)") are append-log history, not kanban
            return st
    return None


def emoji_token(header: str):
    body = header.lstrip("# ")
    for emoji, st in EMOJI:
        if body.startswith(emoji):
            return st
    return "open"


def transform(lines):
    out, stats = [], {"rows": 0, "already": 0, "backfilled": 0, "renamed": 0, "dropped": 0}
    current, legacy_emitted = None, False
    for line in lines:
        if line.startswith("## "):
            kt = kanban_token(line)
            if kt is not None:
                current = kt
                if not legacy_emitted:
                    out.append(LEGACY_HEADER)
                    legacy_emitted = True
                    stats["renamed"] += 1
                else:
                    stats["dropped"] += 1   # header line removed; rows keep its token
                continue
            current = emoji_token(line)
            out.append(line)
            continue
        if _ROW.match(line) and "(none)" not in line:
            stats["rows"] += 1
            if _HAS_FIELD.search(line):
                stats["already"] += 1
                out.append(line)
            elif current:
                out.append(line.rstrip() + f" · status: `{current}`")
                stats["backfilled"] += 1
            else:
                out.append(line)
            continue
        out.append(line)
    return out, stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ledger")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    path = args.ledger
    lines = open(path, encoding="utf-8").read().splitlines()
    out, s = transform(lines)
    report = (
        f"rows={s['rows']} already_field={s['already']} backfilled={s['backfilled']} "
        f"headers: 1 renamed + {s['dropped']} dropped"
    )
    if args.apply:
        open(path, "w", encoding="utf-8").write("\n".join(out) + "\n")
        print("APPLIED " + report)
    else:
        print("DRY-RUN " + report)
        shown = 0
        for orig, new in zip(lines, out):
            if orig != new and shown < 12:
                print("  - " + orig[:110])
                print("  + " + new[:110])
                shown += 1


if __name__ == "__main__":
    main()
