#!/usr/bin/env python3
"""Spec 021 T012/T013 staging: move every displaced section VERBATIM into on-demand files
(new, or appended to a copy of an existing one) and write evidence/move-map.md.

Nothing is deleted: each source heading gets a move-map row naming where its text now lives.
Arm-B CLAUDE.md / core.md / cc-stack CLAUDE.md are hand-written separately (rewritten layer).
Usage: ab-stage.py            (idempotent: rebuilds evidence/staging/ from live files)
"""
import re
import shutil
from pathlib import Path

HOME = Path.home()
LIVE = HOME / ".claude"
REPO = HOME / "projects/cc-stack-2026"
EV = REPO / ".specify/specs/021-context-compression/evidence"
ST = EV / "staging"
OD = ST / "on-demand-rules"
CC = ST / "cc-stack"
MOVED = "\n\n## Moved from {src} (spec 021, 2026-10-21)\n\n"

# global CLAUDE.md "## " section → on-demand file (None = covered by a hook/file, not copied)
GLOBAL_MAP = {
    "Global Rules — Anton Rusakov": "work-style.md",
    "Who I Am": "language.md",
    "How to Work With Me": "work-style.md",
    "Commits & Git": "git-policy.md",
    "Parallel Delegation — Mandatory": "parallel-delegation.md",
    "Research Tools — Cost-Aware Hierarchy": "research-tools.md",
    "Memory System": "memory-system.md",
    "gbrain-native docs": "gbrain-doc-format.md",
    "Public Content Quality": "anti-ai-rules.md",
    "Voice & Style (Anton-voice mode)": "anti-ai-rules.md",
    "Self-Improvement": "self-improvement-mechanics.md",
    "Diary & Trajectory Logging": "diary-and-trajectories.md",
    "Projects": "projects.md",
    "Browser automation": "browser.md",
    "RTK (token proxy)": None,  # ~/.claude/RTK.md holds the full reference; the hook is silent
}
# whole rule files that leave the always-loaded tree → on-demand file of the same name
WHOLE = ["learned/autonomy-depth.md", "learned/prompt-injection-defense.md",
         "learned/re-plan-dont-push.md", "learned/show-before-edit.md",
         "learned/simplicity-first.md", "learned/verify-platform-facts.md",
         "soul.md", "INDEX.md"]


def sections(text):
    """Split on top-level '#'/'##' headings → [(heading, body-with-heading)]."""
    parts = re.split(r"(?m)^(?=#{1,2} )", text)
    out = []
    for p in parts:
        if not p.strip():
            continue
        h = p.splitlines()[0].lstrip("#").strip()
        out.append((h, p.rstrip() + "\n"))
    return out


def headings(text):
    return [l.lstrip("#").strip() for l in text.splitlines() if re.match(r"#{1,6} ", l)]


def main():
    shutil.rmtree(ST, ignore_errors=True)
    OD.mkdir(parents=True)
    CC.mkdir(parents=True)
    rows = []

    # 1. global CLAUDE.md sections, verbatim
    src = LIVE / "CLAUDE.md"
    for h, body in sections(src.read_text()):
        dest = GLOBAL_MAP[h]
        if dest is None:
            rows.append(("~/.claude/CLAUDE.md", h, "hook:rtk", "~/.claude/RTK.md"))
            continue
        out = OD / dest
        if not out.exists():
            live = LIVE / "on-demand-rules" / dest
            out.write_text(live.read_text() if live.exists() else f"# {dest[:-3]}\n")
            out.write_text(out.read_text().rstrip() + MOVED.format(src="~/.claude/CLAUDE.md"))
        out.write_text(out.read_text() + body + "\n")
        rows.append(("~/.claude/CLAUDE.md", h, "verbatim", f"on-demand-rules/{dest}"))

    # 2. whole rule files, verbatim
    for rel in WHOLE:
        f = LIVE / "rules" / rel
        name = Path(rel).name
        shutil.copy(f, OD / name)
        for h in headings(f.read_text()):
            rows.append((f"~/.claude/rules/{rel}", h, "verbatim", f"on-demand-rules/{name}"))

    # 3. cc-stack CLAUDE.md: full copy staged (becomes p2-research/ at promotion)
    cc = REPO / "CLAUDE.md"
    full = CC / "cc-stack-claude-md-2026-10-21.md"
    shutil.copy(cc, full)
    for h in headings(cc.read_text()):
        rows.append(("cc-stack/CLAUDE.md", h, "verbatim", f"cc-stack/{full.name}"))

    with open(EV / "move-map.md", "w") as m:
        m.write("# Move map — spec 021\n\nEvery `#` heading of the always-loaded sources and where its text lives "
                "after compression. Destinations are relative to `evidence/staging/`. Kinds: verbatim | "
                "rewritten | hook:<name> | dropped:<reason>.\n\n| source | heading | kind | destination |\n"
                "|---|---|---|---|\n")
        for r in rows:
            m.write("| " + " | ".join(x.replace("|", "\\|") for x in r) + " |\n")
    print(f"{len(rows)} rows · {len(list(OD.iterdir()))} on-demand files staged")


if __name__ == "__main__":
    main()
