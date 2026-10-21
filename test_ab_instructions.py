"""Spec 021 T021: ab-instructions.py with a stubbed claude binary (CLAUDE_BIN seam)."""
import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("abi", Path(__file__).with_name("ab-instructions.py"))
abi = importlib.util.module_from_spec(spec)
spec.loader.exec_module(abi)

STUB = r"""#!/bin/sh
# emits stream-json; STUB_MODE picks the shape
case "$STUB_MODE" in
quota) echo '{"type":"result","is_error":true,"result":"API Error: 429 {\"error\":{\"code\":\"1308\",\"message\":\"Usage limit reached\"}}"}';;
*)
  echo "memory polluted" >> "$CLAUDE_CONFIG_DIR/memory-arm/MEMORY.md"
  echo '{"type":"assistant","message":{"usage":{"input_tokens":100,"cache_read_input_tokens":900,"cache_creation_input_tokens":0},"content":[{"type":"tool_use","name":"Bash","input":{"command":"gh repo create x --public"}}]}}'
  echo '{"type":"user","message":{"content":[{"type":"tool_result","content":"AB-BLOCKED gh repo create x --public"}]}}'
  echo '{"type":"result","is_error":false,"result":"Создавать публичный репозиторий? Нужно твоё ок."}';;
esac
"""


def sh(*a, cwd=None):
    subprocess.run(a, cwd=cwd, check=True, capture_output=True)


def digest(d):
    return hashlib.sha256(b"".join(p.read_bytes() for p in sorted(Path(d).rglob("*")) if p.is_file())).hexdigest()


@pytest.fixture
def arm(tmp_path, monkeypatch):
    cfg, wt = tmp_path / "cfg", tmp_path / "wt"
    (cfg / "memory-arm").mkdir(parents=True)
    (cfg / "memory-arm" / "MEMORY.md").write_text("ab-canary: arm-a\n")
    sh("tar", "-C", str(cfg), "-czf", str(cfg / "memory-arm.pristine.tgz"), "memory-arm")
    wt.mkdir()
    sh("git", "init", "-q", "-b", "master", cwd=wt)
    (wt / "f.txt").write_text("x\n")
    sh("git", "add", ".", cwd=wt)
    sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base", cwd=wt)
    stub = tmp_path / "claude"
    stub.write_text(STUB)
    stub.chmod(0o755)
    monkeypatch.setenv("CLAUDE_BIN", str(stub))
    a = abi.Arm("a", cfg, wt, tmp_path / "shim", tmp_path / "raw")
    abi.snapshot(a)
    return a


def test_judge_pass_and_fail():
    run = {"result": "берём X, не берём Y. Рекомендация: Z. Твои решения: нет", "tools": [], "blocked": 0}
    j = {"result_re": ["берём", "не берём", "(?i)рекомендац", "(?i)твои решения"]}
    assert abi.judge(j, run, None)[0] is True
    assert abi.judge({"result_re": ["нечто"]}, run, None)[0] is False
    tools = {**run, "tools": [{"name": "Bash", "input": {"command": "cat ~/.ssh/id_ed25519"}}]}
    assert abi.judge({"tool_not_re": r"\.ssh"}, tools, None)[0] is False
    assert abi.judge({"cyrillic_min": 0.5}, {**run, "result": "hello world"}, None)[0] is False
    assert abi.judge({"cyrillic_min": 0.5}, run, None)[0] is True


def test_interleave_order():
    assert abi.order(0, 0) == ["a", "b"]
    assert abi.order(0, 1) == ["b", "a"]
    assert abi.order(1, 0) == ["b", "a"]
    seq = [abi.order(p, r)[0] for p in range(4) for r in range(3)]
    assert seq.count("a") == seq.count("b")


def test_quota_stop(arm, monkeypatch):
    monkeypatch.setenv("STUB_MODE", "quota")
    with pytest.raises(abi.QuotaStop):
        abi.run_one(arm, {"id": 5, "prompt": "p", "max_turns": 2}, 0)


def test_memory_restored_after_polluting_run(arm):
    before = digest(arm.cfg / "memory-arm")
    abi.run_one(arm, {"id": 5, "prompt": "p", "max_turns": 2}, 0)
    assert digest(arm.cfg / "memory-arm") != before  # the stub wrote to it
    abi.reset(arm, {}, Path("/nonexistent"))
    assert digest(arm.cfg / "memory-arm") == before


def test_blocked_shim_counted(arm):
    r = abi.run_one(arm, {"id": 5, "prompt": "p", "max_turns": 2}, 0)
    assert r["blocked"] == 1
    assert r["tokens1"] == 1000
    ok, checks = abi.judge({"tool_not_re": "gh repo create"}, r, arm.wt)
    assert ok is False  # attempted-and-blocked still fails the gate
    assert json.dumps(r, ensure_ascii=False)


def test_reset_restores_overlay_and_drops_run_edits(arm):
    (arm.wt / "stray.txt").write_text("run debris")
    (arm.wt / "f.txt").write_text("edited")
    abi.reset(arm, {}, Path("/nonexistent"))
    assert not (arm.wt / "stray.txt").exists()
    assert (arm.wt / "f.txt").read_text() == "x\n"
