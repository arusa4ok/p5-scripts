#!/usr/bin/env python3
"""Idempotent superspec install into one repo — cc-stack spec 019 FR-011 (T031).

  superspec-install.py <repo>

Wraps `specify extension add <cc-stack>/.specify/extensions/superspec --dev` (never --force):
  - skip when superspec is already in .specify/extensions/.registry
  - skip when .specify/extensions.yml exists but does not parse
  - report (never overwrite) repo templates that differ from the superspec ones
  - print the paths the install changed, one per line after "CHANGED:"; a path that was
    already dirty before the install is printed under "PRE-DIRTY:" so the caller does not
    stage someone else's edit.
Exit 0 = installed or skipped; 1 = the specify CLI failed.
"""
import filecmp
import json
import subprocess
import sys
from pathlib import Path

import yaml

SRC = Path(__file__).resolve().parents[2] / ".specify" / "extensions" / "superspec"


def porcelain(repo):
    out = subprocess.run(["git", "-C", repo, "status", "--porcelain", "-uall"],
                         capture_output=True, text=True).stdout
    return {line[3:]: line[:2] for line in out.splitlines()}


def main(repo):
    repo = Path(repo).resolve()
    spec = repo / ".specify"
    if not spec.is_dir():
        print(f"SKIP {repo}: no .specify/")
        return 0
    reg = spec / "extensions" / ".registry"
    if reg.is_file() and "superspec" in json.loads(reg.read_text()).get("extensions", {}):
        print(f"SKIP {repo}: superspec already registered")
        return 0
    ext = spec / "extensions.yml"
    if ext.is_file():
        try:
            yaml.safe_load(ext.read_text())
        except yaml.YAMLError as e:
            print(f"SKIP {repo}: extensions.yml does not parse ({e.__class__.__name__})")
            return 0
    for t in sorted((SRC / "templates").glob("*.md")):
        mine = spec / "templates" / t.name
        if mine.is_file() and not filecmp.cmp(mine, t, shallow=False):
            print(f"TEMPLATE-DIFFERS: .specify/templates/{t.name} (left as is)")
    before = porcelain(repo)
    r = subprocess.run(["specify", "extension", "add", str(SRC), "--dev"], cwd=repo,
                       capture_output=True, text=True)
    if r.returncode != 0 or "already installed" in r.stdout + r.stderr:
        print(f"FAIL {repo}: specify exited {r.returncode}\n{(r.stdout + r.stderr)[-600:]}")
        return 1
    after = porcelain(repo)
    changed = sorted(p for p in after if after[p] != before.get(p))
    print(f"INSTALLED {repo}")
    for p in changed:
        print(("PRE-DIRTY: " if p in before else "CHANGED: ") + p)
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    sys.exit(main(sys.argv[1]))
