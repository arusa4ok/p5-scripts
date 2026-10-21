#!/usr/bin/env python3
"""Rule-effectiveness benchmark — does injecting a rule reduce code/tokens?
Usage: python3 rule-bench.py [--dry] [--model M] [--runs N]
Output: p5-loop/rule-bench/results/latest.{json,md}
"""
import json, os, re, sys, datetime, shutil, statistics, subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
RULES_DIR = os.path.expanduser("~/.claude/rules/learned")
OUT_DIR = os.path.join(REPO, "p5-loop", "rule-bench", "results")
DEFAULT_MODEL, DEFAULT_RUNS = "claude-haiku-4-5", 3

PRICES = {
    "claude-haiku-4-5":  {"in": 1.0,  "out": 5.0},
    "claude-sonnet-4-6": {"in": 3.0,  "out": 15.0},
    "claude-opus":       {"in": 15.0, "out": 75.0},
}

# ── tasks (5 fresh-code + 5 edit-style, same themes) ─────────────────────────
T = [
    ("email-validator",
     "Write a Python function `is_valid_email(s: str) -> bool` that checks "
     "whether a string is a syntactically valid email address.",
     "Here is an existing email validator:\n```python\nimport re\ndef is_valid_email(s):\n"
     "    return bool(re.match(r'[^@]+@[^@]+\\.[^@]+', s))\n```\n"
     "Add a check that rejects emails longer than 254 characters. Change only what is necessary."),
    ("debounce",
     "Write a JavaScript function `debounce(fn, delay)` that returns a debounced version "
     "of `fn` that fires only after `delay` ms of inactivity.",
     "Here is an existing debounce:\n```javascript\nfunction debounce(fn, delay) {\n"
     "  let t; return function(...a){clearTimeout(t);t=setTimeout(()=>fn(...a),delay);};\n}\n```\n"
     "Add an `immediate` boolean param (default false) that fires on the leading edge when true. "
     "Change only what is necessary."),
    ("csv-column-sum",
     "Write a Python function `csv_column_sum(path: str, col: str) -> float` that reads "
     "a CSV file and returns the numeric sum of the named column.",
     "Here is an existing CSV summer:\n```python\nimport csv\ndef csv_column_sum(path, col):\n"
     "    total=0.0\n    with open(path) as f:\n"
     "        for row in csv.DictReader(f): total+=float(row[col])\n    return total\n```\n"
     "Make it skip rows where the column value is empty string. Change only what is necessary."),
    ("countdown-timer",
     "Write a JavaScript function `countdown(seconds, onTick, onDone)` that calls "
     "`onTick(remaining)` every second and `onDone()` when finished.",
     "Here is an existing countdown:\n```javascript\nfunction countdown(seconds,onTick,onDone){\n"
     "  let r=seconds; const id=setInterval(()=>{onTick(r--);\n"
     "  if(r<0){clearInterval(id);onDone();}},1000);\n}\n```\n"
     "Return the interval ID so the caller can cancel. Change only what is necessary."),
    ("rate-limiter",
     "Write a Python class `RateLimiter(calls_per_second)` with method `allow()->bool` "
     "that returns True if the call is within the rate limit.",
     "Here is an existing rate limiter:\n```python\nimport time\nclass RateLimiter:\n"
     "  def __init__(self,cps): self.interval=1/cps; self._last=0.0\n"
     "  def allow(self):\n    n=time.monotonic()\n    if n-self._last>=self.interval:\n"
     "      self._last=n; return True\n    return False\n```\n"
     "Add a `burst` param (default 1) allowing that many consecutive calls before limiting. "
     "Change only what is necessary."),
]
TASKS_FRESH = [{"id": id_, "prompt": fresh} for id_, fresh, _ in T]
TASKS_EDIT  = [{"id": id_+"-edit", "prompt": edit} for id_, _, edit in T]

def load_all_rules():
    """Every always-loaded rule, concatenated — the stack as the model actually receives it.

    The per-rule arms below answer "does THIS rule earn its keep". They cannot answer the
    question Anton asked on 2026-08-07: what does the WHOLE always-loaded stack do to the same
    task, versus no rules at all. That is a different measurement — 11 rules plus soul.md is
    ~47.3k chars (~12k tokens) prepended to every session, and Anthropic's own Claude 5
    guidance (07-26) moved the opposite way, cutting >80% of their system prompt and loading
    by tree instead. Nobody has ever measured our side of that.
    """
    files = [os.path.join(RULES_DIR, f) for f in sorted(os.listdir(RULES_DIR)) if f.endswith(".md")]
    soul = os.path.expanduser("~/.claude/rules/soul.md")
    if os.path.exists(soul):
        files.append(soul)
    return "\n\n---\n\n".join(open(f, encoding="utf-8").read() for f in files)


ALL_RULES = [{"slug": "ALL-always-loaded", "file": None, "tasks": TASKS_FRESH + TASKS_EDIT}]

RULES = [
    {"slug": "simplicity-first", "file": "simplicity-first.md",  "tasks": TASKS_FRESH},
    {"slug": "surgical-changes", "file": "surgical-changes.md",   "tasks": TASKS_EDIT},
]

# ── helpers ───────────────────────────────────────────────────────────────────
def load_rule(fn): return open(os.path.join(RULES_DIR, fn), encoding="utf-8").read()

def extract_loc(text):
    blocks = re.findall(r"```[^\n]*\n(.*?)```", text, re.DOTALL)
    if not blocks: return None
    code = max(blocks, key=len)
    return len([l for l in code.splitlines()
                if l.strip() and not l.strip().startswith(("#","//","/*","*","'''", '"""'))])

def cost_usd(model, it, ot):
    p = PRICES.get(model, PRICES[DEFAULT_MODEL])
    return (it * p["in"] + ot * p["out"]) / 1_000_000

def med(v): return statistics.median(v) if v else None

def pct(base, wr):
    return round((wr - base) / base * 100, 1) if base and wr and base != 0 else None

def call_api(model, sys_p, user_p):
    # Runs through `claude -p` on the Max subscription (env -u ANTHROPIC_API_KEY so the
    # empty API key doesn't shadow the OAuth). Both arms share Claude Code's system prompt;
    # the only delta between them is `sys_p` (our rule) → the rule-effect is isolated.
    prompt = user_p + ("\n\nOutput ONLY the code in a single fenced block — "
                       "no prose, no tool calls, no file writes.")
    # Absolute path — a bare "claude" is unreachable whenever this runs outside an
    # interactive shell (launchd PATH has no ~/.local/bin). Same bug as report.py's
    # llm_fallback, which lost four weekly news syntheses to it.
    _cc = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
    cmd = [_cc, "-p", prompt, "--model", model, "--output-format", "json",
           "--max-turns", "1", "--tools", "", "--mcp-config", '{"mcpServers":{}}',
           "--strict-mcp-config"]
    if sys_p: cmd += ["--append-system-prompt", sys_p]
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=180, env=env)
    obj = next((json.loads(l) for l in p.stdout.splitlines() if l.strip().startswith("{")), None)
    if obj is None or obj.get("is_error"):
        raise RuntimeError((obj or {}).get("result") or p.stdout[:200] or p.stderr[:200])
    u = obj.get("usage", {}) or {}
    return obj.get("result", "") or "", u.get("input_tokens", 0), u.get("output_tokens", 0)

# ── benchmark ─────────────────────────────────────────────────────────────────
def run_bench(model, runs, rules=None):
    results = []
    for rule in (rules or RULES):
        rt = load_all_rules() if rule["file"] is None else load_rule(rule["file"])
        bl, rl = {"loc":[],"out":[],"in":[]}, {"loc":[],"out":[],"in":[]}
        # Per-task as well as pooled. The pooled median mixes fresh-code and edit tasks;
        # a rule can help on one shape and hurt on the other and the pool reads as "flat".
        per_task = []
        for task in rule["tasks"]:
            tb, tr = {"loc":[],"out":[],"in":[]}, {"loc":[],"out":[],"in":[]}
            for _ in range(runs):
                for pooled, this, sp in [(bl, tb, None), (rl, tr, rt)]:
                    txt, it, ot = call_api(model, sp, task["prompt"])
                    loc = extract_loc(txt)
                    for a in (pooled, this):
                        a["loc"].append(loc); a["out"].append(ot); a["in"].append(it)
            tvb = [x for x in tb["loc"] if x is not None]
            tvr = [x for x in tr["loc"] if x is not None]
            per_task.append({
                "task": task["id"],
                # n_loc_* < runs means some replies had no fenced block at all: a silently
                # dropped sample, not a zero. Print it so the median can be read honestly.
                "n_loc_baseline": len(tvb), "n_loc_with_rule": len(tvr), "runs": runs,
                "median_loc_baseline": med(tvb), "median_loc_with_rule": med(tvr),
                "loc_delta_pct": pct(med(tvb), med(tvr)),
                "median_out_tokens_baseline": med(tb["out"]), "median_out_tokens_with_rule": med(tr["out"]),
                "tokens_delta_pct": pct(med(tb["out"]), med(tr["out"])),
            })
        vbl = [x for x in bl["loc"] if x is not None]
        vrl = [x for x in rl["loc"] if x is not None]
        results.append({
            "per_task": per_task,
            "n_loc_baseline": len(vbl), "n_loc_with_rule": len(vrl),
            "n_calls_per_arm": len(bl["out"]),
            "rule": rule["slug"],
            "median_loc_baseline": med(vbl), "median_loc_with_rule": med(vrl),
            "loc_delta_pct": pct(med(vbl), med(vrl)),
            "median_out_tokens_baseline": med(bl["out"]), "median_out_tokens_with_rule": med(rl["out"]),
            "tokens_delta_pct": pct(med(bl["out"]), med(rl["out"])),
            "cost_usd_baseline":  round(cost_usd(model, sum(bl["in"]), sum(bl["out"])), 4),
            "cost_usd_with_rule": round(cost_usd(model, sum(rl["in"]), sum(rl["out"])), 4),
        })
    return results

# ── output ────────────────────────────────────────────────────────────────────
def write_results(results, model, runs, tasks_per_arm):
    os.makedirs(OUT_DIR, exist_ok=True)
    total = sum(r["cost_usd_baseline"] + r["cost_usd_with_rule"] for r in results)
    payload = {"generated": datetime.date.today().isoformat(), "model": model,
               # len(T) is the THEME count (5); an arm can run fresh-only, edit-only, or both,
               # so the honest figure is what this run actually executed. --all-rules uses all
               # 10 and the file claimed 5, which understates the sample by half.
               "runs_per_cell": runs, "tasks": tasks_per_arm, "rules": results,
               "total_cost_usd": round(total, 4),
               "note": "self-reported A/B bench; baseline=no rule in system prompt vs with_rule=rule text injected"}
    jpath = os.path.join(OUT_DIR, "latest.json")
    with open(jpath, "w") as f: json.dump(payload, f, indent=2)

    def v(x): return "n/a" if x is None else ("✅ leaner" if x<=-10 else ("👀 flat" if x<0 else "⚠️ no gain"))
    rows = ["| Rule | LOC base | LOC rule | LOC Δ% | Tok base | Tok rule | Tok Δ% | Verdict |",
            "|------|----------|----------|--------|----------|----------|--------|---------|"]
    for r in results:
        rows.append(f"| {r['rule']} | {r['median_loc_baseline'] or 'n/a'} | {r['median_loc_with_rule'] or 'n/a'} "
                    f"| {r['loc_delta_pct'] or 'n/a'}% | {r['median_out_tokens_baseline'] or 'n/a'} "
                    f"| {r['median_out_tokens_with_rule'] or 'n/a'} | {r['tokens_delta_pct'] or 'n/a'}% "
                    f"| {v(r['tokens_delta_pct'])} |")
    takeaways = [f"- `{r['rule']}`: LOC {r['loc_delta_pct']:+.1f}% · tokens {r['tokens_delta_pct']:+.1f}% → {v(r['tokens_delta_pct'])}"
                 if r['loc_delta_pct'] is not None else f"- `{r['rule']}`: insufficient data"
                 for r in results]
    detail = []
    for r in results:
        if not r.get("per_task"): continue
        detail += [f"\n## `{r['rule']}` — per task\n",
                   "| Task | LOC base | LOC rule | LOC Δ% | Tok base | Tok rule | Tok Δ% | code blocks b/r |",
                   "|------|----------|----------|--------|----------|----------|--------|-----------------|"]
        for t in r["per_task"]:
            d = lambda k: "n/a" if t[k] is None else f"{t[k]:+.1f}%" if k.endswith("pct") else t[k]
            detail.append(f"| {t['task']} | {d('median_loc_baseline')} | {d('median_loc_with_rule')} | {d('loc_delta_pct')} "
                          f"| {d('median_out_tokens_baseline')} | {d('median_out_tokens_with_rule')} | {d('tokens_delta_pct')} "
                          f"| {t['n_loc_baseline']}/{t['n_loc_with_rule']} of {t['runs']} |")
    md = (f"# Rule-bench — {payload['generated']}\nModel `{model}` · {runs} runs/cell · {tasks_per_arm} tasks\n\n"
          + "\n".join(rows) + f"\n\nTotal: **${total:.4f}**\n\n" + "\n".join(takeaways) + "\n"
          + "\n".join(detail) + "\n")
    mpath = os.path.join(OUT_DIR, "latest.md")
    with open(mpath, "w") as f: f.write(md)
    return jpath, mpath, total

# ── dry run ───────────────────────────────────────────────────────────────────
def dry_run(model, runs):
    print(f"=== rule-bench --dry ===\nModel: {model}  Runs: {runs}\n")
    for rule in RULES:
        txt = load_rule(rule["file"])
        print(f"Rule: {rule['slug']}  ({len(txt):,} chars from {rule['file']})")
    calls = len(RULES) * len(T) * runs * 2
    p = PRICES.get(model, PRICES[DEFAULT_MODEL])
    est = (calls * 500 * p["in"] + calls * 200 * p["out"]) / 1_000_000
    print(f"\nTasks: {len(T)} per rule ({len(RULES)} rules)")
    print(f"Calls: {calls} total  ({len(RULES)} rules × {len(T)} tasks × {runs} runs × 2 arms)")
    print(f"Est. : ~${est:.4f}  (rough: 500 in / 200 out tokens/call)\n")
    print("── Fresh-code tasks (simplicity-first) ──")
    for t in TASKS_FRESH: print(f"  [{t['id']}] {t['prompt'][:90]}…")
    print("\n── Edit-style tasks (surgical-changes) ──")
    for t in TASKS_EDIT:  print(f"  [{t['id']}] {t['prompt'][:90]}…")

# ── main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    args  = sys.argv[1:]
    model = next((args[i+1] for i,a in enumerate(args) if a=="--model" and i+1<len(args)), DEFAULT_MODEL)
    runs  = next((int(args[i+1]) for i,a in enumerate(args) if a=="--runs" and i+1<len(args)), DEFAULT_RUNS)
    if "--dry" in args: dry_run(model, runs); sys.exit(0)
    # --all-rules: one arm, the WHOLE always-loaded stack vs nothing, on all 10 tasks.
    # Answers "what does our rules tax buy", which no per-rule run can.
    if "--all-rules" in args:
        rt = load_all_rules()
        arms, tasks_per_arm = ALL_RULES, len(ALL_RULES[0]["tasks"])
        print(f"ALL-always-loaded: {len(rt):,} chars (~{len(rt)//4:,} tokens) prepended to "
              f"{tasks_per_arm} tasks × {runs} runs × 2 arms\n")
    else:
        arms, tasks_per_arm = RULES, max(len(r["tasks"]) for r in RULES)
    results = run_bench(model, runs, arms)
    jpath, mpath, total = write_results(results, model, runs, tasks_per_arm)
    print(f"JSON: {jpath}\nMD  : {mpath}\nCost: ${total:.4f}")
