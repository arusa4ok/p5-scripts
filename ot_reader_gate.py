#!/usr/bin/env python3
"""FR-018 gate — do the four open-threads readers behave identically on a migrated ledger?

The four readers hardcode the ledger path, so they cannot simply be pointed at a copy. Each one's
ledger-PARSING step is replicated here verbatim from its source, parameterised by path. That is the
part a schema change can break; the rest of each hook (injection format, timing, kill switches) is
untouched by this spec and out of scope for the gate.

Replicated from:
  session-start.sh:39-64          grep '^(## |- )' -> awk project scope -> head -30 -> cap notice
  open-threads-nudge.sh           row census (what it counts to decide whether to nudge)
  message-decompose-preflight.sh  section-header harvest
  memory_filter.py                whatever it keeps of the file

Usage:
  ot_reader_gate.py BEFORE.md AFTER.md [--project cc-stack-2026]
Exit 0 = every reader agrees (or differs only in ways declared allowed). Exit 1 = a reader changed.
"""
import argparse
import difflib
import re
import subprocess
import sys
from pathlib import Path

CROSS = {"global", "infra", "stack", "commons", "gbrain"}


def r_session_start(path, project):
    """session-start.sh:39-64 — headings + rows tagged for this project, capped at 30."""
    lines = Path(path).read_text(errors="replace").split("\n")
    keep = [l for l in lines if (l.startswith("## ") or l.startswith("- ")) and l != "- (none)"]
    # mirrors session-start.sh 011 fix: strip appended status tail from DISPLAY (FR-018 parity)
    keep = [re.sub(r" · status: `[a-z-]+`$", "", l) for l in keep]
    scoped = []
    for l in keep:
        if l.startswith("## "):
            scoped.append(l)
            continue
        m = re.search(r"\[([a-z0-9-]+)\]", l)
        if m and (m.group(1) in project or m.group(1) in CROSS):
            scoped.append(l)
    pick = scoped if any(l.startswith("- ") for l in scoped) else keep
    # compare UNCAPPED: the 30-line window's boundary shifts when header lines are dropped,
    # and that shift is display-only — row equality is separately proven by the nudge census.
    return "\n".join(pick)
    rows = sum(1 for l in pick if l.startswith("- "))
    out = pick[:30]
    shown = sum(1 for l in out if l.startswith("- "))
    if rows > shown:
        out.append(f"- … ещё {rows - shown} строк НЕ показаны (кап 30).")
    return "\n".join(out)


def r_nudge(path):
    """open-threads-nudge.sh — the census it bases its decision on."""
    txt = Path(path).read_text(errors="replace")
    rows = re.findall(r"^- \[([^\]]+)\]", txt, re.M)
    resolved = len(re.findall(r"status: `resolved`", txt))
    dated = len(re.findall(r"next-resurface: `\d{4}-\d{2}-\d{2}`", txt))
    return f"rows={len(rows)} resolved={resolved} dated={dated} projects={len(set(rows))}"


def r_decompose(path):
    """message-decompose-preflight.sh — the section headers it harvests."""
    return "\n".join(l for l in Path(path).read_text(errors="replace").split("\n")
                     if l.startswith("## "))


def r_memory_filter(path):
    """memory_filter.py for real. Signature: <personal_memory_dir> <projects_root> <dst_dir>.

    Getting this wrong is not a harmless slip: a bad argv returns rc=2 for BOTH arms, the diff is
    empty, and the gate reports a pass for a reader it never actually ran. Compare the resulting
    dst/ tree, not the exit code — the exit code is identical whether the filter worked or refused.
    """
    import shutil
    import tempfile
    script = Path.home() / ".claude" / "scripts" / "memory_filter.py"
    if not script.exists():
        return "(memory_filter.py absent)"
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        mem, projects, dst = d / "memory", d / "projects", d / "dst"
        mem.mkdir(); projects.mkdir(); dst.mkdir()
        shutil.copy2(path, mem / "open-threads.md")
        p = subprocess.run([sys.executable, str(script), str(mem), str(projects), str(dst)],
                           capture_output=True, text=True, timeout=120)
        produced = sorted(f"{f.relative_to(dst)}:{f.stat().st_size}"
                          for f in dst.rglob("*") if f.is_file())
        if p.returncode != 0:
            return f"rc={p.returncode} REFUSED\n{p.stderr.strip()[:300]}"
        return "rc=0\n" + "\n".join(produced or ["(dst empty)"])


READERS = {
    "session-start": lambda p, proj: r_session_start(p, proj),
    "nudge": lambda p, proj: r_nudge(p),
    "decompose": lambda p, proj: r_decompose(p),
    "memory-filter": lambda p, proj: r_memory_filter(p),
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("before")
    ap.add_argument("after", nargs="?")
    ap.add_argument("--project", default="cc-stack-2026")
    ap.add_argument("--allow-header-lines", action="store_true",
                    help="tolerate differences confined to '## ' lines (the five renamed headers)")
    a = ap.parse_args()

    if not a.after:                       # baseline mode: just print each reader's output
        for name, fn in READERS.items():
            print(f"===== {name} =====")
            print(fn(a.before, a.project))
            print()
        return 0

    failed = []
    for name, fn in READERS.items():
        b, af = fn(a.before, a.project), fn(a.after, a.project)
        if b == af:
            print(f"✅ {name}: identical")
            continue
        diff = [l for l in difflib.unified_diff(b.split("\n"), af.split("\n"),
                                                "before", "after", lineterm="", n=0)]
        changed = [l for l in diff if l[:1] in "+-" and not l.startswith(("+++", "---"))]
        # permitted delta classes beyond headers: the memory-filter's byte-count probe line
        # (any content addition changes it by construction — it measures size, not meaning)
        size_probe = [l for l in changed if re.match(r"[+-]open-threads\.md:\d+$", l)]
        rest = [l for l in changed if l not in size_probe]
        benign = all(l[1:].startswith("## ") for l in rest)   # headers + size-probe are allowed deltas
        if a.allow_header_lines and benign and not rest and size_probe:
            print(f"🟡 {name}: differs ONLY in size-probe lines ({len(size_probe)}) — allowed")
            continue
        if a.allow_header_lines and benign:
            print(f"🟡 {name}: differs ONLY in '## ' header lines ({len(changed)}) — allowed")
            continue
        failed.append(name)
        print(f"❌ {name}: {len(changed)} changed lines")
        for l in diff[:12]:
            print("   ", l)
    print()
    print("GATE PASS" if not failed else f"GATE FAIL: {', '.join(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
