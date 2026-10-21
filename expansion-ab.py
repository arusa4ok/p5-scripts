#!/usr/bin/env python3
"""
A/B harness for gbrain query-expansion (GLM via z.ai since 2026-07-04; was Gemini).

Question it answers: does LLM query-expansion surface retrieval hits the raw
query misses? Expansion rewrites a query into <=2 alternatives for recall; it
costs an expansion-model call per query (GLM subscription, $0 marginal). We want EFFECTIVENESS (new top-k hits) + COST
before deciding to keep it / extend it into the preflight hook (which today
runs --no-expand for latency).

Method: replay real research queries (from gbrain-preflight.jsonl `ph` field —
the actual prompts that triggered semantic search) through `gbrain query Q`
(expansion ON) vs `gbrain query Q --no-expand` (OFF), diff the top-k slug sets.

  new_hit_rate    = frac of queries where ON surfaced >=1 slug OFF missed
  rank1_changed   = frac where the #1 result changed
  avg_jaccard     = mean top-k set overlap (1.0 = expansion changed nothing)

Run-based, not per-fire (like rule-bench). Writes p5-loop/expansion-ab/latest.json;
the weekly System Overview reads it and renders the verdict.

Yardstick (measure-what-you-ship):
  new_hit_rate >= 20%  -> ✅ material recall gain — keep; consider enabling in preflight
  5–20%               -> 👀 marginal — watch another week
  < 5% for 2 weeks    -> ⚠️ no benefit — turn expansion off (cost isn't the kill trigger; it's pennies)
  ON == OFF entirely  -> ✅ accepted no-op on the free path (PLAN §9, closed) — NOT an action

Cost is volume-based: gbrain does NOT auto-log expansion spend (budget_ledger empty),
so we estimate from query volume; on the GLM flat quota marginal cost is $0.

Usage:
  python3 scripts/p5/expansion-ab.py            # replay last 40 distinct queries
  python3 scripts/p5/expansion-ab.py --n 60     # bigger sample
  python3 scripts/p5/expansion-ab.py --dry      # show the corpus, no gbrain calls (free)

Kill switch for the feature itself: remove "expansion_model" from ~/.gbrain/config.json.
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
GBRAIN = HOME / "Library/Application Support/reflex/bun/bin/gbrain"
PREFLIGHT_LOG = HOME / ".claude/logs/gbrain-preflight.jsonl"
OUT_DIR = Path(__file__).resolve().parents[2] / "p5-loop/expansion-ab"
OUT = OUT_DIR / "latest.json"

LIMIT = 5            # top-k compared
MIN_WORDS = 3        # expansion only fires on queries >= 3 words (expansion.ts MIN_WORDS)
GLM_IN_PER_1M = 0.0       # GLM subscription flat quota — $0 marginal (2026-07-04)
EST_TOKENS_PER_Q = 380    # query + system + 2 rewrites, rough


def load_corpus(n: int) -> list[str]:
    """Distinct real research queries from the preflight log, most-recent first."""
    if not PREFLIGHT_LOG.exists():
        return []
    seen: dict[str, None] = {}
    rows = []
    for line in PREFLIGHT_LOG.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        q = str(r.get("ph", "")).strip()
        if not q or len(q.split()) < MIN_WORDS:
            continue
        # drop headless/subagent prompts (not real query intent — same filter as overview)
        if q.lstrip().startswith(("You ", "Analyze ", "Extract ", "Classify ", "#")):
            continue
        rows.append(q)
    # most recent last in file → reverse, dedup preserving order
    out = []
    for q in reversed(rows):
        k = q.lower()
        if k in seen:
            continue
        seen[k] = None
        out.append(q)
        if len(out) >= n:
            break
    return out


def slugs(query: str, expand: bool) -> list[str] | None:
    """Top-k slugs for a query. None on failure (timeout / no binary)."""
    cmd = [str(GBRAIN), "query", query, "--limit", str(LIMIT)]
    if not expand:
        cmd.append("--no-expand")
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30,
            env={**os.environ},
        ).stdout
    except Exception:
        return None
    res = []
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("["):
            continue
        # A result line is `[0.9887] slug -- text`. gbrain also prints diagnostics in the
        # same bracket shape — `[ai.gateway] expansion disabled: [expand] invalid x-api-key` —
        # and the old parser turned that whole sentence into a "slug", i.e. the ON run scored
        # a phantom hit for the very message saying expansion had FAILED. Require a numeric
        # score inside the bracket so a diagnostic can never be counted as a result.
        head = line[1:].split("]", 1)[0]
        try:
            float(head)
        except ValueError:
            continue
        rest = line.split("] ", 1)[-1]
        slug = rest.split(" -- ", 1)[0].strip()
        if slug:
            res.append(slug)
    return res


def main() -> int:
    n = 40
    dry = "--dry" in sys.argv
    if "--n" in sys.argv:
        n = int(sys.argv[sys.argv.index("--n") + 1])

    corpus = load_corpus(n)
    if not corpus:
        print("No corpus — gbrain-preflight.jsonl empty or absent. Nothing to replay.")
        return 1
    print(f"Corpus: {len(corpus)} distinct research queries (>= {MIN_WORDS} words) from preflight log.")
    if dry:
        for i, q in enumerate(corpus, 1):
            print(f"  {i:2}. {q[:90]}")
        print("\n--dry: no gbrain calls made, $0.")
        return 0

    if not GBRAIN.exists():
        print(f"gbrain binary not found at {GBRAIN}", file=sys.stderr)
        return 1

    new_hits = 0
    rank1_changed = 0
    jaccs = []
    identical = 0
    scored = 0
    on_empty = 0  # ON returned nothing while OFF did — the "expansion path broke" signature
    examples = []
    for q in corpus:
        on = slugs(q, expand=True)
        off = slugs(q, expand=False)
        if on is None or off is None:
            continue
        scored += 1
        if not on and off:
            on_empty += 1
        son, soff = set(on), set(off)
        if son == soff and on == off:
            identical += 1
        gained = son - soff
        if gained:
            new_hits += 1
            if len(examples) < 8:
                examples.append({"q": q[:80], "new": sorted(gained)[:3]})
        if on and off and on[0] != off[0]:
            rank1_changed += 1
        union = son | soff
        jaccs.append(len(son & soff) / len(union) if union else 1.0)

    if scored == 0:
        print("All replays failed (gbrain returned nothing). Check the binary / DB.", file=sys.stderr)
        return 1

    # if EVERY pair is byte-identical, expansion is not active (Google key not set yet)
    expansion_active = identical < scored
    new_hit_rate = round(100 * new_hits / scored, 1)
    rank1_rate = round(100 * rank1_changed / scored, 1)
    avg_jacc = round(sum(jaccs) / len(jaccs), 3)
    est_monthly = round(40 * 30 * EST_TOKENS_PER_Q / 1_000_000 * GLM_IN_PER_1M, 4)  # ~40 q/day

    # BROKEN != no-benefit. When the ON path returns nothing while OFF returns results, the expansion
    # dependency (GLM via z.ai) was down at replay time — the byte-identical INACTIVE guard misses this
    # (empty ON != non-empty OFF reads as "different = active"), so a downed GLM silently produced a
    # "turn off" verdict. Caught 2026-07-20: the 07-19 run scored jaccard=0 + new_hit=0 = empty ON on
    # every query, while GLM was exhausted. Flag it as invalid instead of recommending a kill.
    expansion_broken = scored > 0 and on_empty == scored

    if expansion_broken:
        verdict = ("🚫 expansion BROKEN — ON returned 0 slugs on every query while OFF worked; the GLM "
                   "expansion dependency was down at replay. Measurement INVALID — do NOT act on it, re-run "
                   "when GLM is back.")
    elif not expansion_active:
        # DO NOT name a cause this run did not measure (2026-07-29). The old text asserted
        # "z.ai key not set" — the key WAS set (49 chars, in ~/.config/secrets/secrets.env,
        # and the weekly does inject it). What the replay actually observes is only ON==OFF.
        # The measured cause, found by hand: under the ZAI_API_KEY -> ANTHROPIC_API_KEY +
        # z.ai base_url redirect the expansion is a SILENT no-op — no error, byte-identical
        # results. Through a real Anthropic key it genuinely re-ranks. So the fork is a MONEY
        # decision (free-but-dead vs paid-and-working), which is Anton's, not a bug to fix.
        # A verdict may state what it measured; the cause belongs in the plan, not here.
        # No ⚠️: this state is DIAGNOSED and ACCEPTED, so it must not keep minting a forced
        # action. PLAN §9 closed it on 2026-07-29 — both silent defects under it were fixed
        # in ~/gbrain (f0ad550a schemaName/schemaDescription; 59a42741 the embed abort floor,
        # which was answering `No results` on ~half of all valid queries), the money fork was
        # REFUSED ("make it work for free"), and what remains is the known residue: on the
        # free GLM path the expansions come back empty, so expansion adds nothing and — after
        # 59a42741 — subtracts nothing. A signal that keeps raising a question already answered
        # is the same disease as a fossil ⚠️ line; it burns a cycle re-litigating a closed call.
        verdict = ("✅ expansion inactive on the free path — ON==OFF, known and accepted "
                   "(PLAN §9, closed 2026-07-29: adds nothing, subtracts nothing). Re-open only "
                   "if the paid path is ever chosen — that is a money decision, not a defect.")
    elif new_hit_rate >= 20:
        verdict = "✅ material recall gain — keep; consider enabling in preflight"
    elif new_hit_rate >= 5:
        verdict = "👀 marginal — watch another week"
    else:
        verdict = "⚠️ no benefit — candidate to turn off (cost is pennies, but it buys nothing)"

    result = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "n_corpus": len(corpus),
        "n_scored": scored,
        "expansion_active": expansion_active,
        "expansion_broken": expansion_broken,
        "on_empty_count": on_empty,
        "new_hit_rate_pct": new_hit_rate,
        "rank1_changed_pct": rank1_rate,
        "avg_jaccard": avg_jacc,
        "est_monthly_cost_usd": est_monthly,
        "cost_note": "GLM subscription flat quota — $0 marginal; gbrain does not auto-log spend",
        "verdict": verdict,
        "examples": examples,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\nScored {scored}/{len(corpus)} queries.")
    print(f"  new-hit rate   : {new_hit_rate}%  (expansion surfaced a top-{LIMIT} slug the raw query missed)")
    print(f"  rank-1 changed : {rank1_rate}%")
    print(f"  avg jaccard    : {avg_jacc}  (1.0 = expansion changed nothing)")
    print(f"  est. cost      : ~${est_monthly}/mo  ({result['cost_note']})")
    print(f"  verdict        : {verdict}")
    if examples:
        print("  examples of new hits:")
        for e in examples[:5]:
            print(f"    • {e['q']}  +{e['new']}")
    print(f"\nWrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
