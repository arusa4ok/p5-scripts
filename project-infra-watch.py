#!/usr/bin/env python3
"""project-infra-watch — per-project infra freshness across ALL projects.

Complements stack-watch (which checks GLOBAL tool versions, one row per tool).
This checks PER-PROJECT drift that a global check can't see:

  1. spec-kit scaffold drift — each project's .specify/init-options.json
     `speckit_version` vs the global `specify --version`. (The 2026-07-11 finding:
     60+ project scaffolds frozen at 0.8.9.dev0 while the global CLI moved to
     0.12.4 — a per-project drift the global speckit row in stack-watch is blind
     to, because it only sees the global CLI.)

  2. stale local skill copies — a project-local skill (`<proj>/.claude/skills/<name>`)
     that shadows a global skill of the same name (`~/.claude/skills/<name>`) but whose
     SKILL.md content differs. Every project inherits ALL user-scope (~/.claude)
     hooks/skills/rules/MCP automatically, so a project can NOT be *missing* global
     infra — it can only carry a STALE LOCAL copy that shadows the global one, or a
     drifted scaffold. Those are the two things worth watching; "missing global hook"
     is not a real failure mode under CC's settings scoping.

Emits markdown (appended to the weekly by report.py, same cadence as stack-watch)
and one JSONL line per (project, check) per run to ~/.claude/logs/project-infra-watch.jsonl.
Always exits 0 — a watcher must never block its caller.

Usage:  python3 project-infra-watch.py         # human digest + log
        python3 project-infra-watch.py --md     # markdown only (for the weekly)

Kill-threshold: 3 consecutive weeks where every flagged row is a false positive
(a scaffold Anton deliberately pins, or a local skill intentionally customized) ->
drop the noisy check, don't just silence it. Local-skill diffs that turn out to be
deliberate overrides get added to STALE_SKILL_ALLOWLIST below rather than re-flagged.
"""
import json
import os
import sys
import glob
import hashlib
import subprocess
import datetime
import pathlib

HOME = pathlib.Path.home()
GLOBAL_SKILLS = HOME / ".claude" / "skills"
LOG = HOME / ".claude" / "logs" / "project-infra-watch.jsonl"
TIMEOUT = 20

# Where projects live. Worktrees (basename contains '--') are excluded — they share
# the parent's scaffold and would just N-plicate every row.
PROJECT_GLOBS = [str(HOME / "projects" / "*"), str(HOME / "connectpro")]

# Project-local skills that legitimately differ from the global copy (deliberate
# override, not drift). (project_basename, skill_name) pairs. Empty until a diff is
# confirmed intentional — see kill-threshold note above.
STALE_SKILL_ALLOWLIST = set()


def sh(cmd):
    try:
        out = subprocess.run(["bash", "-lc", cmd], capture_output=True, text=True, timeout=TIMEOUT)
        return out.stdout.strip()
    except Exception:
        return ""


def semver_tuple(v):
    v = (v or "").lstrip("vV").strip()
    parts = []
    for seg in v.split("."):
        num = "".join(ch for ch in seg if ch.isdigit())
        parts.append(int(num) if num else 0)
    return tuple(parts)


def cmp_semver(a, b):
    """-1 if a<b, 0 if equal, 1 if a>b (zero-padded)."""
    ta, tb = semver_tuple(a), semver_tuple(b)
    n = max(len(ta), len(tb))
    ta += (0,) * (n - len(ta))
    tb += (0,) * (n - len(tb))
    return -1 if ta < tb else (1 if ta > tb else 0)


def iter_projects():
    seen = set()
    for pat in PROJECT_GLOBS:
        for p in sorted(glob.glob(pat)):
            path = pathlib.Path(p)
            if not path.is_dir():
                continue
            if "--" in path.name:  # worktree
                continue
            if path in seen:
                continue
            seen.add(path)
            yield path


def _file_hash(p):
    try:
        return hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()
    except Exception:
        return ""


def check_scaffold_drift(global_specify):
    """Rows for projects whose .specify scaffold is behind the global CLI."""
    rows = []
    for proj in iter_projects():
        opts = proj / ".specify" / "init-options.json"
        if not opts.exists():
            continue
        try:
            ver = json.loads(opts.read_text()).get("speckit_version", "")
        except Exception:
            ver = ""
        if not ver:
            continue
        behind = global_specify and cmp_semver(ver, global_specify) < 0
        rows.append({
            "project": proj.name,
            "check": "scaffold",
            "local": ver,
            "reference": global_specify or "?",
            "status": "behind" if behind else "current",
        })
    return rows


def check_stale_local_skills():
    """Rows for project-local skills that shadow a global skill but differ."""
    if not GLOBAL_SKILLS.is_dir():
        return []
    global_names = {p.name for p in GLOBAL_SKILLS.iterdir() if p.is_dir()}
    rows = []
    for proj in iter_projects():
        pskills = proj / ".claude" / "skills"
        if not pskills.is_dir():
            continue
        for sk in sorted(pskills.iterdir()):
            if not sk.is_dir() or sk.name not in global_names:
                continue
            if (proj.name, sk.name) in STALE_SKILL_ALLOWLIST:
                continue
            lp = sk / "SKILL.md"
            gp = GLOBAL_SKILLS / sk.name / "SKILL.md"
            if not lp.exists() or not gp.exists():
                continue
            lh, gh = _file_hash(lp), _file_hash(gp)
            if lh and gh and lh != gh:
                rows.append({
                    "project": proj.name,
                    "check": "local-skill",
                    "skill": sk.name,
                    "status": "differs",
                })
    return rows


EMOJI = {"behind": "⚠️", "current": "✅", "differs": "👀"}


def main():
    md_only = "--md" in sys.argv
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    day = ts[:10]
    global_specify = sh("specify --version | grep -oE '[0-9]+\\.[0-9]+\\.[0-9]+' | head -1")

    scaffold = check_scaffold_drift(global_specify)
    skills = check_stale_local_skills()

    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        for r in scaffold + skills:
            f.write(json.dumps({"ts": ts, **r}) + "\n")

    behind = [r for r in scaffold if r["status"] == "behind"]
    diff_skills = skills  # all rows are "differs"

    lines = [
        f"## Project-infra-watch — {day}",
        f"**spec-kit scaffolds: {len(behind)}/{len(scaffold)} behind global `specify` "
        f"{global_specify or '?'} · local skills shadowing global that differ: {len(diff_skills)}.**",
        "",
    ]
    if behind:
        lines.append("**Scaffolds behind (regenerate `.specify` on next `specify` run):**")
        # Group identical (local->ref) drifts to one line — they're all the same fix.
        by_ver = {}
        for r in behind:
            by_ver.setdefault(r["local"], []).append(r["project"])
        for ver, projs in sorted(by_ver.items()):
            lines.append(f"- `{ver}` → `{global_specify}` · {len(projs)} project(s): "
                         f"{', '.join(sorted(projs))}")
        lines.append("")
    if diff_skills:
        lines.append("**Local skill copies that differ from global (review — stale or intentional override):**")
        for r in sorted(diff_skills, key=lambda x: (x["project"], x["skill"])):
            lines.append(f"- {r['project']}: `{r['skill']}` {EMOJI['differs']} differs from ~/.claude/skills/{r['skill']}")
        lines.append("")
    if not behind and not diff_skills:
        lines.append("_All project scaffolds current; no shadowing local-skill drift. ✅_")
    md = "\n".join(lines)
    print(md)
    if not md_only:
        print(f"\n_log: {LOG}_", file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"## Project-infra-watch — error\nwatcher failed: {e}", file=sys.stderr)
    sys.exit(0)
