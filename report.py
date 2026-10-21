#!/usr/bin/env python3
"""Weekly Report Loop — English "what happened / what broke / how we fixed it / still open".

Reads RAW transcripts (mandatory — distilled docs miss patterns) + git log + corrections.md
+ diary over a window, runs a map-reduce through Kimi (claude-kimi), writes an English report.

Design: p5-loop/weekly-report-design.md
Usage:  python3 report.py [DAYS]      (default 7)   |   --dry  (select+distill, no LLM)
"""
import json, os, re, sys, subprocess, glob, time, datetime, urllib.request, urllib.error

HOME = os.path.expanduser("~")
PROJECTS_DIR = f"{HOME}/.claude/projects"
CORRECTIONS = f"{HOME}/.claude/projects/-Users-rusakovanton/memory/corrections.md"
DIARY_DIR = f"{HOME}/.claude/projects/-Users-rusakovanton/memory/diary"
REPORTS_DIR = f"{HOME}/projects/cc-stack-2026/p5-loop/reports"
GIT_REPOS = [f"{HOME}/projects/identity-os", f"{HOME}/projects/cc-stack-2026",
             f"{HOME}/connectpro", f"{HOME}/.claude"]
# Backend is now GLM (z.ai Coding Plan) as of 2026-07-03 — Kimi subscription lapsed.
KIMI = f"{HOME}/.local/bin/claude-glm"
# Absolute, because the weekly runs under launchd with PATH=/opt/homebrew/bin:/usr/bin:/bin:
# /usr/sbin:/sbin, and `claude` lives in ~/.local/bin. Calling it by bare name made the Haiku
# fallback die with "[Errno 2] No such file or directory: 'claude'" on four consecutive
# Mondays (2026-07-13/20/27, 08-03) — logged every time in weekly-report.err.log, and
# invisible in the report itself, which said only "news synthesis unavailable — LLM error".
# claude-glm survives because it exports its own PATH; this call had nothing.
# 2nd leg runs on Kimi (Moonshot) via the engine switch — a different vendor and quota than the
# GLM 1st leg; the bare binary hit the dead Anthropic API key (spec 020, ADR-0005).
CLAUDE = f"{HOME}/.local/bin/claude-engine"
# $0 third leg (Anton, 2026-09-29): under the z.ai plan Kimi(GLM) and Haiku share ONE quota, so
# W39+W40 lost the narrative to a 429. Tried in order — free models get rate-limited or vanish.
# The prompt is raw session transcripts, so only providers that neither train on nor retain
# prompts: every request carries data_collection=deny and OpenRouter refuses any other route
# (the Nvidia :free endpoints train and retain — they now 404 by construction). Of 16 :free
# models on 2026-09-29 only these two had such a provider; ling answered a 142k-token probe in
# 14 s, qwen timed out at 400 s on it and stays as the second try for smaller prompts.
OPENROUTER_FREE = ["inclusionai/ling-3.0-flash-sante:free",    # 262k ctx, via Novita
                   "qwen/qwen3.8-27b:free"]                     # 262k ctx, via ModelRun
# Keychain, because launchd has no API-key env. NOT omnigent/openrouter: its omni-budget
# workspace guardrail 404s every :free model (model-ignored-by-guardrail, 2026-09-29).
# openrouter-jev is outside that guardrail and capped at $2/day; :free costs $0.
OPENROUTER_KEYCHAIN = ["security", "find-generic-password", "-s", "omnigent", "-a", "openrouter-jev", "-w"]
LLM_LEGS = []  # which leg answered each llm_fallback() call — shown in the report header

MIN_HUMAN_TURNS = 3        # below this = subagent/swarm/trivial, skip
MAX_SESSIONS = 25          # cap meatiest sessions per run; rest logged as dropped
COUNT_LINE_CAP = 20_000    # a human prompt line is never bigger; skip huge tool_result lines when counting
DISTILL_HEAD = 30_000      # per-session distill: keep head + tail if it overflows
DISTILL_TAIL = 10_000
BATCH_BUDGET = 180_000     # chars of distilled text per Kimi map call (ARG_MAX-safe)
BLOCK_TEXT_CAP = 2_000     # truncate any single text/tool-input block


# ---------- selection ----------
def human_turns(path):
    """Count Anton's typed prompts (str/text user blocks, not tool_result, not harness envelopes)."""
    n = 0
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"type":"user"' not in line or len(line) > COUNT_LINE_CAP:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("type") != "user":
                    continue
                c = r.get("message", {}).get("content")
                txt = None
                if isinstance(c, str):
                    txt = c
                elif isinstance(c, list):
                    for b in c:
                        if isinstance(b, dict) and b.get("type") == "text":
                            txt = b.get("text", ""); break
                if txt and not txt.lstrip().startswith("<") and not txt.startswith("Caveat:"):
                    n += 1
    except Exception:
        return 0
    return n


def select_sessions(days):
    cutoff = time.time() - days * 86400
    cand = []
    for p in glob.glob(f"{PROJECTS_DIR}/**/*.jsonl", recursive=True):
        try:
            if os.path.getmtime(p) < cutoff:
                continue
        except OSError:
            continue
        cand.append(p)
    scored = []
    for p in cand:
        ht = human_turns(p)
        if ht >= MIN_HUMAN_TURNS:
            scored.append((ht, os.path.getmtime(p), p))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    selected = scored[:MAX_SESSIONS]
    return selected, len(scored), len(cand)


# ---------- distillation ----------
def distill_transcript(path):
    """JSONL -> compact behaviour trace. Drops tool_result bodies + thinking (the bulk)."""
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                t = '"type":"'
                if t not in line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                typ = r.get("type")
                if typ not in ("user", "assistant"):
                    continue
                c = r.get("message", {}).get("content")
                if isinstance(c, str):
                    s = c.strip()
                    if s and not s.lstrip().startswith("<") and not s.startswith("Caveat:"):
                        out.append("USER: " + s[:BLOCK_TEXT_CAP])
                    continue
                if not isinstance(c, list):
                    continue
                for b in c:
                    if not isinstance(b, dict):
                        continue
                    bt = b.get("type")
                    if bt == "text":
                        s = (b.get("text") or "").strip()
                        if not s:
                            continue
                        who = "USER" if typ == "user" else "AI"
                        if who == "USER" and (s.lstrip().startswith("<") or s.startswith("Caveat:")):
                            continue
                        out.append(f"{who}: " + s[:BLOCK_TEXT_CAP])
                    elif bt == "tool_use":
                        name = b.get("name", "?")
                        inp = b.get("input", {})
                        hint = inp.get("command") or inp.get("file_path") or inp.get("description") \
                            or inp.get("pattern") or inp.get("prompt") or ""
                        out.append(f"TOOL {name}: {str(hint)[:200]}")
                    elif bt == "tool_result" and b.get("is_error"):
                        s = b.get("content")
                        s = s if isinstance(s, str) else json.dumps(s)[:400]
                        out.append("ERROR: " + str(s)[:400])
    except Exception:
        return ""
    text = "\n".join(out)
    if len(text) > DISTILL_HEAD + DISTILL_TAIL:
        text = text[:DISTILL_HEAD] + "\n...[trimmed]...\n" + text[-DISTILL_TAIL:]
    return text


# ---------- Kimi ----------
# --bare + --tools "" on every leg: a text-in/text-out call needs no CLAUDE.md, hooks, memory,
# plugins or tools (with tools on, a one-line prompt wandered into Bash for 4 turns) —
# measured 2026-10-21, a one-line prompt sends ~20.8k input tokens without it and ~1k with it.
def kimi(prompt, timeout=600):
    """Returns (text, cost_usd). Fail-open: returns ('', 0.0) on any error."""
    try:
        p = subprocess.run(
            [KIMI, "-p", prompt, "--bare", "--tools", "", "--output-format", "json", "--max-turns", "4",
             "--mcp-config", '{"mcpServers":{}}', "--strict-mcp-config"],
            capture_output=True, text=True, timeout=timeout)
        # claude-kimi prepends banner lines (subscription/base-url) before the JSON payload.
        obj = next((json.loads(ln) for ln in p.stdout.splitlines()
                    if ln.strip().startswith("{")), None)
        if obj is None:
            raise ValueError(f"no JSON line in stdout: {p.stdout[:200]!r}")
        return obj.get("result", "") or "", float(obj.get("total_cost_usd") or 0.0)
    except Exception as e:
        sys.stderr.write(f"[kimi error] {e}\n")
        return "", 0.0


def openrouter_free(prompt, timeout=600):
    """$0 leg: OpenRouter :free models in order. Returns (text, model_id); ('', '') if all fail.
    The key is read from the keychain at call time and never written anywhere."""
    try:
        key = subprocess.run(OPENROUTER_KEYCHAIN, capture_output=True, text=True,
                             timeout=10).stdout.strip()
        if not key:
            raise ValueError("no key in keychain omnigent/openrouter-jev")
    except Exception as e:
        sys.stderr.write(f"[openrouter fallback error] {e}\n")
        return "", ""
    for model in OPENROUTER_FREE:
        try:
            req = urllib.request.Request(
                "https://openrouter.ai/api/v1/chat/completions",
                data=json.dumps({"model": model,
                                 "messages": [{"role": "user", "content": prompt}],
                                 "provider": {"data_collection": "deny"}}).encode(),
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                obj = json.load(r)
            # A 200 can still carry {"error": ...} and no choices (seen 2026-09-29).
            text = ((obj.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            if text.strip():
                return text, model
            sys.stderr.write(f"[openrouter fallback error] {model}: "
                             f"{str(obj.get('error') or 'empty reply')[:200]}\n")
        except Exception as e:
            body = e.read()[:200] if isinstance(e, urllib.error.HTTPError) else ""
            sys.stderr.write(f"[openrouter fallback error] {model}: {e} {body}\n")
    return "", ""


def _llm_ok(text):
    """A CLI result that is an API error (429 quota, 402, ...) is a failure, not report text."""
    return bool(text.strip()) and not text.lstrip().startswith("API Error")


def llm_fallback(prompt, timeout=600):
    """kimi() (GLM) → Kimi via claude-engine (Moonshot) → OpenRouter :free ($0). An empty reply or an "API Error"
    result from ANY leg counts as failure. Under the z.ai plan the first two legs share one
    quota; the second (Moonshot) and third are off it. Same pattern as news-digest-summarize.py's
    llm(). Returns (text, cost_usd); ('', 0.0) if all fail — caller must fail open."""
    out, cost = kimi(prompt, timeout=timeout)
    if _llm_ok(out):
        LLM_LEGS.append("glm")
        return out, cost
    try:
        p = subprocess.run(
            [CLAUDE, "-p", prompt, "--bare", "--tools", "", "--model", "haiku",
             "--output-format", "json", "--max-turns", "3",
             "--mcp-config", '{"mcpServers":{}}', "--strict-mcp-config"],
            capture_output=True, text=True, timeout=timeout,
            env={**os.environ, "CLAUDE_ENGINE": "kimi"})
        obj = next((json.loads(ln) for ln in p.stdout.splitlines()
                    if ln.strip().startswith("{")), None)
        if obj and _llm_ok(obj.get("result", "") or ""):
            LLM_LEGS.append("kimi")
            return obj["result"], float(obj.get("total_cost_usd") or 0.0)
    except Exception as e:
        sys.stderr.write(f"[haiku fallback error] {e}\n")
    out, model = openrouter_free(prompt, timeout=timeout)
    if out:
        LLM_LEGS.append(model)
        return out, 0.0
    return "", 0.0


def parse_json_blob(text):
    """Strip fences, pull the first JSON array/object out of an LLM reply."""
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    for opener, closer in (("[", "]"), ("{", "}")):
        i, j = text.find(opener), text.rfind(closer)
        if i != -1 and j > i:
            try:
                return json.loads(text[i:j + 1])
            except Exception:
                pass
    return None


MAP_PROMPT = """You are an analyst reviewing one week of Claude Code work sessions for a single user (Anton — a non-developer who directs the AI in natural language). Below are distilled session transcripts: his typed prompts (USER), the AI's replies (AI), tool calls (TOOL <name>), and tool errors (ERROR). Tool outputs were stripped to save space.

For EACH session, extract findings. Output a STRICT JSON array, ONE object per session, in the same order, schema:
[{{"session":"<id>","what_happened":"1-2 sentences, the real work","broke":["errors/failures/dead-ends/friction"],"fixed":["what was resolved and HOW"],"patterns":["recurring behaviours or repeated mistakes visible in the trace that a one-line summary would miss"],"open":["unresolved threads"]}}]

Rules: be concrete and terse. Empty list if a field has nothing. DO NOT invent — only report what the trace shows. Output ONLY the JSON array, no prose, no fences.

=== SESSIONS ===
{sessions}
"""

REDUCE_PROMPT = """You are writing a weekly English report for Anton (non-developer; he directs Claude Code by natural language and only wants to understand what happened and how problems were fixed).

INPUTS:
(A) PER-SESSION FINDINGS extracted from this week's raw transcripts:
{findings}

(B) GIT COMMITS across his repos this week:
{git}

(C) corrections.md — his log of ALREADY-KNOWN recurring mistakes (use to AVOID repeating patterns already captured):
{corrections}

(D) DIARY entries this week:
{diary}

Write a concise English markdown report with EXACTLY these five sections and nothing else:

## What happened
## What broke
## How we fixed it
## Patterns
## Still open

Rules:
- Synthesize themes — do NOT dump the commit list.
- "Patterns" = recurring behaviours from the findings that are NOT already in corrections.md (C). If a pattern is already known in C, skip it. If none are new, say "No new recurring patterns this week."
- Every "Still open" item must be a concrete next step.
- Be terse: the report must be shorter than its inputs. Plain English, no jargon.
- Output ONLY the markdown report.
"""


# ---------- context ----------
def gather_context(days):
    git = []
    for d in GIT_REPOS:
        try:
            r = subprocess.run(["git", "-C", d, "log", f"--since={days} days ago",
                                "--pretty=format:%h %s"], capture_output=True, text=True, timeout=30)
            if r.stdout.strip():
                git.append(f"# {os.path.basename(d)}\n{r.stdout.strip()}")
        except Exception:
            continue
    git_txt = "\n\n".join(git)[:30_000]

    corr = ""
    try:
        with open(CORRECTIONS, encoding="utf-8", errors="replace") as f:
            corr = f.read()[:20_000]
    except Exception:
        pass

    cutoff = time.time() - days * 86400
    diary = []
    for p in sorted(glob.glob(f"{DIARY_DIR}/*.md")):
        try:
            if os.path.getmtime(p) >= cutoff:
                diary.append(open(p, encoding="utf-8", errors="replace").read())
        except Exception:
            continue
    return git_txt, corr, "\n\n".join(diary)[:15_000]


# ---------- map-reduce ----------
def map_sessions(selected):
    """Pack distilled sessions into budget-bounded batches; Kimi -> findings[]."""
    blobs, total_cost = [], 0.0
    sid = lambda p: os.path.basename(p).replace(".jsonl", "")[:8]
    distilled = [(sid(p), distill_transcript(p)) for _, _, p in selected]
    distilled = [(s, d) for s, d in distilled if d]

    batch, size, batches = [], 0, []
    for s, d in distilled:
        chunk = f"\n\n----- SESSION {s} -----\n{d}"
        if size + len(chunk) > BATCH_BUDGET and batch:
            batches.append(batch); batch, size = [], 0
        batch.append(chunk); size += len(chunk)
    if batch:
        batches.append(batch)

    findings = []
    for i, b in enumerate(batches):
        sys.stderr.write(f"[map] batch {i+1}/{len(batches)} ({sum(len(x) for x in b)//1024}KB)\n")
        text, cost = llm_fallback(MAP_PROMPT.format(sessions="".join(b)))
        total_cost += cost
        parsed = parse_json_blob(text)
        if isinstance(parsed, list):
            findings.extend(parsed)
        elif isinstance(parsed, dict):
            findings.append(parsed)
    return findings, total_cost, len(batches)


def must_act_md():
    """The consumer's Anton-facing line: what's broken this week + which one is being fixed.

    Regenerates must-act.json fresh (reality-audit + SYSTEM-OVERVIEW ⚠️ + triage KILL), then renders
    the top broken items. This is the section that closes the measure→consume gap — the whole reason
    the stack was measuring in the first place. Fail-open: returns "" if the script/file is absent.
    """
    try:
        _p5 = os.path.dirname(os.path.abspath(__file__))          # scripts/p5
        _root = os.path.dirname(os.path.dirname(_p5))             # repo root
        script = os.path.join(_p5, "must-act.py")
        maj = os.path.join(_root, "p5-loop", "must-act.json")
        subprocess.run(["python3", script], capture_output=True, timeout=60)
        with open(maj, encoding="utf-8") as f:
            d = json.load(f)
        items = d.get("items", [])
        if not items:
            return "\n## 🟢 Stack self-correction\n\nNothing broken this week — must-act is empty.\n"
        L = [f"\n## 🔴 Stack self-correction — {len(items)} broken this week\n",
             "_The consumer surface (must-act.json). Claude fixes #1 per cycle; fixed ids go to "
             "`must-act-applied.txt`._\n"]
        for i, it in enumerate(items[:6]):
            mark = "**→ fixing #1:** " if i == 0 else "- "
            what = str(it.get("what", "")).replace("*", "").strip()[:100]
            L.append(f"{mark}[{it.get('severity','?')}] {what}")
        if len(items) > 6:
            L.append(f"- …and {len(items) - 6} more")
        return "\n".join(L) + "\n"
    except Exception:
        return ""


def rules_hygiene_md():
    """autoswarm-steal: append the standing-rules + corrections audit to the weekly report.
    Deterministic analytics only ($0, no LLM) — the judge is noisy on local Qwen and stays
    on-demand (`python3 rules-hygiene.py`). This is the PRUNE half rule-intake-gate lacks."""
    import importlib.util
    p = os.path.join(os.path.dirname(__file__), "rules-hygiene.py")
    try:
        spec = importlib.util.spec_from_file_location("rules_hygiene", p)
        rh = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rh)
        rules = rh.load_rules()
        an = rh.analytics(rules)
        corr = rh.corrections_status()
    except Exception as e:
        return f"\n## Rules hygiene\n_(audit failed: {e})_\n"
    L = ["", "## Rules hygiene",
         f"**{an['total_rules']} always-on rules · ~{an['total_tokens']:,} tokens loaded every session.**"]
    if corr["watch_count2"]:
        L.append(f"- ⚠️ **At Count=2 (one more correction → new rule):** {'; '.join(corr['watch_count2'])}")
    if corr["unreviewed"]:
        tot = sum(corr["unreviewed"].values())
        top = ", ".join(f"`{k}`×{v}" for k, v in list(corr["unreviewed"].items())[:5])
        L.append(f"- **{tot} unreviewed correction-candidates** (top: {top})")
    if an["orphans"]:
        L.append(f"- **Orphan rules** (no links in/out — review for relevance): {', '.join(an['orphans'])}")
    if an["no_boundary"]:
        L.append(f"- **Missing `## Boundary`** (intake-gate requires it): {', '.join(an['no_boundary'])}")
    if an["dangling"]:
        L.append(f"- **Dangling `[[links]]`:** {an['dangling']}")
    L.append("- _Full per-rule table + LLM merge/retire verdicts:_ `python3 scripts/p5/rules-hygiene.py`")
    L.extend(rule_outcomes_lines())
    return "\n".join(L) + "\n"


def rule_outcomes_lines():
    """Did the rules we wrote actually change behaviour? Refresh, then read the verdict.

    `cluster_pass.py --rule-outcomes` splits each learned rule's correction cluster into
    before-creation and after-creation halves, so a rule that keeps getting violated after
    it was written is visible as such. It needs NO LLM — it reads the already-clustered
    clusters-latest.md — which is why it can run here, weekly, on a Monday when the z.ai
    quota is exhausted.

    Wired 2026-08-17 because nothing scheduled it and nothing read it: the stream had been
    silent since 2026-07-19 while the weekly kept reporting the rule COUNT and their TOKEN
    cost every Monday, i.e. the price of the rule set with no measure of whether it works.
    The first run after wiring said 7 of 8 measurable rules had recurred after creation and
    exactly one (verify-platform-facts) had gone clean — a number worth a line.
    """
    import subprocess as _sp
    script = os.path.join(HOME, ".claude", "scripts", "constitution", "cluster_pass.py")
    log = os.path.join(HOME, ".claude", "logs", "rules-outcome.jsonl")
    if not os.path.exists(script):
        return []
    try:
        _sp.run([sys.executable, script, "--rule-outcomes"], cwd=os.path.dirname(script),
                capture_output=True, text=True, timeout=180)
    except Exception as e:
        # Fail open — a stalled refresh must not hold up the whole weekly. But say so in the
        # report itself: a fail-open that stays silent is the bug we keep paying for, and the
        # stale numbers below would otherwise read as this week's.
        return [f"- _rule-outcomes refresh failed ({type(e).__name__}) — figures below are the last successful run._"]
    if not os.path.exists(log):
        return []
    try:
        rows = [json.loads(x) for x in open(log, encoding="utf-8") if x.strip()]
    except Exception:
        return []
    if not rows:
        return []
    last_ts = rows[-1].get("ts", "")[:10]
    latest = [r for r in rows if r.get("ts", "")[:10] == last_ts]
    recurred = sorted({r["tool"] for r in latest if r.get("outcome") == "recurred"})
    clean = sorted({r["tool"] for r in latest if r.get("outcome") == "clean"})
    if not (recurred or clean):
        return []
    out = [f"- **Did the rules work?** {len(clean)} clean · {len(recurred)} still recurring "
           f"after the rule was created ({last_ts})"]
    if recurred:
        out.append(f"  - ⚠️ recurring: {', '.join(recurred)}")
    if clean:
        out.append(f"  - ✅ clean: {', '.join(clean)}")
    return out


def rule_bench_md():
    """Rule-effectiveness bench: surfaces the latest self-reported A/B (rule vs no-rule)
    LOC + token deltas for the code-shaping rules. Only code-shaping rules are benchable
    here; behavioral rules are measured via adoption/fire metrics in System Overview, not LOC.
    Silent when no bench has been run (results/latest.json absent)."""
    p = os.path.join(os.path.dirname(__file__), "..", "..",
                     "p5-loop", "rule-bench", "results", "latest.json")
    if not os.path.exists(p):
        return ""
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
    except Exception as e:
        return f"\n## Rule bench\n_(read failed: {e})_\n"
    L = ["", "## Rule bench (code-shaping rules — self-reported A/B)",
         f"_{d.get('generated','?')} · model {d.get('model','?')} · "
         f"{d.get('runs_per_cell','?')} runs/cell · {d.get('tasks','?')} tasks · "
         f"${d.get('total_cost_usd',0):.3f}_"]
    for r in d.get("rules", []):
        loc = r.get("loc_delta_pct")
        tok = r.get("tokens_delta_pct")
        def verdict(x):
            if x is None:
                return "n/a"
            return "✅ leaner" if x <= -10 else ("👀 flat" if x < 0 else "⚠️ no gain")
        L.append(f"- **{r.get('rule')}**: LOC {loc:+}% ({verdict(loc)}) · "
                 f"out-tokens {tok:+}% ({verdict(tok)})")
    L.append("- _Re-run: `python3 scripts/p5/rule-bench.py` (paid ~$1-2; behavioral rules "
             "are measured via adoption metrics, not here)._")
    return "\n".join(L) + "\n"


def stack_watch_md():
    """Stack-watch: installed-vs-upstream version drift for our dev tools (gbrain,
    context-mode, browser-harness, claude-code, …). Surfaces silent version rot in the
    weekly so stack updates get noticed and applied (the gbrain-20-versions-behind blind
    spot, 2026-06-24). Silent on any failure — a watcher must never break the report."""
    p = os.path.join(os.path.dirname(__file__), "stack-watch.py")
    if not os.path.exists(p):
        return ""
    try:
        r = subprocess.run([sys.executable, p, "--md"], capture_output=True,
                           text=True, timeout=180)
        md = r.stdout.strip()
        return ("\n" + md + "\n") if md else ""
    except Exception as e:
        return f"\n## Stack-watch\n_(check failed: {e})_\n"


def signals_md():
    """Signals: default consumer for every log stream no bespoke section claims. Makes
    an unwatched hook structurally impossible instead of merely against the rules — wiring
    the seven orphans of 2026-07-16 by hand would have guaranteed an eighth next month.
    Silent when nothing is unclaimed. Silent on any failure."""
    p = os.path.join(os.path.dirname(__file__), "signals.py")
    if not os.path.exists(p):
        return ""
    try:
        r = subprocess.run([sys.executable, p, "--md"], capture_output=True,
                           text=True, timeout=60)
        md = r.stdout.strip()
        return ("\n" + md + "\n") if md else ""
    except Exception as e:
        return f"\n## Сигналы\n_(check failed: {e})_\n"


def gbrain_usage_md():
    """gbrain usage (spec 013 FR-006): the 30-day recount lands in every weekly digest;
    a zero-call watchlist surface must melt or be consciously retired with a reason row.
    Silent on any failure."""
    p = os.path.join(os.path.dirname(__file__), "gbrain-usage.py")
    if not os.path.exists(p):
        return ""
    try:
        r = subprocess.run([sys.executable, p, "--md"], capture_output=True,
                           text=True, timeout=120)
        md = r.stdout.strip()
        return ("\n" + md + "\n") if md else ""
    except Exception as e:
        return f"\n## gbrain usage\n_(check failed: {e})_\n"


def gbrain_monthly_md():
    """gbrain monthly passes (spec 013 FR-003/FR-008): enrich people hub + contradiction
    scan + schema drift, gated to every 28 days by a marker file; cached result re-printed
    in between. Long timeout is fine — it only bites the Monday the gate is due."""
    p = os.path.join(os.path.dirname(__file__), "gbrain-monthly.py")
    if not os.path.exists(p):
        return ""
    try:
        r = subprocess.run([sys.executable, p, "--md"], capture_output=True,
                           text=True, timeout=900)
        md = r.stdout.strip()
        return ("\n" + md + "\n") if md else ""
    except Exception as e:
        return f"\n## gbrain monthly\n_(check failed: {e})_\n"


def instrument_coverage_md():
    """Instrument-coverage: features registered with no consumer + log streams nobody
    reads. Closes measure-what-you-ship's own loophole — "👀 pending" was a legal value,
    so a feature could ship while nothing rendered its yardstick (10 of 47 features, 7 of
    54 streams, measured 2026-07-16). Silent when green. Silent on any failure."""
    p = os.path.join(os.path.dirname(__file__), "instrument-coverage.py")
    if not os.path.exists(p):
        return ""
    try:
        r = subprocess.run([sys.executable, p, "--md"], capture_output=True,
                           text=True, timeout=60)
        md = r.stdout.strip()
        return ("\n" + md + "\n") if md else ""
    except Exception as e:
        return f"\n## Покрытие измерений\n_(check failed: {e})_\n"


def project_infra_watch_md():
    """Project-infra-watch: PER-PROJECT infra drift that the global stack-watch can't
    see — spec-kit scaffold versions frozen per project (the 2026-07-11 finding: 60+
    scaffolds at 0.8.9.dev0 while the global CLI moved on) + project-local skill copies
    that shadow a global skill but have gone stale. Silent on any failure."""
    p = os.path.join(os.path.dirname(__file__), "project-infra-watch.py")
    if not os.path.exists(p):
        return ""
    try:
        r = subprocess.run([sys.executable, p, "--md"], capture_output=True,
                           text=True, timeout=120)
        md = r.stdout.strip()
        return ("\n" + md + "\n") if md else ""
    except Exception as e:
        return f"\n## Project-infra-watch\n_(check failed: {e})_\n"


def stack_cron_md():
    """Stack-cron: all our launchd jobs in one view — surfaces ONLY when a job last
    exited non-zero or isn't loaded (silent when the whole fleet is green, so the weekly
    doesn't carry a 25-row table every week). Silent on any failure."""
    p = os.path.join(os.path.dirname(__file__), "stack-cron.py")
    if not os.path.exists(p):
        return ""
    # Refresh the plist mirror on the same cadence, since this is already the weekly that
    # owns the launchd surface — no new cron for a `cp`. ~/Library/LaunchAgents is
    # version-controlled nowhere, so this snapshot is the only off-disk copy of the job
    # definitions (p5-loop/launchd/README.md). Silent on failure: a backup that cannot be
    # taken must never cost us the report.
    try:
        subprocess.run([sys.executable, p, "--snapshot"],
                       capture_output=True, text=True, timeout=60)
    except Exception:
        pass
    try:
        r = subprocess.run([sys.executable, p, "--md", "--alerts-only"],
                           capture_output=True, text=True, timeout=60)
        md = r.stdout.strip()
        return ("\n" + md + "\n") if md else ""
    except Exception as e:
        return f"\n## Stack-cron\n_(check failed: {e})_\n"


def claudemd_currency_md():
    """CLAUDE.md currency (spec 002): per-project staleness + bloat of every CLAUDE.md —
    the most-leveraged instruction surface, which silently rots (global 9d / most 44-56d
    when built). Over-budget files are flagged for the native /doctor trim. Also reports
    carry-list drain health. Feeds the CTO-agent P2 inward-watcher (spec-012 §4.6). Silent
    on any failure — a watcher must never break the report."""
    try:
        import importlib.util
        modp = os.path.join(os.path.dirname(__file__), "claudemd_currency.py")
        if not os.path.exists(modp):
            return ""
        spec = importlib.util.spec_from_file_location("claudemd_currency", modp)
        cc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cc)
        # Canonical project list from projects.md (catches MISSING CLAUDE.md, e.g. self-forecast).
        pm = f"{HOME}/.claude/rules/projects.md"
        paths, seen = [f"{HOME}/.claude/CLAUDE.md"], set()
        if os.path.exists(pm):
            for line in open(pm, encoding="utf-8", errors="replace"):
                m = re.search(r"\|\s*(~[^\s|]+|/[^\s|]+)\s*\|", line)
                if m:
                    cmd = os.path.join(os.path.expanduser(m.group(1)), "CLAUDE.md")
                    if cmd not in seen:
                        seen.add(cmd); paths.append(cmd)
        rows = cc.claudemd_currency(paths)
        flagged = [r for r in rows if r.get("staleness_verdict") != "fresh" or r.get("bloat_over")]
        if not flagged:
            return "\n## 📄 CLAUDE.md currency\n_all CLAUDE.md fresh & within budget_\n"
        L = ["", "## 📄 CLAUDE.md currency",
             f"_{len(rows)} files · {len(flagged)} need attention (spec 002)_"]
        for r in sorted(flagged, key=lambda x: (x.get("staleness_verdict") == "missing",
                                                x.get("days_since_edit") or -1), reverse=True):
            proj = r.get("project", "?")
            if not r.get("exists"):
                L.append(f"- ⚠️ **{proj}** — no CLAUDE.md (create one)")
                continue
            bits = []
            sv = r.get("staleness_verdict")
            if sv in ("stale", "aging"):
                bits.append(f"{int(r.get('days_since_edit') or 0)}d {sv}")
            if r.get("bloat_over"):
                bits.append(f"bloat {r.get('lines')}L/~{r.get('tokens_est')}tok → /doctor")
            L.append(f"- {r.get('verdict_emoji', '')} **{proj}** — {' · '.join(bits)}")
        q = f"{HOME}/.claude/projects/-Users-rusakovanton/memory/pending-claudemd.md"
        if os.path.exists(q):
            txt = open(q, encoding="utf-8", errors="replace").read()
            seg = re.search(r"##\s*Pending(.*?)(?:\n##\s|\Z)", txt, re.S)
            body = seg.group(1) if seg else ""
            pend = sum(1 for ln in body.splitlines()
                       if ln.strip().startswith("- [") and "status: pending" in ln
                       and "<" not in ln)  # exclude the format-example template row
            if pend:
                L.append(f"- 📝 **carry-list:** {pend} pending edit(s) awaiting a session-start glance")
        return "\n".join(L) + "\n"
    except Exception as e:
        return f"\n## 📄 CLAUDE.md currency\n_(check failed: {e})_\n"


def automation_radar_md():
    """Automation-candidate radar (spec-012 §4.6 / p2-automation-radar-design): ranks recurring
    agent-dispatched work by frequency × median wall-clock — the biggest repeating time-sink is the
    best automate / reroute-off-quota target. Warming up until ~4 weeks of duration data accumulate
    (agent-usage.jsonl live since 2026-07-13, fix #1). Surfaces skill freq even while warming.
    Silent on any failure — a watcher must never break the report."""
    try:
        import importlib.util
        modp = os.path.join(os.path.dirname(__file__), "automation_radar.py")
        if not os.path.exists(modp):
            return ""
        spec = importlib.util.spec_from_file_location("automation_radar", modp)
        ar = importlib.util.module_from_spec(spec); spec.loader.exec_module(ar)
        logs = f"{HOME}/.claude/logs"
        r = ar.automation_radar(f"{logs}/skill-fires.jsonl", f"{logs}/routing-decisions.jsonl",
                                f"{logs}/agent-usage.jsonl")
        out = ["", "## 🤖 Automation-candidate radar"]
        warming = r.get("warming_up")
        if warming:
            out.append(f"👀 _warming up — {r.get('weeks_of_duration_data', 0):.1f}/4 weeks of duration "
                       f"data (agent-usage live since 2026-07-13); ranking not yet trustworthy._")
        else:
            cands = r.get("candidates", [])
            if cands:
                out.append("_top repeating time-sinks (freq × median wall-clock → automate / reroute):_")
                for c in cands[:5]:
                    sec = (c.get("median_ms") or 0) / 1000
                    out.append(f"- **{c.get('task_key')}** — {c.get('fires_per_wk', 0):.1f}×/wk × "
                               f"~{sec:.0f}s (n={c.get('n_durations')}) → score {c.get('score', 0):,.0f}")
        so = r.get("skill_only", [])
        if so:
            top = ", ".join(f"`{s.get('task_key')}`×{s.get('fires_per_wk', 0):.1f}/wk" for s in so[:5])
            out.append(f"- _skills (freq only — duration is v2):_ {top}")
        if len(out) == 2:  # nothing to say
            return ""
        return "\n".join(out) + "\n"
    except Exception as e:
        return f"\n## 🤖 Automation-candidate radar\n_(check failed: {e})_\n"


NEWS_DIR = f"{HOME}/.claude/news"
STACK_WATCH_LOG = f"{HOME}/.claude/logs/stack-watch.jsonl"
NEWS_SYNTH_LOG = f"{HOME}/.claude/logs/news-synthesis.jsonl"

NEWS_SYNTH_PROMPT = """You are advising on a Claude Code power-user stack (hooks, skills, MCP, subagents, model routing, learned-rules pipeline). Given this week's news takeaways and current tool version drift, output a markdown block: '### Новое → что взять' with 3-7 concrete, prioritized actions for THIS stack (each: one line action + one line why now). For each item, if the news item could REPLACE or MEANINGFULLY UPGRADE a tool WE ALREADY RUN (gbrain, context-mode, spec-kit, superpowers, RTK, serena, pinchtab, browser-harness, our hooks/skills), say so explicitly: name our current tool and what the newcomer does better — the "replace/enrich ours" angle, not only net-new adoption. Then '### Радар' with 1-3 items to watch but not act on. Russian. No preamble. If nothing is genuinely actionable, say so in one line — do not invent actions.

=== THIS WEEK'S NEWS TAKEAWAYS ===
{news}

=== TOOL VERSION DRIFT (latest check per tool) ===
{drift}
"""


def _digest_takeaways(path):
    """Pull the '### Key takeaways' block if the digest was already summarized,
    else the first 15 raw '- ' item lines."""
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except Exception:
        return ""
    m = re.search(r"^### Key takeaways\s*\n+(.*?)(?=\n<details|\n## |\Z)", text, re.M | re.S)
    if m:
        return m.group(1).strip()
    items = [ln for ln in text.splitlines() if ln.startswith("- ")][:15]
    return "\n".join(items)


def _news_synthesis_context(days):
    """Last N days of digest takeaways, skipping missing days."""
    out = []
    today = datetime.date.today()
    for i in range(days):
        d = today - datetime.timedelta(days=i)
        p = f"{NEWS_DIR}/digest-{d.isoformat()}.md"
        if not os.path.exists(p):
            continue
        block = _digest_takeaways(p)
        if block:
            out.append(f"### {d.isoformat()}\n{block}")
    return out


def _stack_watch_latest():
    """Latest stack-watch.jsonl entry per tool."""
    latest = {}
    try:
        with open(STACK_WATCH_LOG, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                tool = r.get("tool")
                if not tool:
                    continue
                if tool not in latest or r.get("ts", "") >= latest[tool].get("ts", ""):
                    latest[tool] = r
    except Exception:
        return []
    return sorted(latest.values(), key=lambda r: r.get("tool", ""))


def _log_news_synthesis(rec):
    try:
        os.makedirs(os.path.dirname(NEWS_SYNTH_LOG), exist_ok=True)
        with open(NEWS_SYNTH_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


GBRAIN_BIN = f"{HOME}/Library/Application Support/reflex/bun/bin/gbrain"


def _gbrain_dedupe_novoe(novoe_text):
    """ONE cheap gbrain query call to drop candidate items already ingested into the news
    namespace (requirement b). Fail-open on any error/timeout/missing key — returns the input
    unchanged. Heuristic: pull candidate action lines, query gbrain once with their combined
    text, and drop any line that shares 2+ significant (5+ char) tokens with a returned hit —
    that hit is already-known, so recommending it again is noise."""
    lines = novoe_text.splitlines()
    bullet_idx = [i for i, l in enumerate(lines) if re.match(r"^\s*(?:-|\d+\.)\s+\S", l)]
    if not bullet_idx or not os.path.exists(GBRAIN_BIN):
        return novoe_text, 0

    key = os.environ.get("OPENAI_API_KEY_REAL") or os.environ.get("OPENAI_API_KEY")
    if not key:
        return novoe_text, 0

    query_text = " ".join(lines[i] for i in bullet_idx)[:300]
    try:
        env = dict(os.environ)
        env["OPENAI_API_KEY"] = key
        env["OPENAI_BASE_URL"] = "https://api.openai.com/v1"
        r = subprocess.run([GBRAIN_BIN, "query", query_text, "--limit", "10", "--no-expand"],
                           capture_output=True, text=True, timeout=10, env=env)
        out = r.stdout
    except Exception:
        return novoe_text, 0

    known_terms = set()
    for line in out.splitlines():
        if not line.startswith("["):
            continue
        rest = line.split("] ", 1)[-1]
        slug = rest.split(" -- ")[0]
        known_terms |= {t.lower() for t in re.findall(r"[A-Za-z]{5,}", slug.replace("-", " ").replace("/", " "))}
    if not known_terms:
        return novoe_text, 0

    # group each bullet line with its immediate non-bullet continuation lines into one block;
    # a leading block of preamble lines before the first bullet is always kept.
    first_bullet = bullet_idx[0]
    kept, dropped = lines[:first_bullet], 0
    blocks, cur = [], []
    for i in range(first_bullet, len(lines)):
        if i in bullet_idx:
            if cur:
                blocks.append(cur)
            cur = [lines[i]]
        else:
            cur.append(lines[i])
    if cur:
        blocks.append(cur)

    for b in blocks:
        toks = {t.lower() for t in re.findall(r"[A-Za-z]{5,}", " ".join(b))}
        if len(toks & known_terms) >= 2:
            dropped += 1
            continue
        kept.extend(b)
    return "\n".join(kept), dropped


def section_news_synthesis(days=7):
    """Weekly news -> concrete stack actions. One llm_fallback() call over the last
    `days` of digest takeaways + current stack-watch version drift. Fail-open: no
    digests / empty LLM reply -> a one-line unavailable note, never breaks the report."""
    digests = _news_synthesis_context(days)
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat()

    if not digests:
        _log_news_synthesis({"ts": ts, "digests": 0, "actions_emitted": -1, "cost_usd": 0.0})
        return "\n## News synthesis\n_(news synthesis unavailable this week — no digests found)_\n"

    drift = _stack_watch_latest()
    drift_txt = "\n".join(f"- {r.get('tool')}: {r.get('installed')} -> {r.get('upstream')} "
                          f"({r.get('status')}, risk={r.get('risk')})" for r in drift) or "(none)"
    news_txt = "\n\n".join(digests)[:40_000]

    body, cost = llm_fallback(NEWS_SYNTH_PROMPT.format(news=news_txt, drift=drift_txt))
    body = body.strip()
    if not body:
        _log_news_synthesis({"ts": ts, "digests": len(digests), "actions_emitted": -1, "cost_usd": cost})
        return "\n## News synthesis\n_(news synthesis unavailable this week — LLM error)_\n"

    novoe, _, radar = body.partition("### Радар")
    novoe_dd, dropped = _gbrain_dedupe_novoe(novoe)
    body = novoe_dd + (("### Радар" + radar) if radar else "")
    actions = len(re.findall(r"(?m)^\s*(?:-|\d+\.)\s+\S", novoe_dd))
    _log_news_synthesis({"ts": ts, "digests": len(digests), "actions_emitted": actions,
                        "gbrain_dedupe_dropped": dropped, "cost_usd": cost})
    return "\n## News synthesis\n\n" + body + "\n"


RAZBORY_QUEUE = f"{HOME}/projects/cc-stack-2026/p5-loop/action-queue-classified.json"
VERDICT_ICON = {"needs_anton": "🔴", "fixed": "✅", "delete": "🗑", "overdue": "⏰"}
VERDICT_LABEL = {"needs_anton": "needs Anton", "fixed": "already fixed",
                 "delete": "delete-request", "overdue": "promised to resurface and did not"}
# How many rows of one verdict get listed before the rest collapse into a count. Reading the whole
# ledger (spec 011) took needs_anton from 14 to 66 in one step — the section is only useful if it
# stays a list a person finishes, so the oldest N are shown and the tail is named, not hidden.
VERDICT_CAP = 12
# Verdicts that are real outcomes but must NOT be listed row-by-row. `tracked_elsewhere` rows
# live in open-threads.md and already surface at session start; `unclassified` means the two
# heuristics found nothing, which is not a task. Both render as one counted line — listing
# them is what turned this section into 62 identical red rows nobody could work through.
COUNT_ONLY = {"tracked_elsewhere": "already owned by open-threads.md",
              "unclassified": "no deterministic signal — not evidence Anton is needed"}


def section_razbory():
    """Renders the classified action-queue (produced by action-queue.py's deterministic
    grep/git-log classification pass) as the weekly's investigated-verdicts section. No LLM
    call here — this is a pure render of a queue built by a sibling script. Silent/empty-safe:
    an absent or unreadable queue file renders one placeholder line, never breaks the report."""
    try:
        with open(RAZBORY_QUEUE, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return "\n## Разборы недели\n\nразборы: очередь пуста\n"
    items = data.get("items", [])
    drift = data.get("projects_drift", [])

    L = ["", "## Разборы недели"]
    if not items:
        L.append("\nразборы: очередь пуста")
    else:
        buckets = {"needs_anton": [], "fixed": [], "delete": []}
        # Default `unclassified`, NOT `needs_anton` — an unreadable verdict is missing evidence,
        # never a claim on Anton's attention.
        for it in items:
            buckets.setdefault(it.get("verdict") or "unclassified", []).append(it)

        L.append(f"\n> {len(items)} flagged item(s) investigated: {len(buckets['needs_anton'])} 🔴 need Anton · "
                 f"{len(buckets['fixed'])} ✅ already fixed · {len(buckets['delete'])} 🗑 delete-request")

        # SC-003 (spec 011) — the overdue line, in TWO numbers. One aggregate lets the July tail
        # mask the live signal permanently: 43 of the 69 are legacy and will still be legacy next
        # week, so a single "69 overdue" never moves and stops being read.
        od = [i for i in items if i.get("ot_overdue_days", 0) > 0]
        if od:
            legacy = sum(1 for i in od if i.get("ot_vintage") == "legacy")
            own = len(buckets.get("overdue", []))
            L.append(f"> ⏰ {len(od)} overdue = **{len(od) - legacy} live rot** + {legacy} legacy tail "
                     f"(pre-08-01) · max {max(i['ot_overdue_days'] for i in od)}d · "
                     f"{own} listed below, {len(od) - own} already carry a stronger verdict")
        cen = data.get("ot_census") or {}
        if cen:
            L.append(f"> 📒 ledger: {cen['live']} live · {cen['undated']} with no working date · "
                     f"{cen['resolved_in_file']} resolved rows still in the live file")
        conflicts = sum(1 for i in items if i.get("ot_conflict"))
        if conflicts:
            L.append(f"> ⚠️ {conflicts} ledger row(s) whose `status:` field contradicts the section "
                     f"they sit in — the field wins; the section is stale")

        for key, label in COUNT_ONLY.items():
            n = len(buckets.get(key, []))
            if n:
                L.append(f"> {n} {key.replace('_', ' ')} — {label}")
        for key in ("needs_anton", "overdue", "fixed", "delete"):
            rows = buckets.get(key, [])
            if not rows:
                continue
            # oldest-promise-first for overdue; source order otherwise
            if key == "overdue":
                rows = sorted(rows, key=lambda r: -r.get("ot_overdue_days", 0))
            L.append(f"\n### {VERDICT_ICON[key]} {VERDICT_LABEL[key]} ({len(rows)})")
            for r in rows[:VERDICT_CAP]:
                what = re.sub(r"\s+", " ", r.get("what", "")).strip()[:140]
                why = re.sub(r"\s+", " ", r.get("why", "")).strip()
                L.append(f"- **{what}** — {why}")
            if len(rows) > VERDICT_CAP:
                L.append(f"- … and {len(rows) - VERDICT_CAP} more — full list in "
                         f"`p5-loop/action-queue-classified.json` (verdict `{key}`)")

        # A verdict this renderer has never heard of must SHOW, not vanish. The previous
        # version looped over three hardcoded keys, so adding a fourth verdict upstream would
        # have silently dropped those rows out of the weekly with no trace anywhere.
        for key, rows in buckets.items():
            if key in ("needs_anton", "overdue", "fixed", "delete") or key in COUNT_ONLY or not rows:
                continue
            L.append(f"\n### ⚠ unknown verdict `{key}` ({len(rows)}) — renderer needs updating")
            for r in rows:
                L.append(f"- **{re.sub(r'\\s+', ' ', r.get('what', '')).strip()[:140]}**")

    L.append("\n### 🗺️ Дрейф карты проектов")
    if drift:
        L.extend(f"- {d}" for d in drift)
    else:
        L.append("карта проектов сходится с реальностью — дрейфа нет")

    # Spec drift (audit fix #3) — refresh + render. Fail-open: never break the report.
    L.append("\n### 📋 Дрейф спеков")
    try:
        subprocess.run([sys.executable, f"{HOME}/projects/cc-stack-2026/scripts/p5/spec-drift.py"],
                       capture_output=True, timeout=60)
        with open(f"{HOME}/projects/cc-stack-2026/p5-loop/spec-drift.json", encoding="utf-8") as f:
            sd = json.load(f)
        sf = sd.get("findings", [])
        if sf:
            for x in sf:
                L.append(f"- **{x['project']}** [{x['kind']}] — {x['detail']}")
        else:
            L.append(f"спеки живые — дрейфа нет ({sd.get('scanned', 0)} проектов проверено)")
    except Exception:
        L.append("_(spec-drift: нет данных — `python3 scripts/p5/spec-drift.py`)_")
    return "\n".join(L) + "\n"


def gbrain_cost_md(days):
    """gbrain embedding spend, reconstructed from content_chunks.token_count × model price.
    Answers 'what does the knowledge base actually cost us' without touching the OpenAI key —
    pure local DB read. gbrain's own budget_ledger is empty on 0.22.x, so we reconstruct.
    Embedding is a one-time cost at ingest; query embeds are negligible. Prices per 1M input
    tokens (OpenAI, 2026-06). Silent on any failure — must never break the report."""
    PRICE = {"text-embedding-3-large": 0.13, "text-embedding-3-small": 0.02,
             "text-embedding-ada-002": 0.10}
    try:
        cfg = json.load(open(os.path.expanduser("~/.gbrain/config.json"), encoding="utf-8"))
        db = cfg.get("database_url")
        if not db:
            return ""
    except Exception:
        return ""

    # psql is keg-only on this box (postgresql@17, not on PATH) — resolve it.
    _psql = "/opt/homebrew/opt/postgresql@17/bin/psql"
    if not os.path.exists(_psql):
        _psql = "psql"

    def q(sql):
        r = subprocess.run([_psql, db, "-tAF,", "-c", sql],
                           capture_output=True, text=True, timeout=30)
        return [ln.split(",") for ln in r.stdout.strip().splitlines() if ln]

    try:
        win = q(f"SELECT model, count(*), coalesce(sum(token_count),0) FROM content_chunks "
                f"WHERE embedded_at >= now() - interval '{int(days)} days' GROUP BY model ORDER BY model;")
        allt = q("SELECT count(*), coalesce(sum(token_count),0) FROM content_chunks;")
    except Exception as e:
        return f"\n## gbrain cost\n_(db read failed: {e})_\n"
    if not allt:
        return ""

    def usd(model, toks):
        return int(toks) * PRICE.get(model, 0.13) / 1_000_000.0

    win_cost = sum(usd(m, t) for m, _, t in win)
    win_chunks = sum(int(c) for _, c, _ in win)
    tot_chunks, tot_toks = (int(allt[0][0]), int(allt[0][1])) if allt[0][0] else (0, 0)
    # all-time cost: needs per-model split; approximate via the dominant model query
    by_model = q("SELECT model, coalesce(sum(token_count),0) FROM content_chunks GROUP BY model;")
    tot_cost = sum(usd(m, t) for m, t in by_model)

    L = ["", "## gbrain cost (embedding spend — reconstructed from token_count)",
         f"**Last {days}d: {win_chunks} chunks embedded · ${win_cost:.4f}.** "
         f"All-time corpus: {tot_chunks} chunks / {int(tot_toks/1000)}k tokens · ${tot_cost:.2f} (sunk).",
         "", "| model | tokens (all-time) | $/1M | corpus cost |", "|---|---|---|---|"]
    for m, t in by_model:
        L.append(f"| {m} | {int(int(t)/1000)}k | ${PRICE.get(m, 0.13):.2f} | ${usd(m, int(t)):.2f} |")
    if win:
        L.append("")
        for m, c, t in win:
            L.append(f"- last {days}d · {m}: {c} chunks · ${usd(m, int(t)):.4f}")
    L.append("- _Embedding is one-time at ingest; query embeds are negligible. "
             "Spikes = full re-syncs. Authoritative $: platform.openai.com/usage._")
    return "\n".join(L) + "\n"


def section_model_quota_radar():
    """Model/quota radar (p2-research/model-quota-radar-2026-07-04.md): weekly diff of two
    free, no-auth JSON catalogs (OpenRouter models API, models.dev) for new free models /
    price drops on a small watchlist (claude/glm/gpt/gemini/kimi/deepseek/qwen). Detection
    only — no auto-switching. Runs the poller fresh each report (cheap, ~1s, two GETs),
    then embeds its diff.md. Silent/fail-open: any script failure renders one placeholder
    line, never breaks the report."""
    p = os.path.join(os.path.dirname(__file__), "model-quota-radar.py")
    diff_path = os.path.join(os.path.dirname(__file__), "..", "..",
                             "p5-loop", "model-quota-diff.md")
    try:
        subprocess.run([sys.executable, p], capture_output=True, text=True, timeout=30)
        with open(diff_path, encoding="utf-8") as f:
            body = f.read().strip()
    except Exception as e:
        return f"\n## Model-quota radar\n_(check failed: {e})_\n"
    return "\n## Model-quota radar\n\n" + body + "\n" if body else ""


def build_report(days):
    selected, n_substantive, n_total = select_sessions(days)
    sys.stderr.write(f"[select] {n_total} transcripts in window, {n_substantive} substantive, "
                     f"{len(selected)} selected (cap {MAX_SESSIONS})\n")
    if "--dry" in sys.argv:
        for ht, _, p in selected:
            sys.stderr.write(f"  {ht:3d} turns  {os.path.basename(p)}\n")
        return None

    findings, map_cost, n_batches = map_sessions(selected)
    git, corr, diary = gather_context(days)

    body, red_cost = llm_fallback(REDUCE_PROMPT.format(
        findings=json.dumps(findings, ensure_ascii=False, indent=0)[:120_000],
        git=git or "(none)", corrections=corr or "(none)", diary=diary or "(none)"))
    total_cost = map_cost + red_cost

    now = datetime.datetime.now()
    iso_year, iso_week, _ = now.isocalendar()
    dropped = max(0, n_substantive - len(selected))
    header = (f"# Weekly Report — {iso_year}-W{iso_week:02d}\n\n"
              f"> Generated {now:%Y-%m-%d %H:%M} · window {days}d · "
              f"{len(selected)}/{n_substantive} substantive sessions read"
              f"{f' ({dropped} dropped over cap)' if dropped else ''} · "
              f"{n_batches} map batch(es) · LLM {'+'.join(dict.fromkeys(LLM_LEGS)) or 'none'} · "
              f"Kimi cost ${total_cost:.3f}\n\n")
    report = header + (body.strip() or "_(report generation returned empty — check Kimi auth)_\n")
    report += must_act_md()
    report += rules_hygiene_md()
    report += rule_bench_md()
    report += stack_watch_md()
    report += signals_md()
    report += gbrain_usage_md()
    report += gbrain_monthly_md()
    report += instrument_coverage_md()
    report += project_infra_watch_md()
    report += stack_cron_md()
    report += claudemd_currency_md()
    report += automation_radar_md()
    report += section_news_synthesis(days)
    report += gbrain_cost_md(days)
    report += section_razbory()
    report += section_model_quota_radar()

    os.makedirs(REPORTS_DIR, exist_ok=True)
    out = f"{REPORTS_DIR}/{iso_year}-W{iso_week:02d}.md"
    with open(out, "w", encoding="utf-8") as f:
        f.write(report)
    return out, total_cost


def notify(msg):
    """Contentless 'report ready' ping — no report text leaves the machine, just the filename.
    Two channels, both fail-open: a local macOS banner, and a phone push via ntfy.sh.
    The ntfy topic IS the secret, so it lives OUTSIDE git: env NTFY_TOPIC, else ~/.claude/.ntfy-topic.
    No topic configured → phone push silently skipped. Anton subscribes the ntfy app to that topic."""
    try:
        subprocess.run(["osascript", "-e",
                        f'display notification "{msg}" with title "Weekly Report"'],
                       capture_output=True, timeout=10)
    except Exception:
        pass
    try:
        topic = os.environ.get("NTFY_TOPIC", "").strip()
        tfile = os.path.expanduser("~/.claude/.ntfy-topic")
        if not topic and os.path.exists(tfile):
            topic = open(tfile, encoding="utf-8").read().strip()
        if topic:
            subprocess.run(["curl", "-fsS", "-m", "10",
                            "-H", "Title: Weekly report ready",
                            "-d", msg, f"https://ntfy.sh/{topic}"],
                           capture_output=True, timeout=15)
    except Exception:
        pass


if __name__ == "__main__":
    days = next((int(a) for a in sys.argv[1:] if a.isdigit()), 7)
    res = build_report(days)
    if res is None:
        sys.exit(0)
    out, cost = res
    print(f"Report written: {out}  (Kimi cost ${cost:.3f})")
    # spec 007 US3 (reframed — analytics FOR Claude, not a human chart) — run the drift detector
    # off the same logs (best-effort, $0, never fails the report). It routes any went-SILENT stream
    # into must-act (my fix-one-per-cycle loop); the build.py chart stays on-demand only.
    try:
        _tui = os.path.expanduser("~/projects/cc-stack-2026/p5-loop/trend-ui")
        _py = os.path.join(_tui, ".venv/bin/python")
        if os.path.exists(_py):
            subprocess.run([_py, os.path.join(_tui, "drift.py")],
                           capture_output=True, timeout=60)
    except Exception:
        pass
    notify(f"Open {os.path.basename(out)} locally")
    notify(f"Ready: {os.path.basename(out)} (${cost:.2f})")
