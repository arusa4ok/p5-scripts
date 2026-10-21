#!/usr/bin/env python3
"""Signals: the default consumer for every log stream nobody claimed.

Why generic and not 7 bespoke sections (2026-07-16): wiring each orphan stream by
hand fixes today's seven and guarantees an eighth next month — the gap re-forms the
moment someone ships a hook and forgets the weekly. So instead: any stream under
~/.claude/logs/*.jsonl that no p5 script names by hand is rendered HERE, automatically.
A new hook is watched by default; being invisible now takes deliberate effort.

Bespoke sections still win where they exist (moshi, razbory, spec-drift…) — this only
picks up what they don't claim, so nothing is rendered twice.

Verdicts are deliberately dumb — activity + rot, not judgment:
  ⚠️ silent  — has history but 0 fires in 14 days (shipped, then died unnoticed)
  ↑ / ↓ / →  — this week vs the previous one
A real yardstick (fires -> outcome -> keep/kill) still belongs in feature-tracking.md;
this guarantees VISIBILITY, which is the thing that was actually missing.

Silent when there is nothing unclaimed. Never raises — a watcher must not break the report.
"""
import datetime
import json
import pathlib
import sys

LOGS = pathlib.Path.home() / ".claude" / "logs"
P5 = pathlib.Path(__file__).parent
# These two scan the log dir dynamically; their source must not count as "a consumer",
# or every stream would look claimed and this section would render nothing.
GENERIC = {"signals.py", "instrument-coverage.py"}


def _claimed():
    src = ""
    for f in P5.rglob("*.py"):
        if f.name in GENERIC:
            continue
        try:
            src += f.read_text(errors="ignore")
        except Exception:
            pass
    return src


def _fires(path, now):
    """(last 7d, previous 7d, total, last-seen date) — tolerant of shape drift."""
    wk = prev = total = 0
    last = ""
    c7 = (now - datetime.timedelta(days=7)).isoformat()
    c14 = (now - datetime.timedelta(days=14)).isoformat()
    try:
        lines = path.read_text(errors="ignore").splitlines()
    except Exception:
        return 0, 0, 0, ""
    for line in lines:
        try:
            d = json.loads(line)
        except Exception:
            continue
        total += 1
        ts = str(d.get("ts") or d.get("timestamp") or "")
        if not ts:
            continue
        last = max(last, ts)
        if ts > c7:
            wk += 1
        elif ts > c14:
            prev += 1
    return wk, prev, total, last[:10]


def main():
    if not LOGS.is_dir():
        return 0
    now = datetime.datetime.now(datetime.timezone.utc)
    claimed = _claimed()
    rows = []
    for p in sorted(LOGS.glob("*.jsonl")):
        # "Claimed" means a script OPENS the file — so match the filename WITH its
        # extension. The bare stem is not proof: `agent-rules-render` sits in
        # reality-audit's RARE_OK set (a list that only silences dead-stream warnings),
        # and a stem-match read that as a consumer and kept the stream invisible —
        # exactly the crude-substring bug this section exists to catch elsewhere.
        if p.name in claimed:
            continue
        wk, prev, total, last = _fires(p, now)
        if total == 0:
            continue  # an empty file is not a signal, it's a placeholder
        # Zero fires THIS week is silence, whatever last week looked like. The first
        # draft called a stream that had not fired in 13 days "↓ реже" — softer than
        # the truth, which is the one thing a rot-detector must never be.
        if wk == 0:
            verdict = f"⚠️ молчит с {last or '?'}"
        elif prev == 0:
            verdict = "🆕 новый"
        elif wk > prev * 1.2:
            verdict = "↑ чаще"
        elif wk < prev * 0.8:
            verdict = "↓ реже"
        else:
            verdict = "→ ровно"
        rows.append((wk, p.stem, prev, total, verdict))
    if not rows:
        return 0

    rows.sort(key=lambda r: -r[0])
    silent = [r for r in rows if r[4].startswith("⚠️")]
    print("## 📡 Сигналы (без личного потребителя)")
    print()
    print("_Рендерятся автоматически: у потока нет своей секции, но он пишет — значит он виден._")
    print()
    print("| поток | 7 дн | пред. 7 дн | всего | |")
    print("|---|---:|---:|---:|---|")
    for wk, name, prev, total, verdict in rows:
        print(f"| `{name}` | {wk} | {prev} | {total} | {verdict} |")
    print()
    if silent:
        names = ", ".join(f"`{r[1]}`" for r in silent)
        print(f"🔴 **Молчат {len(silent)}:** {names} — выкачено и умерло незаметно. "
              f"Починить или снять; молчащий хук хуже отсутствующего, он создаёт "
              f"ложное чувство покрытия.")
        print()
    return 0


if __name__ == "__main__":
    if "--md" not in sys.argv:
        print("usage: signals.py --md", file=sys.stderr)
        sys.exit(2)
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
