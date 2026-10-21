#!/usr/bin/env python3
"""jev-skill-suggestion trial verdict — what the mod adds per prompt vs what it saves per session.

Usage: jev-trial-verdict.py [SINCE_ISO]     (default: 2026-10-02T11:00:00+00:00, the suggest-mode start)

Reads main-loop transcript records stamped at/after SINCE in sessions where the mod was
active (its `[jev-skill-suggestion]` lines) and prints the four numbers the verdict rule needs.
Replaces the 2026-09-21 baseline script, which truncated its sample (an early `return`
inside the directory walk) and compared a metric dominated by project mix.

Verdict rule for the suggest-mode week (pre-registered 2026-10-02, due 2026-10-09):
  KEEP   if median added chars per suggestion < 1000 with zero full bodies (the name-only block
         is ~770 chars by itself — the first threshold, 600, was set before one was seen)
         AND suggested -> Skill-tool-invoked in the same session >= 20%
         (the skill-suggest hook's best month was 19%);
  REMOVE otherwise: delete ~/.claude/skills/jev-skill-suggestion, drop the pluginConfigs
         entry and CLAUDE_CODE_ENABLE_FUNCTION_HOOKS, fall back to the skills prune.
"""
import datetime
import glob
import json
import os
import re
import statistics
import sys

since = datetime.datetime.fromisoformat(sys.argv[1] if len(sys.argv) > 1 else "2026-10-02T11:00:00+00:00")
if since.tzinfo is None:
    since = since.replace(tzinfo=datetime.timezone.utc)
root = os.path.expanduser("~/.claude/projects")
NAME = re.compile(r"Relevant to the current request: ([a-zA-Z0-9:_-]+)")
SKILL_CALL = re.compile(r'"name":"Skill","input":\{"skill":"([^"]+)"')
MOD_LINE = '"content":"jev-skill-suggestion: [jev-skill-suggestion]'
STAMP = re.compile(r'"timestamp":"([0-9T:.-]+)Z"')

sessions = prompts = suggestions = followed = 0
added, latency, listing = [], [], []
for path in glob.glob(os.path.join(root, "*", "*.jsonl")):
    if os.path.basename(path).startswith("agent-") or os.path.getmtime(path) < since.timestamp():
        continue
    # Judge by each record's own timestamp, not the session's start: a resumed session keeps
    # an old first record, and the long-lived sessions are the ones that carry most prompts.
    lines = []
    for line in open(path, errors="replace"):
        stamp = STAMP.search(line)
        if stamp and datetime.datetime.fromisoformat(stamp.group(1) + "+00:00") >= since:
            lines.append(line)
    text = "".join(lines)
    if MOD_LINE not in text:  # the mod's own system record, not a quote of it in a tool result
        continue
    sessions += 1
    prompts += text.count(MOD_LINE + " jev: ")
    latency += [int(ms) for ms in re.findall(r"\[jev-skill-suggestion\] jev: [^\"\\]{0,200}?(\d+)ms", text)]
    used = {name.split(":")[-1] for name in SKILL_CALL.findall(text)}
    for line in lines:
        if '"skill_listing"' in line:
            try:
                att = json.loads(line).get("attachment") or {}
                if att.get("isInitial"):
                    listing.append(len(str(att.get("content"))))
            except ValueError:
                pass
        if "skill_relevance" not in line or '"hook_additional_context"' not in line:
            continue
        try:
            content = (json.loads(line).get("attachment") or {}).get("content")
        except ValueError:
            continue
        for part in content if isinstance(content, list) else [content]:
            if isinstance(part, str) and "skill_relevance" in part:
                suggestions += 1
                added.append(len(part))
                name = NAME.search(part)
                if name and name.group(1).split(":")[-1] in used:
                    followed += 1

if not sessions:
    sys.exit(f"no mod-active records since {since.isoformat()} — nothing to judge yet")
share = lambda a, b: f"{a}/{b} = {a / b * 100:.0f}%" if b else "n/a"
print(f"since {since.isoformat()} · mod-active sessions: {sessions} · classified prompts: {prompts}")
print(f"suggestions: {share(suggestions, prompts)} of prompts")
if added:
    print(f"added chars per suggestion: median {int(statistics.median(added))} · p90 {sorted(added)[int(len(added) * .9)]} · total {sum(added)}"
          f" · full bodies (>1000 chars, i.e. still content mode): {sum(1 for a in added if a > 1000)}")
if listing:
    print(f"listing withheld per session: median {int(statistics.median(listing))} chars · total saved {sum(listing)}")
print(f"suggested -> Skill tool invoked in the same session: {share(followed, suggestions)}")
if latency:
    print(f"classifier latency ms: median {int(statistics.median(latency))} · p90 {sorted(latency)[int(len(latency) * .9)]}")
