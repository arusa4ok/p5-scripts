#!/usr/bin/env python3
"""Spec 021 T004: list files under ~/.claude newer than a stamp that the write-allowlist does not cover.
Usage: ab-write-check.py <stamp-file> <allowlist>   → exit 1 and print offenders if any."""
import fnmatch, os, sys
from pathlib import Path

stamp, allow = Path(sys.argv[1]).stat().st_mtime, Path(sys.argv[2])
home = os.path.expanduser("~")
globs = [l.strip().replace("~", home, 1) for l in allow.read_text().splitlines()
         if l.strip() and not l.startswith("#")]
bad = []
for root, _, files in os.walk(f"{home}/.claude"):
    for f in files:
        p = os.path.join(root, f)
        try:
            if os.lstat(p).st_mtime <= stamp:
                continue
        except OSError:
            continue
        if not any(fnmatch.fnmatch(p, g) for g in globs):
            bad.append(p.replace(home, "~", 1))
print("\n".join(sorted(bad)) or "clean")
sys.exit(1 if bad else 0)
