#!/usr/bin/env python3
"""Spec 016 T008 — ledger-lint.py fixtures (FR-010/FR-011).

Run: python3 -m pytest scripts/p5/test_ledger_lint.py -q
"""
import importlib.util
import io
import json
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location("ledger_lint", Path(__file__).parent / "ledger-lint.py")
ll = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ll)

FIXTURE = """# fixture ledger
## ⏳ Waiting on Anton
- [demo] id: `ot-aaaa0001` · **waits on parent** · status: `waiting` · dependencies: `ot-parent01`
- [demo] id: `ot-aaaa0002` · **waits on Anton** · status: `waiting` · waiting-on: `anton`
- [demo] id: `ot-aaaa0003` · **unexplained wait** · status: `waiting`
- [demo] id: `ot-aaaa0004` · **empty deps do not explain** · status: `waiting` · dependencies: `—`
- [demo] id: `ot-aaaa0005` · **old token** · status: `waiting-anton`
- [demo] id: `ot-aaaa0006` · **no field at all**
## 🔧 In work
- [demo] id: `ot-bbbb0001` · **fine** · status: `in-work`
- [demo] id: `ot-bbbb0002` · **synonym** · status: `active`
"""


def _ledger(tmp_path):
    p = tmp_path / "open-threads.md"
    p.write_text(FIXTURE, encoding="utf-8")
    return p


def test_lint_counts(tmp_path):
    res = ll.lint(_ledger(tmp_path))
    assert res["rows"] == 8 and res["waiting"] == 4
    assert res["counts"] == {"waiting-unexplained": 2, "off-vocab": 2, "no-status": 1}
    ids = {v["id"] for v in res["violations"]}
    assert ids == {"ot-aaaa0003", "ot-aaaa0004", "ot-aaaa0005", "ot-aaaa0006", "ot-bbbb0002"}


def test_sweep_writes_proposal_and_leaves_ledger(tmp_path):
    p = _ledger(tmp_path)
    out, _ = ll.sweep(p, tmp_path / "proposal.md")
    text = out.read_text(encoding="utf-8")
    assert "PROPOSAL — not applied, Anton decides" in text
    assert "`ot-bbbb0002`" in text and "→ `in-work`" in text
    assert "waiting-on: anton" in text
    assert p.read_text(encoding="utf-8") == FIXTURE


def _run_hook(monkeypatch, tmp_path, payload):
    monkeypatch.setattr(ll, "LOGS", tmp_path / "logs")
    monkeypatch.setattr(ll, "CACHE", tmp_path / "logs" / "last.json")
    monkeypatch.setattr(ll, "ERRLOG", tmp_path / "logs" / "err.log")
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert ll.main(["--hook"]) == 0
    return out.getvalue()


def test_hook_speaks_only_on_increase(monkeypatch, tmp_path):
    p = _ledger(tmp_path)
    payload = json.dumps({"tool_input": {"file_path": str(p)}})
    first = _run_hook(monkeypatch, tmp_path, payload)
    assert json.loads(first)["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert _run_hook(monkeypatch, tmp_path, payload) == ""          # same count -> silent
    p.write_text(FIXTURE + "- [demo] id: `ot-cccc0001` · **new** · status: `waiting`\n", encoding="utf-8")
    assert "6 violations" in _run_hook(monkeypatch, tmp_path, payload)


def test_hook_ignores_other_files_and_fails_open(monkeypatch, tmp_path):
    assert _run_hook(monkeypatch, tmp_path, json.dumps({"tool_input": {"file_path": "/x/a.md"}})) == ""
    assert _run_hook(monkeypatch, tmp_path, "not json") == ""
    assert "Traceback" in (tmp_path / "logs" / "err.log").read_text()
