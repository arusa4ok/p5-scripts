#!/usr/bin/env python3
"""Spec 016 — Gate A (plan, before code) and Gate B (acceptance, before `Executed`), Beloborodov v3.

    python3 spec-gate-b.py <spec-dir>             Gate B: exit 0 pass · 1 missing item · 2 high-risk < 7 criteria
    python3 spec-gate-b.py --gate-a <spec-dir>    Gate A: plan reconciled against canon by another model
                                                  + every open task carries CHECK (v3 #14-#20)
    python3 spec-gate-b.py --template [--high|--gate-a] print a blank verdict table

Gate A tables live in evidence/gate-a*.md; Gate B reads every other evidence/*.md.
Header may be bold lines (`**Author model**:`, `**Risk**: high`) or YAML frontmatter
(`author_model:`, `risk: high`).

Inputs, both read from the spec dir:
  spec.md header   `**Author model**: <model>` (absent = unknown = fail, never guessed — Edge Cases)
                   `**Risk**: high` (optional; switches the required set from 3 to 7 criteria)
  evidence/*.md    verify-critic criteria-mode table(s):
                   | criterion | verdict | critic_model | degraded | evidence |

A row counts only if its critic_model is a different model from the author's. Any row flagged
`degraded: same-model` fails the gate outright (v3 #8: a quota fallback is not a pass).
"""
import re
import sys
from pathlib import Path

# Canonical criteria (v3 Gate B, #28). Matched case-insensitively by keyword in the criterion cell.
CRITERIA = {
    "functional": r"function",
    "code&reuse": r"reuse|\bcode\s*(&|and|\+)",
    "whole-system": r"whole[\s-]*system|reconcil",
    "architecture+observability": r"architect|observab",
    "data access": r"data[\s-]*access",
    "secrets": r"secret",
    "service-ness": r"service[\s-]*ness|serviceness",
}
# Gate A (v3 #11, #16): the plan, not the code, is reviewed against the canon before implementation.
CRITERIA_A = {
    "canon-fit": r"canon",
    "boundaries&reuse": r"boundar|contract",
    "scope": r"scope",
    "checkability": r"checkab",
    "rollback": r"rollback",
    # Our canon only checks a plan against our own decisions. This row makes the critic name
    # >=1 primary source on how the problem is normally solved and where the plan departs from
    # it (2026-10-21: spec 020 passed both gates and still missed per-call context size).
    "prior-art": r"prior.art|practice",
}
DEFAULT = ("functional", "code&reuse", "whole-system")
HIGH = tuple(CRITERIA)

# The three tiers are roles, not vendors (cc-stack CLAUDE.md 2026-08-20): opus and sonnet are the
# same model, so "opus wrote, sonnet judged" is a same-model review.
ALIASES = {"opus": "glm-5.3", "sonnet": "glm-5.3", "haiku": "glm-4.7"}
_NOISE = {"main", "loop", "gbrain", "think", "via", "the", "model", "tier", "critic", "author"}

HEADER = "| criterion | verdict | critic_model | degraded | evidence |"


def model_ids(text):
    """'haiku/glm-4.7 (gbrain think)' -> {'glm-4.7'}. Exact tokens, so glm-5.3 != glm-5.3-flash."""
    toks = re.findall(r"[a-z0-9][a-z0-9.\-]*", re.sub(r"\[1m\]", "", text.lower()))
    return {ALIASES.get(t, t) for t in toks if t not in _NOISE and not t.isdigit()}


def read_header(spec_md):
    text = spec_md.read_text(encoding="utf-8") if spec_md.exists() else ""
    am = re.search(r"^(?:\*\*Author model\*\*|author_model):\s*(.+)$", text, re.M | re.I)
    rm = re.search(r"^(?:\*\*Risk\*\*|risk):\s*\"?(\w+)", text, re.M | re.I)
    author = am.group(1).strip() if am else ""
    return author, bool(rm and rm.group(1).lower().startswith("high"))


def table_rows(md_text):
    """Yield dict rows of every table whose header carries `criterion` and `critic_model`."""
    cols = None
    for line in md_text.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            cols = None
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        low = [c.lower().replace(" ", "_") for c in cells]
        if "criterion" in low and "critic_model" in low:
            cols = low
            continue
        if cols is None or all(set(c) <= set("-: ") for c in cells):
            continue
        yield dict(zip(cols, cells))


def open_tasks_without_check(tasks_md):
    """Open tasks with no CHECK: — `- [ ]` bullets (task line + indented lines under it)
    and table rows carrying `[ ]` (the row itself must hold the CHECK)."""
    if not tasks_md.exists():
        return ["tasks.md missing"]
    lines, bad = tasks_md.read_text(encoding="utf-8").splitlines(), []
    for i, line in enumerate(lines):
        if line.strip().startswith("|") and "[ ]" in line and "CHECK:" not in line:
            bad.append(line.strip()[:60])
            continue
        if not re.match(r"\s*- \[ \]", line):
            continue
        block = [line]
        for nxt in lines[i + 1:]:
            if not nxt.strip() or re.match(r"\s*- \[|\s*#|\|", nxt):
                break
            block.append(nxt)
        if not any("CHECK:" in b for b in block):
            bad.append(line.strip()[:60])
    return bad


def check(spec_dir, gate="b"):
    """Return (exit_code, one-line reason)."""
    spec_dir = Path(spec_dir)
    author, high = read_header(spec_dir / "spec.md")
    if not author:
        return 1, "record the author model (`**Author model**:` / `author_model:` in spec.md header)"
    if gate == "a":
        bad = open_tasks_without_check(spec_dir / "tasks.md")
        if bad:
            return 1, f"{len(bad)} open task(s) without CHECK, first: '{bad[0]}'"
    criteria = CRITERIA_A if gate == "a" else CRITERIA
    required = tuple(CRITERIA_A) if gate == "a" else (HIGH if high else DEFAULT)
    # Rework rounds: files are read oldest-first by mtime and a later verdict on the same
    # criterion supersedes an earlier one, so a round-1 fail does not outlive its fix.
    rows, files = [], sorted((spec_dir / "evidence").glob("*.md"), key=lambda f: f.stat().st_mtime)
    for f in files:
        if f.name.startswith("gate-a") == (gate == "a"):
            rows += list(table_rows(f.read_text(encoding="utf-8")))
    if not rows:
        where = "evidence/gate-a*.md" if gate == "a" else "evidence/"
        return 1, f"no criteria-mode verdict table in {where} (run spec-gate-b.py --template)"
    a_ids = model_ids(author)
    latest = {}
    for r in rows:
        name = re.split(r"\s+[—–-]\s+", r.get("criterion", ""), maxsplit=1)[0]
        hits = [k for k, rx in criteria.items() if re.search(rx, name, re.I)]
        # a row outside the named criteria (e.g. "FR-005") supersedes by its own name
        latest[hits[0] if len(hits) == 1 else (name.strip().lower() or id(r))] = r
    covered = set()
    for r in latest.values():
        crit, critic = r.get("criterion", ""), r.get("critic_model", "")
        if "same-model" in r.get("degraded", "").lower():
            return 1, f"degraded: same-model on '{crit[:50]}' — a fallback is not a pass"
        if not critic or not model_ids(critic):
            return 1, f"critic_model empty on '{crit[:50]}'"
        if model_ids(critic) & a_ids:
            return 1, f"critic '{critic}' is the author's model '{author}' on '{crit[:50]}'"
        if r.get("verdict", "").strip().lower() != "pass":
            return 1, f"verdict '{r.get('verdict', '').strip()}' on '{crit[:50]}' — only pass covers a criterion"
        # match the criterion NAME only (text before " — "), so one prose cell cannot cover several
        name = re.split(r"\s+[—–-]\s+", crit, maxsplit=1)[0]
        hits = [k for k, rx in criteria.items() if re.search(rx, name, re.I)]
        if len(hits) == 1:
            covered.add(hits[0])
    missing = [c for c in required if c not in covered]
    if missing and high and gate == "b":
        return 2, f"risk: high needs 7 criteria, covered {7 - len(missing)}; missing: {', '.join(missing)}"
    if missing:
        return 1, f"missing criteria: {', '.join(missing)}"
    return 0, f"PASS gate {gate.upper()}: {len(required)} criteria cross-model (author {author})"


def template(high, gate="b"):
    names = {"functional": "functional — <spec SC / FR lines>", "code&reuse": "code & reuse",
             "whole-system": "whole-system reconciliation", "architecture+observability":
             "architecture + observability", "data access": "data access", "secrets": "secrets",
             "service-ness": "service-ness"}
    out = ["<!-- spec.md header must carry: **Author model**: <model>   (and **Risk**: high if so) -->",
           HEADER, "|---|---|---|---|---|"]
    if gate == "a":
        names = {"canon-fit": "canon-fit — DEC/ADR/constitution", "boundaries&reuse":
                 "boundaries & contracts — reuse seams, no fork", "scope": "scope — in/out, invariants",
                 "checkability": "checkability — every task has CHECK", "rollback": "rollback — per atom",
                 "prior-art": "prior-art — primary source on standard practice + where the plan departs"}
        out[0] = "<!-- save as evidence/gate-a-<date>.md; review the PLAN, before any code -->"
    out += [f"| {names[c]} | pass / fail | <critic model> | — | <command + result> |"
            for c in (tuple(CRITERIA_A) if gate == "a" else (HIGH if high else DEFAULT))]
    return "\n".join(out)


def main(argv):
    gate = "a" if "--gate-a" in argv else "b"
    argv = [a for a in argv if a != "--gate-a"]
    if "--template" in argv:
        print(template("--high" in argv, gate))
        return 0
    if not argv:
        print(__doc__.strip().splitlines()[2])
        return 1
    code, reason = check(argv[0], gate)
    print(reason)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
