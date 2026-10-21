#!/usr/bin/env python3
"""stack-watch — version-drift watcher for Anton's dev stack.

Reads stack-inventory.json, asks each tool for its installed vs upstream version,
emits a markdown digest (for the weekly) and one JSONL line per tool per run.
The point: stack-tool updates surface automatically instead of silently rotting
20 versions behind (the gbrain blind spot, 2026-06-24).

Usage:  python3 stack-watch.py            # human digest + log
        python3 stack-watch.py --md       # markdown only (for appending to the weekly)
Always exits 0 — a watcher must never block its caller.
"""
import json, subprocess, sys, os, datetime, pathlib

HERE = pathlib.Path(__file__).resolve().parent
INVENTORY = HERE / "stack-inventory.json"
LOG = pathlib.Path(os.path.expanduser("~/.claude/logs/stack-watch.jsonl"))
TIMEOUT = 30


def sh(cmd: str) -> str:
    try:
        out = subprocess.run(["bash", "-lc", cmd], capture_output=True, text=True, timeout=TIMEOUT)
        return out.stdout.strip()
    except Exception:
        return ""


def semver_tuple(v: str):
    v = v.lstrip("vV").strip()
    parts = []
    for seg in v.split("."):
        num = "".join(ch for ch in seg if ch.isdigit())
        parts.append(int(num) if num else 0)
    return tuple(parts)


def status(installed: str, upstream: str, mode: str) -> str:
    if not installed or not upstream:
        return "unknown"
    if mode == "equal":
        return "current" if installed == upstream else "behind"
    # semver: zero-pad to equal length, compare
    a, b = semver_tuple(installed), semver_tuple(upstream)
    n = max(len(a), len(b))
    a += (0,) * (n - len(a))
    b += (0,) * (n - len(b))
    if a == b:
        return "current"
    return "behind" if a < b else "ahead"


EMOJI = {"current": "✅", "behind": "⚠️", "ahead": "🔹", "unknown": "❔"}


def main():
    md_only = "--md" in sys.argv
    inv = json.loads(INVENTORY.read_text())
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    day = ts[:10]
    rows = []
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as logf:
        for t in inv["tools"]:
            installed = sh(t["installed"])
            upstream = sh(t["upstream"])
            st = status(installed, upstream, t.get("compare", "semver"))
            rows.append({**t, "installed": installed or "?", "upstream": upstream or "?", "status": st})
            logf.write(json.dumps({"ts": ts, "tool": t["name"], "installed": installed,
                                   "upstream": upstream, "status": st, "risk": t["risk"]}) + "\n")

    behind = [r for r in rows if r["status"] == "behind"]
    lines = [f"## Stack-watch — {day}",
             f"**{len(rows)} tools checked · {len(behind)} behind upstream.**", "",
             "| tool | installed | upstream | status | risk |",
             "|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['name']} | {r['installed']} | {r['upstream']} | "
                     f"{EMOJI[r['status']]} {r['status']} | {r['risk']} |")
    if behind:
        lines.append("")
        for r in behind:
            flag = "🚩 FLAG (Anton's call)" if r["risk"] == "high" else "→ auto-apply OK (reversible)"
            lines.append(f"- **{r['name']}** {r['installed']} → {r['upstream']} · {flag}")
            lines.append(f"  - update: {r['update']}")
            if r.get("note"):
                lines.append(f"  - note: {r['note']}")
    md = "\n".join(lines)
    print(md)
    if not md_only:
        print(f"\n_log: {LOG}_", file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"## Stack-watch — error\nwatcher failed: {e}", file=sys.stderr)
    sys.exit(0)
