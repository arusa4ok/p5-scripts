#!/bin/bash
# gbrain-cutover.sh — one-shot night cut-over of production gbrain (~/gbrain-live) from the fork at
# 0.60.11 to merge/upstream-2026-10-11 (0.60.172.0). Anton's go 2026-10-11: Bun 1.4 yes, night window.
#
#   gbrain-cutover.sh --check   preflight only, changes nothing
#   gbrain-cutover.sh           full run (launchd one-shot, date-guarded); rolls back on any failure
#
# Order: preflight → pg_dump → stop writers → Bun 1.4.2 → promote code → migrations → restart → verify.
# Any failure after the dump: restore DB from the dump, old code, old Bun, restart, notify FAIL.
set -euo pipefail

export HOME=/Users/rusakovanton
# Daemon PATH: prepend, never append (memory: daemon-path-must-mirror-the-machine)
export PATH="/opt/homebrew/opt/postgresql@17/bin:/opt/homebrew/bin:$HOME/.bun/bin:/usr/bin:/bin:/usr/sbin:/sbin"

RUN_DATE="2026-10-12"
LIVE="$HOME/gbrain-live"
DEV="$HOME/gbrain"
BRANCH="merge/upstream-2026-10-11"
EXPECT_HEAD="cc032e113"
OLD_LIVE="fa70f6658"
DB="postgresql://127.0.0.1:5432/gbrain"
REFLEX_BUN="$HOME/Library/Application Support/reflex/bun/bin/bun"
REFLEX_GBRAIN="$HOME/Library/Application Support/reflex/bun/bin/gbrain"
HOME_BUN="$HOME/.bun/bin/bun"
NEW_BUN="$HOME/gbrain-backups/bun-1.4.2/bun"
BK="$HOME/gbrain-backups"
DUMP="$BK/gbrain-schema178-$RUN_DATE.dump"
LOG="$HOME/.claude/logs/gbrain-cutover-$RUN_DATE.log"
DONE="$BK/cutover-$RUN_DATE.done"
NOTIFY="$HOME/.claude/scripts/notify-anton.sh"
SELF_PLIST="$HOME/Library/LaunchAgents/com.anton.gbrain-cutover-once.plist"
JOBS=(com.anton.gbrain-autopilot com.anton.gbrain-mcp-http com.anton.gbrain-semantic-linker
      com.anton.gbrain-advisor-weekly com.anton.gbrain-check-update
      com.career-os.gbrain-sync com.career-os.gbrain-dream)

mkdir -p "$BK" "$(dirname "$LOG")"
log() { echo "$(date '+%F %T') $*" | tee -a "$LOG"; }
UID_=$(id -u)
STAGE=preflight
STOPPED=()

preflight() {
  [ "$(git -C "$DEV" rev-parse --short=9 "$BRANCH")" = "$EXPECT_HEAD" ] || { log "branch head moved"; return 1; }
  git -C "$DEV" merge-base --is-ancestor anton-local "$BRANCH" || { log "anton-local not ancestor"; return 1; }
  [ "$(git -C "$LIVE" rev-parse --short=9 HEAD)" = "$OLD_LIVE" ] || { log "gbrain-live not at $OLD_LIVE"; return 1; }
  [ -z "$(git -C "$LIVE" status --porcelain --untracked-files=no)" ] || { log "gbrain-live dirty"; return 1; }
  [ "$("$NEW_BUN" --version)" = "1.4.2" ] || { log "bun 1.4.2 binary missing"; return 1; }
  psql -Atc "select 1" "$DB" >/dev/null || { log "DB unreachable"; return 1; }
  free_gb=$(df -g "$HOME" | awk 'NR==2{print $4}')
  [ "$free_gb" -ge 15 ] || { log "only ${free_gb}G free"; return 1; }
  log "preflight ok (free ${free_gb}G)"
}

stop_jobs() {
  for j in "${JOBS[@]}"; do
    if launchctl print "gui/$UID_/$j" >/dev/null 2>&1; then
      launchctl bootout "gui/$UID_/$j" 2>/dev/null || true
      STOPPED+=("$j")
    fi
  done
  pkill -f "gbrain.*jobs work" || true
  pkill -f "gbrain.*serve" || true   # resident MCP serves in open sessions: /mcp reconnect in the morning
  sleep 5
  log "stopped: ${STOPPED[*]:-none}"
}

start_jobs() {
  for j in "${STOPPED[@]:-}"; do
    [ -n "$j" ] && launchctl bootstrap "gui/$UID_" "$HOME/Library/LaunchAgents/$j.plist" 2>/dev/null || true
  done
  log "restarted: ${STOPPED[*]:-none}"
}

zai_env() {  # same env as gbrain-autopilot-run.sh — never metered Anthropic
  local k; k=$(grep '^ZAI_API_KEY=' "$HOME/.config/secrets/secrets.env" | cut -d= -f2- | tr -d "'\"")
  [ -n "$k" ] || { log "ZAI_API_KEY missing"; return 1; }
  export ANTHROPIC_API_KEY="$k" ANTHROPIC_BASE_URL="https://api.z.ai/api/anthropic"
  export VOYAGE_API_KEY="$(grep '^VOYAGE_API_KEY=' "$HOME/.config/secrets/secrets.env" | cut -d= -f2- | tr -d "'\"")"
}

rollback() {
  log "ROLLBACK from stage $STAGE"
  set +e
  stop_jobs
  if [ "$STAGE" = migrate ] || [ "$STAGE" = restart ] || [ "$STAGE" = verify ]; then
    pg_restore --clean --if-exists --no-owner -d "$DB" "$DUMP" >>"$LOG" 2>&1 && log "DB restored from dump" || log "DB RESTORE FAILED — manual"
  fi
  [ -f "$BK/bun-1.3.13-home" ] && cp "$BK/bun-1.3.13-home" "$HOME_BUN"
  [ -f "$BK/bun-1.3.13-reflex" ] && cp "$BK/bun-1.3.13-reflex" "$REFLEX_BUN"
  if [ "$(git -C "$LIVE" rev-parse --short=9 HEAD)" != "$OLD_LIVE" ]; then
    git -C "$LIVE" checkout -q --detach "$OLD_LIVE" && (cd "$LIVE" && "$HOME_BUN" install --ignore-scripts >>"$LOG" 2>&1)
  fi
  start_jobs
  "$NOTIFY" "gbrain cut-over FAILED" "Stage $STAGE failed; rolled back to 0.60.11. Log: $LOG" || true
  finish
  exit 1
}

finish() {  # one-shot: never fire again
  touch "$DONE"
  launchctl bootout "gui/$UID_/com.anton.gbrain-cutover-once" 2>/dev/null || true
  [ -f "$SELF_PLIST" ] && mv "$SELF_PLIST" "$SELF_PLIST.done" || true
}

if [ "${1:-}" = "--check" ]; then preflight; exit $?; fi
[ "$(date +%F)" = "$RUN_DATE" ] || { log "not $RUN_DATE — skip"; exit 0; }
[ ! -f "$DONE" ] || { log "already done"; exit 0; }

log "=== cut-over start"
preflight || { "$NOTIFY" "gbrain cut-over skipped" "Preflight failed, nothing changed. Log: $LOG" || true; finish; exit 1; }

STAGE=dump
git -C "$DEV" tag -f pre-upgrade-2026-10-11 anton-local >/dev/null
pg_dump -Fc -f "$DUMP" "$DB"
[ "$(pg_restore -l "$DUMP" | wc -l)" -gt 50 ] || { log "dump looks empty"; finish; exit 1; }
log "dump ok $(du -h "$DUMP" | cut -f1)"
trap rollback ERR

STAGE=stop;    stop_jobs
STAGE=bun
cp "$HOME_BUN" "$BK/bun-1.3.13-home"; cp "$REFLEX_BUN" "$BK/bun-1.3.13-reflex"
cp "$NEW_BUN" "$HOME_BUN"; cp "$NEW_BUN" "$REFLEX_BUN"
[ "$("$HOME_BUN" --version)" = "1.4.2" ] && [ "$("$REFLEX_BUN" --version)" = "1.4.2" ]
log "bun 1.4.2 on both runtimes"

STAGE=promote
git -C "$DEV" merge -q --ff-only "$BRANCH"
git -C "$LIVE" fetch -q "$DEV" anton-local
git -C "$LIVE" checkout -q --detach FETCH_HEAD
log "gbrain-live at $(git -C "$LIVE" rev-parse --short=9 HEAD)"

STAGE=migrate
zai_env
(cd "$LIVE" && "$HOME_BUN" install >>"$LOG" 2>&1)    # postinstall applies v179–v237 + v0.60.31 re-chunk
"$REFLEX_GBRAIN" post-upgrade >>"$LOG" 2>&1
"$REFLEX_GBRAIN" doctor >>"$LOG" 2>&1 || log "doctor non-zero (see log) — continuing to functional verify"
log "migrations applied; version $("$REFLEX_GBRAIN" --version 2>/dev/null | head -1)"

STAGE=restart
before=$(cat "$HOME"/.gbrain/autopilot.{log,err} 2>/dev/null | grep -c api.anthropic.com || true)
start_jobs
sleep 60

STAGE=verify
"$REFLEX_GBRAIN" entity "Razalia" --source people 2>&1 | tee -a "$LOG" | grep -q "people/razalia"
python3 -m pytest -q "$HOME/projects/cc-stack-2026/tests/p5/test_gbrain_mcp_source.py" >>"$LOG" 2>&1
after=$(cat "$HOME"/.gbrain/autopilot.{log,err} 2>/dev/null | grep -c api.anthropic.com || true)
[ "$after" -eq "$before" ] || { log "new api.anthropic.com lines: $before -> $after"; false; }

trap - ERR
log "=== cut-over OK"
"$NOTIFY" "gbrain cut-over OK" "0.60.11 -> 0.60.172 on Bun 1.4.2; migrations applied; entity+MCP verified. /mcp reconnect in open sessions." || true
finish
