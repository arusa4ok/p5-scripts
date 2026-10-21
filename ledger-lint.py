#!/usr/bin/env python3
"""Spec 016 FR-010/FR-011 — open-threads ledger lint. Warn-only: ledger writes are never blocked.

    python3 ledger-lint.py [path] [--json]   report violations (exit 0 always)
    python3 ledger-lint.py --hook            PostToolUse: reads hook JSON on stdin, speaks only when
                                             the violation count went UP since the last fire
    python3 ledger-lint.py --sweep [path]    write p5-loop/ledger-sweep-proposal-YYYY-MM.md
                                             (PROPOSAL only — never touches the ledger)

Violations, per row:
  waiting-unexplained  status `waiting` with neither a non-empty `waiting-on:` nor `dependencies:`
  off-vocab            status field outside spec 011's 7 tokens
  no-status            row carries no status field at all (spec 011: every row has one)

Row parsing is action-queue.py's (parse_ot_row, OT_STATUS, section fallback) — imported, not copied.
"""
import datetime
import importlib.util
import json
import os
import re
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).parent
_spec = importlib.util.spec_from_file_location("action_queue", HERE / "action-queue.py")
aq = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(aq)

LOGS = Path(os.path.expanduser("~/.claude/logs"))
CACHE = LOGS / "ledger-lint-last.json"
ERRLOG = LOGS / "ledger-lint-errors.log"
P5 = HERE.parent.parent / "p5-loop"

_EMPTY = {"", "—", "-", "none", "n/a"}
_LOOSE_ID = re.compile(r"\bid:\s*`?(?P<v>[\w.\-]+)")
_BOLD = re.compile(r"\*\*(?P<v>[^*]+)\*\*")


def _field(line, name):
    m = re.search(name + r":\s*(?:`(?P<a>[^`]*)`|(?P<b>[^\s·]+))", line)
    v = (m.group("a") if m and m.group("a") is not None else m.group("b") if m else "") or ""
    return v.strip()


def _title(raw):
    b = _BOLD.search(raw)
    t = b.group("v") if b else re.sub(r"^.*?id:\s*`?[\w.\-]+`?\s*·?\s*", "", raw)
    t = " ".join(t.split())
    return t if len(t) <= 80 else t[:79] + "…"


def lint(path=None):
    """Return {rows, waiting, counts, violations:[{id, kinds, status, title, raw, ...}]}."""
    lines = Path(path or aq.OPEN_THREADS).read_text(encoding="utf-8").splitlines()
    section, rows, waiting, out = None, 0, 0, []
    for line in lines:
        if line.startswith("## "):
            section = aq._ot_section_status(line)
            continue
        if not line.strip().startswith("- ") or "(none)" in line:
            continue
        r = aq.parse_ot_row(line, section)
        if r is None:
            continue
        rows += 1
        sm = aq._OT_STATUS.search(line)
        field = sm.group("v") if sm else None
        kinds = []
        if field is None:
            kinds.append("no-status")
        elif field not in aq.OT_STATUS:
            kinds.append("off-vocab")
        if field == "waiting":
            waiting += 1
            if _field(line, "waiting-on") in _EMPTY and _field(line, "dependencies") in _EMPTY:
                kinds.append("waiting-unexplained")
        if kinds:
            im = aq._OT_ID.search(line) or _LOOSE_ID.search(line)
            out.append({"id": im.group("v") if im else r["raw"][:40], "kinds": kinds,
                        "status": field or f"(none; section={r['status']})", "section": section,
                        "next_resurface": r["next_resurface"], "title": _title(r["raw"]),
                        "raw": r["raw"]})
    counts = {k: sum(k in v["kinds"] for v in out)
              for k in ("waiting-unexplained", "off-vocab", "no-status")}
    return {"rows": rows, "waiting": waiting, "counts": counts, "violations": out}


def render(res):
    c = res["counts"]
    head = (f"ledger-lint: {len(res['violations'])} violating rows of {res['rows']} · "
            f"waiting-unexplained {c['waiting-unexplained']}/{res['waiting']} · "
            f"off-vocab {c['off-vocab']} · no-status {c['no-status']}")
    return "\n".join([head] + [f"  {v['id']} · {','.join(v['kinds'])} · {v['status']}"
                               for v in res["violations"]])


# ------------------------------------------------------------------ sweep (FR-011), heuristic
_OFF_VOCAB_FIX = {"active": "in-work", "in_progress": "in-work", "in-progress": "in-work",
                  "closed": "resolved", "done": "resolved"}
_EXTERNAL = ("quota", "upstream", "vendor", "reply", "payment", "delivery", "review", "restart",
             "release", "merge", "pr ", "ci ", "deploy")
_DONE = re.compile(r"✅|\b(CLOSED|DONE|RESOLVED|SHIPPED|MERGED)\b")


def propose(v, today):
    """(fix, reason≤12 words). Heuristic — reads the row text, not the world."""
    st, raw = v["status"], v["raw"]
    if _DONE.search(raw):
        return "close (→ `resolved`)", "row text already says done/closed"
    if "off-vocab" in v["kinds"]:
        low = st.lower()
        if low.startswith("waiting"):
            return "fix status token → `waiting` + add `waiting-on: anton`", f"`{st}` encodes the waiter inside the token"
        if low in _OFF_VOCAB_FIX:
            return f"fix status token → `{_OFF_VOCAB_FIX[low]}`", f"`{st}` is a synonym of an in-vocab token"
        return "fix status token → `waiting` + `waiting-on: external:<gate>`", f"`{st}` names a gate, not a status"
    if "no-status" in v["kinds"]:
        sec = (v["section"] or "open")
        return f"add `status: {sec}`", "no field; section header implies this status"
    nr = v["next_resurface"]
    try:
        stale = nr and (today - datetime.date.fromisoformat(nr[:10])).days > 30
    except ValueError:
        stale = False
    if stale:
        return "park", f"resurface date {nr[:10]} is >30 days past"
    low = raw.lower()
    hit = next((w.strip() for w in _EXTERNAL if w in low), None)
    if hit and "anton" not in low:
        return f"add `waiting-on: external:{hit}`", f"row text mentions '{hit}', no Anton action named"
    return "add `waiting-on: anton`", "default for waiting rows; most sit in Waiting-on-Anton"


def sweep(path=None, out=None):
    today = datetime.date.today()
    res = lint(path)
    out = Path(out or P5 / f"ledger-sweep-proposal-{today:%Y-%m}.md")
    c = res["counts"]
    body = [f"# Ledger sweep proposal — {today.isoformat()}", "",
            "**PROPOSAL — not applied, Anton decides.** Generated by `scripts/p5/ledger-lint.py --sweep`",
            "(spec 016 FR-011). Every proposed fix is **heuristic** — it reads the row text, not the",
            "world; check before applying. The ledger itself was not modified.", "",
            f"- source: `{path or aq.OPEN_THREADS}`",
            f"- rows parsed: {res['rows']} · violating rows: {len(res['violations'])}",
            f"- waiting-unexplained: {c['waiting-unexplained']} of {res['waiting']} waiting · "
            f"off-vocab: {c['off-vocab']} · no-status: {c['no-status']}", "",
            "Format: id · current status · title · **proposed fix** — reason (heuristic)", ""]
    tally = {}
    for v in res["violations"]:
        fix, why = propose(v, today)
        key = re.sub(r"`([\w-]+)[^`]*`", r"\1", fix)
        tally[key] = tally.get(key, 0) + 1
        body.append(f"- `{v['id']}` · {v['status']} · {v['title']} · **{fix}** — {why}")
    body += ["", "Proposal tally: " + " · ".join(f"{k} {n}" for k, n in sorted(tally.items())), ""]
    out.write_text("\n".join(body), encoding="utf-8")
    return out, res


# ------------------------------------------------------------------ hook (FR-010), fail-open
def hook(stdin):
    data = json.loads(stdin or "{}")
    fp = (data.get("tool_input") or {}).get("file_path", "")
    if not fp.endswith("open-threads.md"):
        return
    res = lint(fp)
    n = len(res["violations"])
    try:
        last = json.loads(CACHE.read_text()).get("count")
    except (OSError, ValueError):
        last = None
    LOGS.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps({"count": n, "ts": datetime.datetime.now().isoformat(timespec="seconds")}))
    if last is not None and n <= last:
        return
    c = res["counts"]
    msg = (f"ledger-lint: {n} violations (was {last if last is not None else '—'}) — "
           f"waiting-unexplained {c['waiting-unexplained']}, off-vocab {c['off-vocab']}, "
           f"no-status {c['no-status']}. Waiting rows need `waiting-on:` or `dependencies:`; "
           f"status must be one of {', '.join(aq.OT_STATUS)}. Run scripts/p5/ledger-lint.py for ids.")
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": msg}}))


def main(argv):
    if "--hook" in argv:
        try:
            hook(sys.stdin.read())
        except Exception:                              # fail-open, but never silently
            try:
                LOGS.mkdir(parents=True, exist_ok=True)
                with open(ERRLOG, "a", encoding="utf-8") as f:
                    f.write(f"{datetime.datetime.now().isoformat(timespec='seconds')} "
                            f"{traceback.format_exc()}\n")
            except OSError:
                pass
        return 0
    path = next((a for a in argv if not a.startswith("--")), None)
    if "--sweep" in argv:
        out, res = sweep(path)
        print(f"wrote {out} — {len(res['violations'])} violating rows")
        return 0
    res = lint(path)
    print(json.dumps(res, ensure_ascii=False, indent=1) if "--json" in argv else render(res))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
