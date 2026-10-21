#!/usr/bin/env python3
"""Weekly auto self-review (cc-stack P0) — headless consumer of the self-learning pipeline.

The /self-review SKILL is the MANUAL consumer; this is its headless weekly twin. It reads the three
producer stores, runs ONE Kimi pass to adjudicate raw candidates, cluster+count cross-store, and
DRAFT rule/CLAUDE.md proposals for any behavior seen >=3x, writes the drafts to a dated file, pings
Anton via ntfy, and marks the raw candidates reviewed. It NEVER applies a rule or edits CLAUDE.md —
approval stays Anton's (he reads the file, then tells Claude to apply). Mirrors report.py's
launchd + headless-Kimi + ntfy pattern; reuses its kimi() and notify() plumbing.

Grafts (2026-07-03, per p2-research/self-learning-mechanisms-soa-2026-07-03.md):
- G2: reads ~/.claude/projects/-Users-rusakovanton/memory/trajectories/*.md from the last 7 days
  as a 4th input; repeated "what worked" tactics across >=2 trajectories become SKILL-PROPOSAL
  DRAFT lines (never auto-applied — same approval gate as rule proposals).
- G7: a second, independent Kimi pass judges the claimed proposals against ONLY the raw evidence
  (not the first pass's narrative) — "did this actually reach the threshold, yes/no/unclear".

Graft (2026-07-04, RLM-pilot verdict): ~/.claude/constitution/clusters-latest.md (written by
scripts/constitution/cluster_pass.py — one long-context GLM call, replaces the old difflib
similarity clustering) is now the PRIMARY cross-source clustering input, folded into the prompt
as input (F). It beat the old miner's candidates.md (mine_candidates.py — 186/187 count=1
fragments, no real consolidation) decisively in the pilot. candidates.md is read as a FALLBACK
only when clusters-latest.md doesn't exist yet — the miner itself is not removed/deleted.

Usage:  python3 self-review-weekly.py [--dry]
"""
import json, os, re, sys, subprocess, datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# llm_fallback, NOT kimi: kimi() hands back whatever the endpoint said, including the string
# "API Error: Request rejected (429) · Insufficient balance…", which then gets written into the
# deliverable as if it were a review. Three of the four self-reviews before 2026-08-07 (W29, W30,
# W32) are exactly that error and nothing else — 345 bytes where a real one is ~5 KB. The weekly
# report never had the problem because it already routes through llm_fallback, which retries on
# headless Haiku for pennies when Kimi's balance is out.
from report import llm_fallback, notify  # same plumbing as the weekly report, with its fallback

HOME = os.path.expanduser("~")
CAND = f"{HOME}/.claude/logs/correction-candidates.jsonl"
CORR = f"{HOME}/.claude/projects/-Users-rusakovanton/memory/corrections.md"
PATT = f"{HOME}/.claude/projects/-Users-rusakovanton/memory/patterns-candidates.md"
CLUSTERS = f"{HOME}/.claude/constitution/clusters-latest.md"       # primary (2026-07-04)
MINER_FALLBACK = f"{HOME}/.claude/constitution/candidates.md"      # fallback if CLUSTERS absent
CLUSTER_PASS = f"{HOME}/.claude/scripts/constitution/cluster_pass.py"  # producer of CLUSTERS
CLUSTERS_STALE_DAYS = 8  # refresh producer if older than this (self-review is weekly; 8 > 7 = one cycle grace)
TRAJ_DIR = f"{HOME}/.claude/projects/-Users-rusakovanton/memory/trajectories"
RULES_DIR = f"{HOME}/.claude/rules/learned"
OUT_DIR = f"{HOME}/projects/cc-stack-2026/p5-loop/self-review"
MARK = f"{HOME}/.claude/skills/self-review/mark-reviewed.py"
RUN_LOG = f"{HOME}/.claude/logs/self-review-weekly.jsonl"

PROMPT = """You are the self-review consumer for Anton (a non-developer who directs Claude Code in natural language). Read his self-learning stores and DRAFT governance proposals. You NEVER apply anything — you only draft for his approval.

INPUTS:

(A) RAW correction candidates — unreviewed, NOISY string-matches. Many are FALSE POSITIVES (Claude explaining a design, Anton describing something unrelated, an incidental keyword match). Each line is one JSON object:
{candidates}

(B) corrections.md — already-synthesized recurring corrections with counters (TRUSTWORTHY). Use it for cross-store counts and to AVOID re-proposing what's already captured:
{corrections}

(C) patterns-candidates.md — mined POSITIVE tactics (mostly not corrections):
{patterns}

(D) Existing rule slugs in ~/.claude/rules/learned/ — do NOT propose a new rule duplicating one of these (prefer grafting onto it): {rules}

(E) TRAJECTORY POST-MORTEMS from the last 7 days (may be empty — that's fine, just skip step 4). Each is one session's post-mortem, may contain a "what worked" section. Look for the SAME concrete tactic repeated across >=2 DIFFERENT trajectory files (not just twice in one file):
{trajectories}

(F) PRE-CLUSTERED patterns — a long-context GLM pass already clustered corrections.md + diary +
trajectories (last 90 days) into named clusters with cross-source counts and dated verbatim
examples (TRUSTWORTHY, this is the PRIMARY clustering signal — prefer it over re-deriving
clusters from A/B yourself; use A/B mainly to catch anything newer than this pass's run date):
{clusters}

TASK:
1. Adjudicate (A): keep only rows where ANTON is correcting Claude's behavior; drop false positives.
2. Cluster confirmed corrections + (B)'s counters + (F)'s pre-clustered patterns by ROOT behavior (same behavior = one cluster regardless of wording). Count occurrences cross-store; reconcile (F)'s counts with anything new in (A)/(B) instead of re-clustering from scratch.
3. For each cluster with >=3 total occurrences NOT already a rule in (D): draft ONE proposal — either a new rule (one-line trigger + a '## Boundary with neighbours' naming 2-3 closest existing rules) OR a specific CLAUDE.md edit (quote old -> new). Skip clusters already covered by an existing rule.
4. From (E): if the SAME concrete "what worked" tactic literally recurs across >=2 distinct trajectory files, draft it as a skill-proposal line. Do NOT invent recurrence — if nothing repeats across files, say so.

OUTPUT (markdown only):
- First line EXACTLY: `PROPOSALS: <integer>` (count of >=3 clusters you propose from A/B/C only; 0 if none — do NOT count skill proposals from E here).
- `## Adjudication` — one line: N candidates seen, K kept, rest dropped (brief why).
- `## Proposals` — numbered; each: cluster name, occurrence count, the drafted rule/edit, one-line plain rationale. If none qualify: "No cluster reached the >=3 threshold this week."
- `## Skill Proposals (draft)` — one line per repeated tactic, EXACTLY this format: `SKILL-PROPOSAL DRAFT (draft — needs Anton/evidentiary bar, NEVER auto-applied): <tactic> — seen in <N> trajectories: <filenames>`. If none recur: "No tactic recurred across >=2 trajectories this week."
Be terse and concrete. Do NOT invent occurrences. Output ONLY the markdown."""


def read(p):
    try:
        return open(p, encoding="utf-8", errors="replace").read()
    except Exception:
        return ""


def refresh_clusters_if_stale():
    """Producer-refresh guard: cluster_pass (the CLUSTERS producer) has no schedule of its
    own, so before self-review CONSUMES clusters-latest.md we refresh it if missing/stale.
    Rides this weekly job's cadence — no new cron. Fail-open: if the GLM pass errors or
    times out, self-review proceeds on whatever CLUSTERS exists (never blocks the review).
    Root cause it closes: 07-13 and 07-20 self-reviews both read 18-day-stale 07-04 clusters."""
    try:
        age_days = (datetime.datetime.now().timestamp() - os.path.getmtime(CLUSTERS)) / 86400
        fresh = age_days <= CLUSTERS_STALE_DAYS
    except OSError:
        fresh = False  # missing → treat as stale
    if fresh:
        return
    sys.stderr.write("[self-review] clusters-latest.md stale/missing — running cluster_pass first\n")
    try:
        r = subprocess.run([sys.executable, CLUSTER_PASS], timeout=300,
                           capture_output=True, text=True)
    except Exception as e:
        sys.stderr.write(f"[self-review] cluster_pass refresh failed (proceeding on existing): {e}\n")
        return
    # A silent no-op is the failure mode this guard exists to prevent: cluster_pass exits 0
    # on a GLM quota skip (fail-open) and on any path that writes nothing, so returncode
    # alone is not the signal — the file's mtime is. Say so in the log, or the next stale
    # week looks identical to a healthy one (2026-08-03: guard fired, produced nothing,
    # nobody heard; must-act carried "no run in 14d" for two weeks).
    refreshed = False
    try:
        refreshed = (datetime.datetime.now().timestamp() - os.path.getmtime(CLUSTERS)) / 86400 <= 1
    except OSError:
        pass
    if not refreshed:
        tail = ((r.stderr or "") + (r.stdout or "")).strip().replace("\n", " | ")[-300:]
        sys.stderr.write(f"[self-review] cluster_pass wrote NOTHING (rc={r.returncode}) — "
                         f"CLUSTERS still stale, proceeding on the old file: {tail}\n")


def read_trajectories(days=7):
    """G2: trajectory post-mortems from the last `days` days, by YYYY-MM-DD-<slug>.md filename
    date (not mtime — post-mortems are written once, mtime drift shouldn't matter). Fail-open:
    missing dir, unreadable files, or bad filenames are silently skipped — never crashes the weekly."""
    out = []
    try:
        if not os.path.isdir(TRAJ_DIR):
            return out
        cutoff = datetime.date.today() - datetime.timedelta(days=days)
        for fn in sorted(os.listdir(TRAJ_DIR)):
            if not fn.endswith(".md"):
                continue
            m = re.match(r"(\d{4}-\d{2}-\d{2})-", fn)
            if not m:
                continue
            try:
                d = datetime.date.fromisoformat(m.group(1))
            except Exception:
                continue
            if d < cutoff:
                continue
            body = read(os.path.join(TRAJ_DIR, fn))
            if body.strip():
                out.append((fn, body))
    except Exception:
        return out
    return out


def extract_proposals(body):
    """Pull the numbered items out of the '## Proposals' section for the G7 judge pass."""
    m = re.search(r"^## Proposals\s*\n(.*?)(?=\n## |\Z)", body, re.S | re.M)
    if not m:
        return []
    items = re.split(r"\n(?=\d+\.\s)", m.group(1).strip())
    return [it.strip() for it in items if re.match(r"^\d+\.", it.strip())]


JUDGE_PROMPT = """You are an independent auditor cross-checking another pass's claims. You were NOT shown its reasoning — verify each claim COLD, using ONLY the raw evidence below.

CLAIMED PROPOSALS (cluster name + occurrence count claimed by the other pass):
{claims}

RAW EVIDENCE (unreviewed candidates + corrections.md + patterns-candidates.md — the ONLY source of truth, ignore any framing above):
{evidence}

For EACH numbered claim, decide independently: does the raw evidence actually show >=3 real occurrences of that behavior (not a false positive, not invented)? Output ONLY a markdown list, one line per claim, EXACTLY this format:
N: yes|no|unclear — <one-line reason citing what you found or didn't find in the evidence>
No prose before or after. If there are zero claims, output exactly: (no claims to judge)"""


def judge_proposals(body, rows, corr_text, patt_text):
    """G7: cheap second, independent LLM pass — judge given ONLY the claimed proposals + raw
    evidence lines, NOT the first pass's narrative/rationale. Returns (verdict_md, n_disagreements,
    cost). Fail-open: no proposals or LLM failure -> ('', 0, 0.0), never blocks the main output."""
    claims = extract_proposals(body)
    if not claims:
        return "", 0, 0.0
    evidence = ("RAW CANDIDATES:\n" + "\n".join(json.dumps(r, ensure_ascii=False) for r in rows[:80])
                + "\n\nCORRECTIONS.MD:\n" + corr_text[:6000]
                + "\n\nPATTERNS-CANDIDATES.MD:\n" + patt_text[:4000])[:12000]
    prompt = JUDGE_PROMPT.format(claims="\n\n".join(claims)[:4000], evidence=evidence)
    verdict, cost = llm_fallback(prompt)
    verdict = verdict.strip()
    if not verdict or verdict.startswith("API Error"):
        return "", 0, 0.0
    n_disagree = len(re.findall(r"(?m)^\s*\d+:\s*no\b", verdict, re.IGNORECASE))
    return verdict, n_disagree, cost


def log_run(rec):
    """measure-what-you-ship: one JSONL line per run, counts only. Fail-open."""
    try:
        os.makedirs(os.path.dirname(RUN_LOG), exist_ok=True)
        rec.setdefault("ts", datetime.datetime.now(datetime.timezone.utc).isoformat())
        with open(RUN_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def hook_health_md():
    """Dead-man's switch (2026-09-16): effect metrics for instrumented hooks, BEFORE the
    early-exit — a dead hook must be caught on a no-candidates week too. The 17-day dead
    preflight banner (08-30 → 09-16) was invisible to every weekly because they counted
    FIRES, never EFFECT. 🔴 findings notify Anton even when no report is written."""
    try:
        r = subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "hook-health.py"), "--md"],
                           capture_output=True, text=True, timeout=60)
        md = r.stdout.strip()
    except Exception:
        return ""
    if "all green" in md:
        return ""
    notify(f"Hook health: {md.splitlines()[0][:180]}")
    return md + "\n\n"


def main():
    hh = hook_health_md()
    rows = []
    for line in read(CAND).splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if not r.get("reviewed"):
            rows.append(r)

    trajectories = read_trajectories(days=7)  # G2

    if not rows and not trajectories:
        if hh:
            # dead-man finding on an otherwise quiet week — still worth a report file
            now = datetime.datetime.now()
            iso_y, iso_w, _ = now.isocalendar()
            os.makedirs(OUT_DIR, exist_ok=True)
            out = f"{OUT_DIR}/{iso_y}-W{iso_w:02d}.md"
            with open(out, "w", encoding="utf-8") as f:
                f.write(f"# Weekly Self-Review — {iso_y}-W{iso_w:02d}\n\n"
                        f"> Generated {now:%Y-%m-%d %H:%M} · no candidates — hook-health findings only\n\n"
                        + hh + "\n")
            sys.stderr.write(f"[self-review] hook-health findings written -> {out}\n")
        sys.stderr.write("[self-review] no unreviewed candidates, no recent trajectories — nothing to do\n")
        log_run({"trajectories_read": 0, "proposals_emitted": 0, "judge_disagreements": 0, "skipped": True})
        return

    hashes = [r.get("h") for r in rows if r.get("h")]
    existing = sorted(os.path.splitext(f)[0] for f in os.listdir(RULES_DIR)
                      if f.endswith(".md")) if os.path.isdir(RULES_DIR) else []

    traj_txt = "\n\n".join(f"----- {fn} -----\n{body[:3000]}" for fn, body in trajectories)[:20000] \
        or "(none in the last 7 days)"

    refresh_clusters_if_stale()  # producer-refresh: never consume stale correction-clusters
    clusters_txt = read(CLUSTERS)
    if not clusters_txt.strip():
        clusters_txt = read(MINER_FALLBACK)  # fallback to the old difflib miner's output
        clusters_txt = clusters_txt or "(no clusters-latest.md and no candidates.md fallback found)"

    prompt = PROMPT.format(
        candidates=("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) if rows else "(none)"),
        corrections=read(CORR)[:24000],
        patterns=read(PATT)[:8000],
        rules=", ".join(existing),
        trajectories=traj_txt,
        clusters=clusters_txt[:20000])

    if "--dry" in sys.argv:
        sys.stderr.write(f"[dry] {len(rows)} unreviewed candidates · {len(trajectories)} trajectories (7d) · "
                         f"{len(existing)} existing rules · prompt {len(prompt)} chars\n")
        return

    body, cost = llm_fallback(prompt)
    # An error STRING is not a review. The old guard tested only for emptiness, so a 429
    # sailed through and became the file's entire contents — which reads on disk exactly
    # like a week with nothing to say.
    if not body.strip() or body.strip().startswith("API Error"):
        why = "empty" if not body.strip() else body.strip().splitlines()[0][:160]
        sys.stderr.write(f"[self-review] no usable reply from Kimi OR the Haiku fallback "
                         f"— aborting, NOT marking reviewed: {why}\n")
        log_run({"trajectories_read": len(trajectories), "proposals_emitted": 0,
                 "judge_disagreements": 0, "error": "no_usable_llm_reply"})
        return

    n_prop = 0
    first = body.strip().splitlines()[0]
    if first.upper().startswith("PROPOSALS:"):
        try:
            n_prop = int(first.split(":", 1)[1].strip().split()[0])
        except Exception:
            n_prop = 0

    # G7: independent judge pass over the claimed proposals, evidence-only, no narrative.
    judge_md, n_disagree, judge_cost = judge_proposals(body, rows, read(CORR)[:24000], read(PATT)[:8000])
    total_cost = cost + judge_cost
    if judge_md:
        flag = f" · ⚠️ {n_disagree} disagreement(s) with the first pass" if n_disagree else " · no disagreements"
        body = body.rstrip() + (f"\n\n## Independent Judge (second, evidence-only pass){flag}\n\n{judge_md}\n")

    now = datetime.datetime.now()
    iso_y, iso_w, _ = now.isocalendar()
    os.makedirs(OUT_DIR, exist_ok=True)
    out = f"{OUT_DIR}/{iso_y}-W{iso_w:02d}.md"
    header = (f"# Weekly Self-Review — {iso_y}-W{iso_w:02d}\n\n"
              f"> Generated {now:%Y-%m-%d %H:%M} · {len(rows)} raw candidates analyzed · "
              f"{len(trajectories)} trajectory post-mortem(s) (7d) · "
              f"Kimi ${total_cost:.3f} · DRAFTS ONLY — approval is Anton's, nothing was applied\n\n")
    with open(out, "w", encoding="utf-8") as f:
        f.write(header + hh + body.strip() + "\n")

    # mark the raw candidates reviewed (adjudicated = seen) — mirrors skill step 6
    if hashes:
        try:
            subprocess.run(["python3", MARK, *hashes], capture_output=True, timeout=30)
        except Exception:
            pass

    if n_prop > 0:
        notify(f"Self-review: {n_prop} proposal(s) await approval — open {os.path.basename(out)}")
    sys.stderr.write(f"[self-review] {len(rows)} candidates · {len(trajectories)} trajectories · "
                     f"{n_prop} proposals · {n_disagree} judge disagreements -> {out} (${total_cost:.3f})\n")
    print(f"Self-review written: {out}  ({n_prop} proposals, {n_disagree} judge disagreements, "
          f"Kimi ${total_cost:.3f})")
    log_run({"trajectories_read": len(trajectories), "proposals_emitted": n_prop,
             "judge_disagreements": n_disagree, "cost_usd": round(total_cost, 4)})


if __name__ == "__main__":
    main()
