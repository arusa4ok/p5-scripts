#!/usr/bin/env python3
"""Spec 016 T006 — spec-gate-b.py fixtures (FR-007/FR-008).

Run: python3 -m pytest scripts/p5/test_spec_gate_b.py -q
"""
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location("spec_gate_b", Path(__file__).parent / "spec-gate-b.py")
gb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gb)

THREE = ["functional — SC-001..003", "Code & reuse", "whole-system reconciliation"]


def make(tmp_path, rows=None, critic="haiku/glm-4.7", risk=None, degraded="—", verdict="pass"):
    head = "# Feature Specification: demo\n\n**Author model**: glm-5.3\n"
    if risk:
        head += f"**Risk**: {risk}\n"
    (tmp_path / "spec.md").write_text(head, encoding="utf-8")
    ev = tmp_path / "evidence"
    ev.mkdir()
    if rows is not None:
        body = [gb.HEADER, "|---|---|---|---|---|"]
        body += [f"| {c} | {verdict} | {critic} | {degraded} | ok |" for c in rows]
        (ev / "verdict.md").write_text("# acceptance\n\n" + "\n".join(body) + "\n", encoding="utf-8")
    return tmp_path


def test_no_table_fails(tmp_path):
    code, reason = gb.check(make(tmp_path))
    assert code == 1 and "no criteria-mode verdict table" in reason


def test_same_model_fails(tmp_path):
    # sonnet == opus == glm-5.3 on this stack: a same-model review in disguise.
    code, reason = gb.check(make(tmp_path, THREE, critic="sonnet"))
    assert code == 1 and "author's model" in reason


def test_degraded_same_model_fails(tmp_path):
    code, _ = gb.check(make(tmp_path, THREE, degraded="degraded: same-model"))
    assert code == 1


def test_cross_model_three_passes(tmp_path):
    code, reason = gb.check(make(tmp_path, THREE))
    assert code == 0, reason


def test_high_risk_with_three_exits_2(tmp_path):
    code, reason = gb.check(make(tmp_path, THREE, risk="high"))
    assert code == 2 and "covered 3" in reason


def test_unknown_author_fails(tmp_path):
    make(tmp_path, THREE)
    (tmp_path / "spec.md").write_text("# no header\n", encoding="utf-8")
    code, reason = gb.check(tmp_path)
    assert code == 1 and "record the author model" in reason


def test_template_round_trips(tmp_path):
    (tmp_path / "spec.md").write_text("**Author model**: glm-5.3\n**Risk**: high\n", encoding="utf-8")
    (tmp_path / "evidence").mkdir()
    filled = gb.template(high=True).replace("<critic model>", "glm-4.7").replace("pass / fail", "pass")
    (tmp_path / "evidence" / "v.md").write_text(filled, encoding="utf-8")
    assert gb.check(tmp_path)[0] == 0


def test_all_fail_verdicts_do_not_pass(tmp_path):
    # code-review finding 1 (2026-10-11): a failed criterion must never count as covered
    code, reason = gb.check(make(tmp_path, THREE, verdict="fail"))
    assert code == 1 and "only pass covers" in reason


def test_one_prose_row_covers_one_criterion(tmp_path):
    # re-review finding (2026-10-11): a descriptive cell must not cover several criteria
    rows = ["functional — FR-2 secret rotation reconciles architecture"]
    code, reason = gb.check(make(tmp_path, rows))
    assert code == 1 and "missing criteria" in reason


def _gate_a(tmp_path, tasks, verdict="pass", frontmatter=False):
    head = "---\nauthor_model: glm-5.3\nrisk: high\n---\n" if frontmatter else "**Author model**: glm-5.3\n"
    (tmp_path / "spec.md").write_text(head, encoding="utf-8")
    (tmp_path / "tasks.md").write_text(tasks, encoding="utf-8")
    ev = tmp_path / "evidence"
    ev.mkdir(exist_ok=True)
    rows = [gb.HEADER, "|---|---|---|---|---|"] + [
        f"| {c} | {verdict} | glm-4.7 | — | ok |" for c in ("canon-fit", "boundaries & contracts", "scope", "checkability", "rollback", "prior-art")]
    (ev / "gate-a-x.md").write_text("\n".join(rows) + "\n", encoding="utf-8")
    return tmp_path


def test_gate_a_passes_with_checks_and_table(tmp_path):
    d = _gate_a(tmp_path, "- [ ] T1 do x\n  CHECK: pytest green\n- [x] T0 done\n", frontmatter=True)
    assert gb.check(d, "a")[0] == 0


def test_gate_a_fails_on_task_without_check(tmp_path):
    code, reason = gb.check(_gate_a(tmp_path, "- [ ] T1 do x\n- [ ] T2 y CHECK: ok\n"), "a")
    assert code == 1 and "without CHECK" in reason


def test_gate_a_table_does_not_feed_gate_b(tmp_path):
    d = _gate_a(tmp_path, "- [ ] T1 x CHECK: ok\n")
    assert "no criteria-mode verdict table" in gb.check(d, "b")[1]


def test_gate_a_open_table_row_needs_check(tmp_path):
    tasks = "| A1R review | reviewer | A1 | [ ] |\n- [ ] T1 x CHECK: ok\n"
    code, reason = gb.check(_gate_a(tmp_path, tasks), "a")
    assert code == 1 and "A1R" in reason


def test_later_round_supersedes_earlier_fail(tmp_path):
    import os
    d = _gate_a(tmp_path, "- [ ] T1 x CHECK: ok\n", verdict="fail")
    r1 = next((d / "evidence").glob("gate-a*.md"))
    os.utime(r1, (1, 1))
    body = [gb.HEADER, "|---|---|---|---|---|"]
    body += [f"| {n} | pass | haiku/glm-4.7 | — | ok |" for n in
             ("canon-fit", "boundaries & contracts", "scope", "checkability", "rollback", "prior-art")]
    (d / "evidence" / "gate-a-r2.md").write_text("\n".join(body) + "\n", encoding="utf-8")
    assert gb.check(d, "a")[0] == 0


def test_gate_a_fails_without_prior_art(tmp_path):
    d = _gate_a(tmp_path, "- [ ] T1 x\n  CHECK: ok\n")
    f = next((d / "evidence").glob("gate-a*.md"))
    f.write_text("\n".join(ln for ln in f.read_text().splitlines() if "prior-art" not in ln) + "\n")
    rc, msg = gb.check(d, "a")[:2]
    assert rc == 1 and "prior-art" in str(msg)
