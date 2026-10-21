"""Automation-candidate radar — score(task) = fires_per_week x median_duration_ms.

Ranks the biggest repeating time-sink across three logs:
  - ~/.claude/logs/skill-fires.jsonl       (frequency per skill, no duration)
  - ~/.claude/logs/routing-decisions.jsonl (frequency + bucket per agent-dispatch)
  - ~/.claude/logs/agent-usage.jsonl       (duration_ms per completed subagent)

Design doc: specs/012-cto-agent/p2-automation-radar-design.md SS6-8.
Stdlib only. Pure function, no printing, no side effects, fail-soft on bad input.
"""

import json
import statistics
import time
from datetime import datetime, timezone

SECONDS_PER_WEEK = 7 * 24 * 3600
MIN_WEEKS_SPAN = 1.0 / 7.0  # 1 day floor, avoids div0


def _parse_ts(value):
    """Parse an ISO8601 timestamp (with or without trailing Z) to epoch seconds.

    Returns None on any failure (fail-soft).
    """
    if not isinstance(value, str) or not value:
        return None
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def _read_jsonl(path):
    """Read a JSONL file, skipping malformed lines. Missing file -> empty list."""
    rows = []
    if not path:
        return rows
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        return rows
    return rows


def _weeks_span(ts_values):
    """Compute (max-min)/SECONDS_PER_WEEK for a list of epoch-second floats.

    Floors at MIN_WEEKS_SPAN to avoid division by zero / near-zero spans.
    Empty input -> 0.0 (caller decides fallback).
    """
    if not ts_values:
        return 0.0
    span = (max(ts_values) - min(ts_values)) / SECONDS_PER_WEEK
    return max(span, MIN_WEEKS_SPAN)


def automation_radar(
    skill_fires_path,
    routing_path,
    agent_usage_path,
    *,
    now_ts=None,
    window_weeks=4,
    top_n=5,
):
    """Score recurring tasks by fires_per_week x median_duration_ms.

    Returns a dict:
      {
        "weeks_span": float,
        "weeks_of_duration_data": float,
        "warming_up": bool,
        "candidates": [ {task_key, fires_per_wk, median_ms, score,
                          n_dispatches, n_durations, duration_known: True}, ... ],
        "skill_only": [ {task_key, fires_per_wk, duration_known: False}, ... ],
      }

    Pure + deterministic given now_ts. Never raises — bad/missing files degrade
    to empty data, not a crash.
    """
    if now_ts is None:
        now_ts = time.time()

    skill_rows = _read_jsonl(skill_fires_path)
    routing_rows = _read_jsonl(routing_path)
    usage_rows = _read_jsonl(agent_usage_path)

    # attach parsed ts to every row up front (fail-soft: drop rows with bad ts)
    def _with_ts(rows):
        out = []
        for row in rows:
            ts = _parse_ts(row.get("ts"))
            if ts is None:
                continue
            out.append((ts, row))
        return out

    skill_ts = _with_ts(skill_rows)
    routing_ts = _with_ts(routing_rows)
    usage_ts = _with_ts(usage_rows)

    all_ts = [t for t, _ in skill_ts] + [t for t, _ in routing_ts] + [t for t, _ in usage_ts]
    weeks_span = _weeks_span(all_ts) if all_ts else MIN_WEEKS_SPAN

    usage_only_ts = [t for t, _ in usage_ts]
    weeks_of_duration_data = _weeks_span(usage_only_ts) if len(usage_only_ts) >= 2 else 0.0

    warming_up = weeks_of_duration_data < window_weeks

    # index routing decisions by (session_id, subagent_type) -> sorted list of (ts, bucket)
    routing_index = {}
    for ts, row in routing_ts:
        session_id = row.get("session_id")
        subagent_type = row.get("subagent_type")
        bucket = row.get("bucket")
        if session_id is None or subagent_type is None or bucket is None:
            continue
        key = (session_id, subagent_type)
        routing_index.setdefault(key, []).append((ts, bucket))
    for key in routing_index:
        routing_index[key].sort(key=lambda pair: pair[0])

    # count routing-decision rows per bucket (for fires_per_wk of candidates)
    routing_bucket_counts = {}
    for _, row in routing_ts:
        bucket = row.get("bucket")
        if bucket is None:
            continue
        routing_bucket_counts[bucket] = routing_bucket_counts.get(bucket, 0) + 1

    # join: for each agent-usage row, resolve its bucket
    durations_by_bucket = {}
    for ts, row in usage_ts:
        duration_ms = row.get("duration_ms")
        if not isinstance(duration_ms, (int, float)):
            continue
        agent_type = row.get("agent_type")
        session_id = row.get("session_id")
        bucket = None
        candidates = routing_index.get((session_id, agent_type), [])
        if candidates:
            # nearest in time
            bucket = min(candidates, key=lambda pair: abs(pair[0] - ts))[1]
        if bucket is None:
            bucket = agent_type
        if bucket is None:
            continue
        durations_by_bucket.setdefault(bucket, []).append(duration_ms)

    candidates_list = []
    for bucket, durations in durations_by_bucket.items():
        n_durations = len(durations)
        median_ms = statistics.median(durations)
        n_dispatches = routing_bucket_counts.get(bucket, 0)
        fires_per_wk = n_dispatches / weeks_span
        score = fires_per_wk * median_ms
        candidates_list.append(
            {
                "task_key": bucket,
                "fires_per_wk": fires_per_wk,
                "median_ms": median_ms,
                "score": score,
                "n_dispatches": n_dispatches,
                "n_durations": n_durations,
                "duration_known": True,
            }
        )
    candidates_list.sort(key=lambda c: c["score"], reverse=True)
    candidates_list = candidates_list[:top_n]

    skill_counts = {}
    for _, row in skill_ts:
        skill = row.get("skill")
        if skill is None:
            continue
        skill_counts[skill] = skill_counts.get(skill, 0) + 1

    skill_only_list = [
        {
            "task_key": skill,
            "fires_per_wk": count / weeks_span,
            "duration_known": False,
        }
        for skill, count in skill_counts.items()
    ]
    skill_only_list.sort(key=lambda s: s["fires_per_wk"], reverse=True)
    skill_only_list = skill_only_list[:top_n]

    return {
        "weeks_span": weeks_span,
        "weeks_of_duration_data": weeks_of_duration_data,
        "warming_up": warming_up,
        "candidates": candidates_list,
        "skill_only": skill_only_list,
    }
