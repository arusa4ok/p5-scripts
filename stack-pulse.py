#!/usr/bin/env python3
"""stack-pulse — one morning line about how last night went (05:00 daily).

Reads TODAY's events from night-desk.jsonl + nightshift's launchd log and
sends ONE ntfy line via notify-anton.sh (--dry prints instead). Appends one
JSONL fire line per run. Zero LLM tokens.

Measure-what-you-ship: fires = ~/.claude/logs/stack-pulse.jsonl.
Yardstick: the pulse never misses a real night failure (a night_run_never_started
or a non-zero night_run exit must always surface, priority=high).
Weekly verdict: 0 missed failures = keep; a missed failure, or 3 straight weeks
of idle heartbeats while night-desk.jsonl shows real work = the reader is
broken — fix or kill.
Run-N yield: varies with the night itself (wire receipts, run exits, nightshift
tasks) — the pulse READS the night, it does not search for one, so it does not
decay the way a fixed-query cron does.
"""
import json
import pathlib
import re
import subprocess
import sys
from datetime import datetime, timezone

NOW = datetime.now(timezone.utc)
TODAY = NOW.strftime("%Y-%m-%d")
ND_WINDOW_H = 8    # night-desk runs 02:00 local = 22:00 UTC the day before
NS_WINDOW_H = 12   # nightshift 03:30 local; a run can stretch past an hour
HOME = pathlib.Path.home()
ND = HOME / ".claude/logs/night-desk.jsonl"
NS_LOG = HOME / ".local/share/nightshift/logs/launchd-stdout.log"
FIRE = HOME / ".claude/logs/stack-pulse.jsonl"
NOTIFY = HOME / ".claude/scripts/notify-anton.sh"


def today_events(path):
    evs = []
    if path.exists():
        for line in path.read_text(errors="replace").splitlines():
            if not line.strip().startswith("{"):
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            try:
                t = datetime.fromisoformat(str(e.get("ts", "")).replace("Z", "+00:00"))
            except ValueError:
                continue
            if (NOW - t).total_seconds() <= ND_WINDOW_H * 3600:
                evs.append(e)
    return evs


def nightshift_line():
    if not NS_LOG.exists():
        return "nightshift: лога нет"
    mtime = datetime.fromtimestamp(NS_LOG.stat().st_mtime, timezone.utc)
    if (NOW - mtime).total_seconds() > NS_WINDOW_H * 3600:
        return (f"nightshift: за {NS_WINDOW_H}ч не запускался "
                f"(лог от {mtime:%m-%d %H:%M} UTC)")
    txt = NS_LOG.read_text(errors="replace")
    tasks = txt.count("--- Running:")
    done = txt.count("COMPLETED")
    failed = txt.count("FAILED")
    out = f"nightshift: {done}/{tasks} задач completed"
    if failed:
        out += f", {failed} FAILED"
    return out


AUDIT_DIR = HOME / "projects/omni-stack-2026/runs/audits"
AUDIT_WINDOW_H = 30  # nightly auditor fires after nightshift; stale = a finding

def audit_line():
    if not AUDIT_DIR.exists():
        return "❗ auditor: каталога дайджестов нет", True
    newest = max(AUDIT_DIR.glob("nightly-*.md"),
                 key=lambda p: p.stat().st_mtime, default=None)
    if newest is None:
        return "❗ auditor: ни одного nightly-*.md", True
    age_h = (NOW - datetime.fromtimestamp(
        newest.stat().st_mtime, timezone.utc)).total_seconds() / 3600
    if age_h > AUDIT_WINDOW_H:
        return f"❗ auditor: дайджест старый ({age_h:.0f}ч — {newest.name})", True
    return f"аудитор: {newest.name} ({age_h:.0f}ч)", False

nd = today_events(ND)
runs = [e for e in nd if e.get("event") == "night_run"]
wire = [e for e in nd if e.get("event") == "queue_wire"]
never = [e for e in nd if e.get("event") == "night_run_never_started"]
quota = [e for e in nd if e.get("event") == "quota_probe"]

parts = []
if never:
    parts.append(f"❗ night-desk НЕ СТАРТОВАЛ ({len(never)}x) — очередь сохранена")
elif runs:
    exits = ",".join(str(r.get("exit")) for r in runs)
    parts.append(f"night-desk: exit {exits}")
else:
    parts.append("night-desk: не запускался (очередь пуста)")
if wire:
    w = wire[-1].get("receipt", {})
    parts.append(f"wire: рассмотрено {w.get('considered', '?')}, "
                 f"в очередь {w.get('queued', '?')}")
if quota:
    q = quota[-1].get("zai", {})
    parts.append("z.ai: OK" if q.get("ok") else f"z.ai: 429/{q.get('code', '?')}")
parts.append(nightshift_line())
audit_msg, audit_missing = audit_line()
parts.append(audit_msg)
run_ts = None
if runs:
    try:
        run_ts = datetime.fromisoformat(
            str(runs[-1].get("ts", "")).replace("Z", "+00:00"))
    except ValueError:
        run_ts = None
# The report is named by the LOCAL night date: a 02:00 local run starts ~22:xx UTC
# the PREVIOUS day, so the UTC calendar date is off by one (2026-09-18: the pulse
# claimed "отчёта нет" for a report that existed). Try the local date first, then
# the UTC date and today as fallbacks; count the report's own verdict markers so
# an "exit 1" line can be read against what the night actually produced.
cand = []
if run_ts is not None:
    cand.append(run_ts.astimezone().strftime("%Y-%m-%d"))
    cand.append(run_ts.strftime("%Y-%m-%d"))
cand.append(TODAY)
RUNS = HOME / "projects/cc-stack-2026/p5-loop/omni-pilot/night-desk/runs"
report = next((RUNS / f"report-{d}.md" for d in dict.fromkeys(cand)
               if (RUNS / f"report-{d}.md").exists()), None)
if report is not None:
    rtxt = report.read_text(errors="replace")
    n_pr = len(set(re.findall(r"claude-config/pull/\d+", rtxt)))
    n_skip = rtxt.count("TERMINAL = skipped")
    n_fail = rtxt.count("TERMINAL = failed")
    parts.append(f"отчёт: {report.name} · итог: {n_pr} PR / "
                 f"{n_skip} skip / {n_fail} fail")
else:
    parts.append(f"❗ отчёта нет (report-{cand[0]}.md)")

line = " · ".join(parts)
bad_exit = any(str(r.get("exit")) != "0" for r in runs)
priority = "high" if (never or bad_exit or (runs and report is None)
                      or audit_missing) else "default"

FIRE.parent.mkdir(parents=True, exist_ok=True)
with FIRE.open("a") as f:
    f.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "event": "pulse", "priority": priority, "line": line}) + "\n")

if "--dry" in sys.argv:
    print(f"[dry] priority={priority}\n{line}")
elif NOTIFY.exists():
    subprocess.run([str(NOTIFY), "--priority", priority, "--tags", "hammer_and_wrench",
                    f"Stack pulse {TODAY}", line],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("sent:", line)
else:
    print("notify-anton.sh not found — nothing sent")
