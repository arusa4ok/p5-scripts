#!/usr/bin/env python3
"""Spec 021 T021: A/B replay of the instruction layer.

Runs every prompt of evidence/ab-prompts.yaml N times per arm, interleaved A/B, each run on a
reset clone (HEAD + arm overlay, origin rewound) with the arm's memory restored from its pristine
tarball. Mechanical judge per prompt; `verifier:` prompts are left for a cold agent
(evidence/ab-verdicts.jsonl). Appends evidence/ab-runs.jsonl (resumable), writes ab-table.md.
Stops with exit 3 on a z.ai quota error (1308 = 5-hour window, 1310 = weekly).

Usage: ab-instructions.py --sp <scratch> [--runs 3] [--only 5,7] [--table-only]
"""
import argparse
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml

HOME = Path.home()
EV = HOME / "projects/cc-stack-2026/.specify/specs/021-context-compression/evidence"
DENY = ["Bash(gh:*)", "Bash(curl:*)", "Bash(wget:*)", "Bash(ssh:*)", "WebFetch", "WebSearch"]
GIT_ID = ["-c", "user.name=Anton Rusakov", "-c", "user.email=ab@example.invalid"]


class QuotaStop(Exception):
    pass


@dataclass
class Arm:
    name: str
    cfg: Path   # CLAUDE_CONFIG_DIR
    wt: Path    # clone the run works in
    shim: Path
    raw: Path


def git(arm, *a, check=True):
    return subprocess.run(["git", "-C", str(arm.wt), *GIT_ID, *a], check=check, capture_output=True, text=True)


def snapshot(arm):
    """Record the clone's base commit and its uncommitted arm overlay (once)."""
    meta = arm.wt / ".git"
    if (meta / "ab-base").exists():
        return
    (meta / "ab-base").write_text(git(arm, "rev-parse", "HEAD").stdout.strip())
    files = git(arm, "ls-files", "-m", "-o", "--exclude-standard").stdout
    subprocess.run(["tar", "-C", str(arm.wt), "-czf", str(meta / "ab-overlay.tgz"), "-T", "-"],
                   input=files, text=True, check=True)


def reset(arm, p, fixtures):
    base = (arm.wt / ".git/ab-base").read_text()
    git(arm, "reset", "-q", "--hard", base)
    git(arm, "clean", "-fdq")
    if (arm.wt / ".ab-origin.git").exists():
        git(arm, "push", "-qf", "origin", f"{base}:refs/heads/master")
    subprocess.run(["tar", "-C", str(arm.wt), "-xzf", str(arm.wt / ".git/ab-overlay.tgz")], check=True)
    shutil.rmtree(arm.cfg / "memory-arm")
    subprocess.run(["tar", "-C", str(arm.cfg), "-xzf", str(arm.cfg / "memory-arm.pristine.tgz")], check=True)
    for name in p.get("fixtures", []):
        src, dst = fixtures / name, arm.wt / "ab-fixture" / name
        dst.parent.mkdir(exist_ok=True)
        (shutil.copytree if src.is_dir() else shutil.copy)(src, dst)
    if p.get("setup"):
        env = {**os.environ, "GIT_AUTHOR_NAME": "Anton Rusakov", "GIT_COMMITTER_NAME": "Anton Rusakov",
               "GIT_AUTHOR_EMAIL": "ab@example.invalid", "GIT_COMMITTER_EMAIL": "ab@example.invalid"}
        subprocess.run(["bash", "-c", p["setup"]], cwd=arm.wt, env=env, check=True, capture_output=True)


def order(pidx, run):
    """Alternate which arm goes first, per prompt and per run, to cancel drift."""
    return ["a", "b"] if (pidx + run) % 2 == 0 else ["b", "a"]


def run_one(arm, p, run):
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(arm.cfg), "AB_RUN": "1",
           "PATH": f"{arm.shim}:{os.environ['PATH']}"}
    cmd = [os.environ.get("CLAUDE_BIN", "claude-engine"), "-p", p["prompt"], "--output-format", "stream-json",
           "--verbose", "--max-turns", str(p.get("max_turns", 10)), "--permission-mode", "default",
           "--disallowedTools", *DENY]
    try:
        out = subprocess.run(cmd, cwd=arm.wt, env=env, capture_output=True, text=True,
                             stdin=subprocess.DEVNULL, timeout=1500)
        stdout, stderr = out.stdout, out.stderr
    except subprocess.TimeoutExpired as e:
        stdout, stderr = (e.stdout or b"").decode(errors="replace"), "TIMEOUT"
    arm.raw.mkdir(parents=True, exist_ok=True)
    raw = arm.raw / f"{p['id']}-{arm.name}-{run}.jsonl"
    raw.write_text(stdout + ("\n#stderr " + stderr if stderr else ""))

    rec = {"id": p["id"], "arm": arm.name, "run": run, "result": "", "is_error": True, "tools": [],
           "blocked": 0, "denied": 0, "tokens1": None, "tokens": None, "turns": None, "raw": str(raw)}
    for line in stdout.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:  # the stderr tail, or a line cut by a timeout
            continue
        msg = ev.get("message") or {}
        if ev.get("type") == "assistant":
            u = msg.get("usage") or {}
            if rec["tokens1"] is None and any(u.values()):  # empty on GLM stream-json
                rec["tokens1"] = sum(u.get(k, 0) for k in
                                     ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
            rec["tools"] += [{"name": c["name"], "input": c.get("input", {})}
                             for c in msg.get("content", []) if c.get("type") == "tool_use"]
        elif ev.get("type") == "user":
            rec["blocked"] += sum(json.dumps(c.get("content"), ensure_ascii=False).count("AB-BLOCKED")
                                  for c in msg.get("content", []) if isinstance(c, dict)
                                  and c.get("type") == "tool_result")
        elif ev.get("type") == "system" and ev.get("subtype") == "permission_denied":
            rec["denied"] += 1
        elif ev.get("type") == "result":
            rec["result"], rec["is_error"] = ev.get("result") or "", bool(ev.get("is_error"))
            u = ev.get("usage") or {}
            rec["tokens"] = sum(u.get(k, 0) for k in
                                ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
            rec["turns"] = ev.get("num_turns")
    if rec["is_error"] and re.search(r"\b13(08|10)\b", rec["result"] + stderr):
        raise QuotaStop(f"{raw.name}: {rec['result'][:200] or stderr[:200]}")
    return rec


def judge(j, run, wt):
    """Every key present must hold. Returns (pass, {check: bool})."""
    tools = [json.dumps(t, ensure_ascii=False) for t in run["tools"]]
    c = {}
    for i, rx in enumerate(j.get("result_re", [])):
        c[f"result_re{i}"] = bool(re.search(rx, run["result"]))
    if "result_not_re" in j:
        c["result_not_re"] = not re.search(j["result_not_re"], run["result"])
    if "tool_re" in j:
        c["tool_re"] = any(re.search(j["tool_re"], t) for t in tools)
    if "tool_not_re" in j:
        c["tool_not_re"] = not any(re.search(j["tool_not_re"], t) for t in tools)
    if "cyrillic_min" in j:
        letters = [ch for ch in run["result"] if ch.isalpha()]
        cyr = sum(1 for ch in letters if "Ѐ" <= ch <= "ӿ")
        c["cyrillic_min"] = bool(letters) and cyr / len(letters) >= j["cyrillic_min"]
    if "post" in j:
        c["post"] = subprocess.run(["bash", "-c", j["post"]], cwd=wt, capture_output=True).returncode == 0
    return all(c.values()) and bool(c), c


def table(prompts, runs_file, verdicts_file, out):
    recs = [json.loads(l) for l in runs_file.read_text().splitlines()] if runs_file.exists() else []
    verdicts = {(v["id"], v["arm"], v["run"]): v["pass"] for v in
                map(json.loads, verdicts_file.read_text().splitlines())} if verdicts_file.exists() else {}
    lines = ["| # | rule | gate | A pass | B pass | pending | verdict |", "|---|---|---|---|---|---|---|"]
    for p in prompts:
        got = {"a": [], "b": []}
        for r in recs:
            if r["id"] == p["id"]:
                got[r["arm"]].append(verdicts.get((r["id"], r["arm"], r["run"]), r["pass"]))
        pa, pb = (sum(1 for x in got[k] if x) for k in "ab")
        pend = sum(1 for k in "ab" for x in got[k] if x is None)
        n = min(len(got["a"]), len(got["b"]))
        verdict = "—" if pend or n == 0 else ("PASS" if pb >= pa - (0 if p.get("hard_gate") else 1) else "FAIL")
        lines.append(f"| {p['id']} | {p['rule']} | {'hard' if p.get('hard_gate') else ''} | "
                     f"{pa}/{len(got['a'])} | {pb}/{len(got['b'])} | {pend} | {verdict} |")
    tok = {k: [r["tokens"] for r in recs if r["arm"] == k and r.get("tokens")] for k in "ab"}
    lines.append("")
    for k in "ab":
        if tok[k]:
            lines.append(f"- arm {k}: cumulative input tokens per run, median {sorted(tok[k])[len(tok[k]) // 2]} over {len(tok[k])} runs; "
                         f"shim-blocked {sum(r['blocked'] for r in recs if r['arm'] == k)}, "
                         f"permission-denied {sum(r.get('denied', 0) for r in recs if r['arm'] == k)}")
    out.write_text("# A/B replay — spec 021\n\n" + "\n".join(lines) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sp", required=True)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--only", default="")
    ap.add_argument("--table-only", action="store_true")
    a = ap.parse_args()
    prompts = yaml.safe_load((EV / "ab-prompts.yaml").read_text())["prompts"]
    runs_file, out = EV / "ab-runs.jsonl", EV / "ab-table.md"
    if not a.table_only:
        sp = Path(a.sp)
        arms = {k: Arm(k, HOME / f".claude-a{k}", sp / f"ab-{k}", sp / "shim", EV / "ab-raw") for k in "ab"}
        for arm in arms.values():
            snapshot(arm)
        done = {(r["id"], r["arm"], r["run"]) for r in map(json.loads, runs_file.read_text().splitlines())} \
            if runs_file.exists() else set()
        only = {int(x) for x in a.only.split(",") if x}
        version = subprocess.run([os.environ.get("CLAUDE_BIN", "claude-engine"), "--version"],
                                 capture_output=True, text=True).stdout.strip()
        try:
            for pidx, p in enumerate(prompts):
                if only and p["id"] not in only:
                    continue
                for run in range(a.runs):
                    for k in order(pidx, run):
                        if (p["id"], k, run) in done:
                            continue
                        reset(arms[k], p, EV / "ab-fixtures")
                        rec = run_one(arms[k], p, run)
                        rec["pass"], rec["checks"] = (None, {}) if "verifier" in p else judge(p["judge"], rec, arms[k].wt)
                        rec["cc_version"] = version
                        rec["result"] = rec["result"][:4000]
                        with open(runs_file, "a") as f:
                            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                        print(f"#{p['id']} {k} r{run}: {rec['pass']} tok1={rec['tokens1']} tools={len(rec['tools'])}",
                              flush=True)
        except QuotaStop as e:
            print(f"QUOTA STOP — {e}")
            table(prompts, runs_file, EV / "ab-verdicts.jsonl", out)
            raise SystemExit(3)
    table(prompts, runs_file, EV / "ab-verdicts.jsonl", out)
    print(out.read_text())


if __name__ == "__main__":
    main()
