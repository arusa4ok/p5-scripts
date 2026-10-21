#!/usr/bin/env python3
"""Action-Queue Loop — turns measured ⚠️/open items into drafted, approvable actions.

Closes the cc-stack "we measure but don't act" gap (the highest-value loop, per
p2-research/autonomous-loops-soa-2026-06-09.md). The weekly REPORTS; this loop DRAFTS
a concrete next action per flagged/open item, tags it by reversibility zone, and queues
it behind Anton's approval. v1 = draft-only: execution stays manual, behind the plan gate.
No autonomous Stop-hook drain (deferred until the draft-queue proves value for 2-3 weeks).

Sources scanned:  System Overview dashboard (⚠ / [FAIL] / 🔁 flags, latest week block)
                  · evaluation-backlog.md (🔲 todo / 🔬 evaluating rows)
                  · open-threads.md (🔧 In work + ⏳ Waiting on Anton)
                  · latest weekly report's "## Still open" section

Zones:  🟢 read-only / fully reversible (safe to run autonomously later)
        🟡 writes code/config/rules → loop drafts, Anton approves at the plan gate
        🔴 money / outward-facing / irreversible / push → never looped, Anton's call only

Usage:  python3 action-queue.py            (collect → Kimi draft → merge → write → ntfy)
        python3 action-queue.py --dry      (collect + show raw items, no LLM, no write)
"""
import os, re, sys, json, hashlib, datetime, subprocess, glob, importlib.util

HOME = os.path.expanduser("~")
P5 = f"{HOME}/projects/cc-stack-2026/p5-loop"
DASHBOARD = f"{P5}/SYSTEM-OVERVIEW.md"
BACKLOG = f"{P5}/evaluation-backlog.md"
OPEN_THREADS = f"{HOME}/.claude/projects/-Users-rusakovanton/memory/open-threads.md"
REPORTS_DIR = f"{P5}/reports"
OUT = f"{P5}/action-queue.md"
CLASSIFIED_OUT = f"{P5}/action-queue-classified.json"
RAZBORY_LOG = f"{HOME}/.claude/logs/razbory.jsonl"
PROJECTS_MD = f"{HOME}/.claude/rules/projects.md"

# Reuse the weekly report's LLM caller + JSON-blob parser (same conventions; backend is now
# claude-glm — GLM/z.ai — as of 2026-07-03, Kimi lapsed).
_spec = importlib.util.spec_from_file_location("report", os.path.join(os.path.dirname(__file__), "report.py"))
report = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(report)
# llm_fallback, NOT the raw kimi(): kimi() returns whatever the endpoint said, including
# "API Error: … Insufficient balance", and a caller that only tests for emptiness treats that
# sentence as a valid draft. The tell is in this script's own log — the drafting stage cost
# $0.189 / $0.177 / $0.453 / $0.156 and then $0.000 for three straight weeks. Same bug, same
# week, as self-review-weekly.py (fixed 2026-08-07); llm_fallback retries on headless Haiku.
kimi, parse_json_blob = report.llm_fallback, report.parse_json_blob


APPLIED = f"{P5}/action-queue-applied.txt"


def _key(source, text):
    return hashlib.sha1(f"{source}:{text}".encode()).hexdigest()[:8]


def _stable_key(source, text):
    """Identity that survives an EDIT of the source row.

    The plain _key hashes the first 80 chars, so appending `· status: open → in-review` to an
    open-threads bullet minted a brand-new id and retired the old one — the queue counted that
    as one item resolved plus one new item, when nothing had happened. Measured 2026-08-07:
    61 of 112 ids across five weekly snapshots appeared exactly once, while the pile still grew
    28→32→43→51→62, i.e. the churn was mostly re-hashing, not turnover.

    A ledger row's durable part is its `[project]` tag plus its **bold title**; status, dates
    and the running commentary after it are the mutable part. Hash only the durable part."""
    tag = re.match(r"\[([^\]]{1,30})\]", text.strip())
    title = re.search(r"\*\*(.+?)\*\*", text)
    if title:
        stem = f"{tag.group(1) if tag else ''}|{title.group(1)}"
        return hashlib.sha1(f"{source}:{stem}".encode()).hexdigest()[:8]
    return _key(source, text[:80])


def _applied_ids():
    """Ids Anton (or a session) has closed by hand — the queue's only explicit exit door.

    Mirrors must-act-applied.txt, which is the one loop in this stack that actually drains.
    Without it an item leaves only when its source row disappears or a heuristic fires, so a
    handled item comes back every Monday forever.

    Deliberately NOT shared with must-act.py's applied_ids(): that one expires dated `warn:`
    markers after WARN_MARKER_TTL_DAYS so a live signal can speak again, which this queue must
    not do — a closed item stays closed. The two read the same LINE FORMAT, so change both
    together. (ratchet's symbol index flagged the pair on 2026-08-07; kept split on purpose.)"""
    ids = set()
    try:
        for ln in open(APPLIED, encoding="utf-8"):
            tok = ln.strip().split()
            if tok and not ln.startswith("#"):
                ids.add(tok[0])
    except OSError:
        pass
    return ids


# ---------- collection ----------
def from_dashboard():
    """Flagged lines from the latest week block only (file is newest-first)."""
    items = []
    try:
        lines = open(DASHBOARD, encoding="utf-8").read().splitlines()
    except OSError:
        return items
    starts = [i for i, l in enumerate(lines) if l.startswith("## System Overview — Week")]
    if not starts:
        return items
    block = lines[starts[0]: (starts[1] if len(starts) > 1 else len(lines))]
    for l in block:
        s = l.strip()
        if not s:
            continue
        if ("⚠" in s) or ("[FAIL]" in s) or s.startswith("- 🔁"):
            txt = s.lstrip("-* ").strip()
            items.append({"source": "dashboard", "key": _key("dashboard", txt), "raw": txt})
    return items


def from_backlog():
    items = []
    try:
        for l in open(BACKLOG, encoding="utf-8"):
            if not l.startswith("| "):
                continue
            cells = [c.strip() for c in l.strip().strip("|").split("|")]
            if len(cells) < 5 or cells[0] in ("#", "---") or not cells[0].isdigit():
                continue
            status = cells[3]
            if "🔲" in status or "🔬" in status:
                txt = f"backlog #{cells[0]}: {re.sub(r'[*~]', '', cells[1]).strip()} — {cells[2][:160]}"
                items.append({"source": "backlog", "key": _key("backlog", cells[0]), "raw": txt})
    except OSError:
        pass
    return items


# ---------------------------------------------------------------- open-threads, spec 011 (US1)
# Status is a FIELD, not a location. Backlog.md's config calls its `statuses` list "Board columns" —
# the board is a projection of a field. Collecting by section NAME is what made this queue see 71 of
# 228 live rows on 2026-08-10 and read as complete. FR-001, FR-015.
OT_STATUS = ("draft", "open", "in-work", "waiting", "done-unacted", "parked", "resolved")
OT_LIVE = tuple(s for s in OT_STATUS if s != "resolved")

_OT_ROW = re.compile(r"^- \[(?P<project>[^\]]{1,40})\]")          # janitor:21, verbatim
_OT_STATUS = re.compile(r"status: `(?P<v>[\w-]+)`")               # janitor:22, verbatim
_OT_RESURFACE = re.compile(r"next-resurface: `(?P<v>[^`]+)`")     # janitor:23, verbatim
_OT_ID = re.compile(r"\bid: `(?P<v>ot-[0-9a-f]{8})`")             # US2 writes it; US1 only reads

# Section headers whose membership USED to carry the status. After FR-003 they are cosmetic, but
# they stay readable so a row that never gets a field still classifies correctly.
_OT_SECTION_STATUS = (
    ("🔧 In work", "in-work"),
    ("⏳ Waiting on Anton", "waiting"),
    ("✅ Done, not yet acted-on", "done-unacted"),
    ("🔭 Next big threads", "open"),
    ("📌 Parked", "parked"),
)


def _ot_section_status(header):
    for needle, st in _OT_SECTION_STATUS:
        if needle in header:
            return st
    return None


def parse_ot_row(line, section_status=None):
    """One ledger bullet -> a dict. The FIELD always wins; the section is only a fallback.

    Returns None for a non-row. `status_conflict` is set (never silently reconciled) when a row
    carries a field that disagrees with the section it sits in — that disagreement is the migration
    artifact this spec exists to remove, so it must be visible rather than resolved.
    """
    m = _OT_ROW.match(line.strip() if line.strip().startswith("- ") else line)
    if not m:
        return None
    raw = line.strip()[2:].strip()
    sm = _OT_STATUS.search(line)
    field = sm.group("v") if sm else None
    status = field or section_status or "open"
    rm = _OT_RESURFACE.search(line)
    nr = rm.group("v").strip() if rm else ""
    if nr in ("—", "-", ""):
        nr = None
    im = _OT_ID.search(line)
    return {
        "project": m.group("project"),
        "status": status,
        "status_source": "field" if field else ("section" if section_status else "default"),
        "status_conflict": bool(field and section_status and field != section_status),
        "next_resurface": nr,
        "id": im.group("v") if im else None,
        "raw": raw,
    }


def from_open_threads():
    """Collect by FIELD (FR-015). Was: by section name, which saw only two of seven statuses."""
    items = []
    try:
        lines = open(OPEN_THREADS, encoding="utf-8").read().splitlines()
    except OSError:
        return items
    today = datetime.date.today()
    section_status = None
    for l in lines:
        if l.startswith("## "):
            section_status = _ot_section_status(l)
            continue
        if not l.strip().startswith("- ") or "(none)" in l:
            continue
        r = parse_ot_row(l, section_status)
        if r is None or r["status"] not in OT_LIVE:
            continue                                   # resolved rows are not queue work
        overdue_days, vintage = 0, None
        if r["next_resurface"]:
            try:
                due = datetime.date.fromisoformat(r["next_resurface"])
                overdue_days = max(0, (today - due).days)
                # SC-003 wants TWO numbers. "legacy" is a VINTAGE, not a duration: a July-dated
                # promise is at most ~35 days late, so any days-overdue cutoff collapses the split
                # to 1/68 and the legacy tail silently becomes the whole signal. Split on the due
                # date instead — the ledger started carrying dates reliably on 2026-08-07.
                vintage = "legacy" if due < datetime.date(2026, 8, 1) else "current"
            except ValueError:
                pass
        # US2: the ledger now CARRIES the id (spec 011, migrated 2026-08-13). Read it rather
        # than recompute — a written id survives an edit to the bold title, which a recomputed
        # one does not. Recompute only for a row the migration has not reached yet.
        written = _OT_ID.search(l)
        items.append({
            "source": f"open:{r['status']}",
            "key": written.group("v")[3:] if written else _stable_key("open", r["raw"]),
            "ot_id": written.group("v") if written else None,
            "raw": r["raw"],
            "ot_status": r["status"],
            "ot_status_source": r["status_source"],
            "ot_conflict": r["status_conflict"],
            "ot_overdue_days": overdue_days,
            "ot_vintage": vintage,
        })
    return items


def weekly_health():
    """Spec 014 FR-002/FR-003 — the three weekly numbers + the explained-waiting validator.

    live-rot: live rows past their next-resurface (overdue waiting/in-work).
    unexplained-waiting: `waiting` rows with neither `dependencies:` nor `waiting-on:` (FR-002;
    report-only — writes are never blocked). carried ratio is supplied by the caller (render()).
    """
    # spec 019 T024: one spec-start-gate line; fail-open (a missing CLI must not break the digest)
    try:
        spec_gate = subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "spec-gate.py"),
                                    "summary"], capture_output=True, text=True, timeout=10).stdout.strip().replace("\n", "\n> ") or "spec-gate 7d: n/a"
    except (OSError, subprocess.TimeoutExpired):
        spec_gate = "spec-gate 7d: n/a"
    today = datetime.date.today()
    rot = unexplained = waiting_n = 0
    violations = []
    try:
        lines = open(OPEN_THREADS, encoding="utf-8").read().splitlines()
    except OSError:
        return {"live_rot": 0, "unexplained_waiting": 0, "waiting": 0, "violations": [], "spec_gate": spec_gate}
    for line in lines:
        r = parse_ot_row(line)
        if r is None or r["status"] not in OT_LIVE:
            continue
        if r["status"] == "waiting":
            waiting_n += 1
            if "dependencies:" not in line and "waiting-on:" not in line:
                unexplained += 1
                violations.append(r["id"] or r["raw"][:60])
        if r["next_resurface"]:
            try:
                due = datetime.date.fromisoformat(r["next_resurface"][:10])
                if due < today:
                    rot += 1
            except ValueError:
                pass
    return {"live_rot": rot, "unexplained_waiting": unexplained, "waiting": waiting_n,
            "violations": violations, "spec_gate": spec_gate}


def ot_census():
    """Ledger-wide health numbers — the whole file, not just the rows that become queue items.

    Folded in from ~/.claude/scripts/open-threads-janitor.py (FR-017/T011), which was a
    standalone report-only script nothing ever called. Its report() is these six numbers; its
    --archive mode was deliberately NOT carried over — moving the resolved rows out is spec
    011's Q2, an undecided storage-shape question that belongs to Anton, and re-writing fifteen
    lines the day he says yes is cheaper than keeping a mutating path nobody has authorised.
    """
    today = datetime.date.today()
    live = resolved = undated = overdue = legacy = 0
    section = None
    try:
        lines = open(OPEN_THREADS, encoding="utf-8").read().splitlines()
    except OSError:
        return {}
    for line in lines:
        if line.startswith("## "):
            section = _ot_section_status(line)
            continue
        if not line.strip().startswith("- ") or "(none)" in line:
            continue
        r = parse_ot_row(line, section)
        if r is None:
            continue
        if r["status"] == "resolved":
            resolved += 1
            continue
        live += 1
        if not r["next_resurface"]:
            undated += 1
            continue
        try:
            due = datetime.date.fromisoformat(r["next_resurface"])
        except ValueError:
            undated += 1
            continue
        if due < today:
            overdue += 1
            if due < datetime.date(2026, 8, 1):
                legacy += 1
    return {"live": live, "resolved_in_file": resolved, "undated": undated,
            "overdue": overdue, "overdue_legacy": legacy, "overdue_live_rot": overdue - legacy}


def from_weekly_open():
    items = []
    reps = sorted(glob.glob(f"{REPORTS_DIR}/[0-9]*-W[0-9]*.md"))  # weekly reports only, not rules-hygiene-*.md
    if not reps:
        return items
    try:
        lines = open(reps[-1], encoding="utf-8").read().splitlines()
    except OSError:
        return items
    grab = False
    for l in lines:
        if l.startswith("## "):
            grab = l.strip().lower().startswith("## still open")
            continue
        s = l.strip()
        if grab and (re.match(r"^\d+\.\s", s) or s.startswith("- ")):
            txt = re.sub(r"^\d+\.\s*|^-\s*", "", s).strip()
            if txt:
                items.append({"source": "weekly", "key": _key("weekly", txt[:80]), "raw": txt})
    return items


def from_insights_schedule():
    """Inject a high-priority /insights reminder during the first 7 days of each month."""
    today = datetime.date.today()
    if today.day <= 7:
        txt = (f"/insights due this month — run monthly friction-mining pass in Claude Code "
               f"(1-{today.strftime('%B %Y')}). Open Claude Code, type /insights.")
        return [{"source": "scheduled", "key": "insights-monthly-due", "raw": txt}]
    return []


def collect():
    seen, items = _applied_ids(), []
    for fn in (from_insights_schedule, from_dashboard, from_backlog, from_open_threads, from_weekly_open):
        for it in fn():
            if it["key"] in seen:
                continue
            seen.add(it["key"]); items.append(it)
    return items


# ---------- разборы: deterministic classification pass ----------
# Requirement (Anton, 2026-07-04): every flagged item must arrive at the report ALREADY
# investigated — 🔴 needs-Anton / ✅ already-fixed-autonomously (cite evidence) / 🗑 delete-request
# (cite reason). Deterministic-first (grep/stat/git log) — no new LLM calls per item; genuinely
# ambiguous items are bucketed 🔴 needs-Anton with needs_investigation=True so the session-level
# разбор (a human/agent turn, not a script LLM call) can resolve them. Cost discipline per audit.
STOPWORDS = {
    "this", "that", "with", "from", "have", "been", "into", "over", "under", "about",
    "still", "when", "what", "which", "there", "their", "would", "could", "should",
    "action", "queue", "backlog", "dashboard", "weekly", "report", "system", "overview",
}


_PATH_EXT = r"(?:py|md|json|jsonl|plist|sh|yaml|yml|txt)"


def _candidate_paths(raw):
    """Extract STRICT path-looking tokens only — must contain '/' or end in a known extension
    with no embedded spaces. Excludes prose backtick snippets like `gbrain skillopt <name>`
    (a CLI invocation, not a path) that produced false 'delete' verdicts in testing."""
    toks = set()
    for t in re.findall(r"`([^`]+)`", raw):
        if " " in t:
            continue
        if re.search(rf"\.{_PATH_EXT}$", t):
            toks.add(t)
    toks |= set(re.findall(rf"(?:~|\.{{1,2}})?/[\w\-./]+\.{_PATH_EXT}\b", raw))
    toks |= set(re.findall(rf"\b[\w\-]+\.{_PATH_EXT}\b", raw))
    return [t for t in toks if len(t) > 3]


_FILE_INDEX_CACHE = None
# Set False when any root's walk failed; `delete` then refuses to fire for the whole run.
_FILE_INDEX_TRUSTED = True
# Measured 2026-08-17 on an idle machine: .claude 2s · projects 13s (474k files) · connectpro 1s.
# The old 25s was under three seconds of margin on the biggest root, and this script runs at
# 10:35 on a Monday alongside five other launchd jobs — the one moment of the week the machine
# is least able to spare it. 120s costs nothing on a good day and removes the coin flip.
FIND_TIMEOUT_S = 120


def _file_index():
    """One-time cached basename->path(s) index. Collected items span EVERY project (career-os,
    identity-os, spidersilk-pmm, connectpro, ...), not just cc-stack — an early version that
    only indexed ~/.claude + cc-stack-2026 wrongly flagged every cross-project file reference
    as 'delete' (path not found in the wrong roots). ~/projects + connectpro covers the real
    set; ~0.9s on this machine (357k files), cached once per run."""
    global _FILE_INDEX_CACHE, _FILE_INDEX_TRUSTED
    if _FILE_INDEX_CACHE is not None:
        return _FILE_INDEX_CACHE
    idx = {}
    trusted = True
    for root in (f"{HOME}/.claude", f"{HOME}/projects", f"{HOME}/connectpro"):
        if not os.path.isdir(root):
            continue
        try:
            r = subprocess.run(
                ["find", root, "(", "-name", "node_modules", "-o", "-name", ".git",
                 "-o", "-name", "__pycache__", "-o", "-name", ".venv", ")", "-prune",
                 "-o", "-type", "f", "-print"],
                capture_output=True, text=True, timeout=FIND_TIMEOUT_S)
            for p in r.stdout.splitlines():
                idx.setdefault(os.path.basename(p), []).append(p)
        except Exception as e:
            # A HALF-BUILT INDEX MUST NOT ANSWER. This except used to `continue`, which left a
            # partial (or empty) index in place, and an empty index answers every lookup with
            # "not on disk" — the exact input the `delete` verdict is built on. Measured
            # 2026-08-17 on the W34 run: 41 of the 60 basenames the weekly reported as
            # "referenced path(s) not found on disk" resolved on the first try when the same
            # resolver was re-run by hand. Nothing had been restored in between; the index had
            # simply not been there. The failure is silent AND inverted — the safe-looking
            # `continue` turns "I could not look" into "it is gone", and 48 live threads were
            # nominated for deletion on the strength of it.
            print(f"[action-queue] file index INCOMPLETE ({root}: {type(e).__name__}) — "
                  f"path heuristic disabled for this run, no `delete` verdicts will be issued",
                  file=sys.stderr)
            trusted = False
    _FILE_INDEX_CACHE = idx
    _FILE_INDEX_TRUSTED = trusted
    return idx


def _resolve_path(tok):
    """Expand a candidate path token: literal path first, then a basename lookup in the
    cached file index (catches files that live in a subdirectory the raw text didn't name)."""
    tok = tok.strip().rstrip(".,;:)")
    cands = [os.path.expanduser(tok)]
    if not tok.startswith(("~", "/")):
        cands += [os.path.join(P5, tok), os.path.join(os.path.dirname(P5), tok),
                  os.path.join(f"{HOME}/.claude", tok)]
    for c in cands:
        if os.path.exists(c):
            return c
    hits = _file_index().get(os.path.basename(tok))
    return hits[0] if hits else None


def _keywords(raw, n=3):
    """Up to n distinct keywords, deduped by STEM rather than by exact string.

    Stem-dedupe matters because the match below is substring-based: "month" and "monthly"
    were both kept, and one occurrence of "monthly" in a commit message then scored TWO hits
    and auto-resolved the item. That is how the live `/insights due this month` reminder got
    marked "already fixed" by the unrelated commit "T8 capability-routing-audit is HEALTHY
    (monthly cadence, not dead)" — 2026-08-07."""
    words = re.findall(r"[A-Za-z][A-Za-z0-9_\-]{4,}", raw)
    seen, out = set(), []
    for w in words:
        lw = w.lower()
        if lw in STOPWORDS or any(lw.startswith(s) or s.startswith(lw) for s in seen):
            continue
        seen.add(lw)
        out.append(w)
        if len(out) >= n:
            break
    return out


# A source row that declares its own closure is not a queue item. The ledgers already carry
# this state; re-asking Anton about it is the queue disagreeing with its own source.
# ✅ matches ANYWHERE in the row, not just at the start: the marker is usually written after
# the project tag, as in `[career-os] ✅ "US on top" feed ranking — FIXED 2026-07-13`. Anchoring
# it to ^ left rows that literally say FIXED sitting in the red bucket.
_SELF_RESOLVED = re.compile(
    r"status:\s*`?\s*(resolved|closed|done|shipped|landed)\b|\bвердикт:\s*закрыт", re.I)

# ✅ used to be an alternative inside _SELF_RESOLVED, i.e. a bare check mark ANYWHERE in a row
# closed it. Hand-audit of the W34 `fixed` bucket (2026-08-17, SC-006) found 4 of 19 wrong, and
# all four were the same shape: a ✅ ticking a SUB-item deep in the body of a row that is not
# closed — "7 of 12 are already done", "Rung 2 gate = 10/30", "Step 1 done, Step 2 blocked" —
# plus `categories: brain ⚠️50 · skill ✅90 · ops ✅100`, a dashboard legend line that is not a
# thread at all and was being reported to Anton as a closed one.
#
# POSITION is what separates them, and the old comment here had it exactly backwards. It said
# "✅ matches ANYWHERE in the row, not just at the start: the marker is usually written after
# the project tag" — the observation was right and the conclusion too wide. The marker is
# written after the `[project]` tag and the `id:` prefix, i.e. at the HEAD of the title, and
# nowhere else. Every one of the 15 correct hits has it there; every false positive has it
# buried in the body behind a different leading glyph (🗂 🤖 🪜 💸). So strip the prefix and
# require the tick at the head of what remains.
#
# Deliberately NOT gated on the `status:` field, though that was the first attempt: tested on
# the same 19 rows it demoted 13, including `✅ spec-012 CTO-agent DONE` and `✅ 13MB asset-bloat
# почищен`, which are closed by their own words while their status field was never updated. On
# THESE rows the prose is fresher than the field — the opposite of the US1 case one layer up,
# where the field beat a stale kanban section. Same file, two directions; position decides.
_ROW_PREFIX = re.compile(r"^\s*[-*]?\s*\*{0,2}\[[^\]]{1,40}\]\s*(?:id:\s*`?[\w-]+`?\s*[·:-]?\s*)?")
_HEAD_TICK = re.compile(r"^\s*\*{0,2}\s*✅")


_GIT_LOG_CACHE = None


def _recent_git_log(days=30):
    """Combined `git log --oneline` across the same repos the weekly report scans. Cached
    once per process — this is the ONE grep/git call the classification pass needs, run once
    for the whole batch, not per item."""
    global _GIT_LOG_CACHE
    if _GIT_LOG_CACHE is not None:
        return _GIT_LOG_CACHE
    lines = []
    for d in getattr(report, "GIT_REPOS", []):
        try:
            r = subprocess.run(["git", "-C", d, "log", f"--since={days} days ago", "--oneline"],
                                capture_output=True, text=True, timeout=15)
            lines += [ln for ln in r.stdout.splitlines() if ln.strip()]
        except Exception:
            continue
    _GIT_LOG_CACHE = lines
    return lines


def classify_item(it):
    """Assign a verdict, and NEVER hand Anton an item just because we could not classify it.

    Until 2026-08-07 the fall-through was `needs_anton`, so "the two heuristics found nothing"
    rendered in the weekly as a red row addressed to him. 62 of 62 red rows carried the same
    non-reason, which is not a task list — so it was never worked through, so it only grew.
    `needs_anton` now requires a POSITIVE signal; everything else is `unclassified`."""
    raw = it["raw"]
    if _SELF_RESOLVED.search(raw):
        return {"verdict": "fixed", "why": "the source row declares itself resolved/closed"}
    if _HEAD_TICK.search(_ROW_PREFIX.sub("", raw)):
        return {"verdict": "fixed", "why": "the source row is ticked ✅ at the head of its title"}

    paths = _candidate_paths(raw)
    if paths:
        resolved = [(_resolve_path(p) is not None) for p in paths]
        # _resolve_path populates the index, so ask about trust only after the first lookup.
        if resolved and not any(resolved) and _FILE_INDEX_TRUSTED:
            missing = ", ".join(paths[:3])
            return {"verdict": "delete",
                    "why": f"referenced path(s) not found on disk: {missing}"}

    kws = _keywords(raw)
    if len(kws) >= 3:
        log = _recent_git_log()
        for line in log:
            low = line.lower()
            # ALL keywords, on word boundaries. Was: any 2 as bare substrings, which matched
            # almost any prose commit and auto-resolved live items.
            if all(re.search(rf"\b{re.escape(k.lower())}", low) for k in kws):
                return {"verdict": "fixed", "why": f"matches recent commit: {line.strip()[:140]}"}

    # The source itself says a human is blocking → that IS the evidence.
    if it["source"] == "open:waiting":
        return {"verdict": "needs_anton",
                "why": "open-threads.md files it under ⏳ Waiting on Anton",
                "needs_investigation": True}

    # OVERDUE (FR-016, spec 011). This verdict did not exist, which is why the one number the
    # ledger uniquely holds — a thread that promised to resurface and did not — was the one number
    # the weekly never printed. It sits ABOVE tracked_elsewhere on purpose: "owned by that ledger"
    # is true and useless once the ledger itself has stopped moving the row.
    if it.get("ot_overdue_days", 0) > 0:
        return {"verdict": "overdue",
                "why": f"promised to resurface {it['ot_overdue_days']}d ago and did not "
                       f"(status: {it.get('ot_status')})",
                "needs_investigation": False}

    # 🔧 In work rows are the durable ledger. They already surface at session start and are
    # owned there; re-listing 48 of them here was 77% of the pile and duplicated their home.
    if it["source"].startswith("open:"):
        return {"verdict": "tracked_elsewhere",
                "why": "live row in open-threads.md — owned by that ledger, not by this queue"}

    return {"verdict": "unclassified",
            "why": "no deterministic signal (no missing path, no matching commit) — "
                   "not evidence that Anton is needed"}


def classify_all(items):
    out = []
    for it in items:
        c = classify_item(it)
        rec = {"key": it["key"], "source": it["source"], "what": it["raw"][:200],
               "verdict": c["verdict"], "why": c["why"],
               "needs_investigation": c.get("needs_investigation", False)}
        # spec 011: carry the ledger facts through so the weekly can split overdue into
        # live-rot vs legacy tail (SC-003) without re-parsing the file.
        for k in ("ot_status", "ot_status_source", "ot_conflict", "ot_overdue_days", "ot_vintage"):
            if k in it:
                rec[k] = it[k]
        out.append(rec)
    return out


def write_classified(records, drift=None):
    os.makedirs(P5, exist_ok=True)
    with open(CLASSIFIED_OUT, "w", encoding="utf-8") as f:
        json.dump({"generated": datetime.datetime.now().isoformat(), "items": records,
                   "ot_census": ot_census(),
                   "projects_drift": drift or []}, f, ensure_ascii=False, indent=2)


def log_razbory(records):
    try:
        os.makedirs(os.path.dirname(RAZBORY_LOG), exist_ok=True)
        rec = {"ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
               "items": len(records),
               "fixed": sum(1 for r in records if r["verdict"] == "fixed"),
               "needs_anton": sum(1 for r in records if r["verdict"] == "needs_anton"),
               "delete_requests": sum(1 for r in records if r["verdict"] == "delete")}
        with open(RAZBORY_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


# ---------- projects.md drift check ----------
# Requirement (Anton, "4 ok" 2026-07-04): the project map (~/.claude/rules/projects.md) drifts
# from reality — a project marked "Active" goes cold, or a "Maintenance" one gets busy again —
# and nobody notices until it matters. Report-only: never auto-edits projects.md (Anton's file).
_ACTIVE_RE = re.compile(r"\bactive\b|in flight", re.I)
_INACTIVE_RE = re.compile(r"maintenance|ending|deployed", re.I)
BUSY_THRESHOLD = 5  # commits/30d that make a "maintenance"-labeled project look active again


def _parse_projects_md():
    """Rows of the Project|Path|Status|What table ONLY. projects.md also has a second table
    ('Automation Levels' = Project|Level|Next-upgrade) with NO Path column — parsing its rows
    blindly read the Level cell ('L5'/'L2') as a path and reported bogus 'path does not exist'
    drift. Gate on the current table's header actually having a 'Path' column."""
    rows = []
    try:
        lines = open(PROJECTS_MD, encoding="utf-8").read().splitlines()
    except OSError:
        return rows
    in_path_table = False
    for l in lines:
        s = l.strip()
        if not s.startswith("|"):
            in_path_table = False  # left the table block
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if s.startswith("| Project "):  # a table header — does THIS table have a Path column?
            in_path_table = "Path" in cells
            continue
        if s.startswith("|---"):
            continue
        if not in_path_table or len(cells) < 3:
            continue
        rows.append({"name": cells[0], "path": cells[1], "status": cells[2]})
    return rows


def _git_commits_30d(path):
    """Commit count in the last 30d, scoped to `path` (works whether path IS a repo root or a
    subdirectory of one — pathspec '.' is resolved relative to -C's cwd). None = not a git repo."""
    try:
        chk = subprocess.run(["git", "-C", path, "rev-parse", "--is-inside-work-tree"],
                              capture_output=True, text=True, timeout=10)
        if chk.returncode != 0 or chk.stdout.strip() != "true":
            return None
        r = subprocess.run(["git", "-C", path, "log", "--since=30 days ago", "--oneline", "--", "."],
                            capture_output=True, text=True, timeout=15)
        return len([ln for ln in r.stdout.splitlines() if ln.strip()])
    except Exception:
        return None


def drift_check_projects():
    """Compares each projects.md row's Status column against reality: does the path exist, is it
    a git repo, and does 30d commit activity match what the Status implies? Returns a list of
    plain-language finding strings (empty = map agrees with reality)."""
    findings = []
    for row in _parse_projects_md():
        name, path_raw, status = row["name"], row["path"], row["status"]
        path = os.path.expanduser(path_raw)
        if not os.path.exists(path):
            findings.append(f"**{name}** — path `{path_raw}` listed in projects.md does not exist on disk")
            continue
        is_active = bool(_ACTIVE_RE.search(status))
        is_inactive = bool(_INACTIVE_RE.search(status))
        commits = _git_commits_30d(path)
        if commits is None:
            if is_active:
                findings.append(f"**{name}** — status says \"{status}\" but `{path_raw}` is not a git repo — can't verify activity")
            continue
        if is_active and commits == 0:
            findings.append(f"**{name}** — status says \"{status}\" but 0 commits in the last 30d at `{path_raw}`")
        elif is_inactive and commits >= BUSY_THRESHOLD:
            findings.append(f"**{name}** — status says \"{status}\" but {commits} commits in the last 30d at "
                             f"`{path_raw}` — busier than the map claims")
    return findings


def log_projects_drift(findings):
    try:
        os.makedirs(os.path.dirname(RAZBORY_LOG), exist_ok=True)
        rec = {"ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
               "kind": "projects-drift", "findings": len(findings)}
        with open(RAZBORY_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


# ---------- Kimi drafting ----------
DRAFT_PROMPT = """You draft an ACTION QUEUE for Anton — a non-developer who maintains his Claude Code stack ("cc-stack") and directs all work in natural language. Below are flagged or open items pulled from his stack dashboards. For EACH item, draft ONE concrete next action.

Output a STRICT JSON array, one object per input item, same order, schema:
[{{"key":"<echo the input key verbatim>","what":"<the problem in 1 plain sentence, no jargon>","zone":"green|yellow|red","effort":"5min|30min|hours","action":"<the single concrete next step to take>"}}]

ZONE rules (by reversibility — this is the safety classification):
- "green"  = read-only or fully reversible work (investigate, measure, report, draft). Safe to automate later.
- "yellow" = writes code / config / rules. Needs Anton's approval before doing.
- "red"    = spends money / publishes outward / irreversible / pushes to a remote. Anton decides alone — never automate.

Rules: be concrete and terse. If an item is a non-actionable nit (e.g. a cosmetic rule-length WARN), still include it but mark effort "5min" and say so. DO NOT invent items not in the input. Echo each key EXACTLY. Output ONLY the JSON array, no prose, no fences.

=== ITEMS ===
{items}
"""


def draft(items):
    payload = "\n".join(f'- key={it["key"]} | {it["raw"]}' for it in items)
    text, cost = kimi(DRAFT_PROMPT.format(items=payload))
    # Say it out loud when the drafting layer produced nothing. Silently returning {} here is
    # indistinguishable from "the model had no suggestions", which is how three weeks of
    # $0.000 runs passed for ordinary ones.
    if not text.strip() or text.strip().startswith("API Error"):
        why = "empty" if not text.strip() else text.strip().splitlines()[0][:160]
        sys.stderr.write(f"[action-queue] drafting layer produced NOTHING — rows keep their "
                         f"deterministic verdict only: {why}\n")
        return {}, cost
    parsed = parse_json_blob(text)
    drafts = {d["key"]: d for d in parsed if isinstance(d, dict) and d.get("key")} if isinstance(parsed, list) else {}
    return drafts, cost


# ---------- merge + render ----------
ZONE_ICON = {"green": "🟢", "yellow": "🟡", "red": "🔴"}
ZONE_ORDER = {"red": 0, "yellow": 1, "green": 2}  # surface red (your-call) + yellow (approvable) first


def prior_status():
    """Map id -> status from a previous queue, so carried-over items are marked 🔁."""
    seen = {}
    try:
        for m in re.finditer(r"<!-- id:(\w+) status:(\S+)", open(OUT, encoding="utf-8").read()):
            seen[m.group(1)] = m.group(2)
    except OSError:
        pass
    return seen


def render(items, drafts, cost):
    prev = prior_status()
    rows = []
    for it in items:
        d = drafts.get(it["key"], {})
        zone = d.get("zone", "yellow")
        status = "🔁" if it["key"] in prev else "🆕"
        rows.append({"id": it["key"], "status": status, "zone": zone, "src": it["source"],
                     "what": d.get("what", it["raw"][:120]), "effort": d.get("effort", "?"),
                     "action": d.get("action", "(no draft — Kimi returned nothing)")})
    rows.sort(key=lambda r: (ZONE_ORDER.get(r["zone"], 1), r["status"] != "🔁"))
    new_n = sum(1 for r in rows if r["status"] == "🆕")
    carried = len(rows) - new_n

    now = datetime.datetime.now()
    L = [f"# Action Queue — {now:%Y-%m-%d %H:%M}",
         "",
         f"> {len(rows)} items ({new_n} 🆕 new · {carried} 🔁 carried over · "
         f"{carried * 100 // max(len(rows), 1)}% carried) · Kimi ${cost:.3f}",
         "> weekly health: live-rot {live_rot} · unexplained-waiting {unexplained_waiting}/{waiting}\n> {spec_gate}".format(**weekly_health()),
         "> Reply with the ids you want done. I execute 🟡 behind the plan gate; 🔴 is your call only; "
         "🟢 is safe to run on request.",
         ""]
    cur = None
    for r in rows:
        if r["zone"] != cur:
            cur = r["zone"]
            L.append(f"\n## {ZONE_ICON.get(cur,'🟡')} {cur} zone\n")
        L.append(f"- **`{r['id']}`** {r['what']} · _{r['effort']}_ · src:{r['src']} {r['status']}")
        L.append(f"  → {r['action']}")
        L.append(f"  <!-- id:{r['id']} status:{r['status']} zone:{r['zone']} -->")
    # spec 018 (2026-10-20): the weekly Threads digest comes from the open-threads CLI. Fail-open.
    try:
        _dg = subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "thread.py"), "digest"],
                             capture_output=True, text=True, timeout=120).stdout.strip()
        L += ["", _dg or "## 🧵 Threads — digest returned nothing"]
    except Exception as _e:  # noqa: BLE001 — a broken digest must never break the queue
        L += ["", f"## 🧵 Threads — digest unavailable ({_e})"]
    return "\n".join(L) + "\n", len(rows), new_n


def notify(msg):
    try:
        subprocess.run(["osascript", "-e", f'display notification "{msg}" with title "Action Queue"'],
                       capture_output=True, timeout=10)
    except Exception:
        pass
    try:
        topic = os.environ.get("NTFY_TOPIC", "").strip()
        tfile = os.path.expanduser("~/.claude/.ntfy-topic")
        if not topic and os.path.exists(tfile):
            topic = open(tfile, encoding="utf-8").read().strip()
        if topic:
            subprocess.run(["curl", "-fsS", "-m", "10", "-H", "Title: Action queue ready",
                            "-d", msg, f"https://ntfy.sh/{topic}"], capture_output=True, timeout=15)
    except Exception:
        pass


if __name__ == "__main__":
    items = collect()
    drift = drift_check_projects()
    if "--dry" in sys.argv:
        for it in items:
            print(f"[{it['source']:>14}] {it['key']}  {it['raw'][:110]}")
        print(f"\n{len(items)} items collected (no LLM, no write)")
        h = weekly_health()
        print(f"\nweekly health: live-rot {h['live_rot']} · unexplained-waiting "
              f"{h['unexplained_waiting']}/{h['waiting']} · carried {len(items)} collected this run")
        print(h["spec_gate"])
        for v in h["violations"][:10]:
            print(f"  ⚠ unexplained waiting: {v}")
        print(f"\nledger census: {ot_census()}")
        print(f"\nprojects-drift: {len(drift)} finding(s)")
        for d in drift:
            print(f"  - {d}")
        sys.exit(0)
    if not items:
        print("No open/flagged items — queue empty, nothing to draft.")
        write_classified([], drift)
        log_razbory([])
        log_projects_drift(drift)
        sys.exit(0)

    classified = classify_all(items)
    write_classified(classified, drift)
    log_razbory(classified)
    log_projects_drift(drift)
    needs_anton_keys = {r["key"] for r in classified if r["verdict"] == "needs_anton"}
    resolved_n = len(classified) - len(needs_anton_keys)
    print(f"разборы: {len(classified)} classified — {resolved_n} auto-resolved "
          f"(fixed/delete), {len(needs_anton_keys)} need Anton -> {CLASSIFIED_OUT}")

    action_items = [it for it in items if it["key"] in needs_anton_keys]
    if not action_items:
        print("All flagged items auto-resolved this run — no action drafts needed.")
        sys.exit(0)
    drafts, cost = draft(action_items)
    body, total, new_n = render(action_items, drafts, cost)
    os.makedirs(P5, exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(body)
    print(f"Action queue written: {OUT}  ({total} items, {new_n} new, Kimi ${cost:.3f})")
    notify(f"{total} actions queued ({new_n} new) — open action-queue.md")
