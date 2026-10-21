#!/usr/bin/env python3
"""stack-cron — one control panel for all our launchd scheduled jobs.

macOS launchd is the durable scheduler for our shell/python jobs (weekly report,
autopilot, career-os fetch pipeline, …). CC's own CronCreate is session-only
(in-memory, dies with the session, 7-day cap) so it's NOT for these — launchd is.
The gap this fills: our ~25 job plists are scattered across ~/Library/LaunchAgents
with no single view, so "what runs when, and did it last succeed?" meant grepping
plists by hand. This is that view.

Scans plists whose Label starts with one of our prefixes (com.anton, com.career-os,
com.identity-os), parses the schedule, and cross-references `launchctl list` for the
last PID + exit status.

Usage:
  python3 stack-cron.py                # table: job · schedule · last-exit · state
  python3 stack-cron.py --md           # markdown (for the weekly)
  python3 stack-cron.py --disable com.anton.foo   # unload + set Disabled (reversible)
  python3 stack-cron.py --enable  com.anton.foo   # load + clear Disabled
  python3 stack-cron.py --snapshot                # mirror our plists into the repo
Always exits 0 on --list/--md.
"""
import re
import sys
import plistlib
import subprocess
import pathlib

LA = pathlib.Path.home() / "Library" / "LaunchAgents"
PREFIXES = ("com.anton.", "com.career-os.", "com.identity-os.")
DOW = {0: "Sun", 1: "Mon", 2: "Tue", 3: "Wed", 4: "Thu", 5: "Fri", 6: "Sat", 7: "Sun"}
SNAPSHOT_DIR = pathlib.Path(__file__).resolve().parents[2] / "p5-loop" / "launchd"
SECRETISH_KEY = re.compile(r"key|token|secret|password|passwd|auth|credential", re.I)


def _our_plists():
    out = []
    for p in sorted(LA.glob("*.plist")):
        if p.name.startswith(PREFIXES):
            out.append(p)
    return out


def _schedule(pl):
    """Human schedule from StartCalendarInterval / StartInterval."""
    if "StartInterval" in pl:
        s = int(pl["StartInterval"])
        if s % 3600 == 0:
            return f"every {s // 3600}h"
        if s % 60 == 0:
            return f"every {s // 60}m"
        return f"every {s}s"
    ci = pl.get("StartCalendarInterval")
    if ci is None:
        return "on-demand"
    entries = ci if isinstance(ci, list) else [ci]
    parts = []
    for e in entries:
        wd = e.get("Weekday")
        h = e.get("Hour")
        m = e.get("Minute", 0)
        day = e.get("Day")
        when = f"{h:02d}:{m:02d}" if h is not None else f":{m:02d}"
        if wd is not None:
            parts.append(f"{DOW.get(wd, wd)} {when}")
        elif day is not None:
            parts.append(f"day{day} {when}")
        else:
            parts.append(f"daily {when}")
    return ", ".join(parts)


def _launchctl_status():
    """Map label -> (pid, last_exit) from `launchctl list`."""
    st = {}
    try:
        out = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=15).stdout
    except Exception:
        return st
    for line in out.splitlines()[1:]:
        cols = line.split("\t")
        if len(cols) < 3:
            continue
        pid, status, label = cols[0], cols[1], cols[2]
        if label.startswith(PREFIXES):
            st[label] = (pid, status)
    return st


def _load_plist(p):
    """Parsed plist, or None if launchd itself could not load this file.

    plistlib alone is the WRONG oracle: it runs expat, which rejects a `--` inside an
    XML comment, while plutil and launchd both accept it. Two career-os plists document
    their flags that way (`--skip-fetch`) and are perfectly healthy jobs — calling them
    corrupt would be a false alarm on a running system.

    `plutil -convert` is the wrong oracle in the other direction: it happily reads a bare
    JSON fragment as a property list, which is exactly the corruption seen on 2026-08-06.
    `plutil -lint` is the one that draws the line where launchd draws it — strict about the
    file format, tolerant of the comment. So: lint decides, convert only supplies the bytes.
    """
    try:
        return plistlib.loads(p.read_bytes())
    except Exception:
        pass
    try:
        if subprocess.run(["plutil", "-lint", str(p)],
                          capture_output=True, timeout=5).returncode != 0:
            return None
        r = subprocess.run(["plutil", "-convert", "xml1", "-o", "-", str(p)],
                           capture_output=True, timeout=5)
        return plistlib.loads(r.stdout) if r.returncode == 0 else None
    except Exception:
        return None


def _rows():
    status = _launchctl_status()
    rows = []
    for p in _our_plists():
        pl = _load_plist(p)
        if pl is None:
            # A plist launchd cannot load is a job that disappears at the next login:
            # launchd keeps running the copy it loaded earlier, so nothing looks wrong
            # until a reboot silently drops it. Skipping the row hid exactly that —
            # cc-stack-self-review and gbrain-advisor-weekly sat corrupt (a bare JSON
            # fragment, no Label, no ProgramArguments) and were invisible in every
            # listing until 2026-08-06. `exit` is deliberately non-zero so the existing
            # alert filter picks it up with no other change.
            rows.append({"label": p.stem, "schedule": "UNPARSEABLE",
                         "exit": "PARSE", "state": "corrupt-plist"})
            continue
        label = pl.get("Label", p.stem)
        disabled = bool(pl.get("Disabled", False))
        loaded = label in status
        pid, exit_code = status.get(label, ("-", "-"))
        rows.append({
            "label": label,
            "schedule": _schedule(pl),
            "exit": exit_code,
            "state": "disabled" if disabled else ("loaded" if loaded else "not-loaded"),
        })
    return rows


def cmd_list(md=False, alerts_only=False):
    rows = _rows()
    bad = [r for r in rows if r["exit"] not in ("-", "0") or r["state"] == "not-loaded"]
    if alerts_only and not bad:
        return  # weekly: stay silent when every job is healthy
    if md:
        L = [f"## Stack-cron — {len(rows)} jobs · {len(bad)} need a look", "",
             "| job | schedule | last-exit | state |", "|---|---|---|---|"]
        for r in rows:
            flag = "" if (r["exit"] in ("-", "0") and r["state"] != "not-loaded") else " ⚠️"
            L.append(f"| {r['label']} | {r['schedule']} | {r['exit']} | {r['state']}{flag} |")
        print("\n".join(L))
        return
    print(f"{'JOB':40} {'SCHEDULE':22} {'EXIT':6} STATE")
    for r in rows:
        flag = "" if (r["exit"] in ("-", "0") and r["state"] != "not-loaded") else "  <-- check"
        print(f"{r['label']:40} {r['schedule']:22} {str(r['exit']):6} {r['state']}{flag}")


def _plist_for(label):
    p = LA / f"{label}.plist"
    return p if p.exists() else None


def cmd_toggle(label, disable):
    p = _plist_for(label)
    if not p:
        print(f"no plist for {label} in {LA}")
        return 1
    pl = plistlib.loads(p.read_bytes())
    pl["Disabled"] = disable
    p.write_bytes(plistlib.dumps(pl))
    action = "bootout" if disable else "bootstrap"
    tgt = f"gui/{__import__('os').getuid()}"
    if disable:
        subprocess.run(["launchctl", "bootout", f"{tgt}/{label}"], capture_output=True)
    else:
        subprocess.run(["launchctl", "bootstrap", tgt, str(p)], capture_output=True)
    print(f"{'disabled' if disable else 'enabled'} {label} (Disabled={disable}, launchctl {action})")
    return 0


def _carries_a_credential(pl):
    """Env var whose NAME reads like a secret and whose VALUE looks like one. A path
    (KIMI_KEY_FILE=/Users/…) is a pointer, not a secret, and stays snapshottable."""
    env = (pl or {}).get("EnvironmentVariables", {}) or {}
    return [k for k, v in env.items()
            if SECRETISH_KEY.search(k) and isinstance(v, str)
            and not v.startswith("/") and re.fullmatch(r"[A-Za-z0-9_.\-]{20,}", v)]


def cmd_snapshot():
    """Mirror our job plists into the repo so a corrupt or deleted one can be restored.

    Why this exists: ~/Library/LaunchAgents is version-controlled NOWHERE. On 2026-08-06
    two weeklies were found reduced to a bare JSON fragment, and the only reason they were
    recoverable is that launchd still held the loaded copies in memory — `launchctl print`
    rebuilt them. One reboot and both the job and its definition would simply have been
    gone, with nothing on disk or in any history to restore from.

    Restore is a copy back plus `launchctl bootout`/`bootstrap` — no tooling needed, which
    is why none is written here. Verbatim copies, no redaction: the fleet carries no secret
    VALUES today (checked), and a plist that gains one is skipped and named rather than
    quietly rewritten — a snapshot that silently drops content is worse than no snapshot.
    """
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    changed, skipped, keep = [], [], set()
    for p in _our_plists():
        bad = _carries_a_credential(_load_plist(p))
        if bad:
            skipped.append(f"{p.name} ({', '.join(bad)})")
            continue
        keep.add(p.name)
        dest, data = SNAPSHOT_DIR / p.name, p.read_bytes()
        if not dest.exists() or dest.read_bytes() != data:
            dest.write_bytes(data)
            changed.append(p.name)
    gone = [q.name for q in SNAPSHOT_DIR.glob("*.plist")
            if q.name not in keep and not any(s.startswith(q.name) for s in skipped)]
    for name in gone:
        (SNAPSHOT_DIR / name).unlink()
    print(f"stack-cron snapshot: {len(keep)} job(s) mirrored to {SNAPSHOT_DIR}"
          f" · {len(changed)} written · {len(gone)} removed")
    for n in changed:
        print(f"  + {n}")
    for n in gone:
        print(f"  - {n} (no longer installed)")
    for s in skipped:
        print(f"  ! skipped, carries a credential VALUE: {s}")
    _snapshot_crontab()
    return 0


def _snapshot_crontab():
    """The launchd fleet is not the whole fleet: the news radar lives in `crontab -l`.

    Mirroring only LaunchAgents left the more fragile surface uncovered — a plist at least
    sits in a file you can notice, while `crontab -r` wipes both jobs with no file to find
    afterwards. Same reasoning as the plists, one surface short (2026-08-10).
    """
    dest = SNAPSHOT_DIR / "crontab.txt"
    try:
        r = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=20)
    except Exception as e:
        print(f"  ! crontab not mirrored: {e}")
        return
    if r.returncode != 0:                      # no crontab installed is a legitimate state
        dest.unlink(missing_ok=True)
        print("  · no user crontab installed")
        return
    if not dest.exists() or dest.read_text(encoding="utf-8") != r.stdout:
        dest.write_text(r.stdout, encoding="utf-8")
        print(f"  + crontab.txt ({len(r.stdout.splitlines())} lines)")


def main():
    a = sys.argv[1:]
    if "--disable" in a:
        return cmd_toggle(a[a.index("--disable") + 1], True)
    if "--enable" in a:
        return cmd_toggle(a[a.index("--enable") + 1], False)
    if "--snapshot" in a:
        return cmd_snapshot()
    cmd_list(md="--md" in a, alerts_only="--alerts-only" in a)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"stack-cron error: {e}", file=sys.stderr)
        sys.exit(0)
