#!/usr/bin/env python3
"""Our web-research pipeline vs the candidate search agent, on the same questions.

The previous bench (`agent-bench.py`) compared three variants of the SAME thing —
a prompt appended to a bare Claude — and never included our own incumbent. So it
answered "is a specialist prompt better than nothing", which was not the question.
Anton named the right one: our `web-research` against the thing we were going to
adopt, run clean, judged on the outcome AND on what each does internally.

Arms:

  baseline        nothing appended (what general-purpose is)
  agent           ~/.claude/agents/search-specialist.md — the candidate
  web-research    ~/.claude/skills/web-research/SKILL.md — our incumbent

All three get the same question, the same model, the same tools, the same turn
ceiling. Writes are denied; MCP servers are left intact (removing them was the bug
that invalidated the first bench).

## Why the oracle changed

The old oracle counted URLs in the answer text. It cannot tell a URL the model
fetched from one it wrote from memory, so it scores a hallucinated citation as a
win — and in the 48-call run two answers cited sources in a single turn, i.e.
without calling a tool at all.

This one reads the tool-call stream (`--output-format stream-json`). A domain is
GROUNDED only if it passed through a tool call — its input or its result. A domain
that appears solely in the final answer is PHANTOM. That is the number that decides
this comparison; everything else is context for it.

Usage:
  research-bench.py [--runs 3] [--tasks 4] [--model M] [--dry-run]
  research-bench.py --summarize
"""
import collections
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUTDIR = os.path.join(REPO, "p5-loop", "agent-bench")
JSONL = os.path.join(OUTDIR, "research-bench-runs.jsonl")

# Same four tasks as the 48-call run, so the two benches are readable against each
# other. They are real delegated work from the "Verify claims with research" cluster.
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
# Agent/Task are denied, and that is not a fairness shortcut — it removes a
# harness artifact. Anton's global CLAUDE.md makes delegation mandatory, so a
# headless run obeys it, hands the question to a sub-agent, and ends its turn
# with "the agent is running now; I'll wait for the completion notification."
# In `-p` mode that notification never arrives, so the recorded "answer" is a
# 281-character remark about delegating. Every arm must answer in its own
# context or the bench measures the harness.
DENY = ["Write", "Edit", "NotebookEdit", "Agent", "Task", "Monitor"]
URL = re.compile(r"https?://([A-Za-z0-9.-]+)")
WEBBY = re.compile(r"search|fetch|tavily|searxng|jina|consensus|crawl|impersonate|WebFetch|WebSearch", re.I)


def strip_frontmatter(path):
    text = open(path, encoding="utf-8").read()
    if text.startswith("---"):
        text = text.split("---", 2)[2]
    return re.sub(r"<!--.*?-->", "", text, flags=re.S).strip()


def domains(blob):
    return {d.lower().removeprefix("www.") for d in URL.findall(blob)}


def one(task, sys_p, model):
    """Run one call and derive the oracle from the tool stream, not from the prose."""
    cc = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
    # bypassPermissions, because the default mode denied Bash and both the baseline
    # and the web-research arm answered "I don't have permission to access external
    # web sources" while the agent arm — which happened to reach for WebSearch —
    # sailed through. Our own pipeline runs on Bash (`ssh hermes-ts`, fetch_ladder.py),
    # so an asymmetric permission gate crippled precisely the incumbent under test.
    # Writes stay denied at the tool level; the tasks are read-only research.
    cmd = [cc, "-p", task, "--model", model, "--output-format", "stream-json",
           "--verbose", "--max-turns", "24", "--permission-mode", "bypassPermissions",
           "--disallowedTools", *DENY]
    if sys_p:
        cmd += ["--append-system-prompt", sys_p]
    # CC_BENCH stops the session-end synthesizer from writing a diary entry for
    # every call. Without it, one 36-call run put 79 notes about its own test
    # questions into Anton's episodic memory (cleaned 2026-08-09).
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    env["CC_BENCH"] = "1"
    t0 = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=1200, env=env)
    except subprocess.TimeoutExpired:
        return {"error": "timeout"}
    secs = time.time() - t0

    rows = []
    for line in p.stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    # A failed call must say WHY. The first version returned None and the run
    # printed an `x`; the web-research arm failed once and I read the silence as
    # "our pipeline cannot run in a single context by design" — a conclusion the
    # very next probe refuted (it ran fine: 9 turns, SearXNG + 4 page fetches).
    # A drop that carries no reason invites you to invent one.
    result = next((r for r in rows if r.get("type") == "result"), None)
    if result is None:
        return {"error": "no result row", "stderr": (p.stderr or "")[-400:],
                "rc": p.returncode, "secs": round(secs, 1)}
    if result.get("is_error"):
        return {"error": result.get("subtype") or "is_error",
                "detail": str(result.get("result"))[:400], "secs": round(secs, 1)}

    # Every URL the model actually touched: tool inputs (what it asked for) and
    # tool results (what came back). Both count as "the model saw this page exist".
    seen, calls = set(), collections.Counter()
    for r in rows:
        msg = r.get("message")
        if not isinstance(msg, dict):   # some stream rows carry `message` as a plain string
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for c in content:
            if c.get("type") == "tool_use":
                calls[c.get("name", "?")] += 1
                seen |= domains(json.dumps(c.get("input") or {}))
            elif c.get("type") == "tool_result":
                seen |= domains(json.dumps(c.get("content"))[:200_000])

    text = result.get("result", "") or ""
    cited = domains(text)
    return {
        "cited": len(cited),
        "grounded": len(cited & seen),
        "phantom": len(cited - seen),
        "phantom_domains": sorted(cited - seen)[:6],
        "web_calls": sum(v for k, v in calls.items() if WEBBY.search(k)),
        "tools": dict(calls),
        "contradiction": bool(re.search(
            r"contradict|conflict|disagree|inconsisten|differs? from|but another",
            text, re.I)),
        "out": (result.get("usage") or {}).get("output_tokens", 0),
        "cost": result.get("total_cost_usd", 0.0),
        "turns": result.get("num_turns", 0),
        "secs": round(secs, 1),
        "chars": len(text),
        # Keep the answer. The first smoke run reported "0 cited domains" for all
        # three arms on answers thousands of characters long, and there was no way
        # to tell an arm that cited nothing from an oracle that could not see the
        # citations. A metric you cannot audit against the artifact is not evidence.
        "text": text[:6000],
    }


def report(got):
    ok = {k: [r for r in v if r and "grounded" in r] for k, v in got.items()}
    bad = {k: [r for r in v if r and "error" in r] for k, v in got.items()}
    # An arm whose every call failed must still get a column. Filtering on `ok`
    # alone would delete it from the table, and a missing column reads as "not
    # tested" rather than "tested and it fell over every time".
    keys = [k for k in ("baseline", "agent", "web-research") if got.get(k)]
    if not keys:
        print("no parsed runs")
        return

    def line(lbl, fn):
        print(f"{lbl:<28}" + "".join(f"{str(fn(ok[k])):>14}" for k in keys))

    med = lambda v, f: round(statistics.median([f(x) for x in v]), 1)
    print()
    print(f"{'':<28}" + "".join(f"{k:>14}" for k in keys))
    line("n", len)
    print(f"{'failed calls':<28}" + "".join(f"{len(bad.get(k, [])):>14}" for k in keys))
    line("runs with 0 sources", lambda v: f"{sum(1 for x in v if x['cited'] == 0)}/{len(v)}")
    line("runs with a PHANTOM cite", lambda v: f"{sum(1 for x in v if x['phantom'] > 0)}/{len(v)}")
    line("mean GROUNDED domains", lambda v: round(statistics.mean(x["grounded"] for x in v), 2))
    line("mean phantom domains", lambda v: round(statistics.mean(x["phantom"] for x in v), 2))
    line("mean web tool calls", lambda v: round(statistics.mean(x["web_calls"] for x in v), 1))
    line("contradiction flagged", lambda v: f"{sum(1 for x in v if x['contradiction'])}/{len(v)}")
    line("median turns", lambda v: med(v, lambda x: x["turns"]))
    line("median seconds", lambda v: med(v, lambda x: x["secs"]))
    line("median answer chars", lambda v: int(med(v, lambda x: x["chars"])))
    line("total notional $", lambda v: round(sum(x["cost"] for x in v), 2))
    print()
    for k in keys:
        tot = collections.Counter()
        for x in ok[k]:
            tot.update(x["tools"])
        top = ", ".join(f"{n.split('__')[-1]}×{c}" for n, c in tot.most_common(5))
        print(f"  {k:<13} tools: {top or '(none)'}")
    for k in keys:
        for e in bad.get(k, []):
            print(f"  {k:<13} FAILED: {e.get('error')} — {str(e.get('detail') or e.get('stderr') or '')[:160]}")
    ph = [(k, d) for k in keys for x in ok[k] for d in x["phantom_domains"]]
    if ph:
        print("\n  phantom domains cited without ever being fetched:")
        for k in keys:
            got_ = sorted({d for kk, d in ph if kk == k})
            if got_:
                print(f"    {k:<13} {', '.join(got_[:8])}")


def summarize():
    got = collections.defaultdict(list)
    for line in open(JSONL, encoding="utf-8"):
        row = json.loads(line)
        got[row["arm"]].append(row["r"])
    print(f"{sum(len(v) for v in got.values())} calls on record")
    report(got)
    return 0


def main():
    a = sys.argv[1:]
    if "--summarize" in a:
        return summarize()
    runs = int(next((a[i + 1] for i, x in enumerate(a) if x == "--runs"), 3))
    ntask = int(next((a[i + 1] for i, x in enumerate(a) if x == "--tasks"), 4))
    model = next((a[i + 1] for i, x in enumerate(a) if x == "--model"), "claude-haiku-4-5")

    arms = [
        ("baseline", None),
        ("agent", strip_frontmatter(os.path.expanduser("~/.claude/agents/search-specialist.md"))),
        ("web-research", strip_frontmatter(os.path.expanduser("~/.claude/skills/web-research/SKILL.md"))),
    ]
    tasks = TASKS[:ntask]
    for label, sp in arms:
        print(f"  {label:<14}{len(sp or ''):>7,} chars appended")
    print(f"{len(tasks)} tasks x {runs} runs x {len(arms)} arms = "
          f"{len(tasks)*runs*len(arms)} calls on {model}")
    if "--dry-run" in a:
        return 0

    os.makedirs(OUTDIR, exist_ok=True)
    log = open(JSONL, "a", buffering=1, encoding="utf-8")
    got = collections.defaultdict(list)
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
