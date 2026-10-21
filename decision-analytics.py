#!/usr/bin/env python3
"""Decision analytics (cc-stack P1.3) — a zero-LLM dashboard over the decision ledger.

The ledger (~/.claude/decisions/*.jsonl, written by the /decide and /idea skills) records every
fixed decision/commitment/rejection/idea with type, confidence, scope, project, supersession, tags,
timestamp. system-overview.py counts *how much*; this answers *what was decided, where, and what's
still undecided* — the analytics layer Anton asked for on top of the raw log.

Usage:  python3 decision-analytics.py            # print dashboard
        python3 decision-analytics.py --weeks 8  # timeline window (default 12 ISO weeks)
Read-only. Never writes. Fail-open on a malformed line (skip it, note the count).
"""
import json, os, sys, glob, datetime, re
from collections import Counter, defaultdict

DEC_DIR = os.path.expanduser("~/.claude/decisions")
PROJECTS_DIR = os.path.expanduser("~/projects")
SESSIONS_DIR = os.path.expanduser("~/.claude/projects")
SESSION_PREFIX = "-Users-rusakovanton-projects-"
GAP_SESSION_THRESHOLD = 5
GAP_WINDOW_DAYS = 30
WEEKS = 12
if "--weeks" in sys.argv:
    try: WEEKS = int(sys.argv[sys.argv.index("--weeks") + 1])
    except Exception: pass


def load():
    rows, bad = [], 0
    for path in sorted(glob.glob(os.path.join(DEC_DIR, "*.jsonl"))):
        for line in open(path, encoding="utf-8", errors="replace"):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                bad += 1
    return rows, bad


def ts_of(r):
    s = (r.get("source") or {}).get("ts") or r.get("ts") or ""
    return s


def iso_week(s):
    try:
        d = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
        y, w, _ = d.isocalendar()
        return f"{y}-W{w:02d}"
    except Exception:
        return None


def bar(n, scale=1):
    return "█" * min(40, n * scale)


def canonical_projects():
    """Real top-level project dirs under ~/projects/, excluding numbered feature/
    worktree sub-dirs (e.g. identity-os--083-self-analytics-kb). Best-effort, not
    authoritative — this is a heuristic dashboard section, not a registry."""
    names = []
    try:
        for entry in os.scandir(PROJECTS_DIR):
            if not entry.is_dir():
                continue
            if re.search(r"--\d", entry.name):
                continue
            names.append(entry.name)
    except Exception:
        pass
    # longest-first so a session dir is claimed by its most specific canonical project
    return sorted(set(names), key=len, reverse=True)


def sessions_by_project(cutoff_days=GAP_WINDOW_DAYS):
    """Count *.jsonl session files modified in the last cutoff_days, per canonical
    project, by matching ~/.claude/projects/<slug> dir names against project basenames
    under ~/projects/. Read-only, fail-open."""
    counts = Counter()
    claimed_dirs = set()
    now = datetime.datetime.now().timestamp()
    cutoff = now - cutoff_days * 86400

    try:
        session_dirs = [e.name for e in os.scandir(SESSIONS_DIR) if e.is_dir()]
    except Exception:
        return counts

    for proj in canonical_projects():
        target = SESSION_PREFIX + proj
        for d in session_dirs:
            if d in claimed_dirs:
                continue
            if d == target or d.startswith(target + "-"):
                claimed_dirs.add(d)
                try:
                    for f in glob.glob(os.path.join(SESSIONS_DIR, d, "*.jsonl")):
                        try:
                            if os.path.getmtime(f) >= cutoff:
                                counts[proj] += 1
                        except Exception:
                            continue
                except Exception:
                    continue
    return counts


def main():
    rows, bad = load()
    if not rows:
        print("(decision ledger empty — capture calls with /decide or /idea)"); return

    # dedup by id (same DEC can appear in both _global and project files)
    seen, uniq = set(), []
    for r in rows:
        rid = r.get("id")
        if rid and rid in seen:
            continue
        if rid:
            seen.add(rid)
        uniq.append(r)
    rows = uniq

    by_type = Counter(r.get("type", "?") for r in rows)
    by_conf = Counter(r.get("confidence", "?") for r in rows)
    by_scope = Counter(r.get("scope", "?") for r in rows)
    by_proj = Counter(r.get("project", "?") for r in rows)
    tags = Counter(t for r in rows for t in (r.get("tags") or []))

    print("=" * 64)
    print(f"  DECISION ANALYTICS — {len(rows)} entries"
          + (f"  ({bad} malformed lines skipped)" if bad else ""))
    print("=" * 64)

    print("\nBy type:")
    for k, n in by_type.most_common():
        print(f"  {k:12} {n:3}  {bar(n)}")
    print("\nBy confidence:")
    for k, n in by_conf.most_common():
        print(f"  {k:12} {n:3}  {bar(n)}")
    print("\nBy scope / project:")
    for k, n in by_scope.most_common():
        print(f"  scope={k:10} {n:3}")
    for k, n in by_proj.most_common():
        print(f"  {str(k)[:28]:28} {n:3}")

    # supersession chains — where Anton changed his mind
    chains = [(r.get("supersedes"), r.get("id"), str(r.get("title", ""))[:54])
              for r in rows if r.get("supersedes")]
    print(f"\nSupersession chains (changed mind) — {len(chains)}:")
    if chains:
        for old, new, title in chains:
            print(f"  {old} → {new}  «{title}»")
    else:
        print("  (none — no decision has reversed another yet)")

    # open ideas — type=idea not yet superseded by a firm decision
    superseded_ids = {c[0] for c in chains}
    open_ideas = [r for r in rows
                  if r.get("type") == "idea" and r.get("id") not in superseded_ids]
    print(f"\nOpen ideas (tentative, not yet decided) — {len(open_ideas)}:")
    for r in open_ideas:
        print(f"  {r.get('id')}  «{str(r.get('title',''))[:60]}»"
              + (f"  [{r.get('project')}]" if r.get('project') else ""))
    if not open_ideas:
        print("  (none open)")

    # timeline — decisions per ISO week, last N weeks
    wk = Counter(iso_week(ts_of(r)) for r in rows if iso_week(ts_of(r)))
    if wk:
        recent = sorted(wk.keys())[-WEEKS:]
        print(f"\nTimeline (last {len(recent)} active ISO weeks):")
        for w in recent:
            print(f"  {w}  {wk[w]:2}  {bar(wk[w])}")

    if tags:
        print("\nTop tags:")
        for t, n in tags.most_common(10):
            print(f"  {t:18} {n:2}  {bar(n)}")

    # capture gaps — active sessions with no matching decision capture in 30d
    print(f"\nCapture gaps ({GAP_WINDOW_DAYS}d):")
    cutoff_iso = (datetime.datetime.now(datetime.timezone.utc)
                  - datetime.timedelta(days=GAP_WINDOW_DAYS)).isoformat()
    recent_ledger_projects = set()
    for r in rows:
        ts = ts_of(r)
        try:
            if ts and ts.replace("Z", "+00:00") >= cutoff_iso:
                recent_ledger_projects.add(r.get("project"))
        except Exception:
            continue

    session_counts = sessions_by_project()
    gaps = [(proj, n) for proj, n in session_counts.items()
            if n >= GAP_SESSION_THRESHOLD and proj not in recent_ledger_projects]
    if gaps:
        for proj, n in sorted(gaps, key=lambda x: -x[1]):
            print(f"  ⚠ capture gap  {proj:28} {n:3} active sessions, 0 ledger records")
    else:
        print("  (none — every active project has a recent ledger record)")

    print("\n" + "-" * 64)
    firm = by_conf.get("firm", 0)
    print(f"  {firm} firm · {by_conf.get('tentative',0)} tentative · "
          f"{by_type.get('idea',0)} open ideas · {len(chains)} reversals")
    print("-" * 64)


if __name__ == "__main__":
    main()
