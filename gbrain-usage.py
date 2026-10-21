#!/usr/bin/env python3
"""gbrain-usage — 30-day REAL tool_use recount across transcripts (spec 012 FR-005,
wired into the weekly digest by spec 013 T012).
Run on the weekly: zero-use surfaces must show growth or be consciously retired.
The first count (2026-08-30 baseline): 148 calls total; graph/code/chronicle/
context_pack/recall at ZERO. Note: preflight hook queries are INVISIBLE here (CLI,
not MCP) — its ~2.9k fires are counted in its own tuning log.

Modes: default = plain text; --md = weekly-digest markdown section."""
import json, glob, os, sys, collections
from datetime import datetime, timezone, timedelta
HOME = os.path.expanduser("~")

WATCH = ["get_links","traverse_graph","graph","code_def","code_refs","code_callers",
         "context_pack","recall","chronicle_day","chronicle_since","entity","find_contradictions"]

def count():
    since = datetime.now(timezone.utc) - timedelta(days=30)
    cnt = collections.Counter()
    for f in glob.glob(HOME + "/.claude/projects/**/*.jsonl", recursive=True):
        if datetime.fromtimestamp(os.path.getmtime(f), timezone.utc) < since: continue
        try:
            for line in open(f, encoding="utf-8", errors="replace"):
                if '"tool_use"' not in line or 'gbrain' not in line: continue
                try: d = json.loads(line)
                except Exception: continue
                m = d.get("message") or {}
                if m.get("role") != "assistant": continue
                for c in (m.get("content") or []):
                    if isinstance(c, dict) and c.get("type") == "tool_use" and c.get("name","").startswith("mcp__gbrain__"):
                        cnt[c["name"][len("mcp__gbrain__"):]] += 1
        except Exception: pass
    return cnt

cnt = count()
total = sum(cnt.values())
zero = [w for w in WATCH if cnt.get(w,0)==0]

if "--md" in sys.argv:
    lines = [f"## gbrain usage (30d, real MCP calls)", f"**{total}** total · watchlist surfaces at zero: **{len(zero)}**"]
    if zero:
        lines.append("Zero-use: " + ", ".join(f"`{w}`" for w in zero))
    else:
        lines.append("Zero-use: NONE — all watchlist surfaces alive")
    top = ", ".join(f"`{n}`×{c}" for n, c in cnt.most_common(6))
    lines.append(f"Top: {top}" if top else "Top: (no calls)")
    print("\n".join(lines))
else:
    print(f"gbrain MCP real calls (30d): {total}")
    for n, c in cnt.most_common(15): print(f"  {c:>5}  {n}")
    print("zero-use watchlist:", ", ".join(zero) if zero else "NONE — all surfaces alive")
