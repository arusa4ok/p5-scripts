#!/usr/bin/env python3
"""thread: the open-threads ledger CLI (cc-stack spec 018, 2026-10-20).

One global tagged ledger (memory/open-threads.md, section "## Open (current)"), rows read by
action-queue.parse_ot_row. This tool is the ONLY intended writer; it prints the row it wrote so a
follow-up read is never needed. Sub-commands:

  ready [--project P|--all]      due + in-work rows, then an index          (what to act on now)
  inject --project P             the SessionStart block (<= 4,000 chars) + a JSONL log line
  list [--status S] [--project P|--all]   one line per row
  show <id>                      the full row
  add --project T --title X [--status S] [--waiting-on W] [--resurface D] [--next N] [--decision DEC] [prose]
  update <id> [--status] [--next] [--resurface] [--waiting-on] [--decision] [--note TEXT]
  defer <id> --until D [--why TEXT]
  close <id> --outcome TEXT [--decision DEC]   -> resolved, moved to open-threads-archive-YYYY-MM.md
  stale [--days 14]              live rows not touched for N days
  digest                         the weekly "Threads" section (markdown)
  audit [--days 30]              ledger-touching tool calls in session transcripts + inject stats
  mirror                         due + in-work rows -> ~/.claude/tasks/open-threads/N.json
  migrate-legacy [--apply]       one-shot: non-Open sections -> archive-legacy; Open rows normalised

Every write takes a lock, writes atomically and keeps open-threads.md.bak (the memory dir is not in git).
"""
import argparse
import contextlib
import datetime as dt
import fcntl
import glob
import json
import os
import re
import shutil
import sys

HOME = os.path.expanduser("~")
MEM = f"{HOME}/.claude/projects/-Users-rusakovanton/memory"
LEDGER = f"{MEM}/open-threads.md"
LEGACY = f"{MEM}/open-threads-archive-legacy.md"
LOG = f"{HOME}/.claude/logs/open-threads-inject.jsonl"
TASKS = f"{HOME}/.claude/tasks/open-threads"
OPEN_H = "## Open (current)"
STATUS = ("draft", "open", "in-work", "waiting", "done-unacted", "parked", "resolved")
LIVE = tuple(s for s in STATUS if s != "resolved")
SYN = {"active": "in-work", "in_progress": "in-work", "in-progress": "in-work", "wip": "in-work",
       "closed": "resolved", "done": "resolved", "blocked": "waiting",
       "waiting-anton": "waiting", "waiting-on-anton": "waiting"}
CROSS = {"global", "infra", "stack", "commons", "gbrain"}
TODAY = dt.date.today()
ROW_CAP, BLOCK_CAP, INDEX_TITLE, INDEX_MAX = 240, 4000, 50, 15
BASELINE = "baseline 2026-10-20 (30d): 953 ledger-touching calls (census) / 675 (this audit's method), ~2,380 tokens per inject"

_ROW = re.compile(r"^- \[(?P<tag>[^\]]{1,40})\]")
_FIELD_BT = {k: re.compile(rf"\b{k}: `(?P<v>[^`]*)`") for k in ("id", "status", "next-resurface", "updated", "since")}
_FIELD_TX = {k: re.compile(rf"\b{k}: (?P<v>.*?)(?= · |$)") for k in ("waiting-on", "next", "decision", "parent")}
_TITLE = re.compile(r"\*\*(?P<v>[^*]+)\*\*")
_DATE = re.compile(r"\b(20\d\d-\d\d-\d\d)\b")
_SUB = r"(?:ready|inject|list|show|add|update|defer|close|stale|digest|audit|mirror|migrate-legacy)\b"
CLI_RX = re.compile(r"(?:^|[;&|(]\s*|\$\(\s*|/)thread(?:\.py)?\s+" + _SUB, re.M)  # a CLI call anywhere in a command
_CLI_PRE = re.compile(r"thread(?:\.py)?\s+" + _SUB)


# ----------------------------------------------------------------------------- file + rows
def read_lines(path=None):
    with open(path or LEDGER, encoding="utf-8") as f:
        return f.read().splitlines()


def write_lines(lines, path=None):
    """Atomic: .bak of the old file, write a temp file, rename it over the ledger (a concurrent reader never sees a half file)."""
    path = path or LEDGER
    if os.path.exists(path):
        shutil.copy2(path, path + ".bak")
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        f.write("\n".join(lines).rstrip("\n") + "\n")
    os.replace(path + ".tmp", path)


@contextlib.contextmanager
def locked():
    """One writer at a time: two sessions mutating the ledger in the same second must not lose a row."""
    with open(LEDGER + ".lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)


def clean(v):
    """Field values must not carry the row delimiter ' · ' or a newline, or the parser truncates them."""
    return None if v is None else re.sub(r"\s*·\s*", " / ", str(v)).replace("\n", " ").strip()


def open_span(lines):
    """(start, end) line indexes of the rows under '## Open (current)'; end is exclusive."""
    try:
        h = next(i for i, l in enumerate(lines) if l.startswith(OPEN_H))
    except StopIteration:
        sys.exit(f"thread: no '{OPEN_H}' section in {LEDGER}")
    end = next((i for i in range(h + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    return h + 1, end


def parse(line):
    m = _ROW.match(line)
    if not m:
        return None
    r = {"tag": m.group("tag"), "raw": line}
    for k, rx in _FIELD_BT.items():
        mm = rx.search(line)
        r[k] = mm.group("v").strip() if mm else None
    for k, rx in _FIELD_TX.items():
        mm = rx.search(line)
        r[k] = mm.group("v").strip() if mm else None
    t = _TITLE.search(line)
    if t:
        title = t.group("v")
    else:  # no bold title: the first segment that is not a `key: value` field
        segs = [x for x in line[m.end():].split(" · ") if x.strip() and not re.match(r"\s*[\w-]+: ", x)]
        title = segs[0] if segs else line[m.end():]
    r["title"] = re.sub(r"\s+", " ", title).strip()[:120]
    st = (r["status"] or "open").lower()
    r["status"] = SYN.get(st, st)
    r["due"] = None
    if r["next-resurface"] and _DATE.match(r["next-resurface"]):
        try:
            r["due"] = dt.date.fromisoformat(r["next-resurface"][:10])
        except ValueError:
            pass
    r["touched"] = None
    if r["updated"]:
        try:
            r["touched"] = dt.date.fromisoformat(r["updated"][:10])
        except ValueError:
            pass
    r["since_d"] = r["touched"]  # date the current status began; falls back to the last write
    if r["since"]:
        try:
            r["since_d"] = dt.date.fromisoformat(r["since"][:10])
        except ValueError:
            pass
    return r


def rows(lines=None):
    lines = lines if lines is not None else read_lines()
    a, b = open_span(lines)
    out = []
    for i in range(a, b):
        r = parse(lines[i])
        if r:
            r["line_no"] = i
            out.append(r)
    return out, lines


def find(rid, lines=None):
    rs, lines = rows(lines)
    hit = [r for r in rs if r["id"] == rid]
    if not hit:
        sys.exit(f"thread: no row with id `{rid}` in {OPEN_H} (try: thread list --all)")
    if len(hit) > 1:
        sys.exit(f"thread: id `{rid}` appears {len(hit)} times — rename one first (thread list --all | grep {rid})")
    return hit[0], lines


def set_field(line, key, value, backtick):
    """Set or insert ` · key: value` on a raw row. value None removes the field."""
    value = clean(value)
    rx = _FIELD_BT.get(key) or _FIELD_TX.get(key) or re.compile(rf"\b{key}: (?P<v>.*?)(?= · |$)")
    rendered = f"{key}: `{value}`" if backtick else f"{key}: {value}"
    if rx.search(line):
        if value is None:
            return re.sub(rf" · {key}: (`[^`]*`|.*?)(?= · |$)", "", line, count=1)
        return rx.sub(lambda m: rendered, line, count=1)
    if value is None:
        return line
    if key == "id":
        pos = _ROW.match(line).end()
        return line[:pos] + " " + rendered + " ·" + line[pos:]
    # insert after status (or after the title, or after the tag)
    anchor = _FIELD_BT["status"].search(line) or _TITLE.search(line) or _ROW.match(line)
    pos = anchor.end()
    return line[:pos] + " · " + rendered + line[pos:]


def norm_status(s):
    s = (s or "open").lower()
    s2 = SYN.get(s, s)
    if s2 not in STATUS:
        sys.exit(f"thread: status `{s}` is not in the vocabulary {STATUS}")
    return s2, (s2 != s)


def iso(d):
    try:
        return dt.date.fromisoformat(d).isoformat()
    except (ValueError, TypeError):
        sys.exit(f"thread: date `{d}` must be YYYY-MM-DD")


def scoped(rs, project):
    p = (project or "").strip()
    if not p or p.startswith("-private-tmp") or p == "tmp":
        return rs, False
    proper = [r for r in rs if r["tag"] not in CROSS and r["tag"].split("/")[0] in p]
    if not proper:  # unknown project (home dir, headless /tmp run): show everything, never only the cross-cutting rows
        return rs, False
    return proper + [r for r in rs if r["tag"] in CROSS], True


def age(d):
    return (TODAY - d).days if d else None


# ----------------------------------------------------------------------------- views
def fmt_due(r):
    if not r["due"]:
        due = "no date"
    elif r["due"] < TODAY:
        due = f"due {r['due'].isoformat()} ({age(r['due'])}d overdue)"
    elif r["due"] == TODAY:
        due = "due today"
    else:
        due = f"resurface {r['due'].isoformat()}"
    w = f" · waiting-on: {r['waiting-on']}" if r["status"] == "waiting" and r["waiting-on"] else ""
    title = r["title"] if len(r["title"]) <= 90 else r["title"][:89] + "…"
    nxt = r["next"] or f"(no next — set: thread update {r['id']} --next …)"
    nxt = nxt if len(nxt) <= 110 else nxt[:109] + "…"
    s = f"- {r['id']} [{r['tag']}] {r['status']}{w} · {due} · {title} · next: {nxt}"
    return s if len(s) <= ROW_CAP else s[:ROW_CAP - 2] + " …"


def fmt_index(r):
    t = r["title"] if len(r["title"]) <= INDEX_TITLE else r["title"][:INDEX_TITLE - 1] + "…"
    extra = f" ↻{r['due'].isoformat()[5:]}" if r["due"] and r["due"] > TODAY else ""
    return f"{r['id']} {r['status']}{extra} · {t}"


def ready_block(project, all_rows=False):
    rs, _ = rows()
    live = [r for r in rs if r["status"] in LIVE]
    sel, is_scoped = (live, False) if all_rows else scoped(live, project)
    due = sorted([r for r in sel if r["due"] and r["due"] <= TODAY and r["status"] != "in-work"], key=lambda r: r["due"])
    inwork = [r for r in sel if r["status"] == "in-work"]
    rest = sorted([r for r in sel if r not in due and r not in inwork], key=lambda r: (r["due"] is None, r["due"] or TODAY))
    elsewhere = [r for r in live if r not in sel and r["due"] and r["due"] <= TODAY]
    scope = f"project `{project}`" if is_scoped else "all projects"
    L = [f"Open threads for {scope} — act on DUE first. Ledger: memory/open-threads.md · "
         f"CLI: thread ready | show <id> | add | update | defer | close (never edit the file by hand)."]
    tail = f"+{len(elsewhere)} due in other projects (thread ready --all)" if elsewhere else ""
    budget, used, cut = BLOCK_CAP - len(L[0]) - len(tail) - 80, 0, 0
    for label, grp in (("DUE", due), ("IN WORK", inwork)):  # action rows first, each with its next:
        if not grp:
            continue
        L.append(f"{label} ({len(grp)}):"); used += len(L[-1]) + 1
        for i, r in enumerate(grp):
            s = fmt_due(r)
            if used + len(s) + 1 > budget:
                cut += len(grp) - i
                break
            L.append(s); used += len(s) + 1
    if cut:
        L.append(f"- … {cut} more rows cut at {BLOCK_CAP} chars (thread ready)")
    if not due and not inwork:
        L.append("DUE: none · IN WORK: none")
    if rest:  # the index never disappears: it shrinks to what fits, and names the remainder
        idx = []
        for r in rest[:INDEX_MAX]:
            s = fmt_index(r)
            if used + len(s) + 3 > budget:
                break
            idx.append(s); used += len(s) + 3
        more = len(rest) - len(idx)
        where = f" --project {project}" if is_scoped else ""
        L.append(f"INDEX ({len(rest)}): " + " | ".join(idx) + (f" | +{more} more (thread list{where})" if more else ""))
    if tail:
        L.append(tail)
    text = "\n".join(L)
    return text, {"due": len(due), "inwork": len(inwork), "index": len(rest), "elsewhere": len(elsewhere)}


def cmd_ready(a):
    text, _ = ready_block(a.project, a.all)
    print(text)


def cmd_inject(a):
    text, n = ready_block(a.project, False)
    print(text)
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                "project": a.project or "", "src": a.src, "chars": len(text), **n}) + "\n")
    except OSError:
        pass


def cmd_list(a):
    rs, _ = rows()
    if a.status:
        rs = [r for r in rs if r["status"] == norm_status(a.status)[0]]
    if not a.all and a.project:
        rs, _ = scoped(rs, a.project)
    for r in rs:
        d = r["due"].isoformat() if r["due"] else "-"
        u = r["touched"].isoformat() if r["touched"] else "-"
        print(f"{r['id'] or '?':<34} {r['status']:<12} due {d:<10} upd {u:<10} [{r['tag']}] {r['title'][:60]}")
    print(f"({len(rs)} rows)", file=sys.stderr)


def cmd_show(a):
    r, _ = find(a.id)
    print(r["raw"])


# ----------------------------------------------------------------------------- writes
def validate(r):
    if r["status"] == "waiting" and not r["waiting-on"]:
        sys.exit("thread: a `waiting` row needs --waiting-on (anton | external:<gate> | <id>)")
    if r["status"] in ("in-work", "waiting") and not r["next"]:
        sys.exit("thread: `in-work` and `waiting` rows need --next (the concrete next step)")


def slug(title, existing):
    base = "ot-" + re.sub(r"[^a-z0-9]+", "-", title.lower().encode("ascii", "ignore").decode()).strip("-")
    base = "-".join(base.split("-")[:5]) or "ot-thread"
    s, n = base, 2
    while s in existing:
        s, n = f"{base}-{n}", n + 1
    return s


def cmd_add(a):
    with locked():
        rs, lines = rows()
        st, mapped = norm_status(a.status)
        rid = a.id or slug(a.title, {r["id"] for r in rs})
        if any(r["id"] == rid for r in rs):
            sys.exit(f"thread: id `{rid}` already exists")
        parts = [f"- [{a.project}] id: `{rid}` · **{clean(a.title)}** · status: `{st}`"]
        for key, val in (("waiting-on", a.waiting_on), ("next", a.next), ("decision", a.decision), ("parent", a.parent)):
            if val:
                parts.append(f"{key}: {clean(val)}")
        if a.resurface:
            parts.append(f"next-resurface: `{iso(a.resurface)}`")
        parts.append(f"since: `{TODAY.isoformat()}` · updated: `{TODAY.isoformat()}`")
        if a.prose:
            parts.append(clean(" ".join(a.prose)))
        line = " · ".join(parts)
        validate(parse(line))
        _, end = open_span(lines)
        lines.insert(end, line)
        write_lines(lines)
    if mapped:
        print(f"(status `{a.status}` mapped to `{st}`)", file=sys.stderr)
    print(line)


def backfill_since(line):
    """Rows written before `since:` existed: the row's own latest past date (ignoring resurface/updated), else `updated`."""
    r = parse(line)
    if r["since"]:
        return line
    body = re.sub(r"(next-resurface|updated): `[^`]*`", "", line)
    dates = sorted(d for d in _DATE.findall(body) if d < TODAY.isoformat())
    return set_field(line, "since", dates[-1] if dates else (r["updated"] or TODAY.isoformat()), True)


def _mutate(rid, fn):
    with locked():
        r, lines = find(rid)
        line = fn(r["raw"], r)
        line = set_field(line, "updated", TODAY.isoformat(), True)
        if parse(line)["status"] != r["status"]:
            line = set_field(line, "since", TODAY.isoformat(), True)  # since = when the current status began
        else:
            line = backfill_since(line)
        validate(parse(line))
        lines[r["line_no"]] = line
        write_lines(lines)
    print(line)
    return line


def cmd_update(a):
    def fn(line, r):
        if a.status:
            st, mapped = norm_status(a.status)
            line = set_field(line, "status", st, True)
            if mapped:
                print(f"(status `{a.status}` mapped to `{st}`)", file=sys.stderr)
        if a.waiting_on:
            line = set_field(line, "waiting-on", a.waiting_on, False)
        if a.resurface:
            line = set_field(line, "next-resurface", iso(a.resurface), True)
        if a.next:
            line = set_field(line, "next", a.next, False)
        if a.decision:
            line = set_field(line, "decision", a.decision, False)
        if a.note:
            line += f" · {TODAY.isoformat()}: {clean(a.note)}"
        return line
    _mutate(a.id, fn)


def cmd_defer(a):
    until = iso(a.until)
    if dt.date.fromisoformat(until) <= TODAY:
        sys.exit(f"thread: --until {until} is not after today ({TODAY}); a row due now needs `thread update --next`, not a defer")

    def fn(line, r):
        line = set_field(line, "next-resurface", until, True)
        if a.why:
            line += f" · {TODAY.isoformat()}: deferred to {until}: {clean(a.why)}"
        return line
    _mutate(a.id, fn)


def archive_path(day=None):
    day = day or TODAY
    return f"{MEM}/open-threads-archive-{day:%Y-%m}.md"


def append_archive(line, path, heading):
    new = not os.path.exists(path)
    with open(path, "a", encoding="utf-8") as f:
        if new:
            f.write(f"# Open-threads archive {os.path.basename(path)[len('open-threads-archive-'):-3]}\n\n"
                    "Rows closed by `thread close`. Read-only history; the live ledger is open-threads.md.\n\n")
        txt = open(path, encoding="utf-8").read() if not new else ""
        if heading not in txt:
            f.write(f"\n{heading}\n")
        f.write(line + "\n")


def cmd_close(a):
    with locked():
        r, lines = find(a.id)
        line = set_field(r["raw"], "status", "resolved", True)
        line = set_field(line, "updated", TODAY.isoformat(), True)
        if a.decision:
            line = set_field(line, "decision", a.decision, False)
        line += f" · closed {TODAY.isoformat()}: {clean(a.outcome)}"
        append_archive(line, archive_path(), f"## Closed {TODAY:%Y-%m}")  # archive first: a crash here duplicates, never loses
        del lines[r["line_no"]]
        write_lines(lines)
    print(line)
    print(f"→ moved to {archive_path()}", file=sys.stderr)


# ----------------------------------------------------------------------------- reports
def cmd_stale(a):
    rs, _ = rows()
    cut = TODAY - dt.timedelta(days=a.days)
    out = [r for r in rs if r["status"] in LIVE and (r["since_d"] or dt.date(2000, 1, 1)) < cut
           and not (r["due"] and r["due"] > TODAY)]
    for r in sorted(out, key=lambda r: r["since_d"] or dt.date(2000, 1, 1)):
        u = r["since_d"].isoformat() if r["since_d"] else "never"
        print(f"- {r['id']} [{r['tag']}] {r['status']} since {u} · {r['title'][:70]}")
    print(f"({len(out)} stale ≥{a.days}d)", file=sys.stderr)


def closed_since(days):
    cut = TODAY - dt.timedelta(days=days)
    out = []
    for p in glob.glob(f"{MEM}/open-threads-archive-20*.md"):
        for l in read_lines(p):
            m = re.search(r"closed (20\d\d-\d\d-\d\d): (.*)$", l)
            if m and dt.date.fromisoformat(m.group(1)) >= cut:
                r = parse(l)
                out.append((m.group(1), r["id"] if r else "?", (r["title"] if r else l)[:60], m.group(2)[:80]))
    return sorted(out)


def inject_stats(days):
    cut = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    vals = []
    try:
        for l in read_lines(LOG):
            try:
                j = json.loads(l)
                if j.get("src") == "hook" and dt.datetime.strptime(j["ts"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc) >= cut:
                    vals.append(j["chars"])  # hook runs only: manual/test injects must not move the SC-002 mean
            except (ValueError, KeyError):
                continue
    except OSError:
        pass
    if not vals:
        return {"n": 0, "mean": 0, "max": 0}
    return {"n": len(vals), "mean": sum(vals) // len(vals), "max": max(vals)}


def audit(days):
    """Count ledger-touching tool calls in session transcripts (Bash/Edit/Write/Read/Agent/ctx)."""
    cut = dt.datetime.now().timestamp() - days * 86400
    counts = {"bash_read": 0, "bash_write": 0, "cli": 0, "edit": 0, "write": 0, "read": 0, "agent": 0, "ctx": 0}
    sessions = set()
    write_rx = re.compile(r"sed -i|>>|(?<![<>])>\s*\S*open-threads|tee |cat <<|python3 - <<|ot_migrate|\.write\(")
    for p in glob.glob(f"{HOME}/.claude/projects/**/*.jsonl", recursive=True):  # sub-agent transcripts sit one level down
        try:
            if os.path.getmtime(p) < cut:
                continue
            with open(p, encoding="utf-8", errors="ignore") as f:
                for l in f:
                    if '"tool_use"' not in l or not ("open-threads" in l or "thread.py" in l or _CLI_PRE.search(l)):
                        continue
                    try:
                        j = json.loads(l)
                    except ValueError:
                        continue
                    ts = j.get("timestamp", "")
                    if ts and ts[:10] < (dt.date.today() - dt.timedelta(days=days)).isoformat():
                        continue
                    for c in (j.get("message") or {}).get("content") or []:
                        if not isinstance(c, dict) or c.get("type") != "tool_use":
                            continue
                        inp = json.dumps(c.get("input") or {}, ensure_ascii=False)
                        name = c.get("name", "")
                        cmd = str((c.get("input") or {}).get("command", "")) if name == "Bash" else ""
                        if "open-threads" not in inp and "thread.py" not in inp and not CLI_RX.search(cmd):
                            continue
                        sessions.add(p)
                        if name == "Bash" and CLI_RX.search(cmd):
                            counts["cli"] += 1
                        elif name == "Bash":
                            counts["bash_write" if write_rx.search(cmd) else "bash_read"] += 1
                        elif name in ("Edit", "MultiEdit"):
                            counts["edit"] += 1
                        elif name == "Write":
                            counts["write"] += 1
                        elif name == "Read":
                            counts["read"] += 1
                        elif name == "Agent":
                            counts["agent"] += 1
                        elif name.startswith("mcp__plugin_context-mode"):
                            counts["ctx"] += 1
        except OSError:
            continue
    raw = sum(v for k, v in counts.items() if k != "cli")
    return counts, raw, len(sessions)


def cmd_audit(a):
    counts, raw, n_sess = audit(a.days)
    st = inject_stats(a.days)
    total = raw + counts["cli"]
    share = (counts["cli"] * 100 // total) if total else 0
    print(f"ledger-touching tool calls, last {a.days}d: {total} (raw {raw} · cli {counts['cli']} = {share}% cli) in {n_sess} sessions")
    print("  " + " · ".join(f"{k} {v}" for k, v in counts.items()))
    print(f"injections: {st['n']} · mean {st['mean']} chars (~{st['mean'] // 4} tokens) · max {st['max']} chars")
    print(f"  {BASELINE}; targets 2026-11-19: ≤400 calls/30d, ≤4,000 chars/inject")


def cmd_digest(a):
    rs, _ = rows()
    live = [r for r in rs if r["status"] in LIVE]
    by = {}
    for r in live:
        by[r["status"]] = by.get(r["status"], 0) + 1
    due = sorted([r for r in live if r["due"] and r["due"] <= TODAY], key=lambda r: r["due"])
    wait = sorted([r for r in live if r["status"] == "waiting" and (r["waiting-on"] or "").startswith("anton")],
                  key=lambda r: r["since_d"] or dt.date(2000, 1, 1))
    cut = TODAY - dt.timedelta(days=14)
    stale = [r for r in live if (r["since_d"] or dt.date(2000, 1, 1)) < cut and not (r["due"] and r["due"] > TODAY) and r not in due]
    closed = closed_since(7)
    counts, raw, n_sess = audit(7)
    st = inject_stats(7)
    L = [f"## 🧵 Threads — {TODAY.isoformat()}", "",
         f"> live {len(live)} (" + " · ".join(f"{k} {v}" for k, v in sorted(by.items())) + f") · due {len(due)} · "
         f"waiting on Anton {len(wait)} · stale ≥14d {len(stale)} · closed last 7d {len(closed)}",
         f"> ledger writes last 7d: cli {counts['cli']} vs raw {raw} · injections {st['n']} × ~{st['mean']} chars", ""]
    if due:
        L.append("**Due** (oldest first):")
        L += [f"- {r['id']} [{r['tag']}] {r['due'].isoformat()} ({age(r['due'])}d) · {r['title'][:70]} · next: {r['next'] or '—'}" for r in due]
        L.append("")
    if wait:
        L.append("**Waiting on Anton:**")
        L += [f"- {r['id']} [{r['tag']}] since {r['since_d'].isoformat() if r['since_d'] else '?'} ({age(r['since_d']) if r['since_d'] else '?'}d) · {r['title'][:70]} · next: {r['next'] or '—'}" for r in wait]
        L.append("")
    if closed:
        L.append("**Closed last 7 days:**")
        L += [f"- {d} {i} · {t} · {o}" for d, i, t, o in closed]
        L.append("")
    if stale:
        L.append(f"**Stale ≥14d ({len(stale)}):** " + ", ".join(f"{r['id']}" for r in stale[:20]) + (" …" if len(stale) > 20 else ""))
    print("\n".join(L).rstrip())


def cmd_mirror(a):
    rs, _ = rows()
    live = [r for r in rs if r["status"] in LIVE]
    pick = sorted([r for r in live if r["status"] == "in-work" or (r["due"] and r["due"] <= TODAY)],
                  key=lambda r: (r["status"] != "in-work", r["due"] or TODAY))
    os.makedirs(TASKS, exist_ok=True)
    for old in glob.glob(f"{TASKS}/*.json"):
        os.remove(old)
    for n, r in enumerate(pick, 1):
        doc = {"id": str(n), "subject": f"{r['title'][:80]} [{r['tag']}]",
               "description": f"{r['id']} · {r['status']} · due {r['due'].isoformat() if r['due'] else '-'} · next: {r['next'] or '-'}",
               "status": "in_progress" if r["status"] == "in-work" else "pending",
               "activeForm": f"Working on {r['title'][:50]}", "blockedBy": [], "blocks": []}
        with open(f"{TASKS}/{n}.json", "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)
    print(f"{len(pick)} tasks written to {TASKS}/ · test: CLAUDE_CODE_TASK_LIST_ID=open-threads claude  → Ctrl+T")


# ----------------------------------------------------------------------------- one-shot migration
def cmd_migrate_legacy(a):
    if os.path.exists(LEGACY):
        sys.exit(f"thread: migrate-legacy already ran on this machine ({LEGACY} exists); a second run would overwrite that archive, so it refuses")
    lines = read_lines()
    a0, b0 = open_span(lines)
    head_idx = a0 - 1
    first_h = next(i for i, l in enumerate(lines) if l.startswith("## "))
    preamble = lines[:first_h]
    open_rows = lines[a0:b0]
    legacy = lines[b0:] if first_h == head_idx else lines[first_h:head_idx] + lines[b0:]
    n_before = sum(1 for l in lines if _ROW.match(l))
    pulled, kept_legacy = [], []
    for l in legacy:
        r = parse(l)
        if r and r["due"] and r["due"] >= dt.date(2026, 10, 1) and r["status"] in LIVE:
            pulled.append(l + " · (pulled from the legacy sections by thread migrate-legacy, spec 018)")
        else:
            kept_legacy.append(l)
    new_open, closed, notes, seen, pulled_ids = [], [], [], set(), []
    for src, l in [("open", x) for x in open_rows] + [("pulled", x) for x in pulled]:
        r = parse(l)
        if not r:
            if l.strip() and l.strip() != "- (none)":
                new_open.append(l)
            continue
        line = l
        if not r["id"]:
            rid = slug(r["title"], seen)
            line = set_field(line, "id", rid, True); notes.append(f"{rid}: +id (was id-less)")
        elif r["id"] in seen:
            rid = r["id"] + "-2"
            line = set_field(line, "id", rid, True); notes.append(f"{rid}: duplicate id renamed")
        else:
            rid = r["id"]
        seen.add(rid)
        if src == "pulled":
            pulled_ids.append(rid)
        raw_m = _FIELD_BT["status"].search(line)
        raw_st = raw_m.group("v") if raw_m else None
        if raw_st is None:
            line = set_field(line, "status", "open", True); notes.append(f"{rid}: +status open")
        elif raw_st.lower() in SYN or raw_st == "bplus-gated":
            st = "waiting" if raw_st == "bplus-gated" else SYN[raw_st.lower()]
            line = set_field(line, "status", st, True); notes.append(f"{rid}: {raw_st} → {st}")
            if st == "waiting" and not r["waiting-on"]:
                line = set_field(line, "waiting-on", "anton", False)
        elif raw_st not in STATUS:
            line = set_field(line, "status", "open", True); notes.append(f"{rid}: {raw_st} → open (unknown token)")
        if not _FIELD_BT["updated"].search(line):
            dates = sorted(_DATE.findall(line))
            line = set_field(line, "updated", dates[-1] if dates else TODAY.isoformat(), True)
        if parse(line)["status"] == "resolved":
            closed.append(line + f" · closed {TODAY.isoformat()}: archived by thread migrate-legacy (row already marked closed)")
        else:
            new_open.append(line)
    n_open = sum(1 for l in new_open if _ROW.match(l)); n_leg = sum(1 for l in kept_legacy if _ROW.match(l))
    n_after = n_open + n_leg + len(closed)
    print(f"rows before {n_before} = open {n_open} + legacy {n_leg} + closed {len(closed)} = {n_after}  {'OK' if n_before == n_after else 'MISMATCH'}")
    print(f"pulled into Open ({len(pulled_ids)}): " + ", ".join(pulled_ids))
    print(f"normalised ({len(notes)}): " + "; ".join(notes))
    print(f"closed → {archive_path()} ({len(closed)}): " + ", ".join(parse(l)['id'] or '?' for l in closed))
    if n_before != n_after:
        sys.exit("thread: row count mismatch, nothing written")
    if not a.apply:
        print("(dry run; add --apply to write)")
        return
    shutil.copy2(LEDGER, LEDGER + ".pre-018.bak")
    header = ["# Open threads — the live ledger (status of in-flight work)", "",
              "Write ONLY through the CLI: `thread add | update | defer | close` (prints the row; keeps a .bak).",
              "Read: `thread ready` (due + in-work first). Closed rows live in open-threads-archive-YYYY-MM.md;",
              "everything before 2026-10-20 is in open-threads-archive-legacy.md (searchable, read-only).", "",
              OPEN_H]
    write_lines(header + new_open)
    with open(LEGACY, "w", encoding="utf-8") as f:
        f.write("# Open-threads archive — legacy sections (moved 2026-10-20 by thread migrate-legacy)\n\n"
                "Verbatim copy of every section that was not `## Open (current)`. Read-only history.\n\n")
        f.write("\n".join(preamble + kept_legacy).rstrip("\n") + "\n")
    for l in closed:
        append_archive(l, archive_path(), f"## Closed {TODAY:%Y-%m}")
    print(f"written: {LEDGER} ({os.path.getsize(LEDGER)} B), {LEGACY} ({os.path.getsize(LEGACY)} B), backup {LEDGER}.pre-018.bak")


# ----------------------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser(prog="thread", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("ready"); p.add_argument("--project", default=os.path.basename(os.getcwd())); p.add_argument("--all", action="store_true"); p.set_defaults(fn=cmd_ready)
    p = sp.add_parser("inject"); p.add_argument("--project", default=""); p.add_argument("--src", default="manual", help="hook | manual (audit stats count hook runs only)"); p.set_defaults(fn=cmd_inject)
    p = sp.add_parser("list"); p.add_argument("--status"); p.add_argument("--project"); p.add_argument("--all", action="store_true"); p.set_defaults(fn=cmd_list)
    p = sp.add_parser("show"); p.add_argument("id"); p.set_defaults(fn=cmd_show)
    p = sp.add_parser("add"); p.add_argument("--project", required=True, help="tag, e.g. connectpro"); p.add_argument("--title", required=True)
    p.add_argument("--status", default="open"); p.add_argument("--waiting-on"); p.add_argument("--resurface"); p.add_argument("--next"); p.add_argument("--decision"); p.add_argument("--parent"); p.add_argument("--id"); p.add_argument("prose", nargs="*"); p.set_defaults(fn=cmd_add)
    p = sp.add_parser("update"); p.add_argument("id"); p.add_argument("--status"); p.add_argument("--waiting-on"); p.add_argument("--resurface"); p.add_argument("--next"); p.add_argument("--decision"); p.add_argument("--note"); p.set_defaults(fn=cmd_update)
    p = sp.add_parser("defer"); p.add_argument("id"); p.add_argument("--until", required=True); p.add_argument("--why"); p.set_defaults(fn=cmd_defer)
    p = sp.add_parser("close"); p.add_argument("id"); p.add_argument("--outcome", required=True); p.add_argument("--decision"); p.set_defaults(fn=cmd_close)
    p = sp.add_parser("stale"); p.add_argument("--days", type=int, default=14); p.set_defaults(fn=cmd_stale)
    p = sp.add_parser("digest"); p.set_defaults(fn=cmd_digest)
    p = sp.add_parser("audit"); p.add_argument("--days", type=int, default=30); p.set_defaults(fn=cmd_audit)
    p = sp.add_parser("mirror"); p.set_defaults(fn=cmd_mirror)
    p = sp.add_parser("migrate-legacy"); p.add_argument("--apply", action="store_true"); p.set_defaults(fn=cmd_migrate_legacy)
    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
