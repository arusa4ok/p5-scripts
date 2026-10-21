#!/usr/bin/env python3
"""Spec 014 T005 fixture test — weekly_health() explained/unexplained waiting (FR-002).

Run: python3 scripts/p5/test_weekly_health.py   (exit 0 = pass)
"""
import importlib.util
import sys
import tempfile
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "action_queue", Path(__file__).parent / "action-queue.py")
aq = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(aq)

FIXTURE = """# fixture ledger
- [demo] id: `ot-aaaa0001` · thread one waits on a parent · status: `waiting` · dependencies: `ot-parent01`
- [demo] id: `ot-aaaa0002` · thread two waits on Anton · status: `waiting` · waiting-on: `anton`
- [demo] id: `ot-aaaa0003` · thread three waits unexplained · status: `waiting`
- [demo] id: `ot-aaaa0004` · thread four also unexplained · status: `waiting`
- [demo] id: `ot-bbbb0001` · plain open row, no wait · status: `open`
"""


def main():
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
        f.write(FIXTURE)
        path = f.name
    aq.OPEN_THREADS = path
    h = aq.weekly_health()
    assert h["waiting"] == 4, h
    assert h["unexplained_waiting"] == 2, h
    assert sorted(h["violations"]) == ["ot-aaaa0003", "ot-aaaa0004"], h
    assert h["live_rot"] == 0, h
    Path(path).unlink()
    print("PASS: 4 waiting, 2 explained (dependencies/waiting-on), 2 unexplained, rot 0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
