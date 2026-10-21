#!/usr/bin/env python3
"""Spec 021 T015 (SC-004): prove the move map covers every heading of the always-loaded sources.

- every `#`-heading of each source file has a row in move-map.md;
- `verbatim` rows: N sentences sampled (fixed seed) from their sections are found in the destination;
- `rewritten` rows must name the destination line (`file:line`).
Usage: move-map-check.py [--n 20] [--seed 21]   → exit 0 when clean.
"""
import argparse
import random
import re
import sys
from pathlib import Path

HOME = Path.home()
REPO = HOME / "projects/cc-stack-2026"
EV = REPO / ".specify/specs/021-context-compression/evidence"
SOURCES = [HOME / ".claude/CLAUDE.md", REPO / "CLAUDE.md"] + [
    HOME / ".claude/rules" / r for r in (
        "INDEX.md", "soul.md", "learned/autonomy-depth.md", "learned/prompt-injection-defense.md",
        "learned/re-plan-dont-push.md", "learned/show-before-edit.md", "learned/simplicity-first.md",
        "learned/verify-platform-facts.md")]
HEAD = re.compile(r"^(#{1,6}) (.+)$")


def resolve(src):
    return REPO / src[len("cc-stack/"):] if src.startswith("cc-stack/") else Path(src).expanduser()


def section_texts(path):
    out, cur = {}, None
    for line in path.read_text().splitlines():
        m = HEAD.match(line)
        if m:
            cur = m.group(2).strip()
            out.setdefault(cur, [])
        elif cur is not None:
            out[cur].append(line)
    return {h: " ".join(v) for h, v in out.items()}


def norm(s):
    return re.sub(r"\s+", " ", s).strip()


def check(sources, move_map, staging, n=20, seed=21):
    rows = {}
    for line in Path(move_map).read_text().splitlines():
        cells = [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", line)[1:-1]]
        if len(cells) == 4 and cells[2] not in ("kind", "---"):
            rows[(resolve(cells[0]), cells[1])] = (cells[2], cells[3])
    errs, pool = [], []
    for src in sources:
        for h, text in section_texts(Path(src)).items():
            row = rows.get((Path(src), h))
            if row is None:
                errs.append(f"no row: {src} :: {h}")
                continue
            kind, dest = row
            if kind == "verbatim":
                for s in re.split(r"(?<=[.!?])\s+", norm(text)):
                    if len(s) >= 25:
                        pool.append((src, h, s, dest))
            elif kind == "rewritten" and not re.search(r":\d+$", dest):
                errs.append(f"rewritten row without destination line: {src} :: {h}")
    rnd = random.Random(seed)
    for src, h, s, dest in rnd.sample(pool, min(n, len(pool))):
        d = Path(staging) / dest
        if not d.exists() or s not in norm(d.read_text()):
            errs.append(f"not found in {dest}: {s[:70]!r} ({src} :: {h})")
    return errs


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--seed", type=int, default=21)
    a = ap.parse_args()
    errs = check(SOURCES, EV / "move-map.md", EV / "staging", a.n, a.seed)
    print("\n".join(errs) or f"move map OK: every heading mapped, {a.n} sampled sentences found")
    sys.exit(1 if errs else 0)
