# p5-scripts

Operational scripts from the "p5" consumer loop of a Claude Code stack — a solo
operator's setup where Claude maintains its own harness (rules, hooks, memory,
agents) and these scripts measure whether that maintenance works.

Highlights:

- `system-overview.py`, `report.py`, `stack-pulse.py` — weekly system reports, zero LLM tokens
- `thread.py` (+ `test_thread.py`) — CLI for an open-threads ledger that drives what gets worked on next
- `self-review-weekly.py`, `rules-hygiene.py`, `must-act.py`, `ledger-lint.py` — self-improvement loop
- `stack-watch.py` — version-drift watcher for installed tools
- `ab-instructions.py`, `agent-bench.py`, `research-bench.py`, `spec-gate*.py` — A/B and benchmark harnesses
- `INSTALL.md` — launchd cadence setup

Python 3.10+, stdlib-first; some scripts shell out to `gh`, `semgrep`, `gitleaks`.
Paths assume a macOS home layout — edit before reuse.

Secrets discipline: no credentials live in this repo. Scripts read keys at runtime
from `~/.config/secrets/secrets.env` or environment variables.

No license yet — all rights reserved unless stated otherwise.
