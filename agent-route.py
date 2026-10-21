#!/usr/bin/env python3
"""Step 2: which agent should each KIND of our work go to?

Step 1 (`agent-catalog.py`) inventoried 234 agent names / 176 distinct
definitions. Its lexical filter was built and thrown away — the control showed it
scored our own most-used agents at zero. This is the replacement, run in the
opposite direction:

    cluster the 1,113 tasks we really delegated  ->  ask once per cluster
    which of the 176 agents fits                 ->  the answer IS a routing table

~15 model calls instead of 234, and the unit of the question is a kind of work
rather than a keyword, which is what the lexical version could not see.

The output is a PROPOSAL. Nothing is adopted here — a row earns its place only
after the CTO cycle or `/forwhat`, per the three-step plan in agent-catalog.py.

Usage:
  agent-route.py                    full pass (taxonomy + per-cluster matching)
  agent-route.py --model M          default claude-haiku-4-5
  agent-route.py --dry-run          print what would be sent, call nothing
"""
import collections
import importlib.util
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
OUT = os.path.join(REPO, "p5-loop", "agent-routing-proposal.md")
_spec = importlib.util.spec_from_file_location("ac", os.path.join(HERE, "agent-catalog.py"))
ac = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ac)

N_CLUSTERS = 15


def ask(prompt, model, schema_hint):
    """One headless call. Same invocation shape as rule-bench.py: absolute path
    (launchd PATH has no ~/.local/bin), no tools, no MCP — this is a judgement
    call over text, and a tool-enabled agent would wander off and read files."""
    cc = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
    cmd = [cc, "-p", prompt + "\n\n" + schema_hint, "--model", model,
           "--output-format", "json", "--max-turns", "1", "--tools", "",
           "--mcp-config", '{"mcpServers":{}}', "--strict-mcp-config"]
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=300, env=env)
    obj = next((json.loads(l) for l in p.stdout.splitlines() if l.strip().startswith("{")), None)
    if obj is None or obj.get("is_error"):
        raise RuntimeError((obj or {}).get("result") or p.stderr[:300])
    text = obj.get("result", "")
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise RuntimeError("no JSON object in reply: " + text[:200])
    return json.loads(text[start:end + 1]), obj.get("total_cost_usd", 0.0)


def taxonomy(labels, model):
    sample = [l for l, _ in labels[:400]]
    prompt = (
        f"Here are {len(sample)} short task labels. Each one was a task handed to an AI "
        f"sub-agent by a solo operator who runs product, marketing and research work "
        f"across several software projects.\n\n" + "\n".join("- " + s for s in sample) +
        f"\n\nGroup this work into exactly {N_CLUSTERS} kinds. A kind is a type of WORK "
        f"('verify a factual claim on the open web'), never a topic ('China'). Name each "
        f"kind in 2-4 words and give one sentence saying what the work involves.")
    return ask(prompt, model, 'Reply with ONLY JSON: {"clusters":[{"name":"...","what":"..."}]}')


def assign(labels, clusters, model):
    """Every label into exactly one cluster, in batches — a 1,100-line prompt gets
    lossy answers, and a truncated assignment silently shrinks the corpus."""
    names = [c["name"] for c in clusters]
    counts = collections.Counter()
    witness = collections.defaultdict(list)
    batch = 120
    for i in range(0, len(labels), batch):
        chunk = labels[i:i + batch]
        prompt = ("Assign each task label to exactly one kind of work.\n\nKINDS:\n" +
                  "\n".join(f"- {c['name']}: {c['what']}" for c in clusters) +
                  "\n\nLABELS:\n" + "\n".join(f"{j}. {l}" for j, (l, _) in enumerate(chunk)))
        got, _ = ask(prompt, model,
                     'Reply with ONLY JSON: {"assign":{"0":"kind name","1":"kind name",...}} '
                     'covering EVERY index. Use kind names exactly as written.')
        for j, (label, n) in enumerate(chunk):
            k = got.get("assign", {}).get(str(j))
            if k not in names:
                continue
            counts[k] += n
            if len(witness[k]) < 3:
                witness[k].append(label)
    return counts, witness


def match(cluster, examples, agents, model):
    listing = "\n".join(f"- {a['name']}: {a['description'][:150]}" for a in agents)
    prompt = (
        f"KIND OF WORK: {cluster['name']} — {cluster['what']}\n"
        f"Real examples of it:\n" + "\n".join("  - " + e for e in examples) +
        f"\n\nHere is every specialised agent available:\n{listing}\n\n"
        "Which of these agents, if any, would genuinely do this kind of work BETTER than a "
        "general-purpose agent with the same tools? Be strict: open-ended research and "
        "analysis is what a general-purpose agent is already good at, so name an agent only "
        "when its specialisation is the actual bottleneck. Naming none is a valid answer and "
        "is expected for most kinds.")
    return ask(prompt, model,
               'Reply with ONLY JSON: {"picks":[{"agent":"exact name","why":"one sentence"}],'
               '"verdict":"general-purpose is right here" or "route it"}')


def main():
    args = sys.argv[1:]
    model = next((args[i + 1] for i, a in enumerate(args) if a == "--model"), "claude-haiku-4-5")
    cat = ac.catalogue()
    agents = list({a["description"][:120]: a for a in cat.values()}.values())
    labels = collections.Counter(l for l, _ in ac.workload()).most_common()
    print(f"{sum(n for _, n in labels):,} delegated tasks / {len(labels):,} distinct labels")
    print(f"{len(cat)} agent names -> {len(agents)} distinct definitions to choose from")
    if "--dry-run" in args:
        print(f"would make 1 + {-(-len(labels)//120)} + {N_CLUSTERS} calls on {model}")
        return 0

    spent = 0.0
    tax, c = taxonomy(labels, model); spent += c
    clusters = tax["clusters"]
    print(f"\ntaxonomy: {len(clusters)} kinds of work")
    counts, witness = assign(labels, clusters, model)
    print(f"assigned {sum(counts.values()):,} of {sum(n for _, n in labels):,} tasks\n")

    rows = []
    for cl in sorted(clusters, key=lambda c: -counts[c["name"]]):
        n = counts[cl["name"]]
        if not n:
            continue
        res, c = match(cl, witness[cl["name"]] or ["(no example captured)"], agents, model)
        spent += c
        picks = [p for p in res.get("picks", []) if p.get("agent") in cat]
        rows.append((cl, n, picks, res.get("verdict", ""), witness[cl["name"]]))
        head = ", ".join(p["agent"] for p in picks) or "— general-purpose"
        print(f"{n:>5}  {cl['name']:<34} -> {head}")

    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write("# Agent routing — proposal, not adopted\n\n")
        fh.write(f"Generated by `scripts/p5/agent-route.py` on {model}, "
                 f"${spent:.4f}. {sum(counts.values()):,} real delegated tasks, "
                 f"{len(agents)} distinct agent definitions.\n\n")
        fh.write("A row is a CANDIDATE. It earns adoption only after the CTO cycle or "
                 "`/forwhat`, then a line in the routing table.\n\n")
        fh.write("| tasks | kind of work | candidate agent | why | a real task |\n")
        fh.write("|---:|---|---|---|---|\n")
        for cl, n, picks, verdict, wit in rows:
            agent = ", ".join(f"`{p['agent']}`" for p in picks) or "*general-purpose*"
            why = "; ".join(p["why"] for p in picks) or verdict
            fh.write(f"| {n} | {cl['name']} | {agent} | {why[:120]} | {(wit or [''])[0][:44]} |\n")
    print(f"\n${spent:.4f} -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
