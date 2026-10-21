#!/usr/bin/env bash
# spec 019 T020 — synthetic-envelope tests for ~/.claude/hooks/spec-start-gate.py (16 cases).
# Each run uses a throwaway HOME, so state/log/override files never touch the real ~/.claude.
# Usage: test_spec_start_gate.sh [path-to-hook]
set -u
HOOK="${1:-$HOME/.claude/hooks/spec-start-gate.py}"
CLI="$(cd "$(dirname "$0")/.." && pwd)/spec-gate.py"
T="$(mktemp -d "${TMPDIR:-/tmp}/ssg-test.XXXXXX")"
export HOME="$T/home"; mkdir -p "$HOME"
PASS=0; FAIL=0
LOG="$HOME/.claude/logs/spec-start-gate.jsonl"

ok()   { PASS=$((PASS+1)); echo "ok   $1"; }
bad()  { FAIL=$((FAIL+1)); echo "FAIL $1 — $2"; }

# hook <session> <file>  → prints hook stdout; exit code must always be 0
hook() {
  local out rc
  out=$(printf '{"session_id":"%s","tool_name":"Edit","tool_input":{"file_path":"%s"}}' "$1" "$2" | python3 "$HOOK")
  rc=$?
  [ $rc -eq 0 ] || echo "EXIT=$rc"
  printf '%s' "$out"
}
# repo <name> → scratch git repo with .specify/ committed
repo() {
  local r="$T/$1"; mkdir -p "$r/.specify/specs" "$r/src" "$r/docs"
  git -C "$r" init -q && git -C "$r" -c user.email=t@t -c user.name=t commit -q --allow-empty -m init
  touch "$r/.specify/.keep"; git -C "$r" add -A && git -C "$r" -c user.email=t@t -c user.name=t commit -q -m specify
  echo "$r"
}
spec() { # spec <repo> <tree> <name> <status>
  mkdir -p "$1/$2/$3"; printf '# Spec\n\n**Status**: %s\n' "$4" > "$1/$2/$3/spec.md"
}
lastlog() { tail -1 "$LOG" 2>/dev/null; }

# 1 no .specify/ → pass silently
r=$T/plain; mkdir -p $r/src; git -C $r init -q
o=$(hook s1 $r/src/a.py; hook s1 $r/src/b.py)
[ -z "$o" ] && ok "1 no .specify" || bad "1 no .specify" "$o"

# 2 non-code file → pass
r=$(repo r2); o=$(hook s2 $r/a.md; hook s2 $r/b.md)
[ -z "$o" ] && ok "2 non-code" || bad "2 non-code" "$o"

# 3 first code file → pass
r=$(repo r3); o=$(hook s3 $r/src/a.py)
[ -z "$o" ] && ok "3 first code file" || bad "3 first code file" "$o"

# 4 second code file → warn JSON
o=$(hook s3 $r/src/b.ts)
echo "$o" | grep -q '"additionalContext"' && echo "$o" | grep -q 'spec-start-gate' && ok "4 warn" || bad "4 warn" "$o"

# 5 same, enforce → deny JSON
r=$(repo r5); hook s5 $r/src/a.py >/dev/null
o=$(SPEC_GATE_MODE=enforce hook s5 $r/src/b.py)
echo "$o" | grep -q '"permissionDecision": *"deny"' && ok "5 enforce deny" || bad "5 enforce deny" "$o"

# 6 feature.json → open spec touched today → pass (and timing)
r=$(repo r6); spec $r .specify/specs 001-x Draft
echo '{"feature_directory": ".specify/specs/001-x"}' > $r/.specify/feature.json
hook s6 $r/src/a.py >/dev/null
t0=$(python3 -c 'import time;print(time.time())')
o=$(hook s6 $r/src/b.py)
t1=$(python3 -c 'import time;print(time.time())')
[ -z "$o" ] && ok "6 active spec pass ($(python3 -c "print(int(($t1-$t0)*1000))") ms incl. python start)" || bad "6 active spec" "$o"

# 7 feature.json → closed spec → counts
r=$(repo r7); spec $r .specify/specs 001-x "Executed (2026-10-01)"
echo '{"feature_directory": ".specify/specs/001-x"}' > $r/.specify/feature.json
o=$(hook s7 $r/src/a.py; hook s7 $r/src/b.py)
echo "$o" | grep -q additionalContext && ok "7 closed spec counts" || bad "7 closed spec" "$o"

# 8 feature.json → open spec untouched 20 days → counts
r=$(repo r8); spec $r .specify/specs 001-x Draft
echo '{"feature_directory": ".specify/specs/001-x"}' > $r/.specify/feature.json
old=$(python3 -c 'import datetime as d;print((d.datetime.now()-d.timedelta(days=20)).strftime("%Y%m%d%H%M"))')
touch -t $old $r/.specify/specs/001-x/spec.md $r/.specify/specs/001-x
o=$(hook s8 $r/src/a.py; hook s8 $r/src/b.py)
echo "$o" | grep -q additionalContext && ok "8 stale spec counts" || bad "8 stale spec" "$o"

# 9 feature.json → missing dir → falls back to tree scan (open spec present → pass)
r=$(repo r9); spec $r .specify/specs 002-y Draft
echo '{"feature_directory": ".specify/specs/001-gone"}' > $r/.specify/feature.json
o=$(hook s9 $r/src/a.py; hook s9 $r/src/b.py)
[ -z "$o" ] && ok "9 missing dir → tree scan" || bad "9 missing dir" "$o"

# 10 no feature.json + open spec in root specs/ → pass
r=$(repo r10); spec $r specs 004-z Draft
o=$(hook s10 $r/src/a.py; hook s10 $r/src/b.py)
[ -z "$o" ] && ok "10 root specs/ tree" || bad "10 root specs/" "$o"

# 11 exempt dir → pass
r=$(repo r11); o=$(hook s11 $r/docs/a.py; hook s11 $r/p5-loop/b.py; hook s11 $r/node_modules/x/c.js)
[ -z "$o" ] && ok "11 exempt dirs" || bad "11 exempt dirs" "$o"

# 12 override active → pass + override logged
r=$(repo r12)
python3 "$CLI" override --repo "$r" --reason "test emergency" >/dev/null
o=$(SPEC_GATE_MODE=enforce hook s12 $r/src/a.py; SPEC_GATE_MODE=enforce hook s12 $r/src/b.py)
[ -z "$o" ] && grep -q '"outcome": *"override"' "$LOG" && ok "12 override" || bad "12 override" "$o / $(lastlog)"

# 13 override with empty reason → exit 2
python3 "$CLI" override --repo "$r" --reason "" >/dev/null 2>&1; rc=$?
[ $rc -eq 2 ] && ok "13 empty reason exit 2" || bad "13 empty reason" "rc=$rc"

# 14 SWARM_WORKER=1 → skip
r=$(repo r14); o=$(SWARM_WORKER=1 hook s14 $r/src/a.py; SWARM_WORKER=1 SPEC_GATE_MODE=enforce hook s14 $r/src/b.py)
[ -z "$o" ] && ok "14 swarm worker skip" || bad "14 swarm worker" "$o"

# 15 file inside a git worktree of a gated repo → gated
r=$(repo r15); git -C $r worktree add -q $T/r15-wt 2>/dev/null
o=$(hook s15 $T/r15-wt/src/a.py; hook s15 $T/r15-wt/src/b.py)
echo "$o" | grep -q additionalContext && ok "15 worktree gated" || bad "15 worktree" "$o"

# 16 malformed stdin → exit 0, error logged
o=$(echo 'not json' | python3 "$HOOK"); rc=$?
[ $rc -eq 0 ] && [ -z "$o" ] && grep -q '"outcome": *"error"' "$LOG" && ok "16 malformed stdin" || bad "16 malformed" "rc=$rc out=$o"

echo "---"; echo "$PASS/16 passed, $FAIL failed"
rm -rf "$T"
[ $FAIL -eq 0 ] && [ $PASS -eq 16 ]
