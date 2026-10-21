#!/usr/bin/env python3
"""spec-start-gate companion CLI — cc-stack spec 019 (contract: contracts/spec-start-gate.md).

  spec-gate.py override --repo PATH --reason TEXT   4 h repo-scoped pass, logged; empty reason → exit 2
  spec-gate.py report [--days 7]                    per-repo outcomes + warn hits to label + SC-002 line
  spec-gate.py summary                              one line for the weekly digest

Reads ~/.claude/logs/spec-start-gate.jsonl, written by ~/.claude/hooks/spec-start-gate.py.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOME = Path.home() / ".claude"
LOG = HOME / "logs" / "spec-start-gate.jsonl"
STATE = HOME / "state" / "spec-start-gate"
HOOK = HOME / "hooks" / "spec-start-gate.py"
TTL_H = 4


def entries(days):
    since = datetime.now(timezone.utc) - timedelta(days=days)
    if not LOG.is_file():
        return []
    out = []
    for line in LOG.read_text().splitlines():
        try:
            e = json.loads(line)
            if datetime.fromisoformat(e["ts"]) >= since:
                out.append(e)
        except (ValueError, KeyError):
            continue
    return out


def hook_mode():
    try:
        for line in HOOK.read_text().splitlines():
            if line.startswith("MODE = "):
                return line.split('"')[1]
    except OSError:
        pass
    return "?"


def hook_code_ext():
    """FR-003: the hook's CODE_EXT is the one list; read it rather than copy it."""
    import ast
    try:
        tree = ast.parse(HOOK.read_text())
    except OSError:
        return set()
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "CODE_EXT":
            return ast.literal_eval(node.value)
    return set()


def cmd_override(a):
    if not a.reason.strip():
        print("override refused: --reason must say why (it is listed in the weekly digest)", file=sys.stderr)
        return 2
    repo = os.path.realpath(a.repo)
    now = datetime.now(timezone.utc)
    exp = now + timedelta(hours=TTL_H)
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / "overrides.jsonl").open("a") as f:
        f.write(json.dumps({"ts": now.isoformat(timespec="seconds"), "repo": repo,
                            "reason": a.reason.strip(), "expires": exp.isoformat(timespec="seconds")}) + "\n")
    print(f"override until {exp.astimezone().strftime('%H:%M')} for {repo}")
    return 0


def sc002(repo, days):
    """Commits in the window touching >=2 code files vs spec dirs created/changed in it."""
    since = f"{days} days ago"
    try:
        log = subprocess.run(["git", "-C", repo, "log", f"--since={since}", "--name-only", "--format=@@%h"],
                             capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.TimeoutExpired):
        return "SC-002 n/a (git failed)"
    code_ext = hook_code_ext()
    if not code_ext:
        return f"SC-002 n/a (CODE_EXT not readable from {HOOK})"
    builds, specs = 0, set()
    for chunk in log.split("@@")[1:]:
        files = [f for f in chunk.splitlines()[1:] if f]
        if sum(1 for f in files if f.rsplit(".", 1)[-1] in code_ext) >= 2:
            builds += 1
        for f in files:
            parts = f.split("/")
            if parts[:2] == [".specify", "specs"] and len(parts) > 2:
                specs.add(parts[2])
            elif parts[0] == "specs" and len(parts) > 1:
                specs.add(parts[1])
    return f"SC-002 {days}d: {builds} build commits (≥2 code files) · {len(specs)} spec dirs touched"


def cmd_report(a):
    es = entries(a.days)
    by = defaultdict(Counter)
    warns = []
    for e in es:
        by[e.get("repo", "?")][e.get("outcome", "?")] += 1
        if e.get("outcome") in ("warn", "block"):
            warns.append(e)
    print(f"spec-start-gate report, last {a.days} d (mode: {hook_mode()})")
    for repo, c in sorted(by.items()):
        print(f"  {repo}: " + " · ".join(f"{k} {v}" for k, v in sorted(c.items())))
        if repo != "?" and os.path.isdir(repo):
            print(f"    {sc002(repo, a.days)}")
    print("hits to label (true = build work without a spec · false = trivial / covered):")
    seen = set()
    for e in warns:
        key = (e.get("session"), e.get("repo"))
        if key in seen:
            continue
        seen.add(key)
        print(f"  {e['ts'][:16]} {e.get('outcome')} {e.get('repo')} session={str(e.get('session'))[:8]} file={e.get('file')}")
    cutoff = time.time() - 7 * 86400
    if STATE.is_dir():
        for f in STATE.glob("*.json"):
            if f.stat().st_mtime < cutoff:
                f.unlink()
    return 0


def cmd_summary(a):
    es = entries(7)
    c = Counter(e.get("outcome") for e in es)
    hits = len({(e.get("session"), e.get("repo")) for e in es if e.get("outcome") in ("warn", "block")})
    # FR-005: every declared override is listed with its reason, hit by the hook or not
    since = datetime.now(timezone.utc) - timedelta(days=7)
    f = STATE / "overrides.jsonl"
    declared = []
    for line in (f.read_text().splitlines() if f.is_file() else []):
        try:
            o = json.loads(line)
            if datetime.fromisoformat(o["ts"]) >= since:
                declared.append(f"  override {o['ts'][:10]} {o.get('repo')} — {o.get('reason')}")
        except (ValueError, KeyError):
            continue
    print(f"spec-gate 7d: {hits} hits · {len(declared)} overrides · {c['block']} blocks · "
          f"{c['error']} errors (mode: {hook_mode()})")
    if declared:
        print("\n".join(declared))
    return 0


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    o = sub.add_parser("override"); o.add_argument("--repo", required=True); o.add_argument("--reason", required=True)
    r = sub.add_parser("report"); r.add_argument("--days", type=int, default=7)
    sub.add_parser("summary")
    a = p.parse_args()
    return {"override": cmd_override, "report": cmd_report, "summary": cmd_summary}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
