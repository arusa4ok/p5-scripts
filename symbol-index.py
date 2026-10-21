#!/usr/bin/env python3
"""Does this symbol already exist somewhere else in the repo?

`simplicity-first` says "before proposing to BUILD anything, audit what already
exists." The model cannot follow that instruction — it has no index of what
exists. That is an INFORMATION gap, not a judgement gap, and no amount of rule
text closes it. Proof, 2026-08-07: I wrote `_applied_ids` in action-queue.py
duplicating must-act.py's `applied_ids`, with simplicity-first loaded, and a
162-line index caught it in four seconds.

Grafted from ratchet's index-symbols.js (teardown 2026-08-07), with its two
measured defects fixed:

  1. ratchet walks the tree against a FIXED ignore list (node_modules, .venv,
     venv, dist, …) and never reads .gitignore. career-os's virtualenv is named
     `gliner_env`, so 52.3% of its findings were "your role_classifier.py
     duplicates click/utils.py". Here the file list comes from `git ls-files`,
     which already knows exactly what is tracked. Cheaper AND correct.
  2. ratchet reports any repeated name, so every p5 script's `collect()` looked
     like a duplication. A name is a CONVENTION, not a duplication, when it
     recurs widely — so the threshold is derived from the data (see CONVENTION_AT)
     instead of guessed as a hardcoded skip-list.

Usage:
  symbol-index.py [repo]                    report cross-file duplicates
  symbol-index.py [repo] --check NAME       is NAME already taken?
  symbol-index.py [repo] --exclude PATH ... drop a tracked-but-retired tree
"""
import collections
import os
import re
import subprocess
import sys

# A symbol defined at top level in this many files or more is house style
# (main, collect, notify), not an accident worth reporting. Three known-good
# conventions in scripts/p5 sit at 4+; the real duplications found by hand all
# sat at exactly 2. Raise it if reports get noisy; do not lower it below 3.
CONVENTION_AT = 4

DEFS = {
    ".py": [re.compile(r"^(?:async\s+)?def\s+([A-Za-z_]\w*)", re.M),
            re.compile(r"^class\s+([A-Za-z_]\w*)", re.M)],
    ".js": [re.compile(r"^(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)", re.M),
            re.compile(r"^(?:export\s+)?(?:const|class)\s+([A-Za-z_$][\w$]*)", re.M)],
}
for alias in (".ts", ".tsx", ".jsx", ".mjs", ".cjs"):
    DEFS[alias] = DEFS[".js"]


def tracked_files(repo, exclude=()):
    """git ls-files IS the ignore logic — .gitignore, global excludes, and
    everything the repo already decided is not its code. Measured: career-os
    tracks 0 files under its `gliner_env` virtualenv, so this alone removes the
    entire cause of ratchet's 52.3% noise, for free.

    `exclude` covers the case git cannot know about: code a repo deliberately
    KEEPS in history but no longer runs. career-os tracks 220 files under
    `web-astro-RETIRED`, and they were 414 of its 619 duplicate lines — 67% of
    the report. The caller names its own archive; the tool does not guess with a
    hardcoded list, which is the mistake being fixed here."""
    out = subprocess.run(["git", "-C", repo, "ls-files"],
                         capture_output=True, text=True)
    if out.returncode != 0:
        sys.exit(f"symbol-index: {repo} is not a git repo")
    return [f for f in out.stdout.split("\n")
            if f and os.path.splitext(f)[1] in DEFS
            and not any(x in f for x in exclude)]


def build(repo, exclude=()):
    """name -> {file: count}. Top-level definitions only; a nested helper that
    shares a name with another file's nested helper is not a finding."""
    index = collections.defaultdict(set)
    for rel in tracked_files(repo, exclude):
        try:
            with open(os.path.join(repo, rel), encoding="utf-8", errors="ignore") as fh:
                text = fh.read()
        except OSError:
            continue
        for pattern in DEFS[os.path.splitext(rel)[1]]:
            for name in pattern.findall(text):
                index[name].add(rel)
    return index


def duplicates(index):
    """Names in 2..CONVENTION_AT-1 files. Sorted by name length descending —
    a longer name repeated twice is far more likely to be real duplication
    than a short common verb."""
    out = [(n, sorted(fs)) for n, fs in index.items()
           if 2 <= len(fs) < CONVENTION_AT and not n.startswith("test_")]
    return sorted(out, key=lambda kv: (-len(kv[0]), kv[0]))


def main():
    args = [a for a in sys.argv[1:]]
    repo = os.path.abspath(args[0]) if args and not args[0].startswith("--") else os.getcwd()
    exclude = [args[i + 1] for i, a in enumerate(args)
               if a == "--exclude" and i + 1 < len(args)]
    index = build(repo, exclude)

    if "--check" in args:
        i = args.index("--check")
        if i + 1 >= len(args):
            sys.exit("symbol-index: --check needs a NAME")
        name = args[i + 1]
        # Normalised: a leading underscore is a privacy marker, not a different thing.
        # `_applied_ids` vs `applied_ids` is the exact pair that started this.
        want = name.lstrip("_").lower()
        hits = {n: sorted(fs) for n, fs in index.items() if n.lstrip("_").lower() == want}
        if not hits:
            print(f"'{name}': free — no top-level definition anywhere in {os.path.basename(repo)}")
            return 0
        for n, fs in hits.items():
            tag = "house style" if len(fs) >= CONVENTION_AT else "TAKEN"
            print(f"'{n}': {tag} — {len(fs)} file(s)")
            for f in fs:
                print(f"    {f}")
        return 1

    dups = duplicates(index)
    print(f"{os.path.basename(repo)}: {len(tracked_files(repo, exclude)):,} tracked code files, "
          f"{len(index):,} top-level symbols, {len(dups)} cross-file duplicates "
          f"(names in >= {CONVENTION_AT} files treated as convention)")
    for name, files in dups:
        print(f"\n  {name}  ({len(files)} files)")
        for f in files:
            print(f"    {f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
