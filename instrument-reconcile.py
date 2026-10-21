#!/usr/bin/env python3
"""instrument-reconcile (spec 006, US1 report-only MVP) — the GENERAL forcing function.

The dual of `signals`/`instrument-coverage`: instead of "does this log get read?", it asks
"is every behavioural SURFACE that emits a signal actually REGISTERED with a verdict?" — and
"does a registered feature's yardstick name a dimension its log never records?".

US1 = report ONLY (no must-act write). The point is to SEE the real gap list and judge whether
it's signal or noise BEFORE wiring the enforcement (US2). Deterministic, $0, read-only.

Two axes, both would have caught the 2026-07-21 web-research ladder-skip:
  A. log stream in ~/.claude/logs/*.jsonl that no feature-tracking row names  → active-but-unregistered
  B. feature-tracking row whose yardstick mentions a metric NOT present as a field in its log
     → registered-but-yardstick-unbacked (the exact web-research shape)

Scope note (US1): log-stream axis first (highest signal, tightest — a stream means real activity).
Skill/hook/agent-on-disk enumeration is US2+ (looser: not every skill needs a verdict row).
"""
import json
import os
import re
import glob

HOME = os.path.expanduser("~")
LOGS = os.path.join(HOME, ".claude/logs")
FT = os.path.join(HOME, "projects/cc-stack-2026/p5-loop/feature-tracking.md")

# Streams that are raw data / not a behavioural-feature verdict surface — not a gap if unregistered.
# (Same spirit as capability-audit's EXEMPT: decisions, not oversights.)
EXEMPT_STREAMS = {
    "english-coach", "coach-turns", "coach-events",   # personal coaching telemetry
    "decision-pairs", "skill-gaps",                    # capture logs, not features
}


def _mentions(text, name):
    """True if `name` appears in `text` as a whole token — not as a substring of a longer
    stream name. Fixes 'research-runs' matching inside 'web-research-runs', and keeps a stream
    from reading as registered just because its name is a fragment of another registered one."""
    return re.search(r"(?<![a-z0-9-])" + re.escape(name) + r"(?![a-z0-9-])", text) is not None


def registry_text():
    try:
        with open(FT) as f:
            return f.read()
    except OSError:
        return ""


def log_streams():
    """Every ~/.claude/logs/*.jsonl basename (the stream name)."""
    out = {}
    for fp in glob.glob(os.path.join(LOGS, "*.jsonl")):
        name = os.path.basename(fp)[:-6]  # strip .jsonl
        try:
            n = sum(1 for _ in open(fp))
        except OSError:
            n = 0
        out[name] = {"path": fp, "lines": n}
    return out


def log_fields(fp):
    """Union of top-level JSON keys across the first/last few lines of a log."""
    keys = set()
    try:
        lines = open(fp).readlines()
    except OSError:
        return keys
    for ln in (lines[:3] + lines[-3:]):
        try:
            keys.update(json.loads(ln).keys())
        except Exception:
            continue
    return keys


def compute():
    reg = registry_text()
    reg_lc = reg.lower()
    streams = log_streams()

    # Axis A — active (has lines) but unregistered.
    unregistered = []
    for name, meta in sorted(streams.items()):
        if meta["lines"] == 0:
            continue
        base = name.replace("-latest", "")
        if name in EXEMPT_STREAMS or base in EXEMPT_STREAMS:
            continue
        # registered if the stream name (or its base) appears as a whole token in feature-tracking
        if _mentions(reg_lc, name.lower()) or _mentions(reg_lc, base.lower()):
            continue
        unregistered.append((name, meta["lines"]))

    # Axis B — registered rows whose yardstick names a metric absent from the log's fields.
    # Heuristic MVP: for each `.jsonl` named in the registry, pull the row's line, extract words
    # that look like metric names, and flag if none appear as log fields. Report for eyeball, not act.
    unbacked = []
    for name, meta in streams.items():
        if not _mentions(reg_lc, name.lower()):
            continue
        # find the registry line mentioning this stream as a whole token (not a substring)
        row = next((l for l in reg.splitlines() if _mentions(l.lower(), name.lower())), "")
        if not row:
            continue
        fields = {f.lower() for f in log_fields(meta["path"])}
        # metrics the yardstick names: compound tokens ending in -rate/-ratio/-count with a REQUIRED
        # `-`/`_` before the suffix, so "duration"/"decoration"/"proliferation" (…ratio…) don't match.
        yard = row.lower()
        mentions = set(re.findall(r"[a-z][a-z_-]*[-_](?:rate|ratio|count)\b", yard))
        # a mention is "backed" if its stem shows up in the log fields
        missing = [m for m in mentions
                   if not any(m.split("-")[0].split("_")[0] in f for f in fields)]
        if missing:
            unbacked.append((name, sorted(set(missing)), sorted(fields)[:6]))

    return {"streams": len(streams), "registry": bool(reg),
            "unregistered": unregistered, "unbacked": unbacked}


# CONFIRMED hard Axis-B gaps: the log physically cannot produce the yardstick AND no companion
# stream backs it. Only these drive must-act (US2). Empty as of spec 006 US4 (2026-07-21):
# web-research-runs' escalation-rate/ladder-skip-rate are now backed by the web-research-fetch-path
# companion stream (`fetch_path_log.rates()`), the same way block-no-verify-outcome backs verify-gate.
# The remaining Axis-B hits are soft — hand-computed-at-weekly (kept-rate/promotion-rate/hit-rate,
# real data exists just not as a literal field) or a heuristic false-positive — advisory, not enforced.
CONFIRMED_UNBACKED = set()


def render(d):
    print("🔗 instrument-reconcile (spec 006, report) —",
          f"{d['streams']} log streams, registry={'found' if d['registry'] else 'MISSING'}\n")
    un, ub = d["unregistered"], d["unbacked"]
    print(f"== Axis A: active-but-UNREGISTERED ({len(un)}) ==")
    for name, n in un:
        print(f"  ⚠️  {name}  ({n} lines) — no feature-tracking row → step-4 owed")
    if not un:
        print("  ✅ every active log stream is named in the registry")
    print(f"\n== Axis B: registered-but-yardstick-UNBACKED ({len(ub)}) ==")
    for name, missing, fields in ub:
        hard = " [CONFIRMED-hard]" if name in CONFIRMED_UNBACKED else " (likely hand-computed — verify)"
        print(f"  ⚠️  {name}{hard} — yardstick names {missing}; log fields {fields}…")
    if not ub:
        print("  ✅ (heuristic) no registered yardstick references a missing log dimension")


def main():
    import sys
    d = compute()
    if "--json" in sys.argv:
        print(json.dumps({
            "streams": d["streams"],
            "unregistered": [{"stream": n, "lines": c} for n, c in d["unregistered"]],
            "unbacked": [{"stream": n, "missing": m} for n, m, _ in d["unbacked"]
                         if n in CONFIRMED_UNBACKED],
        }))
    else:
        render(d)


if __name__ == "__main__":
    main()
