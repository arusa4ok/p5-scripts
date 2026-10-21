import json, subprocess, sys, pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/p5/tool-verdict.py"


def _run(logs_dir, *extra):
    out = subprocess.run(
        [sys.executable, str(SCRIPT), "--logs", str(logs_dir), "--json",
         "--window-days", "0", *extra],
        capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def _write(logs_dir, name, rows):
    (logs_dir / name).write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def _fixture(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    rows = []
    # tool A: 8 used, 2 wasted -> precision 0.8 -> ✅
    rows += [{"ts": "2026-07-19T10:00:00Z", "tool": "A", "tool_class": "skill", "outcome": "used"}] * 8
    rows += [{"ts": "2026-07-19T10:00:00Z", "tool": "A", "tool_class": "skill", "outcome": "redone"}] * 2
    # tool B: 1 acted, 9 ignored -> precision 0.1, 10 fires -> ⚠️
    rows += [{"ts": "2026-07-19T10:00:00Z", "tool": "B", "tool_class": "hook", "outcome": "acted"}] * 1
    rows += [{"ts": "2026-07-19T10:00:00Z", "tool": "B", "tool_class": "hook", "outcome": "ignored"}] * 9
    # tool C: 2 used, 1 redone -> 3 fires < MIN_FIRES -> 👀
    rows += [{"ts": "2026-07-19T10:00:00Z", "tool": "C", "tool_class": "skill", "outcome": "used"}] * 2
    rows += [{"ts": "2026-07-19T10:00:00Z", "tool": "C", "tool_class": "skill", "outcome": "redone"}] * 1
    _write(logs, "sample-outcome.jsonl", rows)
    return logs


def test_verdict_math(tmp_path):
    d = _run(_fixture(tmp_path))
    by = {t["tool"]: t for t in d["tools"]}
    assert by["A"]["verdict"] == "✅" and abs(by["A"]["precision"] - 0.8) < 1e-9
    assert by["B"]["verdict"] == "⚠️" and by["B"]["fires"] == 10
    assert by["C"]["verdict"] == "👀"   # below MIN_FIRES -> watch, never ⚠️


def test_only_outcome_files_read(tmp_path):
    logs = _fixture(tmp_path)
    # a non-outcome jsonl must be ignored entirely
    _write(logs, "unrelated.jsonl", [{"ts": "2026-07-19T10:00:00Z", "foo": "bar"}])
    d = _run(logs)
    assert {t["tool"] for t in d["tools"]} == {"A", "B", "C"}


def test_verdict_items_shape(tmp_path):
    # the must-act bridge: only ⚠️ tools become items, in finder_items() shape
    out = subprocess.run(
        [sys.executable, str(SCRIPT), "--logs", str(_fixture(tmp_path)),
         "--items", "--window-days", "0"],
        capture_output=True, text=True, check=True)
    items = json.loads(out.stdout)
    assert {it["id"] for it in items} == {"toolverdict:B"}
    for it in items:
        assert {"id", "what", "path", "since", "severity"} <= it.keys()
        assert it["severity"] in {"broken", "dead", "idle"}


def test_empty_logs_no_crash(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    d = _run(empty)
    assert d["tools"] == []


def test_unknown_outcome_token_ignored(tmp_path):
    # a token not in OUTCOME_MAP counts as a fire but neither helped nor wasted:
    # it must not silently inflate precision.
    logs = tmp_path / "logs"
    logs.mkdir()
    _write(logs, "x-outcome.jsonl",
           [{"ts": "2026-07-19T10:00:00Z", "tool": "X", "tool_class": "skill", "outcome": "used"}] * 6
           + [{"ts": "2026-07-19T10:00:00Z", "tool": "X", "tool_class": "skill", "outcome": "banana"}] * 4)
    d = _run(logs)
    x = {t["tool"]: t for t in d["tools"]}["X"]
    assert x["helped"] == 6 and x["wasted"] == 0 and x["fires"] == 10
    assert x["classified"] == 6   # unknowns excluded from precision denominator
