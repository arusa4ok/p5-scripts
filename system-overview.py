#!/usr/bin/env python3
"""P5 weekly System Overview — the whole .claude/ system at a glance, zero LLM tokens.

Supersedes weekly-retro.py as the Monday launchd target. Aggregates every telemetry stream
the stack accumulates (skill-suggest, verify-gate, correction-detector, opus-routing, synth,
research-ingest) + burn-metrics + a live config inventory, then emits heuristic self-improvement
flags. Prepends a dated block to p5-loop/SYSTEM-OVERVIEW.md (newest on top) and fires a macOS
notification with the verdict.

Design: deterministic + fail-safe (a malformed log degrades that one section, never the report).
Deep LLM analysis stays the monthly /insights pass; this is the cheap weekly glance.
Idempotent within an ISO week: a re-run replaces the same-week block at the top.
"""
from __future__ import annotations
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

HOME = Path(os.path.expanduser("~"))
ROOT = Path(__file__).resolve().parents[2]
BURN = ROOT / "scripts" / "p5" / "burn-metrics.py"
OUT = ROOT / "p5-loop" / "SYSTEM-OVERVIEW.md"
SNAP = ROOT / "p5-loop" / "snapshots"  # week-over-week trend snapshots
LOGS = HOME / ".claude" / "logs"
PROJECTS = HOME / ".claude" / "projects"  # CC transcripts: <project>/<session_id>.jsonl
KIMI_LOG = HOME / ".claude" / "projects" / "-Users-rusakovanton" / "memory" / "kimi-usage.log"
# GLM (z.ai) replaced Kimi as the flat-quota engine 2026-07-03 — claude-glm logs separately
# (different path + format: ts<TAB>glm<TAB>worker, no "mode"/"detail" fields).
GLM_LOG = HOME / ".claude" / "logs" / "claude-glm-usage.log"
SKILLS = HOME / ".claude" / "skills"
HOOKS = HOME / ".claude" / "hooks"
RULES = HOME / ".claude" / "rules"
CLAUDEMD = HOME / ".claude" / "CLAUDE.md"
SETTINGS = HOME / ".claude" / "settings.json"
DECISIONS_DIR = HOME / ".claude" / "decisions"  # /decide ledger — project *.jsonl + _global.jsonl


def iso_now():
    n = datetime.now(timezone.utc)
    y, w, _ = n.isocalendar()
    return n, y, w


def in_week(ts_str, y, w):
    """True if an ISO-8601 ts falls in ISO week (y,w). Undated -> True (never silently drop)."""
    if not ts_str:
        return True
    try:
        d = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        dy, dw, _ = d.isocalendar()
        return (dy, dw) == (y, w)
    except Exception:
        return True


def read_jsonl(name):
    p = LOGS / name
    if not p.exists():
        return []
    out = []
    try:
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    except Exception:
        return []
    return out


def week_rows(name, y, w):
    return [r for r in read_jsonl(name)
            if in_week(r.get("ts") or r.get("timestamp") or r.get("at") or r.get("synthesized_at"), y, w)]


def fault_is_live(rows, is_bad, tail=50):
    """Is the fault STILL happening — judged on the last `tail` events, not the week.

    week_rows answers "did it happen this week": the right axis for a COUNT, the wrong one for
    a VERDICT. A fault fixed on Monday keeps its ⚠️ lit until Sunday, so the dashboard cannot
    stop complaining and you learn to stop reading it — the exact death the verdict layer
    exists to prevent.

    Wall-clock is the wrong axis too. (Tried 24h first: the gbrain-MCP fix landed at 10:26 and
    a 24h rule still called it broken 8h later, with 260 clean launches in between. It would
    have "recovered" at 10:26 tomorrow — a promise about the clock, not about the box.)
    Activity is the axis that carries the evidence: 260 clean launches since the last failure
    is proof; six more hours of silence is not.

    So: bad within the last `tail` events ⇒ live. A clean tail ⇒ recovered, and the week's
    tally becomes history that the line still reports, but no longer shouts.
    """
    return any(is_bad(r) for r in rows[-tail:])


def _transcript_index():
    """session_id -> newest transcript path (filename is <session_id>.jsonl)."""
    idx = {}
    try:
        for p in PROJECTS.glob("**/*.jsonl"):
            sid = p.stem
            if len(sid) < 30 or "-" not in sid:
                continue  # skip non-session files (tool-results, etc.)
            if "private-tmp" in p.parent.name:
                continue  # skip swarm-worker / scratchpad sub-transcripts: headless `-p` workers
                          # run under /private/tmp/...-out/ (encoded into the project-dir name), have
                          # tool_uses>=1 so they pass the interactive filter, and falsely deflate
                          # adoption / inflate "zero-skill" (diag 2026-07-09: 52% of "misses" were these).
            cur = idx.get(sid)
            if cur is None or p.stat().st_mtime > cur.stat().st_mtime:
                idx[sid] = p
    except Exception:
        pass
    return idx


def _scan_transcript(path):
    """Return (invoked_skill_slugs:set, interactive:bool) for one session transcript.
    `interactive` filters out headless `claude -p` one-shots (synth/preflight/report) that
    fire skill-suggest but can never adopt: keep sessions with >=2 user msgs or any tool_use."""
    invoked = set()
    user_msgs = 0
    tool_uses = 0
    try:
        txt = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return invoked, False
    for line in txt.splitlines():
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("type") == "user":
            user_msgs += 1
        msg = d.get("message")
        cont = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(cont, list):
            for c in cont:
                if isinstance(c, dict) and c.get("type") == "tool_use":
                    tool_uses += 1
                    if c.get("name") == "Skill":
                        s = (c.get("input") or {}).get("skill")
                        if s:
                            invoked.add(s)
        elif isinstance(cont, str) and "command-name" in cont:
            for m in re.finditer(r"<command-name>/?([a-z0-9:_-]+)</command-name>", cont):
                invoked.add(m.group(1))
    return invoked, (user_msgs >= 2 or tool_uses >= 1)


def skill_adoption(y, w):
    """Outcome metric: in interactive sessions where a skill was SUGGESTED this week, in what
    fraction was it then INVOKED (Skill tool or /slash). Closes the dashboard's recurring
    'confirm it's actually being invoked, not just suggested' flag with a measured rate.
    Returns (per_skill, overall, per_trigger): per_skill[slug] = [suggest_sessions, adopt_sessions];
    overall = (suggest_sessions_total, adopt_total); per_trigger[(trigger,skill)] = [interactive, adopted]
    — the trigger view is the tuning unit (which keyword phrase misfires), feeding suggester-hygiene."""
    sugg = week_rows("skill-suggestions.jsonl", y, w)
    sess_sugg = {}  # session_id -> set(skill)
    sess_trig = {}  # session_id -> set((trigger, skill))
    for r in sugg:
        sid, sk, tr = r.get("session_id"), r.get("skill"), r.get("trigger")
        if sid and sk:
            sess_sugg.setdefault(sid, set()).add(sk)
            if tr:
                sess_trig.setdefault(sid, set()).add((tr, sk))
    if not sess_sugg:
        return {}, (0, 0), {}
    idx = _transcript_index()
    # Bash/slash-invoked skills (english-coach, swarm-cycle, report, …) never appear as a Skill
    # tool_use in the transcript, so _scan_transcript can't see them — but they ARE logged to
    # skill-fires.jsonl with {skill, session_id, via:"bash"} (PostToolUse(Skill) + skill-fires-bash-log).
    # Union them in so adoption counts real invocations, not just Skill-tool ones (diag 2026-07-09:
    # this is why our own skill-heavy sessions read as "zero-skill").
    fires_by_sid = {}
    for r in read_jsonl("skill-fires.jsonl"):
        fsid, fsk = r.get("session_id"), r.get("skill")
        if fsid and fsk:
            fires_by_sid.setdefault(fsid, set()).add(fsk)
    per = {}        # skill -> [suggest_sessions, adopt_sessions]
    per_trig = {}   # (trigger, skill) -> [interactive_sessions, adopt_sessions]
    for sid in sess_sugg:
        path = idx.get(sid)
        if path is None:
            continue  # no transcript -> can't measure, drop from denominator
        invoked, interactive = _scan_transcript(path)
        invoked = invoked | fires_by_sid.get(sid, set())
        if not interactive:
            continue  # headless one-shot, can't adopt
        for sk in sess_sugg[sid]:
            d = per.setdefault(sk, [0, 0])
            d[0] += 1
            if sk in invoked:
                d[1] += 1
        for (tr, sk) in sess_trig.get(sid, ()):
            d = per_trig.setdefault((tr, sk), [0, 0])
            d[0] += 1
            if sk in invoked:
                d[1] += 1
    tot_s = sum(v[0] for v in per.values())
    tot_a = sum(v[1] for v in per.values())
    return per, (tot_s, tot_a), per_trig


def section_suggester_hygiene(per_trig):
    """Weekly trigger prune-candidates: keyword triggers firing in >=3 interactive sessions with
    <15% adoption — the phrase is too broad (fires on meta-talk / wrong context). This is the
    standing loop that makes the suggester better: each week surfaces what to tighten/prune in
    skill-triggers.json. Returns (markdown_or_None, n_candidates)."""
    cands = [(n, tr, sk, a) for (tr, sk), (n, a) in per_trig.items() if n >= 3 and a / n < 0.15]
    if not cands:
        return None, 0
    cands.sort(reverse=True)  # most-fired first = noisiest
    lines = ["### 🧹 Suggester hygiene — trigger prune-candidates",
             "_Keyword fired in ≥3 interactive sessions, <15% adoption ⇒ too broad. Tighten/prune in `~/.claude/hooks/skill-triggers.json`._"]
    for n, tr, sk, a in cands[:8]:
        lines.append(f"- `{tr}` → {sk}: fired {n}× interactive, adopted {a} ({round(100*a/n)}%)")
    return "\n".join(lines), len(cands)


def section_prune_candidates(per_trig):
    """Data-driven catalog-shrink signal for the O2 skill-router 'team model': a trigger→skill
    pair suggested >=5x in interactive sessions this window with ZERO adoptions is pure noise —
    prune it from `~/.claude/hooks/skill-triggers.json`. Stricter than section_suggester_hygiene's
    <15%-of-3+ band (that flags 'too broad'); this flags 'never once useful, remove'.

    DSPy-objective note: this per-trigger suggest->adopt rate (and the finer-grained one in
    skill_adoption/section_suggester_hygiene) IS the objective metric for the 2026-08-03 DSPy
    phrasing-optimization checkpoint — triggers stuck at low/zero adoption over consecutive
    weeks are the first candidates DSPy auto-rephrases rather than manually pruning.
    """
    cands = [(n, tr, sk) for (tr, sk), (n, a) in per_trig.items() if n >= 5 and a == 0]
    if not cands:
        return "### ✂️ Prune candidates (0% adoption)\n- ✅ no prune candidates", 0
    cands.sort(reverse=True)  # most-fired-for-nothing first
    lines = ["### ✂️ Prune candidates (0% adoption)",
             "_Trigger→skill suggested ≥5x interactive, 0 adopted ⇒ noise. Prune in `~/.claude/hooks/skill-triggers.json`._"]
    for n, tr, sk in cands[:8]:
        lines.append(f"- ⚠️ prune candidates: `{tr}` → {sk} ({n}x suggested, 0 adopted)")
    return "\n".join(lines), len(cands)


def section_o2_rollout(y, w):
    """O2 orchestrator rollout-detection precision: of the sessions where the skill-router
    suggested the O2 pipeline this week (mode:"rollout" in skill-suggestions.jsonl), how many
    then actually invoked /o2-run (skill-fires.jsonl, skill=="o2-run") in that same session?
    Yardstick: >=50% precision ✅ keep · >=25% 👀 watch · else ⚠️ investigate the rollout trigger.
    """
    sugg = week_rows("skill-suggestions.jsonl", y, w)
    raw_sids = {r.get("session_id") for r in sugg if r.get("mode") == "rollout" and r.get("session_id")}
    if not raw_sids:
        return "### 🧭 O2 rollout precision\n- no rollout suggestions this week", None
    # Interactive filter (same discipline skill_adoption uses since 2026-07-09): the rollout
    # detector fires on HEADLESS one-shots too (session-end synth, audit jobs) — those can NEVER
    # invoke /o2-run, so counting them in the denominator makes O2 look "0%/ineffective" when the
    # real cause is a polluted metric (all-time: 21 of 27 rollout sessions were headless). Drop them.
    idx = _transcript_index()
    rollout_sids = set()
    headless_dropped = 0
    for sid in raw_sids:
        p = idx.get(sid)
        if p is None:
            headless_dropped += 1  # no transcript → can't confirm adoptable, exclude
            continue
        if _scan_transcript(p)[1]:
            rollout_sids.add(sid)
        else:
            headless_dropped += 1
    if not rollout_sids:
        return ("### 🧭 O2 rollout precision\n"
                f"- no INTERACTIVE rollout suggestions this week ({headless_dropped} headless one-shots excluded)", None)
    fired_sids = {r.get("session_id") for r in read_jsonl("skill-fires.jsonl") if r.get("skill") == "o2-run"}
    total = len(rollout_sids)
    hit = len(rollout_sids & fired_sids)
    pct = round(100 * hit / total) if total else 0
    # Thin-data gate (spec-004 doctrine, MIN_FIRES): a %-ratio on n<3 is coin-flip noise —
    # 0/1 must NOT read as "ineffective" (it flipped a 40% stream to a false ⚠️). O2 rollout is
    # a lower-base-rate stream than tool-fires, so the floor is 3, not spec-004's 5.
    O2_MIN_SUGGESTIONS = 3
    if total < O2_MIN_SUGGESTIONS:
        verdict = f"👀 watch — thin data (n={total}<{O2_MIN_SUGGESTIONS}, no verdict yet)"
    else:
        verdict = ("✅ keep" if pct >= 50 else
                   "👀 watch" if pct >= 25 else
                   "⚠️ ineffective — investigate rollout trigger")
    return "\n".join([
        "### 🧭 O2 rollout precision",
        f"- O2 rollout precision: {hit}/{total} interactive suggestions → `/o2-run` ({pct}%)"
        + (f"  _({headless_dropped} headless excluded)_" if headless_dropped else ""),
        f"- **{verdict}**",
    ]), pct


def compaction_stats(y, w):
    """Compaction-synth effectiveness. synth-session.sh re-runs a Haiku summary on EVERY
    PreCompact. In a long single-window session compaction fires many times/day, but the diary
    write is idempotent-per-day — so most runs re-pay Haiku and discard the summary (wrote:[]).
    Measures real-write rate + money burned on empty writes. Windows on `synthesized_at`."""
    rows = [r for r in read_jsonl("synth-done.jsonl") if in_week(r.get("synthesized_at"), y, w)]

    def kind(r):
        wl = r.get("wrote") or []
        if not wl:
            return "empty"
        if wl == ["diary-fallback"]:
            return "fallback"
        return "real"

    total = len(rows)
    real = sum(1 for r in rows if kind(r) == "real")
    empty = sum(1 for r in rows if kind(r) == "empty")
    fallback = sum(1 for r in rows if kind(r) == "fallback")
    return {
        "total": total,
        "precompact": sum(1 for r in rows if r.get("event") == "PreCompact"),
        "real": real, "empty": empty, "fallback": fallback,
        "cost": sum(float(r.get("cost_usd") or 0) for r in rows),
        "wasted": sum(float(r.get("cost_usd") or 0) for r in rows if not (r.get("wrote") or [])),
        "real_pct": round(100 * real / total) if total else None,
    }


def section_compaction(c):
    """🗜 Compaction — verdict, not raw counts (measure-what-you-ship rule).
    Yardstick = real-write rate: >=60% ✅ keep · >=30% 👀 watch · <30% ⚠️ investigate."""
    if not c["total"]:
        return None
    rp = c["real_pct"]
    verdict = ("✅ working — keep" if rp >= 60 else
               "👀 watch — re-check next week" if rp >= 30 else
               "⚠️ ineffective — investigate (→ evaluation-backlog)")
    return "\n".join([
        "### 🗜 Compaction synth",
        f"- **{c['precompact']}** PreCompact + **{c['total'] - c['precompact']}** SessionEnd synth runs · "
        f"real write **{rp}%** ({c['real']}) · fallback {c['fallback']} · empty {c['empty']}",
        f"- synth cost **${c['cost']:.2f}**/wk · **${c['wasted']:.2f} wasted** "
        f"(paid Haiku, wrote nothing — idempotent skip)",
        f"- **{verdict}**",
    ])


def section_burn():
    try:
        r = subprocess.run([sys.executable, str(BURN)], capture_output=True, text=True, timeout=60)
        return r.stdout.strip() or "_(burn-metrics produced no output)_"
    except Exception as e:
        return f"_(burn-metrics failed: {e})_"


def constitution_verdict(rows):
    """Deterministic yardstick for the constitution surface. The USE-rate (<60% kill-threshold,
    spec §1) is judged by the weekly report.py narrative over transcripts; this is the fire signal."""
    return ("✅ surfacing", len(rows)) if rows else ("👀 not surfaced this week", 0)


def section_hooks(y, w):
    lines = ["### 🪝 Hooks fired (this week)"]
    sugg = week_rows("skill-suggestions.jsonl", y, w)
    top = {}
    for r in sugg:
        top[r.get("skill", "?")] = top.get(r.get("skill", "?"), 0) + 1
    top_str = ", ".join(f"{k}×{v}" for k, v in sorted(top.items(), key=lambda x: -x[1])[:5]) or "—"
    lines.append(f"- skill-suggest: **{len(sugg)}** nudges across {len(top)} skills  (top: {top_str})")

    vg = week_rows("verify-gate.jsonl", y, w)
    lines.append(f"- verify-gate: **{len(vg)}** unverified-completion claims")

    # bash-batch-guard (connectpro token audit 2026-10-19): PreToolUse Bash deny of the 4th
    # one-liner per user turn. Yardstick is the OUTCOME in token-audit below, not fires:
    # requests/turn must fall >=25% (7.4 -> <=5.5) by 2026-11-19 or the guard is removed.
    # Many denies with flat requests/turn = the guard is being routed around (seq-escapes).
    bbg = week_rows("bash-batch-guard.jsonl", y, w)
    bbg_deny = sum(1 for r in bbg if r.get("verdict") == "deny")
    bbg_seq = sum(1 for r in bbg if r.get("verdict") == "seq-escape")
    lines.append(f"- bash-batch-guard: **{len(bbg)}** one-liner checks ({bbg_deny} denies, {bbg_seq} seq-escapes)")
    # token-audit = the outcome stream for the same audit (base trim, batching, delegation).
    # Reads connectpro's session transcripts for the last 7 days; baseline 30d 2026-10-19:
    # 7.38 req/turn, 67% tiny Bash, base 86k, subagents 27.5%, 2.74 Mtok/h.
    tok: dict = {}
    try:
        out = subprocess.run([sys.executable, str(Path.home() / ".claude/scripts/token-audit.py"),
                              "-Users-rusakovanton-connectpro", "--days", "7", "--json"],
                             capture_output=True, text=True, timeout=150).stdout.strip().splitlines()
        tok = json.loads(out[-1]) if out else {}
    except Exception:
        tok = {}
    if tok:
        lines.append(f"- token-audit (connectpro 7d, {tok.get('sessions')} sessions): **{tok.get('requests_per_turn')}** "
                     f"requests/turn (target ≤4.5) · tiny-Bash {tok.get('bash_small_pct')}% (≤40) · base "
                     f"{tok.get('mean_base_k')}k (≤68) · subagents {tok.get('subagent_pct')}% (≤10) · "
                     f"{tok.get('tokens_per_hour_M')} Mtok/h (baseline 2.74)")
    else:
        lines.append("- token-audit: ⚠️ no data (script failed or no connectpro sessions this week)")

    # test-on-commit-guard = the commit-time guard that runs tests before a commit lands.
    # 99+ fires/week but was invisible until the 2026-07-08 stack audit. Yardstick: fires are
    # the healthy state (guard is active); a sustained drop to 0 while commits continue = the
    # guard silently broke. Log: {"action":"pass|block","reason":...,"cmd":...,"root":...,"ts":...}.
    tcg = week_rows("test-on-commit-guard.jsonl", y, w)
    tcg_blocks = sum(1 for r in tcg if r.get("action") == "block")
    lines.append(f"- test-on-commit-guard: **{len(tcg)}** fires ({tcg_blocks} blocks)")

    cc = week_rows("correction-candidates.jsonl", y, w)
    unrev = [r for r in cc if not r.get("reviewed")]
    lines.append(f"- correction-detector: **{len(cc)}** candidates ({len(unrev)} unreviewed)")

    # gbrain-mcp-source = which BOX the MCP server was pinned to at launch (spec 003).
    # Watched because the failure is silent by construction: a wrong pin returns [] , which
    # reads as "no prior work" — indistinguishable from the truth. Five fixes shipped against
    # this before anyone asserted the box. One line per MCP launch, not per query.
    ms = week_rows("gbrain-mcp-source.jsonl", y, w)
    ms_out: dict[str, int] = {}
    for r in ms:
        o = r.get("outcome", "?")
        ms_out[o] = ms_out.get(o, 0) + 1
    ms_str = ", ".join(f"{k}×{v}" for k, v in sorted(ms_out.items(), key=lambda x: -x[1])) or "—"
    # Yardstick (spec §5.2) — outcome, not fires:
    #   ⚠️ any resolver_failed/timeout, or a pin of `default` (the frozen corpus = the bug)
    #   👀 any `unregistered` (a project without a box, or a cwd outside every project)
    #   ✅ every launch pinned, and the pin matches the cwd
    ms_bad = ms_out.get("resolver_failed", 0) + ms_out.get("timeout", 0)
    ms_default = len([r for r in ms if r.get("source") == "default"])
    # "MCP is searching a dead box" is present tense — so judge whether the fault is LIVE
    # (last 50 launches) and let the week's tally be the count. Judging on the week kept this
    # ⚠️ as must-act's #1 forced action 8h after the fix, over 260 clean launches.
    ms_is_bad = (lambda r: r.get("outcome") in ("resolver_failed", "timeout")
                 or r.get("source") == "default")
    if fault_is_live(ms, ms_is_bad):
        ms_flag = "  ⚠️ resolver failing / pinned the frozen `default` — MCP is searching a dead box"
    elif ms_bad or ms_default:
        last_bad = max((str(r.get("ts", "")) for r in ms if ms_is_bad(r)), default="")
        clean_since = len(ms) - max((i for i, r in enumerate(ms) if ms_is_bad(r)), default=0) - 1
        ms_flag = (f"  ✅ снова чисто — последний промах {last_bad[:16] or '?'}, "
                   f"{clean_since} чистых запусков подряд ({ms_bad + ms_default} за неделю)")
    elif ms_out.get("unregistered"):
        ms_flag = "  👀 some launches pinned nothing (cwd outside any registered project)"
    else:
        ms_flag = ""
    lines.append(f"- gbrain-mcp-source: **{len(ms)}** MCP launches — {ms_str}{ms_flag}")

    # gbrain-preflight = the READ side of GBrain. We watch it because the corpus is
    # useless if nothing queries it: 0 fires/week ⇒ intent-gate broke or brain went empty.
    gb = week_rows("gbrain-preflight.jsonl", y, w)
    gb_hits = [r for r in gb if r.get("matched", 0) >= 1]
    gb_rate = f"{round(100 * len(gb_hits) / len(gb))}%" if gb else "—"
    gb_flag = "  ⚠ silent — corpus unqueried" if not gb else ""
    lines.append(f"- gbrain-preflight: **{len(gb)}** fires, {len(gb_hits)} hits ({gb_rate} hit-rate){gb_flag}")
    # degrade-tier telemetry (G5, graft 2026-07-04): gbrain-preflight.sh now tags every fire
    # with tier: semantic (real query) | timeout | error | empty (no match). timeout+error =
    # the READ side silently failing rather than genuinely finding nothing — watch that share.
    gb_tiers: dict[str, int] = {}
    for r in gb:
        t = r.get("tier", "?")
        gb_tiers[t] = gb_tiers.get(t, 0) + 1
    gb_tier_str = ", ".join(f"{k}×{v}" for k, v in sorted(gb_tiers.items(), key=lambda x: -x[1])) or "—"
    gb_bad = gb_tiers.get("timeout", 0) + gb_tiers.get("error", 0)
    gb_bad_pct = round(100 * gb_bad / len(gb)) if gb else 0
    gb_tier_flag = "  ⚠️ silent-degrade — timeout+error >30% of fires" if gb and gb_bad_pct > 30 else ""
    lines.append(f"  - tier breakdown: {gb_tier_str}{gb_tier_flag}")
    # Anton-only view: strip headless/subagent prompts (classifier & extraction agents,
    # session-synthesizer) — they were 64% of fires and don't reflect real query intent. The
    # hook now drops most at source (2026-06-05), but historical rows + the "#…synthesizer"
    # shape still land here, so the aggregate hit-rate above understates what Anton sees.
    _headless = ("You decide", "You are", "You will", "You must", "Your task",
                 "Analyze ", "Extract ", "Classify ", "# ")
    anton = [r for r in gb if not str(r.get("ph", "")).lstrip().startswith(_headless)]
    anton_hits = [r for r in anton if r.get("matched", 0) >= 1]
    anton_rate = f"{round(100 * len(anton_hits) / len(anton))}%" if anton else "—"
    lines.append(f"  - Anton-only: **{len(anton)}** fires, {len(anton_hits)} hits ({anton_rate} hit-rate)")
    # no-output = the brain query returned nothing (empty engine / timeout / bad query). A high
    # share is the early warning that the READ side is degrading even while fires look healthy.
    nob = [r for r in gb if r.get("reason") == "no-output"]
    nob_rate = round(100 * len(nob) / len(gb)) if gb else 0
    nob_flag = "  ⚠ READ side degrading" if gb and nob_rate > 25 else ""
    lines.append(f"  - no-output: **{len(nob)}** ({nob_rate}% of fires){nob_flag}")
    # (reuse-catalog metric removed 2026-07-15 — research/reusable-assets-catalog.md RETIRED after
    # 0 surfacings/4wk; BUILT.md is the replacement "what already exists" surface. The general
    # top_slug logging in gbrain-preflight stays; only the catalog-specific counter was dropped.)

    # decision-preflight = the double-check before a decision is fixed (sibling of gbrain-
    # preflight, decision-intent gate). Low-volume by nature — 0/week is normal (few real
    # decisions), NOT a broken gate, so no silent-alarm flag here.
    dp = week_rows("decision-preflight.jsonl", y, w)
    dp_hits = [r for r in dp if r.get("matched", 0) >= 1]
    dp_rate = f"{round(100 * len(dp_hits) / len(dp))}%" if dp else "—"
    lines.append(f"- decision-preflight: **{len(dp)}** fires, {len(dp_hits)} surfaced priors ({dp_rate} hit-rate)")

    # swarm-nudge = the Kimi-routing nudge fired when a code-writing sub-agent is dispatched.
    # Watches whether code delegation is steered OFF the paid Anthropic/Max quota onto Kimi
    # swarm-write. Pair with the 🟣 Kimi section: nudges high but claude-kimi flat ⇒ ignored.
    sn = week_rows("swarm-nudge.jsonl", y, w)
    lines.append(f"- swarm-nudge: **{len(sn)}** code-delegation nudges → Kimi swarm-write")
    # message-decompose = the multi-intent input gate (backfilled 2026-06-07). Volume only.
    md = week_rows("message-decompose-preflight.jsonl", y, w)
    lines.append(f"- message-decompose: **{len(md)}** multi-intent nudges")
    # open-threads-nudge = mid-session staleness reminder to reconcile open-threads.md before the
    # next compaction wipes live context (shipped 2026-06-30). Pairs with session-start.sh re-inject
    # on source=compact. High count ⇒ ledger left stale often; 0 ⇒ kept fresh (the good case).
    otn = week_rows("open-threads-nudge.jsonl", y, w)
    lines.append(f"- open-threads-nudge: **{len(otn)}** stale-ledger reminders (0 = kept fresh, the good case)")

    # browser-usage = web-fetch tool selection (bh-headless / trafilatura / jina). Logged by
    # ~/Developer/browser-harness/bh-headless.sh and webfetch-clean. Log contract:
    # {"ts":"<iso8601>","tool":"<bh-headless|trafilatura|jina>","mode":"<screenshot|--md|--txt>","host":"<domain>"}
    # Yardstick: N > 0 this week ✅ in use; 0 for 2 consecutive weeks 👀 idle — check producers.
    bu = week_rows("browser-usage.jsonl", y, w)
    bu_by_tool: dict[str, int] = {}
    for r in bu:
        t = r.get("tool", "?")
        bu_by_tool[t] = bu_by_tool.get(t, 0) + 1
    bu_breakdown = ", ".join(f"{k}×{v}" for k, v in sorted(bu_by_tool.items(), key=lambda x: -x[1])) or "—"
    bu_verdict = "✅ in use" if bu else "👀 idle — 0 fires (check bh-headless.sh + webfetch-clean producers)"
    lines.append(f"- browser-usage: **{len(bu)}** fires ({bu_breakdown}) — {bu_verdict}")

    # research-runs = multi-track research executions logged by the research-briefing skill
    # (shipped 2026-07-02, pipeline audit). Log contract:
    # {"ts":"<iso8601>","skill":"research-briefing","topic":"<slug>","tracks":N,"grounding_pass":bool}
    # Low-volume by nature — 0/week is normal (research is bursty), so no silent-alarm flag.
    # Yardstick: when runs exist, grounding_pass should be true on all of them (the new citation-
    # grounding pass actually running); false ⇒ the skill edit isn't converting — investigate.
    rr = week_rows("research-runs.jsonl", y, w)
    rr_grounded = sum(1 for r in rr if r.get("grounding_pass"))
    lines.append(f"- research-runs: **{len(rr)}** runs, {rr_grounded} with citation-grounding pass")

    # constitution surface (system-that-leads-anton Rung 1, shipped 2026-06-30). Each fire = the
    # applicable constitution rule forced to the surface at a decision point.
    cs_rows = week_rows("constitution-surface.jsonl", y, w)
    cs_verdict, cs_n = constitution_verdict(cs_rows)
    lines.append(f"  - constitution surfaced: **{cs_n}** ({cs_verdict})")

    # constitution-clusters (GLM long-context clustering pass, shipped 2026-07-04). Latest run
    # only (not windowed to this week) — the pass runs on a cadence, not per-session; the signal
    # is "did it run recently", not a weekly count.
    cc_rows = read_jsonl("constitution-clusters.jsonl")
    if cc_rows:
        last = cc_rows[-1]
        try:
            last_ts = datetime.fromisoformat(last.get("ts", "").replace("Z", "+00:00"))
            stale = (datetime.now(timezone.utc) - last_ts) > timedelta(days=14)
        except Exception:
            stale = True
        cc_flag = "  ⚠️ no run in 14d" if stale else ""
        lines.append(f"  - constitution-clusters: **{last.get('clusters', '?')}** clusters / "
                     f"{last.get('instances', '?')} instances (latest run){cc_flag}")
    else:
        lines.append("  - constitution-clusters: no run yet")

    # G3 (graft 2026-07-04): nightshift STOP+budget gate. States pr|failed|skipped per task.
    # The plist IS installed and firing nightly at 02:00 (com.local.nightshift) — the old
    # comment here still claimed the log "has never existed", stale since 2026-07-04.
    #
    # Heartbeats are NOT tasks (fixed 2026-07-16): a `night_run` row is the cron proving it
    # woke up, with no `state`. Counting them as tasks made the weekly read "night-shift:
    # 3 tasks (?×3)" while the queue had been EMPTY every night since 07-04 and the only real
    # task (the 07-04 smoke test) failed. An idle pipeline that reports activity is worse than
    # one that reports nothing — the dashboard was the only thing still working.
    ns_rows = week_rows("night-shift.jsonl", y, w)
    ns_tasks = [r for r in ns_rows if r.get("state") or r.get("task")]
    ns_beats = [r for r in ns_rows if r not in ns_tasks]
    if ns_tasks:
        ns_by_state: dict[str, int] = {}
        for r in ns_tasks:
            st = r.get("state", "?")
            ns_by_state[st] = ns_by_state.get(st, 0) + 1
        ns_str = ", ".join(f"{k}×{v}" for k, v in sorted(ns_by_state.items(), key=lambda x: -x[1]))
        ns_stop = any("STOP" in str(r.get("note", "")) for r in ns_tasks if r.get("state") == "skipped")
        ns_stop_note = "  (STOP file present this week)" if ns_stop else ""
        lines.append(f"- night-shift: **{len(ns_tasks)}** tasks ({ns_str}){ns_stop_note}")
    elif ns_beats:
        # 👀, not ⚠️ — the line already said "idle, not broken", and must-act reads the GLYPH,
        # not the prose next to it, so ⚠️ handed the "чиню #1" slot to a pipeline that works.
        # An empty queue means Anton dictated nothing; absence of input is not a fault. The row
        # stays visible on purpose (Anton, 2026-07-17: «не снимай, чтобы просто об этом
        # функционале помнить») — 👀 is exactly that: remembered, watched, not forced.
        lines.append(f"- night-shift: **0** tasks — ran {len(ns_beats)}× on schedule, "
                     f"queue empty every night 👀 (пайплайн жив, ждёт вечернюю надиктовку)")
    else:
        lines.append("- night-shift: not scheduled yet (RAT pending)")

    # The two GATES (built 2026-07-05, unwatched until 2026-07-16 — their rows were misparsed
    # by instrument-coverage, so they never showed up as missing a consumer).
    # Yardstick is the OVERRIDE rate, not the fire count: a gate that gets overridden is a gate
    # that costs friction and buys nothing. Each row's own kill-threshold says so.
    gate_snap: dict[str, int] = {}  # merged into the returned stats — `snap` lives in main()
    for log_name, label, note in (
        ("test-on-commit-guard.jsonl", "testgate", "тесты перед коммитом"),
        ("operating-loop-guard.jsonl", "oploop-guard", "метрики на плановом гейте"),
    ):
        if not (LOGS / log_name).exists():
            continue
        g_all = read_jsonl(log_name)
        g_wk = week_rows(log_name, y, w)
        acts: dict[str, int] = {}
        for r in g_all:
            a = r.get("action", "?")
            acts[a] = acts.get(a, 0) + 1
        tot = sum(acts.values())
        # The VERDICT is computed on this week only; the all-time counts stay as context.
        # First draft (mine, ec6ba28) flagged on the all-time rate, which is a truncation in
        # the TIME axis: 54 of the 56 overrides happened 07-11..07-13 from one forgotten
        # /tmp/cc-testgate-off, and an all-time denominator would have kept "⚠️ обходят" lit
        # forever — including now, hours after the cause (connectpro's red suite) was fixed.
        # A dashboard that cannot stop complaining teaches you to stop reading it.
        wk_acts: dict[str, int] = {}
        for r in g_wk:
            a = r.get("action", "?")
            wk_acts[a] = wk_acts.get(a, 0) + 1
        wk_tot = sum(wk_acts.values())
        wk_ov = wk_acts.get("override", 0)
        ov_pct = round(100 * wk_ov / wk_tot) if wk_tot else 0
        # The FLAG rides the last 50 runs, not the week — same reason fault_is_live() exists.
        # "Is the gate being routed around" is a question about NOW, and a 7-day denominator
        # answers it with a cluster that is already dead: the 07-13 spike came from one
        # forgotten /tmp/cc-testgate-off, whose cause (connectpro's red suite) was fixed
        # 2026-07-16 and whose switch now self-expires after 2h (8592ed7). The week still says
        # 13% ⚠️; the last 50 runs say 8% and no override since the TTL landed. The tail is the
        # one that can go quiet when the fix works — so it is the one the verdict hangs on.
        # The weekly mix stays rendered below as context; it just no longer decides.
        tail = g_all[-50:]
        t_ov = sum(1 for r in tail if r.get("action") == "override")
        t_pct = round(100 * t_ov / len(tail)) if tail else 0
        flag = f" ⚠️ обходят ({t_pct}% из последних {len(tail)})" if t_pct > 10 else ""
        # Render the actions the log ACTUALLY carries, not a hardcoded block/pass/override
        # triple: oploop-guard was re-wired to a UserPromptSubmit nudge (ab0c1d9) and writes
        # {"action":"nudge"}, so the fixed triple rendered "block×0 · pass×0 · override×0"
        # next to 4 real fires — three zeros that read as "nothing happened".
        mix = " · ".join(f"{a}×{n}" for a, n in sorted(wk_acts.items(), key=lambda kv: -kv[1]))
        pct = f" = обходов {ov_pct}%" if wk_ov else ""
        lines.append(
            f"- {label} ({note}): **{len(g_wk)}** за неделю ({mix or '—'}{pct}){flag}"
            f" · всего за всё время {tot}, обходов {acts.get('override', 0)}"
        )
        gate_snap[label.replace("-", "_")] = len(g_wk)
        any_line = True

    # moshi-hook (graft 2026-07-04, weekly-coverage-audit widest blind-spot fix): phone-pairing
    # notification bridge, wired on SessionStart/UserPromptSubmit/Stop/PermissionRequest via the
    # moshi-hook-logged.sh wrapper. Log contract: {"ts":"<iso>","event":"<hook>","rc":<int>}.
    # Kill-threshold: 0 fires in 2 weeks with sessions active → bridge dead, investigate/unwire.
    mh = week_rows("moshi-hook.jsonl", y, w)
    mh_by_event: dict[str, int] = {}
    for r in mh:
        ev = r.get("event", "?")
        mh_by_event[ev] = mh_by_event.get(ev, 0) + 1
    mh_breakdown = ", ".join(f"{k}×{v}" for k, v in sorted(mh_by_event.items(), key=lambda x: -x[1])) or "—"
    mh_verdict = "✅ firing" if mh else "👀 0 fires this week"
    lines.append(f"- moshi-hook: **{len(mh)}** fires ({mh_breakdown}) — {mh_verdict}")

    # NB: opus-blocks.jsonl is the main-loop write-production-code warn log — already
    # represented in the 🔥 Burn section above (write-prod-code warns). The only opus
    # signal worth a separate line is agent-dispatch blocks, which the burn tripwire shows.

    for label, done, err in [
        ("precompact-synth", "synth-done.jsonl", "synth-errors.jsonl"),
        ("research-ingest", "research-ingest-done.jsonl", "research-ingest-errors.jsonl"),
        ("skill-suggest", None, "skill-errors.jsonl"),
    ]:
        d = len(week_rows(done, y, w)) if done else None
        e = len(week_rows(err, y, w))
        seg = f"{d} runs, " if d is not None else ""
        flag = "  ⚠" if e else ""
        lines.append(f"- {label}: {seg}{e} errors{flag}")
    return "\n".join(lines), {**gate_snap,
                              "sugg": sugg, "top": top, "verify": len(vg),
                              "bbg_deny": bbg_deny, "tok_rpt": tok.get("requests_per_turn"),
                              "tok_small": tok.get("bash_small_pct"), "tok_base": tok.get("mean_base_k"),
                              "tok_sub": tok.get("subagent_pct"), "tok_mtok_h": tok.get("tokens_per_hour_M"),
                              "corr_unrev": len(unrev),
                              "swarm_nudge": len(sn), "msg_decomp": len(md),
                              "browser_fires": len(bu), "moshi_fires": len(mh),
                              "mcp_src_launches": len(ms),
                              # The OUTCOME number, not the activity one: launches that ended up
                              # in the wrong box (resolver died/timed out, or pinned the frozen
                              # `default`). Yardstick is 0 — anything above means a session
                              # searched a dead corpus and read the silence as "no prior work".
                              "mcp_src_bad": ms_bad + ms_default,
                              "consti_clusters": (cc_rows[-1].get("clusters") if cc_rows else None)}


DIARY_DIR = HOME / ".claude" / "projects" / "-Users-rusakovanton" / "memory" / "diary"
TRAJ_DIR = HOME / ".claude" / "projects" / "-Users-rusakovanton" / "memory" / "trajectories"


def section_episodic_adoption(y, w):
    """#6 structured episodic notes — derived adoption metric (no new stream). Of the
    diary+trajectory .md files CREATED this week, what fraction carry the `**Facts:**`
    heading from templates/episodic-note.md (adoption of the structured schema, not
    just narrative prose). Kill-threshold: <50% adoption after 3 weeks ⇒ the diary-
    writing mandate isn't wired — fix the habit hook, not the template."""
    total = adopted = 0
    for d in (DIARY_DIR, TRAJ_DIR):
        if not d.exists():
            continue
        for p in d.glob("*.md"):
            try:
                mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
                my, mw, _ = mtime.isocalendar()
            except Exception:
                continue
            if (my, mw) != (y, w):
                continue
            total += 1
            try:
                if "**Facts:**" in p.read_text(encoding="utf-8", errors="replace"):
                    adopted += 1
            except Exception:
                pass
    if not total:
        return "- structured-notes adoption: _(no diary/trajectory files created this week)_", None
    pct = round(100 * adopted / total)
    return f"- structured-notes adoption: **{adopted}/{total}** ({pct}%)", pct


def read_kimi(y, w):
    """Kimi usage log (TSV: ts<TAB>tool<TAB>mode<TAB>detail). Fail-safe — bad line skipped."""
    if not KIMI_LOG.exists():
        return []
    rows = []
    try:
        for line in KIMI_LOG.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = line.split("\t")
            if len(parts) < 2 or not in_week(parts[0], y, w):
                continue
            rows.append({"tool": parts[1],
                         "mode": parts[2] if len(parts) > 2 else "",
                         "detail": parts[3] if len(parts) > 3 else ""})
    except Exception:
        return []
    return rows


def read_glm(y, w):
    """GLM (z.ai) usage log (TSV: ts<TAB>glm<TAB>worker). Fail-safe — bad line skipped."""
    if not GLM_LOG.exists():
        return []
    rows = []
    try:
        for line in GLM_LOG.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = line.split("\t")
            if len(parts) < 2 or not in_week(parts[0], y, w):
                continue
            rows.append({"tool": "claude-glm",
                         "mode": parts[2] if len(parts) > 2 else "",
                         "detail": ""})
    except Exception:
        return []
    return rows


def section_kimi(y, w):
    """Kimi subscription escape-hatch / delegation usage. GLM (z.ai) replaced Kimi as the
    flat-quota engine 2026-07-03 (Kimi lapsed) — this section now sums claude-kimi (legacy,
    should taper to 0) + claude-glm (current) so the offload-habit signal doesn't silently
    drop to zero on the switch. The point of claude-kimi/claude-glm + kimi-do is to spare
    Opus/Max quota on bulk/mechanical work. 0 all week ⇒ either Max never strained (fine) or the
    offload habit didn't form; heavy use ⇒ watch the membership quota. This is where delegation either
    pays off or silently does nothing — exactly the 'where does it leak' signal."""
    rows = read_kimi(y, w) + read_glm(y, w)
    ck = [r for r in rows if r["tool"] == "claude-kimi"]
    gl = [r for r in rows if r["tool"] == "claude-glm"]
    kd = [r for r in rows if r["tool"] == "kimi-do"]
    lines = ["### 🟣 Kimi/GLM subscription (escape-hatch / delegation)"]
    lines.append(f"- claude-kimi sessions: **{len(ck)}** · claude-glm sessions: **{len(gl)}** · kimi-do delegations: **{len(kd)}**")
    if kd:
        topics = ", ".join(sorted({r["detail"][:30] for r in kd if r["detail"]})[:5]) or "—"
        lines.append(f"- delegated topics: {topics}")
    if not rows:
        lines.append("- _(unused this week — fine if Max never hit limits; else the offload habit isn't forming)_")
    return "\n".join(lines), {"kimi_ck": len(ck), "kimi_kd": len(kd), "glm_ck": len(gl)}


def section_delegation(y, w):
    """🤝 Delegation & engines (D0 routing instrument). Reads routing-decisions.jsonl
    (one line per Agent dispatch, written by opus-routing-guard.py) + routing-inspect.jsonl
    (one line per `routing show`). Surfaces: where work routed (bucket/engine mix), and a
    manual-inspection counter whose growth is the baked-in trigger for standing up a per-call
    UI (Langfuse). Declared policy: ~/.claude/routing.yaml."""
    from collections import Counter
    rows = [r for r in read_jsonl("routing-decisions.jsonl") if in_week(r.get("ts"), y, w)]
    inspects = [r for r in read_jsonl("routing-inspect.jsonl") if in_week(r.get("ts"), y, w)]
    insp_n = len(inspects)
    lines = ["### 🤝 Delegation & engines"]
    if not rows:
        lines.append("- _(no delegations logged this week — main loop ran solo, or the routing hook didn't fire)_")
    else:
        buckets = Counter(r.get("bucket", "?") for r in rows)
        engines = Counter(r.get("engine", "?") for r in rows)
        lines.append(f"- delegations: **{len(rows)}** · buckets {dict(buckets)} · engines {dict(engines)}")
        # code-write routed to a paid sub-agent instead of flat-rate swarm = the leak this catches
        code_paid = sum(1 for r in rows if r.get("bucket") == "code-write"
                        and r.get("engine", "").endswith("-subagent"))
        if code_paid:
            lines.append(f"- ⚠️ {code_paid} code-write dispatch(es) went to a PAID sub-agent — candidate for swarm (Kimi/GLM)")
        glm = engines.get("swarm-glm", 0)
        ab_ready = "✅ GLM live" if glm else "⏳ GLM plumbing ready, key not set (Anton's hand)"
        lines.append(f"- engine A/B: {ab_ready} · need ≥20 code-write rows to compare (have {buckets.get('code-write',0)})")
    # manual-inspection counter → Langfuse-upgrade trigger
    verdict = "✅" if insp_n < 3 else ("👀" if insp_n < 6 else "⚠️")
    note = ("" if insp_n < 3
            else " — frequent hand-inspection; if this holds 2+ wks, stand up a per-call UI (Langfuse, deferred)")
    lines.append(f"- manual inspections (`routing show`): **{insp_n}** {verdict}{note}")
    return "\n".join(lines), {"routing_deleg": len(rows), "routing_inspect": insp_n}


def gbrain_usage(y, w):
    """Read-side telemetry: how the main loop actually queries GBrain this ISO week —
    semantic vs graph vs direct-lookup vs write mix, derived from CC transcripts (no per-fire
    hook; mcp__gbrain__* tool_use is the signal, windowed by each line's top-level timestamp).
    VOLUME, not quality: a graph-routing quality eval stays gated on evidence of mis-routing
    (measure-what-you-ship tier 2). Fail-open: unreadable transcripts skipped, never crash."""
    SEM = {"query", "search", "recall", "advisor", "think"}
    GRAPH = {"traverse_graph", "get_links", "get_backlinks", "find_trajectory", "find_experts",
             "find_contradictions", "find_anomalies", "find_orphans", "add_link", "remove_link"}
    DIRECT = {"get_page", "list_pages", "get_chunks", "get_stats", "get_health", "list_jobs"}
    c = {"sem": 0, "graph": 0, "direct": 0, "write": 0, "other": 0}
    cutoff = datetime.now(timezone.utc).timestamp() - 12 * 86400  # bound the scan to recent sessions
    for p in set(_transcript_index().values()):
        try:
            if p.stat().st_mtime < cutoff:
                continue
            txt = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for line in txt.splitlines():
            if "mcp__gbrain__" not in line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if not in_week(d.get("timestamp"), y, w):
                continue
            msg = d.get("message")
            cont = msg.get("content") if isinstance(msg, dict) else None
            if not isinstance(cont, list):
                continue
            for x in cont:
                if not (isinstance(x, dict) and x.get("type") == "tool_use"):
                    continue
                nm = x.get("name", "")
                if not nm.startswith("mcp__gbrain__"):
                    continue
                op = nm[len("mcp__gbrain__"):]
                if op in SEM:
                    c["sem"] += 1
                elif op in GRAPH or op.startswith("code_"):
                    c["graph"] += 1
                elif op in DIRECT:
                    c["direct"] += 1
                elif op.startswith(("put_", "sources_", "sync_", "submit_", "delete_", "put_raw",
                                    "add_tag", "add_timeline")):
                    c["write"] += 1
                else:
                    c["other"] += 1
    return c


def section_gbrain_maintenance(y, w):
    """GBrain-side maintenance streams, live but uncounted until now (weekly-coverage-audit
    2026-07-04, gap #4): gbrain-volunteer (UserPromptSubmit), gbrain-advisor-weekly (launchd,
    Mon), memory-to-brain (launchd, nightly), gbrain-glm-usage (dream-nightly's own $ ledger —
    money-adjacent, so weekly token totals per model). Fail-open per stream: a stream whose
    log file doesn't exist yet is omitted entirely, never rendered as a false zero."""
    lines = ["### 🌱 GBrain maintenance streams"]
    snap = {}
    any_line = False

    # read-side query mix (semantic vs graph) — derived from transcripts, always computable
    gu = gbrain_usage(y, w)
    gu_total = sum(gu.values())
    if gu_total:
        gshare = round(100 * gu["graph"] / gu_total)
        lines.append(
            f"- gbrain-usage (read-side mix): **{gu_total}** calls — "
            f"semantic {gu['sem']} · graph {gu['graph']} ({gshare}%) · direct {gu['direct']} · write {gu['write']}"
            "  _(👀 volume, not quality — graph-routing eval gated on evidence)_"
        )
    else:
        lines.append("- gbrain-usage (read-side mix): **0** calls this week  ⚠️ brain unused — check preflight/MCP")
    snap["gb_usage_total"] = gu_total
    snap["gb_usage_sem"] = gu["sem"]
    snap["gb_usage_graph"] = gu["graph"]
    snap["gb_usage_direct"] = gu["direct"]
    any_line = True

    if (LOGS / "gbrain-volunteer.jsonl").exists():
        vol = week_rows("gbrain-volunteer.jsonl", y, w)
        vol_hits = [r for r in vol if r.get("matched", 0) >= 1]
        lines.append(f"- gbrain-volunteer: **{len(vol)}** fires, {len(vol_hits)} matched")
        snap["gb_volunteer"] = len(vol)
        any_line = True

    if (LOGS / "gbrain-advisor-runs.jsonl").exists():
        adv = week_rows("gbrain-advisor-runs.jsonl", y, w)
        crit = sum(r.get("critical", 0) or 0 for r in adv)
        warn = sum(r.get("warn", 0) or 0 for r in adv)
        flag = "  ⚠️" if crit else ""
        lines.append(f"- gbrain-advisor-weekly: **{len(adv)}** run(s), {crit} critical / {warn} warn{flag}")
        snap["gb_advisor"] = len(adv)
        any_line = True

    if (LOGS / "memory-to-brain.jsonl").exists():
        m2b = week_rows("memory-to-brain.jsonl", y, w)
        by_stage = {}
        for r in m2b:
            st = r.get("stage", "?")
            by_stage[st] = by_stage.get(st, 0) + 1
        stage_str = ", ".join(f"{k}×{v}" for k, v in sorted(by_stage.items(), key=lambda x: -x[1])) or "—"
        lines.append(f"- memory-to-brain: **{len(m2b)}** stage-events ({stage_str})")
        snap["mem2brain"] = len(m2b)
        any_line = True

    # diary-to-brain = the EPISODIC loop (daily diary → gbrain), sibling of memory-to-brain but
    # distinct (episodic vs semantic memory). Surfaced 2026-07-08 audit — it feeds the brain
    # daily yet was invisible. Yardstick: syncs should appear ~daily; 0 for 2+ days = the
    # episodic feed broke. Log: {"ts":...,"stage":"sync","detail":...}.
    if (LOGS / "diary-to-brain.jsonl").exists():
        d2b = week_rows("diary-to-brain.jsonl", y, w)
        lines.append(f"- diary-to-brain: **{len(d2b)}** episodic-loop syncs (diary → brain)")
        snap["diary2brain"] = len(d2b)
        any_line = True

    # agent-usage = who we delegate to, and how long it takes. Live since 2026-07-13, read only
    # by the automation-radar for its own scoring — nothing ever showed a human the delegation
    # mix. 316 rows sat unrendered (audit 2026-07-16). Yardstick: a registered agent_type that
    # never appears is a dead agent; `?` rows are the hook failing to capture the type.
    # NB deliberately NOT median tokens, which the registration row promised: `tokens` is null
    # in 316/316 rows — the hook writes the key and never fills it. Rendering a field that is
    # always null is how a dashboard starts lying. duration_ms IS present 316/316.
    if (LOGS / "agent-usage.jsonl").exists():
        au = week_rows("agent-usage.jsonl", y, w)
        by_type = {}
        for r in au:
            by_type.setdefault(r.get("agent_type") or "?", []).append(r)
        if by_type:
            parts = []
            for t, rs in sorted(by_type.items(), key=lambda kv: -len(kv[1]))[:5]:
                ds = sorted(r.get("duration_ms") or 0 for r in rs)
                med = ds[len(ds) // 2] if ds else 0
                parts.append(f"{t} ×{len(rs)}" + (f" (~{med // 1000}s)" if med > 999 else ""))
            lines.append(f"- agent-usage: **{len(au)}** делегаций — " + " · ".join(parts))
            snap["agent_runs"] = len(au)
            any_line = True

    # dream-nightly's own token/spend ledger — distinct from claude-glm-usage.log (that's the
    # main-loop engine switch); this one is the dream cycle's $ cost, invisible until now.
    if (LOGS / "gbrain-glm-usage.jsonl").exists():
        glm = week_rows("gbrain-glm-usage.jsonl", y, w)
        by_model = {}
        for r in glm:
            for m in (r.get("by_model") or []):
                mod = m.get("model", "?")
                d = by_model.setdefault(mod, {"tin": 0, "tout": 0})
                d["tin"] += m.get("tin", 0) or 0
                d["tout"] += m.get("tout", 0) or 0
        mod_str = " · ".join(f"{k}: {v['tin']//1000}k in / {v['tout']//1000}k out"
                             for k, v in by_model.items()) if by_model else "—"
        lines.append(f"- gbrain-glm-usage (dream-cycle spend): **{len(glm)}** run(s) — {mod_str}")
        any_line = True

    if not any_line:
        return None, {}
    lines.append("- _rule: any stream above at 0 fires for 4 consecutive weeks → check its producer_")
    return "\n".join(lines), snap


def section_decide_ledger(y, w):
    """/decide ledger surface (weekly-coverage-audit gap #5): the skill's actual output
    (~/.claude/decisions/*.jsonl — per-project ledgers + _global.jsonl for scope=global)
    was never counted; only the separate decision-pairs.jsonl (recommended-choice capture)
    was. Scope-global rows land ONLY in _global.jsonl (not duplicated per-project), so a
    flat glob-and-sum across all files does not double-count."""
    if not DECISIONS_DIR.exists():
        return None, None
    rows = []
    for p in DECISIONS_DIR.glob("*.jsonl"):
        try:
            txt = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for line in txt.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            ts = (d.get("source") or {}).get("ts")
            if in_week(ts, y, w):
                rows.append(d)
    if not rows:
        return "### 🗳 /decide ledger\n- 0 decisions logged this week", 0
    global_n = sum(1 for r in rows if r.get("scope") == "global")
    proj_n = sum(1 for r in rows if r.get("scope") == "project")
    types = {}
    for r in rows:
        t = r.get("type", "?")
        types[t] = types.get(t, 0) + 1
    type_str = ", ".join(f"{k}×{v}" for k, v in sorted(types.items(), key=lambda x: -x[1]))
    return (f"### 🗳 /decide ledger\n"
            f"- **{len(rows)}** entries this week — {global_n} global / {proj_n} project ({type_str})"), len(rows)


def section_skills(stats):
    lines = ["### 🎯 Skills"]
    n_skills = len([d for d in SKILLS.glob("*/") if (d / "SKILL.md").exists()]) if SKILLS.exists() else 0
    lines.append(f"- installed (custom): **{n_skills}** + plugins")
    # mapped-but-cold: in trigger map but never suggested this week
    try:
        tmap = json.loads((HOOKS / "skill-triggers.json").read_text())
        mapped = set(tmap.get("triggers", {}).keys())
        suggested = set(stats["top"].keys())
        cold = sorted(mapped - suggested)
        lines.append(f"- mapped triggers: {len(mapped)}; nudged this week: {len(suggested)}")
        if cold:
            lines.append(f"- cold (mapped, not nudged this week): {', '.join(cold[:12])}"
                         + (f" +{len(cold)-12}" if len(cold) > 12 else ""))
    except Exception:
        pass
    asg, aad = stats.get("adopt_overall", (0, 0))
    if asg:
        lines.append(f"- **adoption: {round(100*aad/asg)}%** — suggested skills invoked in the same "
                     f"interactive session ({aad}/{asg} suggest-sessions; headless `-p` calls excluded)")
    return "\n".join(lines)


def section_flags(stats):
    flags = []
    adoption_flags = []  # (rate, text) — skills with real interactive suggest-sessions
    blind = []           # skills only ever nudged in headless `-p` calls (not adoptable)
    for skill, n in stats["top"].items():
        if n < 5:
            continue
        ap = stats.get("adopt_per", {}).get(skill)
        if ap and ap[0]:
            rate = round(100 * ap[1] / ap[0])
            # A2 verdict (Keep/Rewrite/Remove): a per-skill triage off this week's adoption.
            # Guard on suggest-sessions ≥3 — a verdict from 1-2 sessions is noise. Rewrite routes
            # to A6 skillopt (rewrite SKILL.md); Remove = nudged enough, almost never invoked.
            if ap[0] < 3:
                verdict = "👀 thin signal"
            elif rate >= 40:
                verdict = "✅ Keep"
            elif rate >= 15:
                verdict = "✏️ Rewrite → A6 skillopt"
            else:
                verdict = "🗑 Remove-cand"
            adoption_flags.append((rate, f"- 🔁 `{skill}` nudged {n}× → invoked in **{rate}%** of suggest-sessions ({ap[1]}/{ap[0]}) — **{verdict}**"))
        else:
            blind.append(skill)
    for _, txt in sorted(adoption_flags):  # lowest adoption first — most actionable on top
        flags.append(txt)
    if blind:
        flags.append(f"- 🔁 measurement-blind (nudged only in headless `-p` calls, not adoptable): {', '.join(blind[:8])}"
                     + (f" +{len(blind)-8}" if len(blind) > 8 else ""))
    if stats["corr_unrev"] >= 3:
        flags.append(f"- ⚠ {stats['corr_unrev']} unreviewed correction candidates — review `memory/correction-candidates.jsonl`; ≥3 of a pattern ⇒ propose a rule")
    if stats["verify"] >= 5:
        flags.append(f"- ⚠ {stats['verify']} unverified-completion claims this week — consider tightening verification-before-completion")
    # /insights done-marker: ~/.claude/logs/insights-runs.jsonl gets one line per run
    # (Claude appends it when Anton reports running /insights). Flag stays up ALL month
    # until the marker lands — not just week 1 (Anton 2026-07-03: "чтобы не забывать").
    _ins_month = datetime.now(timezone.utc).strftime("%Y-%m")
    _ins_log = os.path.expanduser("~/.claude/logs/insights-runs.jsonl")
    _ins_done = False
    try:
        with open(_ins_log, encoding="utf-8") as _fh:
            _ins_done = any(_ins_month in _l for _l in _fh)
    except OSError:
        pass
    if not _ins_done:
        flags.append(f"- 📅 Monthly `/insights` NOT yet run in {_ins_month} — 30-day friction-mining pass; flag clears when a run is logged to insights-runs.jsonl")
    if not flags:
        flags.append("- ✓ nothing flagged — system within normal bands")
    return "### 🧭 Self-improvement flags\n" + "\n".join(flags)


def section_skill_fires():
    """Universal skill instrumentation: fires/week per skill over trailing 4 weeks.

    Source: ~/.claude/logs/skill-fires.jsonl (PostToolUse(Skill) hook, one line per
    invocation). Verdict: ✅ ≥2/wk active · 👀 ~1/wk watch · ⚠️ 0 for the whole
    window = dead weight → route to `gbrain skillopt`. Qualitative skills
    (humanizer/user-voice/forwhat/grill/idea/write-post/verify-critic) are
    expected-low-frequency and excluded from the ⚠️ kill list.
    """
    QUALITATIVE = {"humanizer", "humanizer-ru", "user-voice", "anton-voice",
                   "write-post", "grill", "idea", "forwhat", "verify-critic"}
    rows = read_jsonl("skill-fires.jsonl")
    if not rows:
        return "### 🎛 Skill fires (trailing 4 wks)\n- no fires logged yet (hook live; accrues from 2026-06-30)"
    cutoff = datetime.now(timezone.utc) - timedelta(days=28)
    counts = {}
    for r in rows:
        ts = r.get("ts", "")
        try:
            t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except Exception:
            continue
        if t < cutoff:
            continue
        counts[r.get("skill", "?")] = counts.get(r.get("skill", "?"), 0) + 1
    if not counts:
        return "### 🎛 Skill fires (trailing 4 wks)\n- no fires in window (hook live)"
    lines = ["### 🎛 Skill fires (trailing 4 wks)"]
    for sk, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        per = n / 4.0
        v = "✅" if per >= 2 else ("👀" if per >= 1 else "·")
        lines.append(f"- {v} `{sk}`: {n} fires ({per:.1f}/wk)")
    # dead-weight = never fired in window, non-qualitative (can't list zeros we
    # never saw; this is a hint about the verdict mechanism for the report layer)
    lines.append(f"- _⚠️ rule: a skill at 0 fires for 3+ wks (excl. {len(QUALITATIVE)} qualitative) → `gbrain skillopt`_")
    return "\n".join(lines)


STOPWORDS = set("""the a an and or of to in for on with is are was were be this that it as at by from
я ты мы вы он она они это этот эта эти как что чтобы для на в по из у не да нет ли же бы или если
давай сделай проверь нужно надо можно есть был была было будет мне тебе нам там тут вот еще ещё все всё
хочу может быть очень просто когда потом сейчас теперь тогда почему зачем какой какая какие где
please can you let's make check need want just now then why what how where which our your my
""".split())


def section_skill_gaps(y, w):
    """Option B (forwhat find-skills, Anton's go 2026-07-03): mine skill-suggest misses
    for recurring themes, query the skills.sh registry for ready-made candidates.

    HARD GATE (Snyk ToxicSkills: 13.4% of registry skills critical, 76 malicious):
    this section only SURFACES candidates. Install path is always: Claude reads the
    full SKILL.md (prompt-injection scan) → verdict to Anton → his explicit go.
    Never `npx skills add` automatically, from this script or anywhere else.
    """
    rows = [r for r in read_jsonl("skill-gaps.jsonl") if in_week(r.get("ts", ""), y, w)]
    if not rows:
        return "### 🧩 Skill-gap candidates (registry watch)\n- no unmatched task-shaped prompts this week (log live; accrues from 2026-07-03)"
    freq = {}  # word -> set of prompt indices (repeats across DISTINCT prompts)
    for i, r in enumerate(rows):
        for wd in re.findall(r"[a-zA-Zа-яА-ЯёЁ][a-zA-Zа-яА-ЯёЁ-]{3,}", r.get("prompt_head", "").lower()):
            if wd not in STOPWORDS:
                freq.setdefault(wd, set()).add(i)
    themes = sorted(((wd, len(ids)) for wd, ids in freq.items() if len(ids) >= 3),
                    key=lambda kv: -kv[1])[:3]
    lines = [f"### 🧩 Skill-gap candidates (registry watch) — {len(rows)} unmatched prompts"]
    if not themes:
        lines.append("- no theme repeated across ≥3 prompts — noise, nothing to query")
        return "\n".join(lines)
    for wd, n in themes:
        hits = "_registry query failed/offline_"
        try:
            r = subprocess.run(["npx", "--yes", "skills", "find", wd],
                               capture_output=True, text=True, timeout=60)
            top = [l.strip() for l in r.stdout.splitlines() if l.strip()][:3]
            if top:
                hits = " · ".join(top)
        except Exception:
            pass
        lines.append(f"- **{wd}** ({n} prompts) → {hits}")
    lines.append("- _⛔ gate: candidates only — install requires full SKILL.md read + Anton's go (Snyk ToxicSkills). No auto-add, ever._")
    return "\n".join(lines)


def section_health():
    lines = ["### 🩺 Config health"]
    try:
        s = json.loads(SETTINGS.read_text())
        miss = 0
        for blocks in s.get("hooks", {}).values():
            for blk in blocks:
                for h in blk.get("hooks", []):
                    m = re.search(r"(/Users/\S+\.(?:sh|py|mjs))", h.get("command", ""))
                    if m and not os.path.exists(m.group(1)):
                        miss += 1
        n_hooks = len(list(HOOKS.glob("*.sh")) + list(HOOKS.glob("*.py")) + list(HOOKS.glob("*.mjs")))
        n_rules = len(list((RULES / "learned").glob("*.md"))) if (RULES / "learned").exists() else 0
        lines.append(f"- settings.json: valid · hook files on disk: {n_hooks} · missing-hook refs: {miss}{'  ⚠' if miss else ''}")
        lines.append(f"- learned rules: {n_rules}")
    except Exception as e:
        lines.append(f"- ⚠ settings.json check failed: {e}")
    # gbrain update signal (weekly com.anton.gbrain-check-update cron writes this).
    try:
        gu = json.loads((LOGS / "gbrain-update.json").read_text())
        if gu.get("git_update_available"):
            lines.append(f"- ⬆️ gbrain update: {gu.get('commits_behind','?')} commit(s) behind origin/master "
                         f"(branch {gu.get('branch','?')}) — `cd ~/gbrain && git pull` when you choose")
        else:
            lines.append(f"- gbrain: up to date (branch {gu.get('branch','?')})")
    except Exception:
        lines.append("- gbrain update: no check yet (cron runs Mon 08:00)")
    # G6 dream sentinel (graft 2026-07-04): gbrain-dream-YYYY-MM.log gets a
    # "WARN: previous run did not exit cleanly" line when the prior dream run crashed.
    # Cheap grep of the current month's log only.
    dream_month = datetime.now(timezone.utc).strftime("%Y-%m")
    dream_log = LOGS / f"gbrain-dream-{dream_month}.log"
    dream_n = 0
    if dream_log.exists():
        try:
            dream_n = sum(1 for l in dream_log.read_text(encoding="utf-8", errors="replace").splitlines()
                          if "WARN: previous run did not exit cleanly" in l)
        except Exception:
            dream_n = 0
    dream_v = "✅" if dream_n == 0 else "⚠️"
    lines.append(f"- dream crashes: **{dream_n}** {dream_v}")
    return "\n".join(lines)


def _rule_files():
    out = []
    for base in (RULES, RULES / "learned"):
        if base.exists():
            out += list(base.glob("*.md"))
    return out


def _frontmatter_keys(path):
    """Top-level YAML frontmatter keys (no yaml dep). None = no frontmatter block."""
    try:
        txt = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    m = re.match(r"^---\n(.*?)\n---", txt, re.DOTALL)
    if not m:
        return None
    return set(re.findall(r"^([A-Za-z0-9_-]+):", m.group(1), re.MULTILINE))


def section_config_audit():
    """Deterministic slice of the audit-setup health pass, run weekly for free.
    Catches config drift between the on-demand LLM audits: oversized always-loaded
    rules, broken CLAUDE.md @import refs, and skill-trigger phrase collisions.
    Returns (markdown, issue_count)."""
    issues = []
    for p in _rule_files():
        try:
            n = sum(1 for _ in p.open(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        if n > 60:
            issues.append(f"[FAIL] rule `{p.name}` {n} lines (>60, always-loaded tax)")
        elif n > 40:
            issues.append(f"[WARN] rule `{p.name}` {n} lines (>40)")
    # broken @import / referenced .md paths in CLAUDE.md
    if CLAUDEMD.exists():
        txt = CLAUDEMD.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(r"@(?:import\s+)?(\S+\.md)", txt):
            raw = m.group(1)
            cands = [HOME / ".claude" / raw.lstrip("~/"), HOME / raw.lstrip("~/"),
                     Path(raw).expanduser()]
            if not any(c.exists() for c in cands):
                issues.append(f"[WARN] CLAUDE.md @import missing: {raw}")
    # skill-trigger phrase collisions (same phrase mapped to 2+ skills)
    try:
        tmap = json.loads((HOOKS / "skill-triggers.json").read_text()).get("triggers", {})
        seen = {}
        for slug, phrases in tmap.items():
            for ph in phrases:
                seen.setdefault(ph.lower(), []).append(slug)
        for ph, slugs in sorted(seen.items()):
            if len(slugs) > 1:
                issues.append(f'[WARN] trigger collision "{ph}" → {", ".join(slugs)}')
    except Exception:
        pass
    # frontmatter drift vs ~/.claude/rules/TEMPLATE.md — own (top-level) skills need name+description.
    # Ground-truth scan of current files (not the append-only hook log, which can go stale).
    try:
        fm_bad = []
        for sk in (SKILLS.glob("*/SKILL.md") if SKILLS.exists() else []):
            keys = _frontmatter_keys(sk)
            if keys is None or "name" not in keys or "description" not in keys:
                fm_bad.append(sk.parent.name)
        if fm_bad:
            issues.append(f"[WARN] {len(fm_bad)} own skill(s) missing name/description frontmatter "
                          f"(see templates/TEMPLATE.md): {', '.join(sorted(fm_bad)[:8])}"
                          + (f" +{len(fm_bad)-8}" if len(fm_bad) > 8 else ""))
        # real-time PostToolUse hook activity (validate-frontmatter.py) — ISSUES-ONLY stream:
        # the hook fires on every PostToolUse but only APPENDS a row when it finds a problem.
        # Verified live 2026-07-04 — silence (no rows, or none this window) means the hook is
        # firing and finding nothing wrong, NOT that it went stale. Never read a long gap here
        # as staleness.
        fa = read_jsonl("frontmatter-audit.jsonl")
        if fa:
            issues.append(f"[INFO] frontmatter hook logged {len(fa)} drift event(s) (issues-only stream)")
        else:
            issues.append("[INFO] frontmatter hook: 0 drift events logged (issues-only stream — silence = healthy)")
    except Exception:
        pass
    n = len([i for i in issues if not i.startswith("[INFO]")])
    head = f"### 🔧 Config audit ({'✓ clean' if not n else str(n) + ' issues'})"
    body = "\n".join(f"- {i}" for i in issues) if issues else \
        "- ✓ rules within size, CLAUDE.md refs resolve, no trigger collisions"
    return head + "\n" + body, n


def section_git():
    """feat/fix/chore commit mix for cc-stack this week — build-vs-patch direction signal."""
    try:
        out = subprocess.run(
            ["git", "-C", str(ROOT), "log", "--since=7 days ago", "--format=%s"],
            capture_output=True, text=True, timeout=15).stdout
    except Exception:
        return "### 📦 Commits (7d)\n- _(git unavailable)_"
    subs = [l for l in out.splitlines() if l.strip()]
    types = {}
    for s in subs:
        m = re.match(r"(\w+)(?:\(|:|!)", s)
        t = m.group(1).lower() if m else "other"
        types[t] = types.get(t, 0) + 1
    total = len(subs)
    mix = ", ".join(f"{k}×{v}" for k, v in sorted(types.items(), key=lambda x: -x[1])) or "—"
    line = f"### 📦 Commits (7d): {total}\n- {mix}"
    fixn = types.get("fix", 0)
    if total and fixn / total > 0.5:
        line += f"\n- ⚠ fix ratio {fixn}/{total} >50% — patching more than building"
    return line


def write_snapshot(y, w, snap):
    try:
        SNAP.mkdir(parents=True, exist_ok=True)
        (SNAP / f"{y}-W{w:02d}.json").write_text(json.dumps(snap, ensure_ascii=False))
    except Exception:
        pass


def section_trends(snap):
    """Week-over-week delta — turns the static snapshot into a trend detector
    (the gstack-retro pattern worth porting). Reads last week's snapshot file."""
    prev = None
    try:
        d = datetime.now(timezone.utc) - timedelta(days=7)
        py, pw, _ = d.isocalendar()
        pf = SNAP / f"{py}-W{pw:02d}.json"
        if pf.exists():
            prev = json.loads(pf.read_text())
    except Exception:
        prev = None
    if not prev:
        return "### 📈 Trend vs last week\n- _(no prior snapshot — baseline set this week)_"
    labels = [("sugg_n", "skill nudges"), ("verify", "unverified-done"),
              ("corr_unrev", "corrections"), ("config_issues", "config issues"),
              ("burn_warns", "burn warns"), ("kimi_ck", "claude-kimi runs"),
              ("kimi_kd", "kimi-do delegations"),
              ("swarm_nudge", "swarm→Kimi nudges"), ("msg_decomp", "msg-decompose nudges"),
              ("browser_fires", "browser-tool fires"),
              ("adopt_pct", "skill adoption %"), ("prune_n", "suggester prune-candidates"),
              ("compacts", "compactions"),
              ("bbg_deny", "bash-batch denies"), ("tok_rpt", "connectpro requests/turn (→≤4.5)"),
              ("tok_small", "connectpro tiny-Bash % (→≤40)"), ("tok_base", "connectpro base ctx k (→≤68)"),
              ("tok_sub", "connectpro subagent % (→≤10)"), ("tok_mtok_h", "connectpro Mtok/h (→1.8)"), ("synth_real_pct", "synth real-write %"),
              ("exp_new_hit", "expansion new-hit %"),
              ("consti_surface", "constitution surfaces"),
              ("decision_pairs", "decision-pairs logged (→30)"),
              ("deleg_spawns", "subagent spawns"), ("deleg_density", "delegation density"),
              ("opus_mtok", "main-loop Opus Mtok"),
              ("capaudit_gaps", "capability gaps (idle+unrouted)"),
              ("night_shift_tasks", "night-shift tasks"),
              ("dream_crashes", "dream crashes"),
              ("episodic_adopt_pct", "structured-notes adoption %"),
              ("mcp_src_bad", "gbrain MCP launches in the WRONG box (→0)"),
              ("gb_usage_total", "gbrain calls (read-side)"),
              ("gb_usage_graph", "gbrain graph-arm calls"),
              ("gb_volunteer", "gbrain-volunteer fires"),
              ("gb_advisor", "gbrain-advisor runs"),
              ("mem2brain", "memory→brain events"),
              ("decide_ledger_n", "/decide entries logged"),
              ("consti_clusters", "constitution-clusters (latest run)"),
              ("gbrain_fails", "gbrain doctor fails"),
              ("prune0_n", "0%-adoption prune candidates"),
              ("o2_precision_pct", "O2 rollout precision %")]
    rows = []
    for k, lbl in labels:
        if k in snap and k in prev and snap[k] is not None and prev[k] is not None:
            a, b = prev[k], snap[k]
            d = b - a
            arrow = "→" if d == 0 else ("↑" if d > 0 else "↓")
            mag = round(abs(d), 1) if isinstance(d, float) else abs(d)
            rows.append(f"- {lbl}: {a} → {b}  {arrow}{mag if d else ''}")
    return "### 📈 Trend vs last week\n" + ("\n".join(rows) if rows else "- _(no comparable metrics)_")


def rung_streak():
    """Count consecutive recent ISO weeks where the constitution surface fired
    (proxy for the Rung-1 ✅ streak). The TRUE ✅ is the use-rate verdict from the
    weekly report narrative; this is the cheap deterministic watch signal."""
    streak = 0
    d = datetime.now(timezone.utc)
    for _ in range(8):  # look back up to 8 weeks
        py, pw, _ = d.isocalendar()
        pf = SNAP / f"{py}-W{pw:02d}.json"
        if not pf.exists():
            d -= timedelta(days=7)
            continue
        try:
            cs = json.loads(pf.read_text()).get("consti_surface")
        except Exception:
            cs = None
        if cs and cs > 0:
            streak += 1
            d -= timedelta(days=7)
        else:
            break
    return streak


# A decision-pairs row carries its verdict under a different key depending on who wrote it:
# log-decision.py writes `verdict` from its own past-tense vocabulary, hand-appended rows have
# used `outcome` — 21 rows carry 13 distinct shapes. Counting ONE key against ONE spelling made
# this metric structurally blind: log-decision.py can only emit {accepted, overrode, modified,
# accepted-and-modified, rejected} while the count looked for {override, modify} — an empty
# intersection, so a decision logged through the official writer could never be counted as an
# override, and "all-accept, weak signal" was guaranteed forever. Match on meaning instead:
# 9 of the 21 rows are overrides/modifications; the old expression saw 2, both by accident.
VERDICT_KEYS = ("verdict", "outcome")
NOT_A_PLAIN_ACCEPT = re.compile(r"overr|modif|reject|extend", re.I)


def is_override(row):
    """True when Anton did not simply take the recommendation as given."""
    return any(NOT_A_PLAIN_ACCEPT.search(str(row.get(k, ""))) for k in VERDICT_KEYS)


def section_rung_gates():
    """Forcing function for the deferred rung roadmap (system-that-leads-anton design §7).
    A gate written only into a backlog doc rots (catalog-#3 lesson). This surfaces each
    gate's progress in the weekly Anton already reads, so it self-announces when met."""
    streak = rung_streak()
    prs = read_jsonl("decision-pairs.jsonl")  # decision-pairs log (shipped 2026-06-30)
    pairs = len(prs)
    # verdict balance: an all-accept log is a weak training signal for the Rung-2 taste model —
    # it learns nothing without examples where Anton overrode/modified the recommendation.
    overrides = sum(1 for d in prs if is_override(d))
    bal = f"{overrides} override/modify" if overrides else "all-accept so far — weak signal until overrides appear"
    r3 = "✅ GATE MET — Rung 3 ready (confirm use-rate ≥60% in weekly report)" if streak >= 4 \
        else f"👀 {streak}/4 weeks of constitution-surfacing (proxy; true ✅ = use-rate from report)"
    r2 = "✅ GATE MET — ≥30 pairwise decisions logged → Rung 2 ready" if pairs >= 30 \
        else (f"👀 {pairs}/30 pairwise decisions logged ({bal})" if pairs
              else "⏳ 0/30 — no pairwise-decision log exists yet; Rung 2's clock starts once one is written")
    return ("### 🪜 Rung gates (deferred roadmap watch)\n"
            f"- **Rung 3** (classifier C + router D) — gate: Rung 1 ✅ × 4 weeks → {r3}\n"
            f"- **Rung 2** (taste model + transcript mining) — gate: ≥30 pairwise decisions → {r2}")


def delegation_signal(y, w):
    """Per-ISO-week delegation metrics from CC transcripts (~/.claude/projects/**/*.jsonl).
    Answers Anton's 2026-06-30 question: is the main Opus loop handing work to cheap subagents,
    or grinding mechanical/parallel work solo? Subagent turns live in .../subagents/agent-*.jsonl;
    main-loop files carry the `Agent` tool_use spawn blocks.
      - spawns: `Agent` tool_use calls in main-loop sessions = explicit delegation decisions.
        PRIMARY signal — proportional to delegation, not to model pricing (subagent token-share
        reads low ~7-15% even at heavy delegation because subagents run Sonnet = small output).
      - opus_mtok: main-loop output tokens from an opus model, in millions.
      - sub_share_pct: subagent output / total output (secondary cost view).
      - density: spawns per 1M main-loop Opus output tokens — the discipline ratio.
    Scans only files modified in the last ~12 days for speed (covers the current ISO week)."""
    cutoff = datetime.now(timezone.utc).timestamp() - 12 * 86400
    spawns = opus_out = sub_out = total_out = 0
    try:
        for fpath in PROJECTS.glob("**/*.jsonl"):
            try:
                if fpath.stat().st_mtime < cutoff:
                    continue
            except Exception:
                continue
            is_sub = "subagents" in fpath.parts  # subagent transcripts live here (isSidechain=True)
            try:
                txt = fpath.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            for line in txt.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if not in_week(d.get("timestamp"), y, w):
                    continue
                msg = d.get("message")
                if not isinstance(msg, dict) or msg.get("role") != "assistant":
                    continue
                out = (msg.get("usage") or {}).get("output_tokens", 0) or 0
                model = (msg.get("model") or "").lower()
                if out > 0:
                    total_out += out
                    if is_sub:
                        sub_out += out
                    elif "opus" in model:
                        opus_out += out
                if not is_sub:
                    for c in (msg.get("content") or []):
                        if isinstance(c, dict) and c.get("type") == "tool_use" and c.get("name") == "Agent":
                            spawns += 1
    except Exception:
        pass
    opus_mtok = opus_out / 1_000_000
    return {
        "spawns": spawns,
        "opus_mtok": round(opus_mtok, 2),
        "sub_share_pct": round(100 * sub_out / total_out, 1) if total_out else None,
        "density": round(spawns / opus_mtok, 1) if opus_mtok >= 0.05 else None,
    }


def _trailing_density(weeks=3):
    """Mean delegation density over up to `weeks` prior ISO-week snapshots that carry it.
    None until enough history exists (the metric calibrates before it can verdict)."""
    vals = []
    d = datetime.now(timezone.utc) - timedelta(days=7)
    for _ in range(weeks):
        py, pw, _ = d.isocalendar()
        pf = SNAP / f"{py}-W{pw:02d}.json"
        if pf.exists():
            try:
                v = json.loads(pf.read_text()).get("deleg_density")
                if v:
                    vals.append(v)
            except Exception:
                pass
        d -= timedelta(days=7)
    return sum(vals) / len(vals) if vals else None


def section_opus_discipline(sig):
    """Opus-discipline watch (Anton 2026-06-30): is the main Opus loop delegating to cheap
    subagents, or grinding mechanical/parallel work solo? Yardstick = delegation density
    (Agent spawns per 1M main-loop Opus output tokens). Verdict vs the trailing-3-week mean:
    >= baseline ✅ · 60-100% 👀 watch · <60% AND Opus volume rising ⚠️ under-delegating
    → review the week's heavy sessions for serial work that should fan out. Pairing spawns with
    Opus volume stops a genuinely-light week from false-alarming as solo-grinding."""
    spawns, dens = sig["spawns"], sig["density"]
    base = _trailing_density()
    opus_rising = False
    try:
        d = datetime.now(timezone.utc) - timedelta(days=7)
        py, pw, _ = d.isocalendar()
        pf = SNAP / f"{py}-W{pw:02d}.json"
        if pf.exists():
            prev_opus = json.loads(pf.read_text()).get("opus_mtok")
            if prev_opus is not None and sig["opus_mtok"] > prev_opus:
                opus_rising = True
    except Exception:
        pass
    if dens is None:
        verdict = "👀 too little main-loop Opus this week to judge delegation"
    elif base is None:
        verdict = f"👀 calibrating — baseline density {dens} recorded (need 2+ prior weeks to verdict)"
    elif dens >= base:
        verdict = f"✅ delegating — density {dens} ≥ {round(base, 1)} trailing baseline"
    elif dens >= 0.6 * base:
        verdict = f"👀 watch — density {dens} vs {round(base, 1)} baseline (within band)"
    else:
        tail = (" AND Opus volume rising ⇒ investigate serial work that should fan out (→ evaluation-backlog)"
                if opus_rising else " (but Opus volume not rising — likely a light week, not solo-grinding)")
        verdict = f"⚠️ density {dens} <60% of {round(base, 1)} baseline{tail}"
    share = sig["sub_share_pct"]
    share_str = f"{share}%" if share is not None else "—"
    return "\n".join([
        "### 🧠 Opus discipline (main-loop delegation)",
        f"- **{spawns}** subagent spawns · main-loop Opus out **{sig['opus_mtok']}M** tok · "
        f"subagent token-share {share_str}",
        f"- delegation density: **{dens if dens is not None else '—'}** spawns/M-Opus-tok",
        f"- **{verdict}**",
    ])


def refresh_expansion_ab():
    """Refresh p5-loop/expansion-ab/latest.json before rendering, so the weekly
    launchd run (this script, Mon 10:00) carries fresh A/B data — no separate job.
    Expansion runs on the GLM subscription since 2026-07-04 (Gemini key retired):
    models.expansion = anthropic:claude-sonnet-4-6, which z.ai maps to GLM under
    the ANTHROPIC_BASE_URL redirect below. Also routes OPENAI to real OpenAI for
    the query embeddings (launchd has no shell env). Fail-open: any error leaves
    the previous latest.json in place. Skips if <3 days old so repeated manual
    runs don't re-spend."""
    f = ROOT / "p5-loop/expansion-ab/latest.json"
    if f.exists():
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(
                json.loads(f.read_text()).get("ts", "").replace("Z", "+00:00"))
            if age < timedelta(days=3):
                return
        except Exception:
            pass
    env = dict(os.environ)
    secrets = Path.home() / ".config/secrets/secrets.env"
    if secrets.exists():
        for line in secrets.read_text(errors="replace").splitlines():
            m = re.match(r'\s*(?:export\s+)?(ZAI_API_KEY|OPENAI_API_KEY_REAL)=["\']?([^"\'\n]+)', line)
            if m:
                env.setdefault(m.group(1), m.group(2))
    if env.get("ZAI_API_KEY"):
        env["ANTHROPIC_API_KEY"] = env["ZAI_API_KEY"]
        env["ANTHROPIC_BASE_URL"] = "https://api.z.ai/api/anthropic"
    if env.get("OPENAI_API_KEY_REAL"):
        env["OPENAI_API_KEY"] = env["OPENAI_API_KEY_REAL"]
        env["OPENAI_BASE_URL"] = "https://api.openai.com/v1"
    try:
        subprocess.run([sys.executable, str(ROOT / "scripts/p5/expansion-ab.py"), "--n", "60"],
                       env=env, timeout=600, capture_output=True)
    except Exception:
        pass  # keep the prior latest.json


def section_expansion_ab():
    """gbrain query-expansion (GLM via z.ai) A/B effectiveness. Run-based, not per-fire:
    reads p5-loop/expansion-ab/latest.json produced by `python3 scripts/p5/expansion-ab.py`.
    Yardstick: new-hit rate >=20% ✅ keep · 5–20% 👀 watch · <5% ⚠️ no benefit (kill).
    Cost is a volume estimate (gbrain doesn't auto-log expansion spend)."""
    f = ROOT / "p5-loop/expansion-ab/latest.json"
    if not f.exists():
        return ("### 🔎 Query expansion (GLM) A/B\n"
                "- _(no run yet — `python3 scripts/p5/expansion-ab.py` after the Google key is set)_"), None
    try:
        d = json.loads(f.read_text())
    except Exception:
        return "### 🔎 Query expansion (GLM) A/B\n- _(latest.json unreadable)_", None
    lines = ["### 🔎 Query expansion (GLM) A/B",
             f"- replayed **{d.get('n_scored', '?')}** real queries (on vs `--no-expand`), as of {d.get('ts', '?')[:10]}",
             f"- new-hit rate: **{d.get('new_hit_rate_pct', '?')}%** · rank-1 changed: {d.get('rank1_changed_pct', '?')}% · jaccard {d.get('avg_jaccard', '?')}",
             f"- est. cost: ~${d.get('est_monthly_cost_usd', '?')}/mo  ({d.get('cost_note', '')})",
             f"- **{d.get('verdict', '—')}**"]
    return "\n".join(lines), d.get("new_hit_rate_pct")


def refresh_capability_audit():
    """Re-run the global capability-routing audit on a MONTHLY cadence (Anton: "раз в
    месяц, рядом с /insights"). Skips if the latest report is <25 days old, so the
    weekly System Overview render picks it up without a separate launchd job. Fail-open:
    any error leaves the previous report in place."""
    f = Path.home() / ".claude/logs/capability-audit-latest.json"
    if f.exists():
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(
                json.loads(f.read_text()).get("ts", "").replace("Z", "+00:00"))
            if age < timedelta(days=25):
                return
        except Exception:
            pass
    try:
        subprocess.run([sys.executable, str(Path.home() / ".claude/scripts/capability-routing-audit.py")],
                       timeout=600, capture_output=True)
    except Exception:
        pass  # keep the prior report


def section_capability_audit():
    """Adopted-but-unrouted / idle MCP capabilities (the inverse of kill-threshold).
    Reads ~/.claude/logs/capability-audit-latest.json from the global detector. Monthly
    cadence — the line is stable between refreshes. Yardstick: 0 real gaps ✅ · ≥1 gap ⚠️
    (decide per tool: wire a routing rule or retire via /forwhat)."""
    f = Path.home() / ".claude/logs/capability-audit-latest.json"
    if not f.exists():
        return ("### 🧭 Capability-routing audit\n"
                "- _(no run yet — `python3 ~/.claude/scripts/capability-routing-audit.py`)_"), None
    try:
        d = json.loads(f.read_text())
    except Exception:
        return "### 🧭 Capability-routing audit\n- _(latest.json unreadable)_", None
    rows = d.get("rows", [])
    gaps = [r["capability"] for r in rows if r["bucket"] == "under_used_unrouted"]
    idle = [r["capability"] for r in rows if r["bucket"] == "routed_idle"]
    undoc = [r["capability"] for r in rows if r["bucket"] == "used_unrouted"]
    verdict = ("⚠️ " + str(len(gaps)) + " adopted-but-unrouted gap(s) — wire a routing rule or retire (/forwhat)"
               if gaps else "✅ no adopted-but-unrouted gaps")
    lines = ["### 🧭 Capability-routing audit",
             f"- {d.get('transcripts_scanned', '?')} transcripts, last {d.get('window_days', '?')}d, "
             f"idle<{d.get('idle_thresh', '?')} · as of {d.get('ts', '?')[:10]}",
             f"- {'⚠️' if gaps else '✅'} gap (idle+unrouted): {', '.join(gaps) if gaps else 'нет'}",
             f"- 👀 routed-but-idle (/forwhat): {', '.join(idle) if idle else '—'}",
             f"- 📝 used-but-unrouted (document): {', '.join(undoc) if undoc else '—'}",
             f"- **{verdict}**"]
    return "\n".join(lines), len(gaps)


def refresh_gbrain_doctor():
    """Run `gbrain doctor --json` (81 read-only health checks, NO LLM / NO spend) and
    cache to p5-loop/gbrain-doctor-latest.json + append a compact history line to
    ~/.claude/logs/gbrain-doctor.jsonl. Skips if the cache is <3 days old so repeated
    manual renders don't re-run it. Fail-open: any error leaves the prior cache in place.
    doctor is on the gbrain safe-list (never reindexes); we still redirect ANTHROPIC to
    z.ai when a key is present (belt-and-suspenders — doctor makes no metered calls)."""
    cache = ROOT / "p5-loop/gbrain-doctor-latest.json"
    if cache.exists():
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(
                json.loads(cache.read_text()).get("ts", "").replace("Z", "+00:00"))
            if age < timedelta(days=3):
                return
        except Exception:
            pass
    reflex_bin = Path.home() / "Library/Application Support/reflex/bun/bin"
    bun, gb = reflex_bin / "bun", reflex_bin / "gbrain"
    cmd = ([str(bun), str(gb)] if bun.exists() and gb.exists() else ["gbrain"]) + ["doctor", "--json"]
    env = dict(os.environ)
    secrets = Path.home() / ".config/secrets/secrets.env"
    if secrets.exists():
        for line in secrets.read_text(errors="replace").splitlines():
            m = re.match(r'\s*(?:export\s+)?ZAI_API_KEY=["\']?([^"\'\n]+)', line)
            if m:
                env.setdefault("ZAI_API_KEY", m.group(1))
    if env.get("ZAI_API_KEY"):
        env["ANTHROPIC_API_KEY"] = env["ZAI_API_KEY"]
        env["ANTHROPIC_BASE_URL"] = "https://api.z.ai/api/anthropic"
    # cycle_freshness: upstream defaults to warn 6h / FAIL 24h (doctor.ts:3802-3803). Our
    # maintenance cycle is DAILY on purpose — a cycle re-embeds, and embeddings are metered
    # (zeroentropyai:zembed-1), so doubling the cadence to satisfy a 24h line would spend money to
    # silence a clock. With a daily cycle plus jitter the age lands at 19-25h, so a 24h fail line
    # is red roughly half the time BY CONSTRUCTION — measured 2026-07-30: sources at 19/20/21/23h
    # (warn) and connectpro at 25h (fail). 30h states the real invariant: "the daily cycle ran".
    # This is a threshold correction, not a mute — the check still fires the moment a cycle is
    # genuinely SKIPPED, and it stays a WARN from 6h so drift is still visible.
    env.setdefault("GBRAIN_CYCLE_FRESHNESS_FAIL_HOURS", "30")
    env["PATH"] = str(reflex_bin) + ":" + env.get("PATH", "")
    try:
        r = subprocess.run(cmd, env=env, timeout=600, capture_output=True, text=True)
        d = json.loads(r.stdout)
    except Exception:
        return  # keep prior cache
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    d["ts"] = ts
    try:
        cache.write_text(json.dumps(d, ensure_ascii=False))
    except Exception:
        pass
    checks = d.get("checks", [])
    hist = {"ts": ts, "status": d.get("status"), "health": d.get("health_score"),
            "n_fail": sum(1 for c in checks if c.get("status") == "fail"),
            "n_warn": sum(1 for c in checks if c.get("status") == "warn"),
            "categories": d.get("category_scores")}
    try:
        with (LOGS / "gbrain-doctor.jsonl").open("a") as f:
            f.write(json.dumps(hist, ensure_ascii=False) + "\n")
    except Exception:
        pass


# Upstream doctor checks whose FAIL is NOT actionable work for us. This is deliberately a
# NAMED, EVIDENCED allow-list, not a mute button: the docstring below promises "the fail names
# ARE the action list", and that promise is only honest if a name that CANNOT be actioned is
# kept out of it. Each entry must carry the proof that its remedy is a no-op — an unevidenced
# entry here is indistinguishable from hiding a real bug, which is the failure this guards.
DOCTOR_KNOWN_NOISE = {
    "sync_freshness":
        "upstream measures the CLOCK, not content drift. `last_sync_at` records the last time "
        "content CHANGED, not the last sync ATTEMPT — so a quiet source accrues age forever and "
        "the check screams 'brain search is stale!' while the brain is perfectly current. Its own "
        "prescribed remedy (`gbrain sync --source <id>`) cannot clear it. PROVEN 2026-07-16: "
        "synced stack-docs (20.7d) and identity-universe (12.1d) → both 'Already up to date', "
        "0 changed, $0.00, and last_sync_at did NOT move. identity-universe: 0 commits since its "
        "last sync — gbrain is RIGHT, the corpus is current. Doctor's own details corroborate: "
        "unchanged_count=13. Re-check if upstream ever stamps last_sync_at on a no-op sync.",
    "content_sanity_audit_recent":
        "the check INVERTS its own severity. Source: doctor.ts:6376-6381 — `hardBlocked = "
        "hard_block + reject + quarantine`, and `hardBlocked > 0 -> fail`, with no threshold and "
        "no window knob. But `quarantine` means the sanity filter FOUND junk and hid it, i.e. the "
        "filter WORKING. PROVEN 2026-07-30 on a live `gbrain doctor --json`: the single hard event "
        "was quarantine=1 with pattern `captcha_required=1` — one captcha page picked up by web "
        "ingestion, correctly hidden from search. So any brain that ingests the web (we do, via "
        "web-research + ctx_fetch) is permanently red after its first captcha. The prescribed "
        "direction is worse than a no-op: the only way to clear it is `gbrain quarantine clear`, "
        "which would UNHIDE the captcha page. Compare the severities: 12449 pages quarantined is a "
        "WARN (`quarantined_pages`) while ONE correctly-quarantined page is a FAIL. Re-check if "
        "upstream ever separates 'junk was blocked' from 'a block FAILED'.",
}


def section_gbrain_health():
    """gbrain doctor's 81 health checks distilled to a weekly verdict — the analytics
    layer Anton asked for: knowing the brain works AS INTENDED, not silently-wrong.
    Reads p5-loop/gbrain-doctor-latest.json (refreshed by refresh_gbrain_doctor).
    Yardstick: 0 fail ✅ · 1–2 fail 👀 · ≥3 fail ⚠️. The fail names ARE the action list.
    Returns (md, n_fail) for the week-over-week snapshot."""
    cache = ROOT / "p5-loop/gbrain-doctor-latest.json"
    if not cache.exists():
        return "### 🧠 GBrain health (doctor)\n- _(no run yet — `gbrain doctor --json`)_", None
    try:
        d = json.loads(cache.read_text())
    except Exception:
        return "### 🧠 GBrain health (doctor)\n- _(latest.json unreadable)_", None
    checks = d.get("checks", [])
    all_fails = [c.get("name") for c in checks if c.get("status") == "fail"]
    fails = [f for f in all_fails if f not in DOCTOR_KNOWN_NOISE]
    noise = [f for f in all_fails if f in DOCTOR_KNOWN_NOISE]
    warns = [c.get("name") for c in checks if c.get("status") == "warn"]
    n_ok = sum(1 for c in checks if c.get("status") in ("ok", "pass"))
    cat = d.get("category_scores", {})
    cv = lambda s: "✅" if s >= 90 else ("👀" if s >= 70 else "⚠️")
    cat_line = " · ".join(f"{k} {cv(v)}{v}" for k, v in cat.items()) if cat else "—"
    if not fails:
        verdict = "✅ healthy — 0 fails"
    elif len(fails) <= 2:
        verdict = "👀 " + str(len(fails)) + " fail(s): " + ", ".join(fails)
    else:
        verdict = "⚠️ " + str(len(fails)) + " fail(s): " + ", ".join(fails)
    lines = ["### 🧠 GBrain health (doctor)",
             f"- status **{d.get('status', '?')}** · health {d.get('health_score', '?')}/100 · "
             f"{len(checks)} checks: {n_ok} ok / {len(warns)} warn / {len(fails)} fail · as of {d.get('ts', '?')[:10]}",
             f"- categories: {cat_line}",
             f"- 👀 warns: {', '.join(warns[:6]) if warns else '—'}",
             f"- **{verdict}**"]
    if noise:
        lines.append(f"- 🔇 upstream noise (fails, but the remedy is a no-op — not our work): "
                     f"{', '.join(noise)} — see DOCTOR_KNOWN_NOISE for the proof")
    return "\n".join(lines), len(fails)


def section_coverage_watch(y, w):
    """Coverage watchdog (2026-07-08 stack audit) — catches the two failure modes the
    per-section verdicts miss:
      (1) a CONTINUOUS stream that silently FROZE (the feed broke but no section alarms), and
      (2) a SHIPPED mechanism whose log stayed EMPTY = it shipped WITHOUT working measurement
          (the o2-runs smoking gun: o2 auto-run shipped 2026-07-07, log stayed 0 bytes, weekly
          rested on an empty stream). This is the measure-what-you-ship gate enforced at
          read-time — a feature is not 'shipped' until its log proves it measures itself.
    Deliberately NARROW to avoid crying wolf: issues-only / bursty / event-gated streams
    (frontmatter-audit, operating-loop-guard, research-runs, decision-preflight) are NOT
    watched — silence is HEALTHY there, confirmed in the 2026-07-08 audit."""
    now = datetime.now(timezone.utc)

    def newest_ts(name):
        for r in reversed(read_jsonl(name)):
            try:
                return datetime.fromisoformat(str(r.get("ts", "")).replace("Z", "+00:00"))
            except Exception:
                continue
        return None

    lines = ["### 🩺 Coverage watch (silence + ship-without-measure)"]
    alerts = []
    # (1) streams that MUST grow ~daily — frozen >2d ⇒ the feed broke.
    CONTINUOUS = ["memory-to-brain.jsonl", "diary-to-brain.jsonl", "opus-blocks.jsonl",
                  "routing-decisions.jsonl", "gbrain-volunteer.jsonl"]
    for name in CONTINUOUS:
        ts = newest_ts(name)
        if ts is None:
            alerts.append(f"⚠️ **{name}** — no dated rows (feed dead or schema drift)")
        elif (now - ts) > timedelta(days=2):
            alerts.append(f"⚠️ **{name}** — frozen {(now - ts).days}d (must grow daily)")
    # (2) SHIPPED mechanisms whose log must be non-empty — EMPTY ⇒ investigate.
    #     Add a stream here the moment you ship it (that IS the ship-time gate).
    #     Map log → the accurate meaning of "empty" for THAT stream. o2-runs' instrument
    #     was VERIFIED working end-to-end 2026-07-15 (init→resume→Stop-hook→advance all
    #     wrote correctly), so empty is NOT "broken measurement" — it's "0 real rollouts
    #     ever reached `init`" = an adoption signal (is the o2 auto-orchestrator wanted?),
    #     not a measurement defect. Don't mislabel the two.
    # o2-runs watch PAUSED 2026-07-16: the fence did its job — it forced the adoption
    # question and the answer is "o2 is being reframed as a pipeline-composer, deferred by
    # Anton's own open thread (open-threads.md → 'o2 pipeline-composer', 'позже напишем')".
    # An adoption=0 signal for a mechanism whose owner has deferred it is not a weekly ⚠️;
    # it just nags. Re-instate this watch the moment o2 ships for real (first real `init`).
    SHIPPED_MUST_LOG = {
        # "o2-runs.jsonl": "... — re-add when o2-composer ships and runs on a real task",
    }
    for name, meaning in SHIPPED_MUST_LOG.items():
        if not read_jsonl(name):
            alerts.append(f"⚠️ **{name}** — registered but EMPTY ({meaning})")
    if alerts:
        lines.extend(f"- {a}" for a in alerts)
    else:
        lines.append("- ✅ all watched streams fresh + non-empty")
    lines.append(f"- _watches {len(CONTINUOUS)} daily-feed + {len(SHIPPED_MUST_LOG)} shipped stream(s); "
                 "issues-only/bursty streams excluded by design (silence = healthy there)_")
    return "\n".join(lines), len(alerts)


def section_cto(y, w):
    """CTO-agent + finder-agent verdict — the measure-what-you-ship layer for the tools that
    watch the stack and the deployed apps.

    Two streams, two yardsticks:
    - finder-findings.jsonl: perf-engineer/incident-responder findings. must-act CLAIMS this file
      (so signals.py won't auto-render it) but only feeds the FIX loop — this is its weekly verdict.
      Yardstick reads OPEN findings (logged − cleared via must-act-applied.txt): findings should get
      fixed, not pile up. 0 ✅ · 1-2 👀 · 3+ ⚠️ (backlog accreting = the loop isn't consuming).
    - cto-agent.jsonl: distinguishes the owner-digest weekly HEARTBEAT (must fire ≥1/wk or the
      watcher stopped) from actual cto-cycle EVALUATIONS (event-driven — 0 is fine, just reported).
      The split exists because the log conflated them until 2026-07-19 (cto-cycle logs via cto-log.py)."""
    # ---- finder findings: open vs fixed ----
    applied = set()
    ap = ROOT / "p5-loop" / "must-act-applied.txt"
    if ap.exists():
        # One id per line, optionally followed by the date it was applied (must-act.py's
        # WARN_MARKER_TTL_DAYS). A bare .split() would fold those dates and the header
        # comments into the id set — harmless for `finder:` lookups, wrong in principle.
        applied = {ln.split()[0] for ln in ap.read_text().splitlines()
                   if ln.strip() and not ln.lstrip().startswith("#")}
    finds = {}
    for r in read_jsonl("finder-findings.jsonl"):
        fid = r.get("id")
        if fid:
            finds[fid] = r          # newest line per id wins
    open_finds = [r for fid, r in finds.items() if fid not in applied]
    fixed_n = len(finds) - len(open_finds)
    if open_finds:
        fv = "👀 разбирай" if len(open_finds) <= 2 else "⚠️ копятся — чини"
    else:
        fv = "✅ пусто (все разобраны)" if finds else "👀 находок нет"
    # ---- cto runs: heartbeat (rolling 7d) vs evaluation (this ISO week) ----
    # Heartbeat liveness uses a ROLLING 7-day window, NOT the ISO week. On a Monday (ISO-week
    # start) the calendar-week count reads 0 until that week's first digest both fires AND the
    # overview regenerates after it — so the Monday digest reported "не сработал" in the very run
    # that proved it fired (chicken-and-egg, fixed 2026-07-20). 7d rolling matches the real intent
    # ("fired ≥1×/week or the watcher died") without the calendar-boundary false-negative.
    _cto_all = read_jsonl("cto-agent.jsonl")
    _hb_cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    digest_n = 0
    for r in _cto_all:
        if r.get("agent") != "owner-digest":
            continue
        try:
            if datetime.fromisoformat(str(r.get("ts", "")).replace("Z", "+00:00")) >= _hb_cutoff:
                digest_n += 1
        except Exception:
            continue
    eval_n = sum(1 for r in week_rows("cto-agent.jsonl", y, w) if r.get("agent") == "cto-cycle")
    hb = "✅ жив" if digest_n else "⚠️ дайджест не сработал за 7 дней"

    lines = ["### 🤖 CTO + finder агенты"]
    lines.append(f"- finder-находки: **{len(open_finds)}** открытых · {fixed_n} закрыто — {fv}")
    for r in open_finds[:5]:
        proj = r.get("project", "")
        lines.append(f"    - {r.get('severity','idle')} [{proj}] {(r.get('what','') or '')[:80]}")
    lines.append(f"- CTO прогоны: heartbeat owner-digest ×{digest_n}/7дн — {hb} · "
                 f"оценок tool-cycle ×{eval_n} (неделя, event-driven, 0 = норм)")
    return "\n".join(lines), {"finder_open": len(open_finds), "finder_fixed": fixed_n,
                              "cto_digest": digest_n, "cto_eval": eval_n}


def section_tool_health():
    """Per-tool COMPUTED verdict (spec 004): every tool that logs an `outcome` gets a ✅/👀/⚠️ from
    tool-verdict.py — precision arithmetic, not a hand-asserted registry claim. This is the readable
    surface for the tool-effectiveness loop; ⚠️ rows also flow into must-act via tool_verdict_items().
    Shells out ($0, no import — the script name has a hyphen); fails soft to a 'collecting' line so
    the rail is visible before any tool is instrumented."""
    tv = ROOT / "scripts" / "p5" / "tool-verdict.py"
    if not tv.exists():
        return ""
    try:
        out = subprocess.run([sys.executable, str(tv), "--json"],
                             capture_output=True, text=True, timeout=30)
        tools = (json.loads(out.stdout).get("tools", []) if out.stdout.strip() else [])
    except (subprocess.SubprocessError, ValueError):
        return ""
    lines = ["### 🧪 Инструменты — вычисленный вердикт (не заявленный)"]
    if not tools:
        lines.append("- _пока нет outcome-логов — рельс жив, ждёт первую инструментованную фичу "
                     "(см. p5-loop/tool-tracking/OUTCOME-CONVENTION.md)_")
        return "\n".join(lines)
    order = {"⚠️": 0, "👀": 1, "✅": 2}
    for r in sorted(tools, key=lambda r: order.get(r["verdict"], 9)):
        p = "—" if r["precision"] is None else f"{r['precision']:.0%}"
        lines.append(f"- {r['verdict']} **{r['tool']}** ({r['tool_class']}): помог {p} "
                     f"из {r['fires']} ({r['helped']}✓/{r['wasted']}✗)")
    return "\n".join(lines)


def main():
    now, y, w = iso_now()
    tag = f"Week {y}-W{w:02d}"
    hooks_md, stats = section_hooks(y, w)
    burn_md = section_burn()
    cfg_md, cfg_n = section_config_audit()
    kimi_md, kimi_stats = section_kimi(y, w)
    deleg_md, deleg_stats = section_delegation(y, w)  # D0 routing instrument
    adopt_per, (adopt_s, adopt_a), per_trig = skill_adoption(y, w)  # outcome metric: suggested -> invoked
    stats["adopt_per"] = adopt_per
    stats["adopt_overall"] = (adopt_s, adopt_a)
    hygiene_md, prune_n = section_suggester_hygiene(per_trig)
    prune0_md, prune0_n = section_prune_candidates(per_trig)  # O2 team-model: 0%-adoption prune signal
    o2_md, o2_pct = section_o2_rollout(y, w)  # O2 team-model: rollout-detection precision
    comp = compaction_stats(y, w)
    comp_md = section_compaction(comp)
    refresh_expansion_ab()  # weekly: re-run the A/B so latest.json is fresh (no separate job)
    exp_md, exp_new_hit = section_expansion_ab()
    refresh_capability_audit()  # monthly cadence (skips if <25d old) — no separate job
    capaudit_md, capaudit_gaps = section_capability_audit()
    refresh_gbrain_doctor()  # weekly: re-run doctor's 81 health checks (read-only, skips if <3d) — no separate job
    gbhealth_md, gbrain_fails = section_gbrain_health()
    deleg_sig = delegation_signal(y, w)  # Opus-discipline: is the main loop delegating, or solo-grinding?
    epi_line, epi_pct = section_episodic_adoption(y, w)  # #6 structured episodic notes adoption
    hooks_md = hooks_md + "\n" + epi_line
    gbm_md, gbm_snap = section_gbrain_maintenance(y, w)  # gap #4: gbrain-volunteer/advisor/mem2brain/glm-spend
    cov_md, cov_alerts = section_coverage_watch(y, w)  # 2026-07-08 audit: silence + ship-without-measure watchdog
    hooks_md = hooks_md + "\n\n" + cov_md
    decide_md, decide_n = section_decide_ledger(y, w)  # gap #5: /decide ledger
    cto_md, cto_snap = section_cto(y, w)  # measure-what-you-ship for CTO + finder agents

    # G6 dream-crash count for the snapshot (section_health renders the same count in prose)
    _dream_log = LOGS / f"gbrain-dream-{datetime.now(timezone.utc).strftime('%Y-%m')}.log"
    dream_crashes = None
    if _dream_log.exists():
        try:
            dream_crashes = sum(1 for l in _dream_log.read_text(encoding="utf-8", errors="replace").splitlines()
                                 if "WARN: previous run did not exit cleanly" in l)
        except Exception:
            dream_crashes = None

    # flat snapshot for week-over-week trend
    bm = re.search(r"(\d+)[^\n]{0,30}write-prod-code", burn_md)
    snap = {
        "sugg_n": len(stats["sugg"]),
        "verify": stats["verify"],
        "bbg_deny": stats.get("bbg_deny"), "tok_rpt": stats.get("tok_rpt"), "tok_small": stats.get("tok_small"),
        "tok_base": stats.get("tok_base"), "tok_sub": stats.get("tok_sub"), "tok_mtok_h": stats.get("tok_mtok_h"),
        "corr_unrev": stats["corr_unrev"],
        "config_issues": cfg_n,
        "burn_warns": int(bm.group(1)) if bm else None,
        "kimi_ck": kimi_stats["kimi_ck"],
        "kimi_kd": kimi_stats["kimi_kd"],
        "glm_ck": kimi_stats["glm_ck"],
        "routing_deleg": deleg_stats["routing_deleg"],
        "routing_inspect": deleg_stats["routing_inspect"],
        "swarm_nudge": stats.get("swarm_nudge", 0),
        "msg_decomp": stats.get("msg_decomp", 0),
        "browser_fires": stats.get("browser_fires", 0),
        "adopt_pct": round(100 * adopt_a / adopt_s) if adopt_s else None,
        "prune_n": prune_n,
        "prune0_n": prune0_n,
        "o2_precision_pct": o2_pct,
        "compacts": comp["precompact"],
        "synth_real_pct": comp["real_pct"],
        "exp_new_hit": exp_new_hit,
        "consti_surface": len(week_rows("constitution-surface.jsonl", y, w)),
        "decision_pairs": len(read_jsonl("decision-pairs.jsonl")),
        "rung_streak": rung_streak(),
        "deleg_spawns": deleg_sig["spawns"],
        "deleg_density": deleg_sig["density"],
        "opus_mtok": deleg_sig["opus_mtok"],
        "sub_share_pct": deleg_sig["sub_share_pct"],
        "capaudit_gaps": capaudit_gaps,
        "night_shift_tasks": len(week_rows("night-shift.jsonl", y, w)) or None,
        "dream_crashes": dream_crashes,
        "episodic_adopt_pct": epi_pct,
        "gb_usage_total": gbm_snap.get("gb_usage_total"),
        "gb_usage_sem": gbm_snap.get("gb_usage_sem"),
        "gb_usage_graph": gbm_snap.get("gb_usage_graph"),
        "gb_usage_direct": gbm_snap.get("gb_usage_direct"),
        "gb_volunteer": gbm_snap.get("gb_volunteer"),
        "gb_advisor": gbm_snap.get("gb_advisor"),
        "mem2brain": gbm_snap.get("mem2brain"),
        "diary2brain": gbm_snap.get("diary2brain"),
        "coverage_alerts": cov_alerts,
        "decide_ledger_n": decide_n,
        "moshi_fires": stats.get("moshi_fires", 0),
        "consti_clusters": stats.get("consti_clusters"),
        "gbrain_fails": gbrain_fails,
        "finder_open": cto_snap["finder_open"],
        "finder_fixed": cto_snap["finder_fixed"],
        "cto_eval": cto_snap["cto_eval"],
    }

    block = "\n\n".join([
        f"## System Overview — {tag}  (generated {now.strftime('%Y-%m-%d %H:%M UTC')})",
        section_trends(snap),
        section_rung_gates(),
        section_opus_discipline(deleg_sig),
        "### 🔥 Burn\n" + burn_md,
        hooks_md,
        kimi_md,
        deleg_md,
        section_git(),
        section_skills(stats),
        section_skill_fires(),
        section_skill_gaps(y, w),
        cfg_md,
        section_flags(stats),
    ] + ([comp_md] if comp_md else []) + ([hygiene_md] if hygiene_md else [])
      + [prune0_md, o2_md]
      + ([gbm_md] if gbm_md else []) + ([decide_md] if decide_md else []) + [cto_md]
      + ([th_md] if (th_md := section_tool_health()) else [])
      + [
        exp_md,
        capaudit_md,
        gbhealth_md,
        section_health(),
    ])

    intro = "# P5 System Overview (append-only, newest first)\n\n---\n"
    text = OUT.read_text() if OUT.exists() else intro
    head, sep, body = text.partition("\n---\n")
    body = body.lstrip("\n")
    # replace an existing same-week block at top
    if re.match(rf"## System Overview — {re.escape(tag)}\b", body):
        body = re.sub(r"^## System Overview —.*?(?=\n## System Overview —|\Z)", "", body, count=1, flags=re.DOTALL).lstrip("\n")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(f"{head}{sep}\n\n{block}\n\n{body}".rstrip() + "\n")
    write_snapshot(y, w, snap)  # AFTER read, so this week's delta used last week's file

    # notify
    n_flags = block.count("⚠")
    msg = f"{tag}: {len(stats['sugg'])} nudges, {stats['corr_unrev']} corrections, {cfg_n} config, {n_flags} warns"
    try:
        subprocess.run(["/usr/bin/osascript", "-e",
                        f'display notification "{msg}" with title "cc-stack System Overview"'],
                       check=False, timeout=10)
    except Exception:
        pass
    print(f"wrote {tag} to {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
