#!/usr/bin/env python3
"""P5 weekly retro runner. Zero LLM tokens.

SUPERSEDED 2026-05-29 by system-overview.py as the Monday launchd target — system-overview is a
superset (burn + ALL telemetry streams + inventory + self-improvement flags). Kept for reference /
its burn+correction logic; no longer scheduled. Prepended weekly blocks to p5-loop/retro-log.md.
"""
from __future__ import annotations
import json, os, re, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXTRACTOR = ROOT / "scripts" / "p5" / "burn-metrics.py"
RETRO = ROOT / "p5-loop" / "retro-log.md"
# Tier-3 #11: correction candidates staged by ~/.claude/hooks/correction-detector.py
CORRECTIONS = Path(os.path.expanduser("~/.claude/logs/correction-candidates.jsonl"))


def correction_candidates_block(iso_year: int, iso_week: int) -> str:
    """Compact, zero-LLM surface of un-reviewed correction candidates for this ISO week.

    The detector hook stages candidates deterministically; this only LISTS them so the
    weekly review can do the LLM/human promote-to-corrections.md pass. Never mutates the log.
    """
    if not CORRECTIONS.exists():
        return ""
    rows = []
    try:
        for line in CORRECTIONS.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("reviewed"):
                continue
            ts = r.get("ts", "")
            try:
                d = datetime.fromisoformat(ts)
                wy, ww, _ = d.isocalendar()
                if (wy, ww) != (iso_year, iso_week):
                    continue
            except Exception:
                pass  # keep undated/unparseable so nothing is silently dropped
            rows.append(r)
    except Exception:
        return ""
    if not rows:
        return ""
    lines = [f"### Correction candidates — {len(rows)} un-reviewed (Tier-3 #11)",
             "_Promote real ones into memory/corrections.md (bump counts); mark reviewed in the log._"]
    for r in rows[:15]:
        snip = (r.get("snippet", "") or "")[:120]
        lines.append(f"- `{r.get('ts','')[:10]}` {snip}")
    if len(rows) > 15:
        lines.append(f"- … +{len(rows) - 15} more")
    return "\n".join(lines)


def notify(title: str, msg: str) -> None:
    try:
        subprocess.run(
            ["/usr/bin/osascript", "-e",
             f'display notification "{msg}" with title "{title}"'],
            check=False, timeout=10,
        )
    except Exception:
        pass  # notification is best-effort; never fail the retro over it


def main() -> int:
    block = subprocess.run(
        [sys.executable, str(EXTRACTOR)], capture_output=True, text=True, timeout=60
    ).stdout.strip()
    if not block:
        return 0

    now = datetime.now(timezone.utc)
    iso_year, iso_week, _ = now.isocalendar()
    week_tag = f"Week {iso_year}-W{iso_week:02d}"

    cand = correction_candidates_block(iso_year, iso_week)
    if cand:
        block = f"{block}\n\n{cand}"

    text = RETRO.read_text() if RETRO.exists() else "# P5 retro log (append-only)\n\n---\n"
    head, sep, body = text.partition("\n---\n")

    # drop an existing same-week block at the very top of body (re-run within the same week)
    body = body.lstrip("\n")
    if body.startswith(f"## {week_tag}") or re.match(rf"## {re.escape(week_tag)}\b", body):
        body = re.sub(r"^## .*?(?=\n## |\Z)", "", body, count=1, flags=re.DOTALL).lstrip("\n")

    new = f"{head}{sep}\n\n{block}\n\n{body}".rstrip() + "\n"
    RETRO.write_text(new)

    headline = next((l for l in block.splitlines() if "write-prod-code warns" in l), "")
    verdict = next((l for l in block.splitlines() if "VERDICT" in l), "VERDICT: ?")
    notify("cc-stack P5 retro", f"{verdict.replace('- ', '')} — {headline.strip('- *')[:80]}")
    print(f"appended {week_tag} to {RETRO}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
