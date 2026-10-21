#!/usr/bin/env python3
"""P5 burn-metrics extractor — the headline metric of the cc-stack-2026 self-improvement loop.

Reads ~/.claude/logs/opus-blocks.jsonl (written by the P3-A/P3-C PreToolUse guards) and emits a
compact, append-ready markdown block. Pure deterministic parsing — ZERO LLM tokens.

Headline metric = write-production-code warn rate (Opus main loop writing prod code directly instead
of delegating to Sonnet). P3 proved this is the real Opus-burn driver, NOT stray Opus agent calls
(agent-dispatch is ~0 and tracked here only as a tripwire).

Usage:
  burn-metrics.py                 # current ISO week + trend vs previous week (default, for weekly retro)
  burn-metrics.py --all           # whole log, no windowing
  burn-metrics.py --since 14      # last N days
  burn-metrics.py --log <path>    # override log path
"""
from __future__ import annotations
import argparse, json, sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_LOG = Path.home() / ".claude" / "logs" / "opus-blocks.jsonl"
REGRESS_THRESHOLD = 0.20  # week-over-week write-warn-rate growth that flips VERDICT to REGRESS


def load(log: Path) -> list[dict]:
    if not log.exists():
        return []
    rows = []
    for line in log.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # tolerate a torn final line
    return rows


def ts(row: dict) -> datetime | None:
    raw = row.get("ts")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def project_of(file_path: str) -> str:
    if not file_path:
        return "(no path)"
    marker = "/projects/"
    if marker in file_path:
        return file_path.split(marker, 1)[1].split("/", 1)[0]
    # ~/.claude/... and other non-project writes
    home = str(Path.home())
    rel = file_path[len(home):] if file_path.startswith(home) else file_path
    return rel.strip("/").split("/", 1)[0] or "(root)"


def window_rows(rows: list[dict], start: datetime, end: datetime) -> list[dict]:
    out = []
    for r in rows:
        t = ts(r)
        if t and start <= t < end:
            out.append(r)
    return out


def warn_rate_per_day(rows: list[dict], span_days: float) -> float:
    n = sum(1 for r in rows if r.get("phase") == "write-production-code")
    return n / span_days if span_days > 0 else 0.0


def render(rows: list[dict], label: str, span_days: float, prev_rate: float | None) -> str:
    if not rows:
        return f"## {label}\n- no guard events in window\n"
    write_warns = [r for r in rows if r.get("phase") == "write-production-code"]
    agent_calls = [r for r in rows if r.get("phase") == "agent-dispatch"]
    false_blocks = [r for r in rows if r.get("verdict") in ("block",)]  # actual enforce-time blocks
    rate = warn_rate_per_day(rows, span_days)

    by_project = Counter(project_of(r.get("file_path", "")) for r in write_warns)
    top = by_project.most_common(5)

    times = sorted(t for t in (ts(r) for r in rows) if t)
    win = f"{times[0]:%Y-%m-%d %H:%M} → {times[-1]:%Y-%m-%d %H:%M}" if times else "—"

    if prev_rate is None:
        trend = "baseline (no prior week)"
        verdict = "BASELINE"
    elif prev_rate == 0:
        trend = f"prev week 0/day → {rate:.0f}/day"
        verdict = "REGRESS" if rate > 0 else "OK"
    else:
        delta = (rate - prev_rate) / prev_rate
        trend = f"{delta:+.0%} vs prev week ({prev_rate:.0f}/day → {rate:.0f}/day)"
        verdict = "REGRESS" if delta > REGRESS_THRESHOLD else "OK"

    lines = [
        f"## {label}",
        f"- window: {win}  ({span_days:.1f}d)",
        f"- **write-prod-code warns: {len(write_warns)}  ({rate:.0f}/day)**  [trend: {trend}]",
        f"- agent-dispatch opus calls: {len(agent_calls)}  (tripwire — expect 0)",
        f"- false-blocks (enforce): {len(false_blocks)}  (expect 0)",
    ]
    if top:
        lines.append("- top burn locations:")
        for proj, n in top:
            lines.append(f"  - {proj} — {n} warns")
    lines.append(f"- VERDICT: {verdict}")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="whole log, no windowing")
    ap.add_argument("--since", type=int, metavar="DAYS", help="last N days")
    ap.add_argument("--log", type=Path, default=DEFAULT_LOG)
    args = ap.parse_args()

    rows = load(args.log)
    if not rows:
        print(f"## P5 burn-metrics\n- log empty or missing: {args.log}\n")
        return 0

    now = datetime.now(timezone.utc)

    if args.all:
        times = sorted(t for t in (ts(r) for r in rows) if t)
        span = max((times[-1] - times[0]).total_seconds() / 86400, 1e-9) if times else 1
        print(render(rows, "P5 burn-metrics — ALL", span, None))
        return 0

    if args.since:
        start = now - timedelta(days=args.since)
        cur = window_rows(rows, start, now)
        print(render(cur, f"P5 burn-metrics — last {args.since}d", float(args.since), None))
        return 0

    # default: current ISO week (Mon 00:00 UTC) + trend vs previous week
    week_start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    prev_start = week_start - timedelta(days=7)
    cur = window_rows(rows, week_start, now)
    prev = window_rows(rows, prev_start, week_start)
    cur_span = max((now - week_start).total_seconds() / 86400, 1e-9)
    prev_rate = warn_rate_per_day(prev, 7.0) if prev else None
    iso_year, iso_week, _ = now.isocalendar()
    label = f"Week {iso_year}-W{iso_week:02d}  (generated {now:%Y-%m-%d %H:%M} UTC)"
    print(render(cur, label, cur_span, prev_rate))
    return 0


if __name__ == "__main__":
    sys.exit(main())
