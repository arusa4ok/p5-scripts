#!/bin/bash
# RETIRED 2026-09-02 — do NOT reuse this self-unload pattern. The migration finished
# 2026-08-26, but the "self-unloading" line below NEVER worked: `launchctl unload ... &`
# runs in the background, and when the main bash exits, launchd tears down remaining
# children before the unload completes — the job stayed loaded (209 runs) and fired
# the DONE notification every 15 min (200 identical ntfy pushes). Fixed by hand:
# `launchctl bootout gui/$(id -u)/<label>` from the PARENT shell + rm the plist.
# The notify channel now also has a 30-min exact-duplicate guard (notify-anton.sh).
#
# Migration self-retrier: every 15 min continue the voyage-4 migration until done.
# Survives the post-hard-kill embed lock (expires on its own in <=60 min) and the
# CC background-task reaper (launchd-owned, not task-owned). Self-unloads when the
# status reports no outstanding chunks.
export PATH="$HOME/.bun/bin:$HOME/.local/bin:$HOME/.npm-global/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export GBRAIN_EMBED_CONCURRENCY=1
set -a; . "$HOME/.config/secrets/secrets.env"; set +a
GB="$HOME/Library/Application Support/reflex/bun/bin/gbrain"
LOG="$HOME/.claude/logs/zembed-migrate-20260826.log"
echo "=== $(date -u +%FT%TZ) resume-retrier tick ===" >> "$LOG"
OUT=$("$GB" migrate embeddings --to voyage:voyage-4 --dim 1024 --batch-size 2000 --yes 2>&1)
RC=$?
echo "$OUT" | tail -5 >> "$LOG"
if [ $RC -eq 0 ] && ! echo "$OUT" | grep -q "not in the target embedding space"; then
  echo "=== $(date -u +%FT%TZ) resume-retrier: DONE, self-unloading ===" >> "$LOG"
  "$HOME/.claude/scripts/notify-anton.sh" "zembed → voyage-4: ГОТОВО" "Миграция завершена. Лог: zembed-migrate-20260826.log"
  /bin/launchctl unload "$HOME/Library/LaunchAgents/com.anton.gbrain-zembed-resume.plist" >> /dev/null 2>&1 &
fi
exit 0
