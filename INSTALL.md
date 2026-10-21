# P5 cadence — launchd install (Anton runs these)

Claude never edits system config. These are the steps to wire the P5 loop to local cron (launchd).
Two agents: **weekly retro** (Mon 10:00) and **monthly /insights reminder** (1st, 10:00). Both zero-token.

> Why local launchd and not a remote `/schedule` agent: the metric reads a **local** file
> (`~/.claude/logs/opus-blocks.jsonl`); remote agents run on Anthropic infra and can't see the Mac FS.

Python path is pinned to `/opt/homebrew/bin/python3` (3.14) — the script uses 3.10+ syntax, so the
system `/usr/bin/python3` (3.9) would fail. Verify with `which python3` before installing.

---

## 1. Weekly retro — `~/Library/LaunchAgents/com.anton.cc-stack-p5-weekly.plist`

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.anton.cc-stack-p5-weekly</string>
  <key>ProgramArguments</key>
  <array>
    <string>/opt/homebrew/bin/python3</string>
    <string>/Users/rusakovanton/projects/cc-stack-2026/scripts/p5/weekly-retro.py</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Weekday</key><integer>1</integer>  <!-- Monday -->
    <key>Hour</key><integer>10</integer>
    <key>Minute</key><integer>0</integer>
  </dict>
  <key>StandardOutPath</key><string>/Users/rusakovanton/.claude/logs/p5-weekly.out.log</string>
  <key>StandardErrorPath</key><string>/Users/rusakovanton/.claude/logs/p5-weekly.err.log</string>
</dict>
</plist>
```

## 2. Monthly /insights reminder — `~/Library/LaunchAgents/com.anton.cc-stack-p5-monthly.plist`

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.anton.cc-stack-p5-monthly</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/osascript</string>
    <string>-e</string>
    <string>display notification "Run /insights in Claude Code today (monthly friction-mining)" with title "cc-stack P5"</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Day</key><integer>1</integer>
    <key>Hour</key><integer>10</integer>
    <key>Minute</key><integer>0</integer>
  </dict>
</dict>
</plist>
```

## 3. Load both

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.anton.cc-stack-p5-weekly.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.anton.cc-stack-p5-monthly.plist
# verify:
launchctl list | grep cc-stack-p5
```

(Older macOS fallback: `launchctl load ~/Library/LaunchAgents/com.anton.cc-stack-p5-*.plist`.)

## Test without waiting for the schedule

```bash
launchctl kickstart -k gui/$(id -u)/com.anton.cc-stack-p5-weekly   # runs the weekly job now
cat ~/.claude/logs/p5-weekly.err.log                                # check for errors
```

## Uninstall / kill switch

```bash
launchctl bootout gui/$(id -u)/com.anton.cc-stack-p5-weekly
launchctl bootout gui/$(id -u)/com.anton.cc-stack-p5-monthly
rm ~/Library/LaunchAgents/com.anton.cc-stack-p5-*.plist
```
