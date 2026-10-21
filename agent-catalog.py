#!/usr/bin/env python3
"""Which of the catalogued agents would our REAL work have used?

The problem, measured 2026-08-08: 90% of every `Agent` call passes
`subagent_type=general-purpose` (1,003 of 1,113), and 98% of all subagent spend
lands there. Meanwhile ~188 distinct agents sit in the plugin marketplace cache.
The gap is not quality — it is that nobody has ever matched the catalogue against
what we actually delegate.

So this does NOT score agents on how good they look. It scores them against the
1,113 task descriptions we have really dispatched, which are on disk in the
transcripts. An agent that matches nothing we do is not a missed opportunity, it
is correctly unused — and that is worth knowing too, because the honest answer
may be that `general-purpose` deserves most of its share.

Three steps:

  1. INVENTORY   dedupe the catalogue, read each agent's frontmatter   <- this file
  2. FILTER      cluster our task corpus, then match per cluster       <- next
  3. INTEGRATE   survivors go through the CTO cycle / `/forwhat`, and one that
                 lives earns a row in the routing table. Manual on purpose:
                 adopting a tool is a judgement call.

## Step 2 was attempted lexically and REFUTED by its own control (2026-08-08)

First cut ranked agents by word overlap between their description and our 1,113
task labels. Two rounds of it:

  * matching on `description` + `prompt` -> all 234 agents matched ~1,000 tasks.
    A multi-paragraph prompt contains two of ANY agent's words; the ranking was
    measuring prompt length.
  * matching on the 3-5 word `description` only -> 105 matched, best score 16
    tasks (1.4%). That looked like a real finding: "nothing in the catalogue fits
    our work, so general-purpose's 98% is correct."

The control killed it. Our OWN six agents — which we know fit, and which have
167 / 104 / 71 / 19 real invocations — scored 4, 3, 3, 1, 1 and **0**.
`cto-safety-reviewer` scored zero on 167 real runs. A matcher that cannot see
agents we demonstrably use cannot be trusted about agents we do not.

So the lexical filter is gone rather than shipped with a caveat, and step 2 is a
cheap LLM pass in the other direction: cluster the 1,113 labels into ~15 kinds of
work, then ask once per cluster which agents fit. Fifteen calls, not 234, and the
output IS the routing table. Do not rebuild the word-overlap version.

Usage:
  agent-catalog.py                 the inventory, grouped by source
  agent-catalog.py --duplicates    agents shipped under several names
  agent-catalog.py --workload      our deduped task labels, input for step 2
"""
import collections
import glob
import json
import os
import re
import sys

PLUGINS = os.path.expanduser("~/.claude/plugins")
OWN = os.path.expanduser("~/.claude/agents")
PROJECTS = os.path.expanduser("~/.claude/projects")
WORD = re.compile(r"[a-z][a-z-]{3,}")

# Terms that appear in most agent descriptions carry no signal — "use this agent
# proactively to review code" describes half the catalogue. Computed, not guessed:
# any term present in more than this share of agents is dropped before matching.
STOPBAND = 0.20


def frontmatter(path):
    """name / description / tools out of the YAML header. Hand-parsed: the field
    set is three keys and adding a yaml dependency for that is the kind of thing
    simplicity-first exists to stop."""
    try:
        text = open(path, encoding="utf-8", errors="ignore").read(8000)
    except OSError:
        return None
    if not text.startswith("---"):
        return None
    head = text.split("---", 2)
    if len(head) < 3:
        return None
    out, key = {}, None
    for line in head[1].split("\n"):
        m = re.match(r"^([a-zA-Z_]+):\s*(.*)$", line)
        if m:
            key, out[key] = m.group(1), m.group(2).strip().strip('"\'')
        elif key and line.startswith(("  ", "\t")):
            out[key] += " " + line.strip()
    return out if out.get("name") and out.get("description") else None


def catalogue():
    """Every agent definition on disk, deduped by name. 376 files collapse to
    ~188 agents: the marketplace tree and its cache hold the same definitions
    twice, and several marketplaces ship overlapping sets."""
    seen = {}
    for base, tag in ((OWN, "own"), (PLUGINS, "plugin")):
        for path in sorted(glob.glob(os.path.join(base, "**", "*.md"), recursive=True)):
            if tag == "plugin" and os.sep + "agents" not in path:
                continue
            fm = frontmatter(path)
            if not fm:
                continue
            name = fm["name"]
            if name in seen:          # first writer wins, and OWN is walked first
                continue
            rel = os.path.relpath(path, base).split(os.sep)
            seen[name] = {
                "name": name,
                "description": fm["description"],
                "tools": fm.get("tools", ""),
                "source": tag if tag == "own" else "/".join(rel[:2]),
            }
    return seen


def workload():
    """What we ACTUALLY delegate: the `description` of every Agent/Task call ever
    made. This is the whole point — the catalogue is scored against real history,
    not against an imagined roadmap."""
    out = []
    for path in glob.glob(os.path.join(PROJECTS, "*", "*.jsonl")):
        try:
            fh = open(path, encoding="utf-8", errors="ignore")
        except OSError:
            continue
        with fh:
            for line in fh:
                if '"Task"' not in line and '"Agent"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                cs = (d.get("message") or {}).get("content")
                if not isinstance(cs, list):
                    continue
                for c in cs:
                    if not isinstance(c, dict) or c.get("type") != "tool_use":
                        continue
                    if c.get("name") not in ("Task", "Agent"):
                        continue
                    # `description` ONLY, never `prompt`. First cut used both and
                    # every one of 234 agents matched ~1,000 of 1,113 tasks: a
                    # multi-paragraph prompt contains two of ANY agent's words, so
                    # the ranking was measuring prompt length. The 3-5 word label
                    # is the task, and it is what makes the overlap mean something.
                    dsc = str((c.get("input") or {}).get("description", "")).strip()
                    if dsc:
                        out.append((dsc, set(WORD.findall(dsc.lower()))))
    return out


def main():
    args = sys.argv[1:]
    cat = catalogue()

    if "--workload" in args:
        seen = collections.Counter(label for label, _ in workload())
        print(f"# {sum(seen.values()):,} delegated tasks, {len(seen):,} distinct labels")
        for label, n in seen.most_common():
            print(f"{n:>4}  {label}")
        return 0

    if "--duplicates" in args:
        by = collections.defaultdict(list)
        for a in cat.values():
            by[a["description"][:120]].append(a["name"])
        for names in sorted(by.values(), key=len, reverse=True):
            if len(names) > 1:
                print(f"{len(names)}x  {', '.join(sorted(names))}")
        return 0

    by_src = collections.defaultdict(list)
    for a in cat.values():
        by_src[a["source"]].append(a)
    uniq = len({a["description"][:120] for a in cat.values()})
    print(f"{len(cat)} distinct agent names, {uniq} distinct definitions "
          f"(the rest are the same agent shipped under several plugin names)\n")
    for src in sorted(by_src, key=lambda s: -len(by_src[s])):
        print(f"== {src}  ({len(by_src[src])})")
        for a in sorted(by_src[src], key=lambda a: a["name"]):
            print(f"   {a['name']:<44}{a['description'][:58]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
