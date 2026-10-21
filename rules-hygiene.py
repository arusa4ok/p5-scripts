#!/usr/bin/env python3
"""rules-hygiene — audit the always-on learned-rules set + corrections pipeline.

WHAT / WHY
  rule-intake-gate guards the ENTRY of new rules; nothing audits the standing set.
  14 rules / ~500 lines load into context EVERY session, so each carries a permanent
  token cost — and they can go stale, contradict, or duplicate silently. This is the
  missing PRUNE half.

STOLEN FROM (honest attribution)
  arteemg/autoswarm `online/reflector.py` — its second judge pass flags skillbook
  entries to remove (wrong / contradictory / vague / redundant) with a safety valve
  that refuses to wipe the whole book. We steal that pass.

IMPROVED OVER autoswarm
  1. PROPOSE, never silently delete — autoswarm removes in place; self-refine.md
     (arXiv:2603.07670, self-reinforcing error in reflective memory) says don't. Report
     only; the human applies.
  2. Deterministic analytics autoswarm lacks: context weight, age, the [[link]]/Boundary
     contradiction graph, orphans, and correction-candidate clustering vs the >=3 rule.
  3. Judge runs on the LOCAL model ($0) — defaults to OPENAI_API_BASE (Anton's :8080
     Qwen). --no-llm gives analytics-only; LLM failure degrades gracefully.

Usage:  python3 rules-hygiene.py [--no-llm] [--out PATH]
Exit 0 always (it's a report, never a gate).
"""
from __future__ import annotations
import json, os, re, subprocess, sys, urllib.request
from datetime import date
from pathlib import Path

HOME = Path.home()
RULES_DIR = HOME / ".claude/rules/learned"
MEM = HOME / ".claude/projects/-Users-rusakovanton/memory"
CORRECTIONS = MEM / "corrections.md"
CANDIDATES = HOME / ".claude/logs/correction-candidates.jsonl"
DEFAULT_OUT = HOME / "projects/cc-stack-2026/p5-loop/reports" / f"rules-hygiene-{date.today():%Y-%m-%d}.md"

LINK_RE = re.compile(r"\[\[([a-z0-9-]+)\]\]")
TITLE_RE = re.compile(r"^#\s+(.+)$", re.M)
# A Boundary section may be titled "## Boundary with neighbours", "## Boundary / Source",
# "## Boundary with `x`", etc. Match any heading that starts with "Boundary" (not just the
# "Boundary with" phrasing) so combined headings like self-refine's aren't false-flagged.
BOUNDARY_RE = re.compile(r"^#{2,}\s+Boundary\b", re.M)
TOK = lambda s: len(s) // 4  # rough est-tokens


def git_added(path: Path) -> str:
    """First-seen date of a file, or '—' if not in a git repo."""
    try:
        r = subprocess.run(
            ["git", "-C", str(path.parent), "log", "--diff-filter=A",
             "--follow", "--format=%as", "-1", "--", path.name],
            capture_output=True, text=True, timeout=10)
        return (r.stdout.strip().splitlines() or ["—"])[-1] or "—"
    except Exception:
        return "—"


def load_rules() -> list[dict]:
    rules = []
    for f in sorted(RULES_DIR.glob("*.md")):
        text = f.read_text(encoding="utf-8", errors="replace")
        title = (TITLE_RE.search(text) or [None, f.stem])[1] if TITLE_RE.search(text) else f.stem
        rules.append({
            "slug": f.stem,
            "title": title,
            "lines": text.count("\n") + 1,
            "tokens": TOK(text),
            "outlinks": sorted(set(LINK_RE.findall(text)) - {f.stem}),
            "has_boundary": bool(BOUNDARY_RE.search(text)),
            "added": git_added(f),
            "summary": next((l.strip() for l in text.splitlines()
                             if l.strip() and not l.startswith("#")), "")[:160],
        })
    return rules


def analytics(rules: list[dict]) -> dict:
    by_slug = {r["slug"]: r for r in rules}
    backlinks = {r["slug"]: 0 for r in rules}
    for r in rules:
        for tgt in r["outlinks"]:
            if tgt in backlinks:
                backlinks[tgt] += 1
    for r in rules:
        r["backlinks"] = backlinks[r["slug"]]
        r["orphan"] = r["backlinks"] == 0 and not r["outlinks"]
        r["dangling"] = [t for t in r["outlinks"] if t not in by_slug]
    return {
        "total_rules": len(rules),
        "total_tokens": sum(r["tokens"] for r in rules),
        "no_boundary": [r["slug"] for r in rules if not r["has_boundary"]],
        "orphans": [r["slug"] for r in rules if r["orphan"]],
        "dangling": {r["slug"]: r["dangling"] for r in rules if r["dangling"]},
    }


def corrections_status() -> dict:
    """Patterns at Count==2 (one short of the >=3 rule trigger) + unreviewed candidate clusters."""
    watch = []
    if CORRECTIONS.exists():
        txt = CORRECTIONS.read_text(encoding="utf-8", errors="replace")
        for block in re.split(r"\n##\s+Pattern:", txt)[1:]:
            name = block.splitlines()[0].strip()
            cm = re.search(r"\*\*Count:\s*(\d+)", block)
            if not cm:
                continue
            # skip patterns that already produced a rule (Count=2 but resolved)
            already = re.search(r"TRIGGERED|rule created|Создан", block, re.I)
            if int(cm.group(1)) == 2 and not already:
                watch.append(name)
    clusters: dict[str, int] = {}
    if CANDIDATES.exists():
        for line in CANDIDATES.read_text(errors="replace").splitlines():
            try:
                row = json.loads(line)
            except Exception:
                continue
            if row.get("reviewed"):
                continue
            for mk in row.get("markers", ["?"]):
                clusters[mk] = clusters.get(mk, 0) + 1
    return {"watch_count2": watch,
            "unreviewed": dict(sorted(clusters.items(), key=lambda x: -x[1]))}


JUDGE_PROMPT = """\
You audit a set of ALWAYS-ON behavioral rules that load into an AI coding assistant's
context every session — so each rule has a permanent token cost. Below are the rules
(slug · summary · which neighbours each borders) and recent user corrections.

For each rule decide ONE verdict:
- KEEP    — load-bearing and distinct (default; prefer this)
- MERGE   — substantially overlaps another rule; name which in "into"
- SHARPEN — too vague/generic to ever change behavior
- RETIRE  — wrong, obsolete, or never load-bearing

Flag ONLY rules with a real problem. Most should be KEEP. Output STRICT JSON, no prose:
[{"rule":"<slug>","verdict":"MERGE|SHARPEN|RETIRE","into":"<slug or null>","reason":"<=18 words with evidence"}]

RULES:
{rule_list}

RECENT CORRECTIONS (signal for what actually matters):
{corrections}
"""


def call_judge(rules: list[dict], corr_txt: str) -> tuple[list[dict], str]:
    base = os.environ.get("OPENAI_API_BASE") or os.environ.get("OPENAI_BASE_URL") or "http://127.0.0.1:8080/v1"
    model = os.environ.get("OPENAI_MODEL", "local")
    key = os.environ.get("OPENAI_API_KEY", "")
    rule_list = "\n".join(
        f'- {r["slug"]} · {r["summary"]} · borders: {", ".join(r["outlinks"]) or "none"}'
        for r in rules)
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": JUDGE_PROMPT
            .replace("{rule_list}", rule_list).replace("{corrections}", corr_txt[:2000])}],
        "stream": False, "temperature": 0,
    }).encode()
    req = urllib.request.Request(f"{base.rstrip('/')}/chat/completions", data=body,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            content = json.load(resp)["choices"][0]["message"]["content"]
    except Exception as e:
        return [], f"_(judge unavailable: {e} — analytics-only)_"
    m = re.search(r"\[.*\]", content, re.S)
    if not m:
        return [], "_(judge returned no JSON — analytics-only)_"
    try:
        flags = json.loads(m.group(0))
    except Exception:
        return [], "_(judge JSON unparseable — analytics-only)_"
    # Safety valve (stolen from autoswarm): refuse a mass-wipe; bad model output.
    flags = [f for f in flags if f.get("verdict") in ("MERGE", "SHARPEN", "RETIRE")]
    if len(flags) >= max(2, len(rules) // 2):
        return [], f"_(judge over-flagged {len(flags)}/{len(rules)} — ignored as noise)_"
    return flags, f"{model} @ {base}"


def render(rules, an, corr, flags, judge_src) -> str:
    L = [f"# Rules Hygiene — {date.today():%Y-%m-%d}", "",
         f"**{an['total_rules']} always-on rules · ~{an['total_tokens']:,} tokens loaded every session.**",
         f"Judge: {judge_src}", ""]
    L.append("## ➡️ Proposed actions (you apply — nothing auto-edited)")
    if flags:
        order = {"RETIRE": 0, "MERGE": 1, "SHARPEN": 2}
        for f in sorted(flags, key=lambda x: order.get(x["verdict"], 9)):
            into = f' → `{f["into"]}`' if f.get("into") else ""
            L.append(f"- **{f['verdict']}** `{f['rule']}`{into} — {f.get('reason','')}")
    else:
        L.append("- _(no rule flagged — set is clean, or judge offline)_")
    L += ["", "## 🔗 Structure signals (deterministic)"]
    if an["orphans"]:
        L.append(f"- **Orphans** (no backlink, link nothing — review for relevance): {', '.join(an['orphans'])}")
    if an["no_boundary"]:
        L.append(f"- **Missing `## Boundary`** (intake-gate requires it): {', '.join(an['no_boundary'])}")
    if an["dangling"]:
        L.append(f"- **Dangling `[[links]]`** (point to nonexistent rules): {an['dangling']}")
    L += ["", "## 📊 Per-rule (sorted by context cost)", "",
          "| rule | tokens | lines | backlinks | boundary | added |",
          "|---|--:|--:|--:|:--:|---|"]
    for r in sorted(rules, key=lambda x: -x["tokens"]):
        L.append(f"| {r['slug']} | {r['tokens']} | {r['lines']} | {r['backlinks']} | "
                 f"{'✓' if r['has_boundary'] else '—'} | {r['added']} |")
    L += ["", "## 🔁 Corrections pipeline"]
    if corr["watch_count2"]:
        L.append(f"- **At Count=2 (one more → rule trigger):** {'; '.join(corr['watch_count2'])}")
    if corr["unreviewed"]:
        tot = sum(corr["unreviewed"].values())
        L.append(f"- **{tot} unreviewed candidates** by marker: " +
                 ", ".join(f"`{k}`×{v}" for k, v in corr["unreviewed"].items()))
    if not corr["watch_count2"] and not corr["unreviewed"]:
        L.append("- _(clean — no patterns near threshold, no unreviewed candidates)_")
    return "\n".join(L) + "\n"


def main():
    use_llm = "--no-llm" not in sys.argv
    out = Path(sys.argv[sys.argv.index("--out") + 1]) if "--out" in sys.argv else DEFAULT_OUT
    rules = load_rules()
    an = analytics(rules)
    corr = corrections_status()
    corr_txt = CORRECTIONS.read_text(errors="replace")[-2000:] if CORRECTIONS.exists() else ""
    flags, judge_src = call_judge(rules, corr_txt) if use_llm else ([], "_(--no-llm)_")
    report = render(rules, an, corr, flags, judge_src)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    print(report)
    print(f"\n→ written to {out}")


if __name__ == "__main__":
    main()
