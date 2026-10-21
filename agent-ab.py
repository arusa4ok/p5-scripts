#!/usr/bin/env python3
"""Does this agent beat plain general-purpose? A/B with a blind pairwise judge.

`research-bench.py` could count domains, because web research has a countable
artefact. Design work and market analysis do not: there is no honest scalar for
"is this a better wireframe". Counting words, headings or bullet points measures
verbosity, which is the failure mode these benches keep falling into.

So the oracle here is a **blind pairwise judge**: a fresh Claude sees the task and
two anonymised answers, in an order shuffled per pair, and picks the one that does
the job better. It never learns which arm is which, and the A/B position is
recorded so a positional bias would show up as a suspiciously even split by slot
rather than by arm.

Alongside it, three counts that are facts about the artefact rather than opinions:
concrete quantities, named standards (WCAG/ARIA/contrast for design; a named
source or a stated assumption for analysis), and whether the answer hedges instead
of deciding.

  agent-ab.py --agent ui-ux-designer [--runs 3] [--model sonnet] [--dry-run]
  agent-ab.py --agent startup-analyst --summarize
"""
import collections
import json
import os
import random
import re
import shutil
import statistics
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUTDIR = os.path.join(REPO, "p5-loop", "agent-bench")

# Real work, rewritten as self-contained prompts. Taken from the clusters these
# agents were routed for: the design ones are the secrets-shop mockup batch, the
# analyst ones are the competitor/positioning and unit-economics work.
TASKS = {
    "ui-ux-designer": [
        "Design the checkout page for a small store selling digital goods. Give the "
        "layout, the component hierarchy, every state (loading, error, empty, "
        "success), and the accessibility requirements.",
        "Design an age-gate modal for a store that must verify 18+. Cover the "
        "interaction, what happens when the visitor refuses, and how it avoids "
        "nagging a returning visitor while staying accessible.",
        "Design the empty state for a product listing page when a filter returns "
        "nothing. Give the copy, the layout, and what to show instead of nothing.",
    ],
    "startup-analyst": [
        "Adapty vs RevenueCat for a solo founder shipping a mobile subscription app: "
        "compare positioning and pricing, and say which one fits and why.",
        "Size the market for an AI-assisted job-application tool aimed at mid-career "
        "marketing professionals relocating internationally. TAM/SAM/SOM, with every "
        "assumption stated.",
        "A B2B SaaS charges $29/mo, loses 4% of customers a month, and pays $60 to "
        "acquire one. Is this viable? Show the unit economics and what must change.",
    ],
}
DENY = ["Write", "Edit", "NotebookEdit", "Agent", "Task", "Monitor"]
QUANT = re.compile(r"\b\d+(?:[.,]\d+)?\s*(?:%|px|rem|pt|ms|s\b|:1|USD|\$|€|k\b|m\b|x\b)|\$\s?\d")
STANDARD = re.compile(r"WCAG|ARIA|aria-|contrast ratio|focus (?:ring|state|order)|keyboard|"
                      r"screen reader|tab order|role=|alt text|CAC|LTV|churn|TAM|SAM|SOM|"
                      r"payback|gross margin|ARPU", re.I)
HEDGE = re.compile(r"\bit depends\b|\byou (?:may|might|could) want\b|\bconsider (?:whether|if)\b|"
                   r"\bthere is no one[- ]size\b|\bdepends on your\b", re.I)


def body(path):
    text = open(path, encoding="utf-8").read()
    if text.startswith("---"):
        text = text.split("---", 2)[2]
    return re.sub(r"<!--.*?-->", "", text, flags=re.S).strip()


def call(prompt, sys_p, model, turns=20):
    cc = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
    cmd = [cc, "-p", prompt, "--model", model, "--output-format", "json",
           "--max-turns", str(turns), "--permission-mode", "bypassPermissions",
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
        # 25 min per call. 900s was not enough: startup-analyst timed out on the
        # market-sizing task while its baseline took 198s and 18 turns against a
        # 20-turn cap. These tasks pull live pricing lookups, so they run long in
        # both arms — a timeout there would score the agent as a failure for being
        # thorough, which is the opposite of what the bench is asking.
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=1500, env=env)
    except subprocess.TimeoutExpired:
        return None, {"error": "timeout"}
    obj = next((json.loads(l) for l in p.stdout.splitlines() if l.strip().startswith("{")), None)
    if obj is None:
        return None, {"error": "no json", "stderr": (p.stderr or "")[-300:]}
    if obj.get("is_error"):
        return None, {"error": obj.get("subtype") or "is_error"}
    return obj.get("result", "") or "", {
        "secs": round(time.time() - t0, 1),
        "turns": obj.get("num_turns", 0),
        "cost": obj.get("total_cost_usd", 0.0),
        "chars": len(obj.get("result", "") or ""),
    }


def counts(text):
    return {
        "quantities": len(QUANT.findall(text)),
        "standards": len(set(m.lower() for m in STANDARD.findall(text))),
        "hedges": len(HEDGE.findall(text)),
    }


JUDGE = """You are judging two answers to the same task. You do not know who wrote them.

TASK
{task}

ANSWER 1
{a}

ANSWER 2
{b}

Which answer does the job better — more specific, more decided, more directly usable
by someone who has to act on it today? Padding, restated requirements and hedging are
worse, not better. Reply with ONLY a JSON object:
{{"winner": 1 or 2, "why": "one short sentence"}}"""


def judge(task, a, b, model):
    out, _ = call(JUDGE.format(task=task, a=a[:9000], b=b[:9000]), None, model, turns=1)
    if not out:
        return None
    m = re.search(r'\{.*\}', out, re.S)
    try:
        return json.loads(m.group(0)) if m else None
    except json.JSONDecodeError:
        return None


def main():
    a = sys.argv[1:]
    agent = next((a[i + 1] for i, x in enumerate(a) if x == "--agent"), None)
    if not agent:
        sys.exit("agent-ab: --agent NAME is required")
    jsonl = os.path.join(OUTDIR, f"ab-{agent}.jsonl")
    if "--summarize" in a:
        return summarize(agent, jsonl)
    runs = int(next((a[i + 1] for i, x in enumerate(a) if x == "--runs"), 3))
    model = next((a[i + 1] for i, x in enumerate(a) if x == "--model"), "claude-sonnet-5")
    tasks = TASKS[agent]
    sys_p = body(os.path.expanduser(f"~/.claude/agents/{agent}.md"))
    print(f"{agent}: {len(sys_p):,} chars appended · {len(tasks)} tasks x {runs} runs "
          f"x 2 arms + {len(tasks)*runs} judge calls = {len(tasks)*runs*3} calls on {model}")
    if "--dry-run" in a:
        return 0

    os.makedirs(OUTDIR, exist_ok=True)
    log = open(jsonl, "a", buffering=1, encoding="utf-8")
    rnd = random.Random(20260809)          # fixed seed: the shuffle must be reproducible
    for ti, task in enumerate(tasks):
        for ri in range(runs):
            base, bm = call(task, None, model)
            ag, am = call(task, sys_p, model)
            print("." if base else "x", end="", flush=True)
            print("." if ag else "x", end="", flush=True)
            row = {"task": ti, "run": ri, "model": model,
                   "baseline": {**bm, **(counts(base) if base else {}), "text": (base or "")[:6000]},
                   "agent": {**am, **(counts(ag) if ag else {}), "text": (ag or "")[:6000]}}
            if base and ag:
                # Shuffle which arm is shown first, and record it. If the judge has a
                # positional bias, the wins split by SLOT instead of by arm and the
                # result is readable as bias rather than as a verdict.
                agent_first = rnd.random() < 0.5
                first, second = (ag, base) if agent_first else (base, ag)
                v = judge(task, first, second, model)
                if v:
                    winner_is_agent = (v.get("winner") == 1) == agent_first
                    row["judge"] = {"winner": "agent" if winner_is_agent else "baseline",
                                    "slot": v.get("winner"), "agent_first": agent_first,
                                    "why": str(v.get("why"))[:200]}
                print("J" if v else "j", end="", flush=True)
            log.write(json.dumps(row) + "\n")
    log.close()
    print()
    return summarize(agent, jsonl)


def summarize(agent, jsonl):
    rows = [json.loads(l) for l in open(jsonl, encoding="utf-8")]
    print(f"\n{agent} — {len(rows)} pairs on record")
    for k in ("quantities", "standards", "hedges", "chars", "secs", "turns"):
        vals = {arm: [r[arm][k] for r in rows if r[arm].get(k) is not None] for arm in ("baseline", "agent")}
        if not vals["baseline"] or not vals["agent"]:
            continue
        print(f"  {k:<12} baseline {statistics.mean(vals['baseline']):>8.1f}   "
              f"agent {statistics.mean(vals['agent']):>8.1f}")
    j = [r["judge"] for r in rows if r.get("judge")]
    if j:
        w = collections.Counter(x["winner"] for x in j)
        slot = collections.Counter(x["slot"] for x in j)
        print(f"\n  blind judge: agent {w['agent']} · baseline {w['baseline']} of {len(j)}")
        print(f"  by slot (bias check): first {slot[1]} · second {slot[2]}")
        for x in j[:4]:
            print(f"    → {x['winner']:<9} {x['why'][:110]}")
    cost = sum(r[arm].get("cost", 0) for r in rows for arm in ("baseline", "agent"))
    print(f"\n  ${cost:.2f} notional")
    return 0


if __name__ == "__main__":
    sys.exit(main())
