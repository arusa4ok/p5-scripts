import json, subprocess, sys, pathlib, importlib.util
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parents[2]

# Import must-act.py for its own parser. The hyphen in the filename keeps it out of
# normal import syntax, hence the spec dance. Sharing the parser is the point: the
# previous version of test_applied_marker_clears re-implemented it as
# `set(applied.read_text().split())`, which splits on every whitespace character —
# so comment prose ("# ids are digit-free…") and the tab-separated dates became
# "applied ids", and, more importantly, it could not know that dated `warn:` markers
# EXPIRE. An expired marker resurfacing is the designed behaviour, and the naive test
# read it as a leak. It failed on 2026-08-20 against a real expired marker.
_spec = importlib.util.spec_from_file_location("must_act", ROOT / "scripts/p5/must-act.py")
must_act = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(must_act)


def test_must_act_shape(tmp_path):
    out = tmp_path / "must-act.json"
    subprocess.run([sys.executable, str(ROOT / "scripts/p5/must-act.py"), "--out", str(out)], check=True)
    d = json.loads(out.read_text())
    assert "items" in d and isinstance(d["items"], list)
    assert "top" in d
    ids = {it["id"] for it in d["items"]}
    for it in d["items"]:
        assert {"id", "what", "path", "since", "severity"} <= it.keys()
        assert it["severity"] in {"broken", "dead", "idle"}
    # top must reference an actual item id (or be null when empty)
    assert d["top"] is None or d["top"] in ids
    # ids are unique (dedupe works)
    assert len(ids) == len(d["items"])


def test_applied_marker_clears(tmp_path):
    out = tmp_path / "must-act.json"
    subprocess.run([sys.executable, str(ROOT / "scripts/p5/must-act.py"), "--out", str(out)], check=True)
    d = json.loads(out.read_text())
    # Nothing the marker still suppresses may be surfaced. Asked through the real
    # parser, so TTL expiry is honoured rather than guessed at.
    leaked = {it["id"] for it in d["items"]} & must_act.applied_ids()
    assert not leaked, f"suppressed ids were surfaced anyway: {sorted(leaked)}"


def test_expired_warn_marker_resurfaces(tmp_path, monkeypatch):
    """A dated `warn:` marker past its TTL must stop suppressing.

    This is the behaviour the old naive test mistook for a bug. Pinned here so the
    next person to see an expired marker in the output knows it is the design.
    """
    marker = tmp_path / "applied.txt"
    today = datetime.now(timezone.utc).date()
    stale = today - timedelta(days=must_act.WARN_MARKER_TTL_DAYS + 1)
    marker.write_text(
        "# comment line with spaces that must not become an id\n"
        f"warn:stale-thing\t{stale}\n"
        f"warn:fresh-thing\t{today}\n"
        "finder:undated-thing\n"
    )
    monkeypatch.setattr(must_act, "APPLIED", marker)
    ids = must_act.applied_ids()
    assert "warn:stale-thing" not in ids       # expired — the live signal speaks again
    assert "warn:fresh-thing" in ids           # still inside its TTL
    assert "finder:undated-thing" in ids       # undated markers never expire
    assert not any(" " in i or i.startswith("#") for i in ids)   # comments are not ids
