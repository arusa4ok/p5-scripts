#!/usr/bin/env bash
# Spec 021 atom 1: build a throwaway A/B arm profile + sandbox repo.
#   ab-profile.sh <a|b> <scratch-dir>
# Creates ~/.claude-a<x> (CLAUDE_CONFIG_DIR) and <scratch>/ab-<x> (repo clone).
# Arm content is identical to live at build time; arm B is edited afterwards (T010+).
# A clone, not a worktree: worktrees share the main repo's config, so setting
# their origin would rewrite the live repo's remote.
set -euo pipefail

ARM=${1:?arm a|b}; SP=${2:?scratch dir}
[[ $ARM == a || $ARM == b ]] || { echo "arm must be a or b" >&2; exit 2; }
case $SP in "$HOME"/*) echo "scratch dir must be outside \$HOME (ancestor load)" >&2; exit 2;; esac

LIVE=$HOME/.claude
REPO=$HOME/projects/cc-stack-2026
MEM=$LIVE/projects/-Users-rusakovanton-projects-cc-stack-2026/memory
P=$HOME/.claude-a$ARM
WT=$SP/ab-$ARM
EV=$REPO/.specify/specs/021-context-compression/evidence

# 1. fresh profile (rm on the dir itself; symlink targets are never followed)
rm -rf "$P"
mkdir -p "$P/projects"

# read-mostly entries → symlinks; state that a run writes stays inside the arm
NOLINK=" CLAUDE.md rules on-demand-rules settings.json settings.local.json projects .git
 history.jsonl sessions session-env file-history debug todos shell-snapshots paste-cache
 statsig daemon daemon.lock daemon.log daemon.status.json .claude.json "
for e in "$LIVE"/* "$LIVE"/.[!.]*; do
  n=${e##*/}
  [[ $NOLINK == *" $n "* || $n == settings.json.* || $n == *.bak* ]] && continue
  ln -s "$e" "$P/$n"
done
cp "$LIVE/CLAUDE.md" "$P/"
cp -R "$LIVE/rules" "$LIVE/on-demand-rules" "$P/"

# 2. settings copy: hook allowlist, permission mode, Bash sandbox, arm memory
KEEP='pre-commit-guard|block-no-verify|test-on-commit-guard|rtk hook claude|ru-spelling-guard|opus-routing-guard|brief-contract-guard|code-review-commit-nudge|read-limit-guard|spec-start-gate|session-start\.sh|context-mode-cache-heal|news-digest-inject|handoff-inject|o2-compact-resume|claudemd-carry-inject|operating-loop-guard|skill-suggest|gbrain-preflight|gbrain-volunteer|decision-preflight|message-decompose-preflight|prior-art-preflight'
jq --arg keep "$KEEP" --arg mem "$P/memory-arm" '
  .hooks |= ( with_entries(select(.key|IN("PreToolUse","SessionStart","UserPromptSubmit")))
              | map_values( map(.hooks |= map(select(.command|test($keep))))
                            | map(select(.hooks|length>0)) ) )
  | .permissions.defaultMode = "default"
  | .sandbox = {enabled:true, failIfUnavailable:true, allowUnsandboxedCommands:false,
                network:{allowedDomains:[], strictAllowlist:true}}
  | .autoMemoryDirectory = $mem
  | del(.agentPushNotifEnabled, .preferredNotifChannel)
' "$LIVE/settings.json" > "$P/settings.json"
mkdir -p "$EV"
jq -r '.hooks|to_entries[]|.key as $e|.value[]|.hooks[]|"\($e)\t\(.command)"' "$P/settings.json" | sort > "$EV/hooks-kept.txt"
jq -r '.hooks|to_entries[]|.key as $e|.value[]|.hooks[]|"\($e)\t\(.command)"' "$LIVE/settings.json" | sort \
  | comm -23 - "$EV/hooks-kept.txt" > "$EV/hooks-dropped.txt"

# 3. .claude.json without account identity
jq 'del(.oauthAccount, .customApiKeyResponses)' "$HOME/.claude.json" > "$P/.claude.json"
chmod 600 "$P/.claude.json"

# 4. arm memory: copy + canary + pristine tarball (restored before every replay run)
cp -R "$MEM" "$P/memory-arm"
printf '\nab-canary: arm-%s\n' "$ARM" >> "$P/memory-arm/MEMORY.md"
tar -C "$P" -czf "$P/memory-arm.pristine.tgz" memory-arm

# 5. sandbox repo: clone at HEAD + live working-tree CLAUDE.md and local settings; origin → local bare
# origin lives inside the clone (excluded from git): the Bash sandbox only writes under cwd
rm -rf "$WT" "$SP/origin-$ARM.git"
git clone -q --shared "$REPO" "$WT"
git init -q --bare "$WT/.ab-origin.git"
echo ".ab-origin.git/" >> "$WT/.git/info/exclude"
git -C "$WT" remote set-url origin "$WT/.ab-origin.git"
git -C "$WT" push -q origin HEAD:refs/heads/master
cp "$REPO/CLAUDE.md" "$WT/CLAUDE.md"
cp "$REPO/.claude/settings.local.json" "$WT/.claude/settings.local.json"

# 6. PATH shim (shared by both arms)
mkdir -p "$SP/shim"
for t in gh curl wget ssh git-remote-https git-remote-http; do
  printf '#!/bin/sh\necho "AB-BLOCKED %s $*" >&2\nexit 1\n' "$t" > "$SP/shim/$t"
  chmod +x "$SP/shim/$t"
done

echo "arm $ARM: profile $P · repo $WT · hooks kept $(wc -l < "$EV/hooks-kept.txt") dropped $(wc -l < "$EV/hooks-dropped.txt")"
