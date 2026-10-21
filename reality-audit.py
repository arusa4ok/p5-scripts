#!/usr/bin/env python3
"""Reality-audit — is each DECLARED feature actually alive, or a dead row that reads ✅?

The disease this catches: feature-tracking.md is appended-to but never verified against reality.
A row can claim a log stream that is empty or whose file doesn't exist. This script is deterministic
($0, no LLM, no quota): it extracts every declared log stream from feature-tracking.md, then checks
each against the real ~/.claude/logs/ — exists? recent fires (14d/30d)? — and classifies:

  MISSING  — declared stream, but the log file does not exist            (broken or never wired)
  DEAD     — file exists, 0 entries in the last 30 days                  (stopped firing)
  RARE-OK  — 0 recent, but the feature is qualitative/rare-by-design     (expected, not a defect)
  ALIVE    — fired within the last 14 days

Also lists UNDECLARED logs (a stream firing with no feature-tracking row = untracked).
Run: python3 scripts/p5/reality-audit.py   (add --json for machine output feeding the consumer loop)
"""
from __future__ import annotations
import json, os, re, sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

HOME = Path(os.path.expanduser("~"))
ROOT = Path(__file__).resolve().parents[2]
FT = ROOT / "p5-loop" / "feature-tracking.md"
LOGS = HOME / ".claude" / "logs"
NOW = datetime.now(timezone.utc)

# Streams that are 0-by-design (fire rarely / on rare events) — a 0 here is NOT a defect.
# Sourced from feature-tracking.md kill-switch notes ("0 in 8 wks = fine", "rare by design", etc.).
RARE_OK = {
    "privacy-strip", "night-shift", "model-quota-radar", "constitution-miner",
    "research-runs", "workflow-recipes", "gbrain-glm-usage", "agent-rules-render",
    "skill-gaps", "decision-pairs", "constitution-surface",
    # routing-inspect fires only on a manual `routing show`; 0 is expected, not broken.
    # It was in must-act's copy of this set and missing here — the two lists were kept in sync
    # by hand and had already drifted by one entry. must-act.py now imports THIS set instead of
    # mirroring it, so a future addition cannot land in only one of them. (2026-07-29)
    "routing-inspect",
    # skill-errors is an ERROR log, not an activity log: skill-suggest.sh writes to it only
    # from _log_error, i.e. when jq fails to build an entry or the append to
    # skill-suggestions.jsonl fails. An absent file therefore means the hook has never once
    # failed — the best possible reading — yet it sat at the top of must-act as
    # "broken · MISSING" and was served to every session as the #1 thing to fix.
    # It is safe to mute here ONLY because the hook's liveness is measured by its paired
    # activity stream: skill-suggestions is ALIVE (291 KB, written the same morning this was
    # muted, 2026-08-17). If skill-suggest is ever unwired, THAT row goes DEAD and says so.
    # A silent error log is only trustworthy while something else proves the writer runs.
    "skill-errors",
    # pr-watcher is written BY the pr-watcher skill when it watches a PR, not by a hook, so
    # its row counts skill invocations and nothing else. One row exists, from the T016
    # dogfood on 2026-07-22. That is an unused skill, which is a routing question, not a
    # broken stream — and it was already acknowledged once in must-act-applied.txt without
    # the audit ever agreeing. Re-check when the background-agent draft-PR flow gets busy
    # enough to want watching; until then a 0 here is the truth, not a fault. (2026-08-17)
    "pr-watcher",
}

# Regex artifacts — doc placeholders / date-templated names that are NOT real declared streams.
# "outcome": bare token bled from the glob `*-outcome.jsonl` (regex can't start on `*`/`-`, so it
# extracts "outcome"); the real streams are `<feature>-outcome.jsonl`, each with its own full name.
NOT_A_STREAM = {"err", "global", "gbrain-dream-YYYY-MM", "outcome"}


def parse_ts(s):
    if not s:
        return None
    # Epoch seconds as int/float (some hooks log `"ts": int(time.time())`, e.g. frontmatter-audit) —
    # fromisoformat can't take a number, so a whole stream would read timestamp-less → false DEAD.
    if isinstance(s, (int, float)):
        try:
            return datetime.fromtimestamp(s, tz=timezone.utc)
        except Exception:
            return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def recent_counts(path: Path):
    """(total, n14, n30, last_date_str). Handles jsonl ('ts'/'timestamp'/...) and TSV .log (ts col0)."""
    total = n14 = n30 = 0
    last = None
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            total += 1
            ts = None
            if line[0] in "{[":
                try:
                    d = json.loads(line)
                    ts = parse_ts(d.get("ts") or d.get("timestamp") or d.get("at")
                                  or d.get("synthesized_at") or d.get("ingested_at"))
                except Exception:
                    ts = None
            else:
                ts = parse_ts(line.split("\t")[0])
            if ts is None:
                continue
            age = NOW - ts
            if age <= timedelta(days=14):
                n14 += 1
            if age <= timedelta(days=30):
                n30 += 1
            if last is None or ts > last:
                last = ts
    except Exception:
        pass
    return total, n14, n30, (last.strftime("%Y-%m-%d") if last else "—")


def declared_streams():
    """Every `<name>.jsonl` / `<name>.log` mentioned in feature-tracking.md (regex, no table parsing)."""
    txt = FT.read_text(encoding="utf-8", errors="replace")
    names = set(re.findall(r"([A-Za-z0-9][\w-]*)\.(?:jsonl|log)\b", txt))
    return sorted(names - NOT_A_STREAM)


def main():
    as_json = "--json" in sys.argv
    declared = declared_streams()
    existing = {p.stem for p in LOGS.glob("*.jsonl")} | {p.stem for p in LOGS.glob("*.log")}
    rows = []
    for name in declared:
        p = LOGS / f"{name}.jsonl"
        if not p.exists():
            p = LOGS / f"{name}.log"
        if not p.exists():
            # A rare-by-design stream that has legitimately never fired has NO FILE — there is
            # nothing to create it. Reporting that as MISSING made the audit cry broken twice on
            # every run about two streams whose own registry entries say "0 in 8 wks = fine, do
            # not kill" and "0 is expected, not broken". The set was already defined above and
            # simply was not consulted on this branch. (2026-07-29)
            rows.append((name, "RARE-OK" if name in RARE_OK else "MISSING", 0, 0, 0, "—"))
            continue
        total, n14, n30, last = recent_counts(p)
        if n14 > 0:
            verdict = "ALIVE"
        elif n30 == 0:
            verdict = "RARE-OK" if name in RARE_OK else "DEAD"
        else:
            verdict = "ALIVE"  # fired in 15-30d window
        rows.append((name, verdict, total, n14, n30, last))
    undeclared = sorted(existing - set(declared))

    order = {"MISSING": 0, "DEAD": 1, "RARE-OK": 2, "ALIVE": 3}
    rows.sort(key=lambda r: (order.get(r[1], 9), -r[4]))

    if as_json:
        print(json.dumps({
            "ts": NOW.isoformat(),
            "rows": [{"stream": r[0], "verdict": r[1], "total": r[2],
                      "n14": r[3], "n30": r[4], "last": r[5]} for r in rows],
            "undeclared": undeclared,
        }, indent=2))
        return

    tally = {}
    for _, v, *_ in rows:
        tally[v] = tally.get(v, 0) + 1
    print(f"# Reality-audit — {NOW.strftime('%Y-%m-%d')} — {len(rows)} declared streams")
    print(f"# {tally.get('MISSING',0)} MISSING · {tally.get('DEAD',0)} DEAD · "
          f"{tally.get('RARE-OK',0)} RARE-OK · {tally.get('ALIVE',0)} ALIVE\n")
    print(f"{'stream':32} {'verdict':8} {'total':>6} {'14d':>5} {'30d':>5}  last")
    print("-" * 72)
    for name, v, total, n14, n30, last in rows:
        print(f"{name:32} {v:8} {total:>6} {n14:>5} {n30:>5}  {last}")
    if undeclared:
        print(f"\n# UNDECLARED (log fires, no feature-tracking row): {', '.join(undeclared)}")


if __name__ == "__main__":
    main()
