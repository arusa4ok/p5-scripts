#!/bin/bash
# One-shot night migration gbrain: zembed-1 -> voyage-4. Run by launchd 2026-08-26 00:25 local.
# Plan ratified by Anton 2026-08-25 ("сегодня ночью после 12 по Дубаю"). Runbook:
# p5-loop/runbooks/zembed-migration-2026-09-01.md. Log-everything, notify Anton on completion.
set -uo pipefail
LOG="$HOME/.claude/logs/zembed-migrate-20260826.log"
# launchd PATH must MIRROR THE MACHINE (memory: daemon-path-must-mirror-the-machine).
# Attempt 3 died in 6 seconds: rc=127, "env: bun: No such file or directory" — the
# gbrain shim's shebang is #!/usr/bin/env bun, and bun lives in ~/.bun/bin, which a
# bare launchd PATH does not carry. Prepend, never append (append once handed workers
# Apple's python instead of homebrew's).
export PATH="$HOME/.bun/bin:$HOME/.local/bin:$HOME/.npm-global/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
GB="$HOME/Library/Application Support/reflex/bun/bin/gbrain"
NOTIFY="$HOME/.claude/scripts/notify-anton.sh"
exec >>"$LOG" 2>&1
echo "=== $(date -u +%FT%TZ) night migration start ==="
# set -a turns every assignment in secrets.env into an EXPORTED variable.
# Without it the key is a shell-local variable and the child gbrain process
# never sees it — attempt 2 died exactly there ("Voyage AI embedding requires
# VOYAGE_API_KEY", 2026-08-26 20:26 UTC, zero changes made). The file has no
# export prefixes by design; this is where they come from.
set -a
. "$HOME/.config/secrets/secrets.env"
set +a
[ -n "${VOYAGE_API_KEY:-}" ] || { echo "FATAL: no VOYAGE_API_KEY"; "$NOTIFY" "zembed миграция: НЕТ КЛЮЧА" "Сорвалось — VOYAGE_API_KEY не найден"; exit 1; }

echo "--- stop autopilot worker (dry-run warned: live worker writes count stale) ---"
launchctl unload "$HOME/Library/LaunchAgents/com.anton.gbrain-autopilot.plist" 2>&1 || true
sleep 5

echo "--- live migration (resumable) ---"
# --yes is REQUIRED in non-interactive contexts: without it the command prints the
# confirm prompt, gets no TTY answer and exits rc=2 (docs/guides/embedding-migration.md).
# Caught in the pre-flight doc audit 2026-08-25 — without this flag tonight's run
# would have failed at 00:25 with an honest "СБОЙ rc=2" notification.
"$GB" migrate embeddings --to voyage:voyage-4 --dim 1024 --yes 2>&1
RC=$?
echo "migrate rc=$RC"

echo "--- post status ---"
"$GB" migrate embeddings --status 2>&1 | tail -8
echo "--- doctor ---"
"$GB" doctor 2>&1 | tail -6

echo "--- facts re-embed pass (guide: fact embeddings rewrite on next write or an extract pass) ---"
"$GB" extract --stale 2>&1 | tail -4 || true

echo "--- restart autopilot ---"
launchctl load "$HOME/Library/LaunchAgents/com.anton.gbrain-autopilot.plist" 2>&1 || true

if [ $RC -eq 0 ]; then
  "$NOTIFY" "zembed → voyage-4: ГОТОВО" "Миграция прошла. Лог: ~/.claude/logs/zembed-migrate-20260826.log"
else
  "$NOTIFY" "zembed → voyage-4: СБОЙ rc=$RC" "Миграция прервана (resumable — можно продолжить). Лог: zembed-migrate-20260826.log"
fi
echo "=== $(date -u +%FT%TZ) done rc=$RC ==="
# Self-disable LAST. It was FIRST in attempt 1 and killed the run: launchctl
# unload terminates the job's own running process too, so the script SIGTERMed
# itself one line after start (log 2026-08-26: start line, then silence).
# At the end it is safe. If the script dies before this line, tomorrow's 00:25
# re-run is harmless by design: a finished migration reports nothing-to-migrate,
# an in-flight one resumes where it stopped.
launchctl unload "$HOME/Library/LaunchAgents/com.anton.gbrain-zembed-migrate.plist" 2>/dev/null || true
