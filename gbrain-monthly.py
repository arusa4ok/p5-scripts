#!/usr/bin/env python3
"""gbrain-monthly — monthly canon passes (spec 013 FR-003/FR-008/T009):
  1. enrich pass over the people hub (thin pages → compiled truth, idempotent)
  2. find_contradictions (brain-wide scan, JSON verdicts)
  3. schema drift check over the people source (detect → one line)
Wired into the weekly report via report.py::gbrain_monthly_md(); a marker file
gates the expensive passes to every 28 days so the weekly stays cheap between runs.
The last result is cached and re-printed in between.

Note: enrich/contradictions need LLM — routed via config (provider_base_urls.anthropic
→ z.ai anthropic-compat, anthropic_api_key = ZAI key, models anthropic:glm-4.7;
fixed 2026-08-31, see memory gbrain-llm-routing-glm). Runs with env stripped so the
CONFIG route is what's exercised (same path the launchd autopilot uses).

Modes: --md = digest markdown (gated, reads cache when not due); --force = run now
ignoring the gate; default = plain summary."""
import json, os, subprocess, sys, time
from datetime import datetime, timezone

GATE = os.path.expanduser("~/.claude/logs/gbrain-monthly.last")
CACHE = os.path.expanduser("~/.claude/logs/gbrain-monthly-cache.json")
INTERVAL = 28 * 86400
GB = os.path.expanduser("~/Library/Application Support/reflex/bun/bin/gbrain")
if not os.path.exists(GB):
    GB = "gbrain"

def run(cmd, timeout=900):
    env = {k: v for k, v in os.environ.items()
           if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")}
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        return (r.stdout or "") + (r.stderr or ""), r.returncode
    except Exception as e:
        return str(e), 1

def clean(s):
    return "\n".join(l for l in s.splitlines()
                     if "UPGRADE_AVAILABLE" not in l and "self-upgrade" not in l)

def passes():
    out = {"ran_at": datetime.now(timezone.utc).isoformat()[:19]}
    # 1. enrich people hub (idempotent; skips already-compiled pages)
    e, rc = run([GB, "enrich", "--source", "people", "--max-usd", "off", "--limit", "50"])
    e = clean(e)
    e_tail = e.strip().splitlines()[-1][:120] if e.strip() else "(no output)"
    if rc != 0 or ("failed" in e.lower() and "0 failed" not in e):
        out["enrich"] = "failed: " + e_tail
    else:
        out["enrich"] = next((l.strip() for l in e.splitlines() if l.startswith("Done:")), e_tail)
    # 2. contradictions (brain-wide JSON scan)
    c, _ = run([GB, "call", "find_contradictions"], 900)
    c = clean(c)
    try:
        cj = json.loads(c[c.index("{"):])
        cons = cj.get("contradictions", [])
        by_verdict = {}
        for x in cons:
            v = x.get("verdict", "?")
            by_verdict[v] = by_verdict.get(v, 0) + 1
        out["contradictions"] = {"count": len(cons), "by_verdict": by_verdict,
                                 "top": [f"{x['a'].get('slug','?')} ↔ {x['b'].get('slug','?')} [{x.get('verdict','?')}]" for x in cons[:3]]}
    except Exception:
        out["contradictions"] = {"error": c.strip().splitlines()[-1][:120] if c.strip() else "no output"}
    # 3. schema drift (people source)
    s, _ = run([GB, "schema", "detect", "--source", "people"], 300)
    s = clean(s)
    untyped = next((l.strip() for l in s.splitlines() if "without type" in l), "?")
    cands = [l.strip() for l in s.splitlines() if "suggest type" in l]
    out["schema"] = {"untyped": untyped, "candidates": len(cands)}
    # 4. entity-federation canary — detects the fork patch being silently lost
    #    (e.g. a live-tree upgrade overwrites the working tree). A healthy run
    #    resolves a known hub entity from an explicitly isolated cwd.
    c, _ = run([GB, "entity", "Razalia", "--source", "people"], 60)
    out["entity_canary"] = "OK" if "people/razalia" in c else ("LOST — fork patch missing from live tree" if "No entity found" in c else "ERROR: " + c.strip().splitlines()[-1][:80] if c.strip() else "no output")
    return out

def md(d):
    L = ["## gbrain monthly passes (28d gate)"]
    L.append(f"_ran {d.get('ran_at','?')}_")
    e = d.get("enrich", "?")
    L.append(f"- **enrich people**: {e}")
    c = d.get("contradictions", {})
    if "error" in c:
        L.append(f"- **contradictions**: ERROR — {c['error']}")
    else:
        L.append(f"- **contradictions**: {c.get('count',0)} found" +
                 ("".join(f", {k}×{v}" for k, v in sorted(c.get("by_verdict", {}).items()))))
        for t in c.get("top", []):
            L.append(f"  - {t}")
    sc = d.get("schema", {})
    L.append(f"- **schema drift** (people): {sc.get('untyped','?')} · {sc.get('candidates',0)} type candidates")
    canary = d.get("entity_canary", "?")
    L.append(f"- **entity federation canary**: {canary}" + (" — re-apply fork commit 745b6ca6d to ~/gbrain-live" if "LOST" in str(canary) else ""))
    return "\n".join(L)

if "--force" in sys.argv:
    d = passes()
    json.dump(d, open(CACHE, "w"), indent=1)
    open(GATE, "w").write(str(time.time()))
    print(json.dumps(d, indent=1))
elif "--md" in sys.argv:
    due = not os.path.exists(GATE) or (time.time() - os.path.getmtime(GATE)) > INTERVAL
    if due:
        try:
            d = passes()
            json.dump(d, open(CACHE, "w"), indent=1)
            open(GATE, "w").write(str(time.time()))
        except Exception as ex:
            d = {"error": str(ex)}
        print(md(d))
    else:
        try:
            print(md(json.load(open(CACHE))))
        except Exception:
            print("")  # nothing cached, nothing due — stay silent
else:
    d = passes()
    print(json.dumps(d, indent=1))
