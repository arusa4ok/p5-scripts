#!/usr/bin/env python3
"""Head-to-head: does a specialist agent actually beat general-purpose?

Step 3 of the agent mechanism. Steps 1-2 (`agent-catalog.py`, `agent-route.py`)
produced candidates; this is the part that decides whether a candidate keeps its
routing row. Nothing here trusts a description — the routing pass named
`search-specialist` for 16% of our work on the strength of what it CLAIMS to do,
and that is a hypothesis.

Three arms, identical task, identical tools:

  baseline  = the Claude Code system prompt alone (what general-purpose is)
  vendor    = plus the candidate EXACTLY as its author shipped it
  ours      = plus our adapted copy from ~/.claude/agents/

The vendor arm exists because Anton caught a real gap (2026-08-08). When grafting
`search-specialist` I replaced its "WebFetch Deep Dive" section with our extraction
ladder and $0-first search order — and asserted that was an improvement without
measuring it. That is the same move `verify-platform-facts` forbids, applied to my
own edit. Now the two versions run against each other and the answer is a number.

Same shape as `rule-bench.py`, one difference that matters: **tools are ON**. A
search agent cannot be tested with `--tools ""` — its whole claim is how it
searches, so a no-tools bench would measure prose about searching. That was the
first design here and it was wrong.

## The oracle

Counting words a model wrote about its own quality measures nothing. These are
countable properties of the artifact:

  * distinct domains cited   — the multi-source claim, falsifiable
  * total citations          — volume without diversity is one source repeated
  * contradiction flagged    — the specialist promises to surface disagreement
  * output tokens / seconds / $ — what the quality costs

Usage:
  agent-bench.py --agent search-specialist [--runs 2] [--tasks 2] [--model M]
  agent-bench.py --agent search-specialist --dry-run
  agent-bench.py --agent search-specialist --summarize   # table from the JSONL
"""
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import time

AGENTS = os.path.expanduser("~/.claude/agents")
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Real tasks, taken verbatim from the "Verify claims with research" cluster in
# p5-loop/agent-routing-proposal.md. Invented tasks would test the bench, not the
# agent — these are things we actually asked a sub-agent to do.
TASKS = [
    "China work permit for foreigners in 2026 — what are the current eligibility "
    "rules and the documented processing time? Cite sources.",
    "Citizenship by investment: which programmes are open in 2026, at what minimum "
    "investment? Cite sources.",
    "B200 GPU availability in the UAE — who actually sells or rents them, at what "
    "price? Cite sources.",
    "LinkedIn Recruiter Lite vs Sales Navigator in 2026 — current price and the "
    "concrete search limits of each. Cite sources.",
]
# NO --tools restriction and NO empty --mcp-config. The first version passed
# `--mcp-config {"mcpServers":{}} --strict-mcp-config`, which removed every MCP
# server — so ctx_fetch_and_index, searxng and tavily did not exist in the sandbox
# and the ladder arm could not have used its own ladder even if it wanted to. The
# bench would have "measured" our adaptation against tools it had deleted.
# Verified 2026-08-08: headless DOES expose the MCP servers when they are left alone.
DENY = ["Write", "Edit", "NotebookEdit"]
URL = re.compile(r"https?://([A-Za-z0-9.-]+)")


def body(path):
    """The agent definition minus its YAML frontmatter — the frontmatter is routing
    metadata for the harness, not instructions for the model."""
    text = open(path, encoding="utf-8").read()
    text = text.split("---", 2)[2] if text.startswith("---") else text
    # strip our provenance HTML comment so the two arms differ only in guidance
    return re.sub(r"<!--.*?-->", "", text, flags=re.S).strip()


def one(task, sys_p, model):
    cc = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
    cmd = [cc, "-p", task, "--model", model, "--output-format", "json",
           "--max-turns", "18", "--disallowedTools", *DENY]
    if sys_p:
        cmd += ["--append-system-prompt", sys_p]
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    t0 = time.time()
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=900, env=env)
    secs = time.time() - t0
    obj = next((json.loads(l) for l in p.stdout.splitlines() if l.strip().startswith("{")), None)
    if obj is None or obj.get("is_error"):
        return None
    text = obj.get("result", "") or ""
    domains = {d.lower().removeprefix("www.") for d in URL.findall(text)}
    return {
        "domains": len(domains),
        "citations": len(URL.findall(text)),
        "contradiction": bool(re.search(
            r"contradict|conflict|disagree|inconsisten|differs? from|but another",
            text, re.I)),
        "out": (obj.get("usage") or {}).get("output_tokens", 0),
        "cost": obj.get("total_cost_usd", 0.0),
        "secs": round(secs, 1),
        "turns": obj.get("num_turns", 0),
    }


def med(rows, k):
    vals = [r[k] for r in rows if r]
    return round(statistics.median(vals), 1) if vals else None


def report(got):
    """Print the comparison table. Takes {arm: [result|None]} — the same shape the
    live run holds and the shape --summarize rebuilds from the JSONL, so a run that
    died halfway is read exactly like one that finished."""
    ok = {k: [r for r in v if r] for k, v in got.items()}
    print()
    print(f"{'metric':<22}" + "".join(f"{k:>14}" for k in ok))
    for key, label in (("domains", "distinct domains"), ("citations", "citations"),
                       ("out", "output tokens"), ("turns", "turns"), ("secs", "seconds")):
        print(f"{label:<22}" + "".join(f"{str(med(ok[k], key)):>14}" for k in ok))
    print()
    for label in ok:
        rows = ok[label]
        flag = sum(1 for r in rows if r["contradiction"])
        print(f"{label:<10} contradiction {flag}/{len(rows)}   "
              f"${sum(r['cost'] for r in rows):.4f} notional   "
              f"{len(rows)}/{len(got[label])} parsed")


def summarize(agent):
    jsonl = os.path.join(REPO, "p5-loop", "agent-bench", f"{agent}-runs.jsonl")
    got = {}
    for line in open(jsonl, encoding="utf-8"):
        row = json.loads(line)
        got.setdefault(row["arm"], []).append(row["r"])
    n = sum(len(v) for v in got.values())
    print(f"{n} calls on record in {os.path.basename(jsonl)}")
    report(got)
    return 0


def main():
    a = sys.argv[1:]
    agent = next((a[i + 1] for i, x in enumerate(a) if x == "--agent"), None)
    runs = int(next((a[i + 1] for i, x in enumerate(a) if x == "--runs"), 2))
    ntask = int(next((a[i + 1] for i, x in enumerate(a) if x == "--tasks"), 2))
    model = next((a[i + 1] for i, x in enumerate(a) if x == "--model"), "claude-haiku-4-5")
    if not agent:
        sys.exit("agent-bench: --agent NAME is required")
    if "--summarize" in a:
        return summarize(agent)

    vendor_path = os.path.join(REPO, "p5-loop", "agent-bench", f"{agent}.vendor.md")
    arms = [("baseline", None)]
    if os.path.exists(vendor_path):
        arms.append(("vendor", body(vendor_path)))
    arms.append(("ours", body(os.path.join(AGENTS, f"{agent}.md"))))

    tasks = TASKS[:ntask]
    for label, sp in arms:
        print(f"  {label:<10}{len(sp or ''):>7,} chars appended")
    print(f"{len(tasks)} tasks x {runs} runs x {len(arms)} arms = "
          f"{len(tasks)*runs*len(arms)} calls on {model}; MCP left intact, "
          f"writes denied ({', '.join(DENY)})")
    if "--dry-run" in a:
        return 0

    # Append every result the moment it lands. The first version accumulated in
    # memory and wrote once at the end: on 2026-08-08 a 48-call run was killed by
    # its own `timeout` on call 48 and all 47 finished results died with the
    # process — ~90 minutes of calls, nothing on disk. A long run must survive
    # its own death, and the waiter that reported "exit 0" could not tell the
    # difference.
    jsonl = os.path.join(REPO, "p5-loop", "agent-bench", f"{agent}-runs.jsonl")
    log = open(jsonl, "a", buffering=1, encoding="utf-8")
    got = {label: [] for label, _ in arms}
    for ti, t in enumerate(tasks):
        for ri in range(runs):
            for label, sp in arms:
                r = one(t, sp, model)
                got[label].append(r)
                log.write(json.dumps({"arm": label, "task": ti, "run": ri,
                                      "model": model, "r": r}) + "\n")
                print("." if r else "x", end="", flush=True)
    log.close()
    print()

    report(got)
    out = os.path.join(REPO, "p5-loop", "agent-bench", f"{agent}-latest.json")
    json.dump({"agent": agent, "model": model, "runs": runs, "tasks": tasks,
               "arms": {k: v for k, v in got.items()}}, open(out, "w"), indent=1)
    print(f"raw -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
