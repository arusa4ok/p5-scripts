#!/usr/bin/env bash
# Spec 021 T012: overlay the staged arm-B content onto ~/.claude-ab and <scratch>/ab-b.
#   ab-overlay.sh <scratch-dir>      (run after ab-profile.sh b and ab-stage.py)
# Staged files carry final ~/.claude paths; the arm copy points them at ~/.claude-ab so pointers resolve inside the arm.
set -euo pipefail
SP=${1:?scratch dir}
P=$HOME/.claude-ab
ST=$HOME/projects/cc-stack-2026/.specify/specs/021-context-compression/evidence/staging
arm() { sed 's#~/\.claude/on-demand-rules#~/.claude-ab/on-demand-rules#g' "$1"; }

arm "$ST/global/CLAUDE.md" > "$P/CLAUDE.md"
rm -rf "$P/rules" && mkdir "$P/rules"
arm "$ST/global/rules/core.md" > "$P/rules/core.md"
cp "$ST"/on-demand-rules/*.md "$P/on-demand-rules/"
cp "$ST/memory/MEMORY.md" "$P/memory-arm/MEMORY.md"
printf '\nab-canary: arm-b\n' >> "$P/memory-arm/MEMORY.md"
tar -C "$P" -czf "$P/memory-arm.pristine.tgz" memory-arm
cp "$ST/cc-stack/CLAUDE.md" "$SP/ab-b/CLAUDE.md"
mkdir -p "$SP/ab-b/p2-research"
cp "$ST/cc-stack/cc-stack-claude-md-2026-10-21.md" "$SP/ab-b/p2-research/"
echo "arm b overlaid"
