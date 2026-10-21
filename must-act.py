#!/usr/bin/env python3
"""must-act — collapse the stack's ⚠️ signal into ONE forced action list.

The disease (verified 2026-07-15): the verdict layer READS its inputs fine, but its OUTPUT
(SYSTEM-OVERVIEW.md) has no consumer, so ⚠️ flags accumulate unread. This script is that missing
consumer. It pulls the real broken signal from three sources:

  1. reality-audit MISSING/DEAD  — declared streams whose log file is absent or stopped firing
  2. SYSTEM-OVERVIEW.md newest week's ⚠️ lines — the unconsumed weekly verdicts (the core gap)
  3. stream-triage KILL rows      — write-only-forever streams flagged for retire

Emits must-act.json {generated, items:[{id,what,path,since,severity}], top}. The session-start hook
surfaces `top` every session until an APPLIED marker (must-act-applied.txt) clears it — so one broken
thing gets fixed per cycle instead of sitting unflagged for weeks.
"""
from __future__ import annotations
import argparse, json, re, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
AUDIT = ROOT / "scripts" / "p5" / "reality-audit.py"
TOOL_VERDICT = ROOT / "scripts" / "p5" / "tool-verdict.py"
RECONCILE = ROOT / "scripts" / "p5" / "instrument-reconcile.py"   # spec 006 forcing function
OVERVIEW = ROOT / "p5-loop" / "SYSTEM-OVERVIEW.md"
TRIAGE = ROOT / "p5-loop" / "cto-inward" / "stream-triage.md"
APPLIED = ROOT / "p5-loop" / "must-act-applied.txt"   # ids already fixed, one per line
DEFAULT_OUT = ROOT / "p5-loop" / "must-act.json"
LOGS = Path.home() / ".claude" / "logs"
# Two scheduled watchers that FIND problems but write only to a log nobody reads (proven
# 2026-07-18): gbrain-advisor ("7 new since last run" → a .log file) and capability-audit
# (used_unrouted: consensus, stitch → a JSON nobody opens). must-act is their missing consumer.
CAP_AUDIT = LOGS / "capability-audit-latest.json"
CAP_AUDIT_SCRIPT = Path.home() / ".claude" / "scripts" / "capability-routing-audit.py"
CAP_STALE_H = 12   # refresh the audit cache if older than this; else a 2-day-stale cache
                   # cries wolf (consensus showed unrouted for 2 days after it was routed).
ADVISOR_RUNS = LOGS / "gbrain-advisor-runs.jsonl"
# Finder-agents (performance-engineer, incident-responder, …) are read-only RECOMMENDERS: they
# find real problems but their findings evaporate when the sub-agent's context is discarded. The
# main loop appends each actionable finding to finder-findings.jsonl (via scripts/p5/finder-log.py)
# so must-act — the one consumer — surfaces it and Claude fixes one per cycle instead of it being
# lost. Same pattern as advisor_items/capability_items: a watcher whose output nobody read.
FINDER_LOG = LOGS / "finder-findings.jsonl"

SEV = {"MISSING": "broken", "DEAD": "dead"}
# Hours after which the overview counts as a fossil, not a verdict. 24h (was 48): a ⚠️ line
# is served to the session as a CURRENT fact, and at 29h of age on 2026-07-30 three of them
# described work that had already been finished — one of them quoting a cause the code had
# stopped emitting the day before. A day-old dashboard is the outer bound of "current".
STALE_H = 24
# Regenerating the overview takes minutes, so must-act (a ~1s session-start consumer) can
# never wait for it. When the overview is stale it kicks the generator off DETACHED and
# suppresses that run's ⚠️ lines — the next session reads a fresh dashboard instead of the
# fossil. Guarded by a marker file so N sessions in a row don't spawn N generators.
REFRESH_MARK = ROOT / "p5-loop" / ".overview-refresh-kicked"
REFRESH_COOLDOWN_H = 3
BLOCK_HDR = re.compile(r"^## System Overview — Week")
# rare-by-design streams: a MISSING here is expected, not a broken item.
# Imported, NOT mirrored — the hand-kept copy had already drifted from reality-audit's by one
# entry, which is exactly how two detectors start disagreeing about the same fact. (2026-07-29)
try:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from importlib import import_module
    RARE_OK = import_module("reality-audit".replace("-", "_")).RARE_OK  # noqa: N816
except Exception:  # pragma: no cover — module name has a dash; fall back to loading by path
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location("_ra", Path(__file__).resolve().parent / "reality-audit.py")
    _ra = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_ra)
    RARE_OK = _ra.RARE_OK
# ⚠️ lines that are legend/rubric/threshold text, not actionable warnings — skip them.
LEGEND = re.compile(r"^_|^(rule|categories|gate|rung)\b", re.I)


def audit_items():
    out = subprocess.run([sys.executable, str(AUDIT), "--json"],
                         capture_output=True, text=True, check=True).stdout
    d = json.loads(out)
    items = []
    for r in d.get("rows", []):
        if r["verdict"] in SEV and r["stream"] not in RARE_OK:
            items.append({"id": f"stream:{r['stream']}", "what": f"{r['stream']} — {r['verdict']}",
                          "path": f"~/.claude/logs/{r['stream']}", "since": r.get("last", "—"),
                          "severity": SEV[r["verdict"]]})
    return items


def kick_overview_refresh():
    """Start a detached SYSTEM-OVERVIEW regeneration. Best-effort; never blocks or raises.

    This is what makes a warning expire BY FACT: the ⚠️ lines are a projection of the
    dashboard, so once the dashboard is rebuilt a fixed thing simply stops being emitted and
    the item disappears without anyone ticking a box. Before this, the only way to silence a
    fixed warning was an APPLIED marker — a manual step that was skipped three times in two
    weeks, leaving closed work at the top of the queue.
    """
    try:
        if REFRESH_MARK.exists():
            age_h = (datetime.now(timezone.utc).timestamp() - REFRESH_MARK.stat().st_mtime) / 3600
            if age_h < REFRESH_COOLDOWN_H:
                return
        # Never start a second generator: two of them write the same SYSTEM-OVERVIEW.md and
        # snapshot files, and the loser's partial write is what the next session would read.
        # (Observed live 2026-07-30 — a manual run and a kicked run overlapped.)
        if subprocess.run(["pgrep", "-f", "p5/system-overview.py"],
                          capture_output=True).returncode == 0:
            return
        REFRESH_MARK.parent.mkdir(parents=True, exist_ok=True)
        REFRESH_MARK.write_text(datetime.now(timezone.utc).isoformat())
        log = open(ROOT / "p5-loop" / ".overview-refresh.log", "ab")
        subprocess.Popen([sys.executable, str(ROOT / "scripts" / "p5" / "system-overview.py")],
                         cwd=str(ROOT), stdout=log, stderr=log, start_new_session=True)
    except Exception:
        pass


def overview_stale():
    """The producer's own health, as item #1 when it has stopped updating.

    must-act is a 1-second consumer of a 5-minute weekly producer, so every warning it
    serves is only as true as the last SYSTEM-OVERVIEW run. When that run stops happening,
    must-act keeps forcing action on warnings about already-fixed things — confidently,
    because nothing downstream can tell a fresh verdict from a fossil.

    Origin (2026-07-16): a NameError in section_hooks killed the generator at 15:55 UTC and
    NOTHING noticed — the overview simply stayed at its 11:16 text, and must-act went on
    naming a top item ("MCP is searching a dead box") whose last real failure was 10:26.
    A dead producer must be louder than anything it produces, so it outranks every warning.
    """
    if not OVERVIEW.exists():
        return [{"id": "stale:system-overview-missing", "what":
                 "SYSTEM-OVERVIEW.md отсутствует — весь ⚠️-слой ниже слеп",
                 "path": "scripts/p5/system-overview.py", "since": "—", "severity": "broken"}]
    head = OVERVIEW.read_text(encoding="utf-8", errors="replace")[:4000]
    m = re.search(r"generated (\d{4}-\d{2}-\d{2} \d{2}:\d{2}) UTC", head)
    if not m:
        return []
    try:
        gen = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    except ValueError:
        return []
    hours = (datetime.now(timezone.utc) - gen).total_seconds() / 3600
    if hours <= STALE_H:
        return []
    kick_overview_refresh()
    return [{"id": "stale:system-overview", "what":
             f"SYSTEM-OVERVIEW не обновлялся {int(hours)}ч — проверь, что "
             f"`python3 scripts/p5/system-overview.py` не падает; всё ⚠️ ниже может быть "
             f"про уже починенное",
             "path": "scripts/p5/system-overview.py",
             "since": m.group(1), "severity": "broken"}]


def overview_warnings(stale=False):
    """⚠️ lines from the NEWEST week block of SYSTEM-OVERVIEW.md (the unconsumed verdicts).

    `stale` suppresses them wholesale: a fossil line is not a weaker signal, it is an
    unverified claim about the past, and serving it as this cycle's forced action is how
    already-finished work stayed at the top of the queue for a week. The `stale:` item that
    replaces them is louder, and the refresh it kicks off restores the real lines next run.
    """
    if stale or not OVERVIEW.exists():
        return []
    lines = OVERVIEW.read_text(encoding="utf-8", errors="replace").splitlines()
    # slice from the first week-block header to the second (= newest week only)
    starts = [i for i, ln in enumerate(lines) if BLOCK_HDR.match(ln)]
    if not starts:
        block = lines
    else:
        block = lines[starts[0]: (starts[1] if len(starts) > 1 else len(lines))]
    items = []
    for ln in block:
        if "⚠️" in ln:
            text = ln.strip().lstrip("-*# ").replace("⚠️", "").strip()
            if not text or LEGEND.match(text):
                continue
            # Identity comes from the SIGNAL, not from its current value: digits are stripped
            # before slugging. `testgate: 34 за неделю (pass×32 · block×1)` used to slug to
            # `warn:testgate-34-pass-32-…`, so next week's counts minted a NEW id and an
            # APPLIED marker silenced a warning exactly once — the queue looked drained while
            # the same signal kept coming back wearing a different number.
            wid = "warn:" + re.sub(r"[^a-z]+", "-", text.lower())[:48].strip("-")
            items.append({"id": wid, "what": text[:120], "path": "p5-loop/SYSTEM-OVERVIEW.md",
                          "since": "this week", "severity": "broken"})
    return items


def triage_kills():
    if not TRIAGE.exists():
        return []
    items = []
    for line in TRIAGE.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r"\|\s*`?([\w.-]+)`?\s*\|.*\|\s*\*\*KILL\*\*\s*\|", line)
        if m:
            items.append({"id": f"kill:{m.group(1)}", "what": f"{m.group(1)} — write-only, retire",
                          "path": "p5-loop/cto-inward/stream-triage.md", "since": "—", "severity": "idle"})
    return items


def _refresh_cap_audit_if_stale():
    """Regenerate the capability-audit cache if it is missing or older than CAP_STALE_H.
    Without this must-act reads whatever the last manual run left behind — a 2-day-old cache
    flagged `consensus` unrouted for two days after it was routed. Bounded to ~2 runs/day;
    best-effort with a timeout so session-start never blocks on a slow transcript scan."""
    try:
        fresh = CAP_AUDIT.exists() and (
            datetime.now(timezone.utc).timestamp() - CAP_AUDIT.stat().st_mtime < CAP_STALE_H * 3600)
        if fresh or not CAP_AUDIT_SCRIPT.exists():
            return
        subprocess.run([sys.executable, str(CAP_AUDIT_SCRIPT)],
                       capture_output=True, timeout=90)
    except Exception:
        pass   # fall back to the existing cache — a stale audit beats no audit


def capability_items():
    """capability-audit finds tools USED without a routing rule (or routed-but-idle) and
    writes them to capability-audit-latest.json — which nothing reads. Surface the
    actionable bucket (used_unrouted) into must-act: a tool being used with no routing
    rule is exactly the 'wire a rule or /forwhat-retire' action Anton keeps missing."""
    _refresh_cap_audit_if_stale()
    if not CAP_AUDIT.exists():
        return []
    try:
        d = json.loads(CAP_AUDIT.read_text())
    except Exception:
        return []
    items = []
    for r in d.get("rows", []):
        if r.get("bucket") == "used_unrouted":
            cap = r.get("capability", "?")
            items.append({"id": f"unrouted:{cap}",
                          "what": f"{cap} — используется без правила роутинга (впиши правило или /forwhat-ретайр)",
                          "path": "~/.claude/logs/capability-audit-latest.json",
                          "since": d.get("ts", "—")[:10], "severity": "idle"})
    return items


def advisor_items():
    """gbrain-advisor runs weekly and ends its report with 'N new since last run' — into a
    .log nobody opens. Surface its warn/critical tally: >0 means the brain's own doctor
    flagged something. 0/0 emits nothing (no false item)."""
    if not ADVISOR_RUNS.exists():
        return []
    try:
        last = [l for l in ADVISOR_RUNS.read_text().splitlines() if l.strip()][-1]
        d = json.loads(last)
    except (IndexError, ValueError):
        return []
    crit, warn = d.get("critical", 0), d.get("warn", 0)
    if crit == 0 and warn == 0:
        return []
    sev = "broken" if crit else "idle"
    return [{"id": f"advisor:{d.get('ts','')[:10]}-{crit}c-{warn}w",
             "what": f"gbrain-advisor: {crit} critical / {warn} warn — разбери (gbrain-advisor-weekly.log)",
             "path": "~/.claude/logs/gbrain-advisor-weekly.log",
             "since": d.get("ts", "—")[:10], "severity": sev}]


def finder_items():
    """Finder-agent findings that the main loop appended to finder-findings.jsonl. Each open one
    becomes a must-act item so the fix enters the one-per-cycle loop instead of dying with the
    sub-agent's context. Fixed items are cleared the normal way (must-act-applied.txt), so this
    emits every logged finding and lets applied_ids() in main() drop the done ones — same as every
    other source. Newest line per id wins, so a re-run that re-reports a finding just refreshes it."""
    if not FINDER_LOG.exists():
        return []
    by_id = {}
    for line in FINDER_LOG.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        fid = d.get("id")
        if not fid:
            continue
        sev = d.get("severity", "idle")
        proj = d.get("project", "")
        what = (d.get("what", "") or "")[:120]
        by_id[fid] = {
            "id": fid,
            "what": (f"[{proj}] " if proj else "") + f"{d.get('agent', 'finder')}: {what}",
            "path": d.get("path", "—"),
            "since": (d.get("ts", "—") or "—")[:10],
            "severity": sev if sev in ("broken", "dead", "idle") else "idle",
        }
    return list(by_id.values())


def tool_verdict_items():
    """⚠️ per-tool verdicts from tool-verdict.py (computed precision, not asserted). A tool that
    fired ≥MIN_FIRES times and helped <30% of the time is a real weak-spot — surface it so it gets
    trimmed/rewritten/retired one-per-cycle, same as any finder finding. Shells out (tool-verdict.py
    has a hyphen → not importable) and fails soft: no outcome logs yet ⇒ no items, never a crash."""
    if not TOOL_VERDICT.exists():
        return []
    try:
        out = subprocess.run([sys.executable, str(TOOL_VERDICT), "--items"],
                             capture_output=True, text=True, timeout=30)
        return json.loads(out.stdout) if out.stdout.strip() else []
    except (subprocess.SubprocessError, ValueError):
        return []


# A `warn:` marker silences a MEASURED signal, so it must expire: the measurement is re-run
# every week, and if the same warning is still firing 14 days after someone declared it handled,
# the declaration was wrong and the queue should say so again. Markers for one-time ACTS
# (`register:` a stream, retire a `stream:`, fix a `finder:` finding) describe something that
# happened once and never expire. Legacy lines carry no date and are treated as permanent.
WARN_MARKER_TTL_DAYS = 14


def applied_ids():
    """Ids to suppress. Lines are `<id>` or `<id>\t<YYYY-MM-DD>`; dated `warn:` lines expire."""
    if not APPLIED.exists():
        return set()
    today = datetime.now(timezone.utc).date()
    out = set()
    for line in APPLIED.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        wid = parts[0]
        if wid.startswith("warn:") and len(parts) > 1:
            try:
                if (today - datetime.strptime(parts[1].strip()[:10], "%Y-%m-%d").date()).days > WARN_MARKER_TTL_DAYS:
                    continue   # expired — let the live signal speak again
            except ValueError:
                pass
        out.add(wid)
    return out


def dedupe(items):
    seen, out = set(), []
    for it in items:
        if it["id"] in seen:
            continue
        seen.add(it["id"])
        out.append(it)
    return out


def reconcile_items():
    """spec 006 — the general instrumentation forcing function. Every behavioural log stream that
    is ACTIVE but has no feature-tracking verdict row, and every CONFIRMED-unbacked yardstick, gets
    a fix task. The `what` names the IMPROVEMENT (assign a yardstick / add the missing field), not
    just the existence — tracking must drive outcome, not activity (measure-what-you-ship)."""
    try:
        out = subprocess.run([sys.executable, str(RECONCILE), "--json"],
                             capture_output=True, text=True, check=True).stdout
        d = json.loads(out)
    except Exception:
        return []
    items = []
    for r in d.get("unregistered", []):
        items.append({
            "id": f"register:{r['stream']}",
            "what": (f"{r['stream']} active ({r['lines']} lines) but no verdict row — register + "
                     f"define its yardstick (what proves it HELPS, not just that it fires)"),
            "path": "p5-loop/feature-tracking.md", "since": "reconcile", "severity": "idle"})
    for r in d.get("unbacked", []):
        items.append({
            "id": f"instrument:{r['stream']}",
            "what": (f"{r['stream']} yardstick names {r['missing']} but the log can't produce it — "
                     f"add the missing dimension so the verdict is real"),
            "path": "p5-loop/feature-tracking.md", "since": "reconcile", "severity": "idle"})
    return items


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    args = ap.parse_args()
    done = applied_ids()
    stale = overview_stale()
    items = dedupe(stale + audit_items() + overview_warnings(stale=bool(stale))
                   + advisor_items() + capability_items() + finder_items()
                   + tool_verdict_items() + triage_kills() + reconcile_items())
    items = [it for it in items if it["id"] not in done]
    order = {"broken": 0, "dead": 1, "idle": 2}
    # A stale producer sorts above everything: while it is stale, every other item here is
    # a claim about the past, and "fix the thing that tells you what to fix" comes first.
    items.sort(key=lambda it: (0 if it["id"].startswith("stale:") else 1,
                               order.get(it["severity"], 9)))
    top = items[0]["id"] if items else None
    Path(args.out).write_text(json.dumps(
        {"generated": datetime.now(timezone.utc).isoformat(), "items": items, "top": top}, indent=2))
    print(f"must-act: {len(items)} open, top={top}")


if __name__ == "__main__":
    main()
