#!/usr/bin/env python3
"""Per-agent token cost, read from the transcripts where it has always been.

Closes the gap ~/.claude/hooks/agent-usage-log.py documents in its own comment:
"the <usage> block shown to the main model is display-only and is NOT passed to
hooks, so tokens are unavailable here — known gap: per-agent token/cost needs a
separate source = fix #3". The separate source is on disk:
~/.claude/projects/**/agent-*.jsonl, one file per subagent run, 48,770 usage
records as of 2026-08-07. Grafted from agentacct (2026-08-07 teardown) — the
field path, not the tool.

Every record carries `attributionAgent` (the agent type), `message.model`, `cwd`
and `message.usage`, so no join against agent-usage.jsonl is needed at all.

Usage:
  subagent-cost.py [--weeks N] [--json]
"""
import json
import os
import pathlib
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

PROJECTS = pathlib.Path.home() / ".claude" / "projects"
OUT = pathlib.Path(__file__).resolve().parents[2] / "p5-loop" / "subagent-cost-latest.json"

# List prices per 1M tokens, for a NOTIONAL figure only. Anthropic models run on
# the Max subscription and GLM on the z.ai flat quota, so real marginal spend is
# $0 — the dollar column answers "what would this have cost on the open market",
# never "what Anton paid". Reporting it as spend would repeat the exact error we
# caught Echo's "1/3 the cost" making.
PRICES = {
    "claude-haiku":  {"in": 1.0,  "out": 5.0},
    "claude-sonnet": {"in": 3.0,  "out": 15.0},
    "claude-opus":   {"in": 15.0, "out": 75.0},
    "glm":           {"in": 0.6,  "out": 2.2},
}


def price_for(model):
    for prefix, p in PRICES.items():
        if model.startswith(prefix):
            return p
    return None


def scan(since):
    """One row per subagent usage record. Full scan — ~4.7k files, a few seconds,
    and a weekly job does not need incremental complexity."""
    rows = []
    for path in PROJECTS.rglob("agent-*.jsonl"):
        try:
            fh = path.open(encoding="utf-8")
        except OSError:
            continue
        with fh:
            for line in fh:
                if '"usage"' not in line:
                    continue
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                msg = o.get("message") or {}
                usage = msg.get("usage") if isinstance(msg, dict) else None
                if not isinstance(usage, dict):
                    continue
                ts = o.get("timestamp", "")[:10]
                if since and ts < since:
                    continue
                rows.append({
                    "date": ts,
                    "agent": o.get("attributionAgent") or "(unattributed)",
                    "model": msg.get("model") or "(unknown)",
                    "project": os.path.basename(o.get("cwd") or "") or "(no cwd)",
                    "effort": o.get("effort") or "",
                    "input": usage.get("input_tokens", 0),
                    "output": usage.get("output_tokens", 0),
                    "cache_creation": usage.get("cache_creation_input_tokens", 0),
                    "cache_read": usage.get("cache_read_input_tokens", 0),
                })
    return rows


def group(rows, key):
    out = defaultdict(lambda: {"records": 0, "input": 0, "output": 0,
                               "cache_creation": 0, "cache_read": 0})
    for r in rows:
        a = out[r[key]]
        a["records"] += 1
        for f in ("input", "output", "cache_creation", "cache_read"):
            a[f] += r[f]
    for a in out.values():
        # BILLED-NEW is the only honest spend figure. cache_read is the same cached
        # prefix re-sent every turn; summing it reached 3.9B against 977M actually
        # billed on the first run — a number 4x too large that looks catastrophic.
        a["billed_new"] = a["output"] + a["cache_creation"]
    return dict(sorted(out.items(), key=lambda kv: -kv[1]["billed_new"]))


def notional_usd(rows):
    total, priced, unpriced = 0.0, 0, 0
    for r in rows:
        p = price_for(r["model"])
        if p is None:
            unpriced += 1
            continue
        priced += 1
        total += ((r["input"] + r["cache_creation"] + r["cache_read"]) * p["in"]
                  + r["output"] * p["out"]) / 1_000_000
    return round(total, 2), priced, unpriced


def main():
    args = sys.argv[1:]
    weeks = next((int(args[i + 1]) for i, a in enumerate(args)
                  if a == "--weeks" and i + 1 < len(args)), None)
    since = ((datetime.now(timezone.utc) - timedelta(weeks=weeks)).date().isoformat()
             if weeks else None)

    rows = scan(since)
    if not rows:
        print(f"[subagent-cost] no records{' since ' + since if since else ''}")
        return 0

    by_agent, by_model, by_project = group(rows, "agent"), group(rows, "model"), group(rows, "project")
    tot = {f: sum(r[f] for r in rows) for f in ("input", "output", "cache_creation", "cache_read")}
    tot["billed_new"] = tot["output"] + tot["cache_creation"]
    usd, priced, unpriced = notional_usd(rows)

    payload = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window": since or "all time",
        "records": len(rows),
        "totals": tot,
        "notional_list_price_usd": usd,
        "notional_priced_records": priced,
        "notional_unpriced_records": unpriced,
        "by_agent": by_agent, "by_model": by_model, "by_project": by_project,
        "note": ("billed_new = output + cache_creation. cache_read is the cached prefix "
                 "re-sent every turn and is NEVER spend. notional_list_price_usd is what this "
                 "would cost at open-market list rates; actual marginal cost is $0 on the Max "
                 "subscription and the z.ai flat quota."),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    if "--json" in args:
        print(json.dumps(payload, indent=2))
        return 0

    def m(n):
        return f"{n/1e6:.1f}M" if n >= 1e6 else f"{n/1e3:.1f}k"

    print(f"Subagent cost — {payload['window']} · {len(rows):,} usage records")
    print(f"  billed-new {m(tot['billed_new'])}  (output {m(tot['output'])} + "
          f"cache-creation {m(tot['cache_creation'])})")
    print(f"  cache-read {m(tot['cache_read'])} — re-read context, not spend")
    print(f"  notional at list price: ${usd:,.2f}"
          + (f"  ({unpriced:,} records unpriced)" if unpriced else "")
          + "   [actual marginal: $0 — flat plans]")
    for title, d in (("by agent", by_agent), ("by model", by_model)):
        print(f"\n  {title}:")
        for k, v in list(d.items())[:8]:
            print(f"    {k[:34]:34} {m(v['billed_new']):>8}  ({v['records']:,} rec)")
    print(f"\nwritten: {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
