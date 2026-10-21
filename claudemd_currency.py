#!/usr/bin/env python3
"""CLAUDE.md currency (staleness + bloat) — pure function, no printing, no side effects
beyond a read-only `git log` per file.

Design: .specify/specs/002-claudemd-currency/spec.md §5.3 (FR-005/FR-006).
Pure function only — wiring into report.py's weekly section happens separately.
"""
import os
import subprocess
import time

VERDICT_EMOJI = {"fresh": "✅", "aging": "👀", "stale": "⚠️", "missing": "⚠️"}


def _git_last_commit_ts(path):
    """Return the last-commit epoch seconds for `path` via git, or None if unavailable."""
    d = os.path.dirname(path) or "."
    b = os.path.basename(path)
    try:
        r = subprocess.run(
            ["git", "-C", d, "log", "-1", "--format=%ct", "--", b],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0 and r.stdout.strip():
            return int(r.stdout.strip())
    except Exception:
        pass
    return None


def claudemd_currency(claude_md_paths, *, now_ts=None, staleness_days=(30, 60),
                       bloat_lines=200, bloat_tokens=1500):
    """Compute a per-project CLAUDE.md currency report.

    Returns a list of dicts, one per input path:
      {project, path, exists, days_since_edit, staleness_verdict, lines,
       tokens_est, bloat_over, verdict_emoji, doctor_flag}
    """
    now = now_ts if now_ts is not None else time.time()
    fresh_d, stale_d = staleness_days
    rows = []

    for path in claude_md_paths:
        project = os.path.basename(os.path.dirname(path)) or path
        row = {
            "project": project,
            "path": path,
            "exists": False,
            "days_since_edit": None,
            "staleness_verdict": "missing",
            "lines": None,
            "tokens_est": None,
            "bloat_over": None,
            "verdict_emoji": VERDICT_EMOJI["missing"],
            "doctor_flag": False,
        }
        try:
            if not os.path.isfile(path):
                rows.append(row)
                continue

            row["exists"] = True

            last_ts = _git_last_commit_ts(path)
            if last_ts is None:
                try:
                    last_ts = os.path.getmtime(path)
                except OSError:
                    last_ts = now

            days = max(0.0, (now - last_ts) / 86400.0)
            row["days_since_edit"] = days

            if days < fresh_d:
                verdict = "fresh"
            elif days < stale_d:
                verdict = "aging"
            else:
                verdict = "stale"
            row["staleness_verdict"] = verdict
            row["verdict_emoji"] = VERDICT_EMOJI[verdict]

            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
            lines = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
            tokens_est = len(text) // 4
            row["lines"] = lines
            row["tokens_est"] = tokens_est
            bloat_over = lines > bloat_lines or tokens_est > bloat_tokens
            row["bloat_over"] = bloat_over
            row["doctor_flag"] = bloat_over

        except Exception:
            # Fail-soft: never raise out of the function. Surface as missing.
            row["exists"] = os.path.isfile(path) if _safe_isfile(path) else False
            row["staleness_verdict"] = row["staleness_verdict"] or "missing"
            row["verdict_emoji"] = VERDICT_EMOJI.get(row["staleness_verdict"], "⚠️")

        rows.append(row)

    return rows


def _safe_isfile(path):
    try:
        return os.path.isfile(path)
    except Exception:
        return False


if __name__ == "__main__":
    import sys
    paths = sys.argv[1:] or [os.path.expanduser("~/.claude/CLAUDE.md")]
    for r in claudemd_currency(paths):
        print(r)
