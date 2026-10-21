#!/usr/bin/env python3
"""model-quota-radar — weekly diff of free/cheap-LLM capacity across two open JSON catalogs.

Detection only (per Anton's Track A/B verdict, p2-research/model-quota-radar-2026-07-04.md):
NO auto-switching, NO LiteLLM install. Polls two free, no-auth JSON endpoints, diffs against
the last snapshot, and emits a markdown block the weekly report embeds as its own section.

Sources (both no-auth, JSON, free):
  - https://openrouter.ai/api/v1/models  -> live catalog incl. ":free" variants + per-token prices
  - https://models.dev/api.json          -> cross-provider pricing DB (github.com/sst/models.dev)

Watchlist families (only these get price-drop tracking — the free-model diff covers everything):
  claude, glm, gpt, gemini, kimi, deepseek, qwen

Fail-open: a network/parse failure on either source keeps the OLD snapshot untouched, notes the
failure in the diff markdown, logs fetch_ok=false, and still exits 0 — a watcher must never break
the weekly report or the launchd chain that calls it.

Usage: python3 model-quota-radar.py        (writes snapshot + diff.md + log line, exits 0)
       python3 model-quota-radar.py --md   (print diff.md content only, no writes — for report.py)
"""
import json
import os
import re
import sys
import urllib.request
import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent  # cc-stack-2026/
SNAPSHOT = ROOT / "p5-loop" / "snapshots" / "model-quota-latest.json"
DIFF_MD = ROOT / "p5-loop" / "model-quota-diff.md"
LOG = Path(os.path.expanduser("~/.claude/logs/model-quota-radar.jsonl"))

WATCHLIST = ("claude", "glm", "gpt", "gemini", "kimi", "deepseek", "qwen")
PRICE_DROP_PCT = 20.0  # flag drops >= this %
TIMEOUT = 20


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "cc-stack-model-quota-radar/1.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def is_watchlisted(model_id):
    low = model_id.lower()
    return any(fam in low for fam in WATCHLIST)


def snapshot_openrouter():
    """-> (free_ids: set[str], prices: {id: {"prompt": float, "completion": float}}) per-token USD.
    Raises on any fetch/parse problem (caller decides fail-open)."""
    data = fetch_json("https://openrouter.ai/api/v1/models")
    models = data.get("data", [])
    free_ids, prices = set(), {}
    for m in models:
        mid = m.get("id", "")
        if not mid:
            continue
        pricing = m.get("pricing") or {}
        try:
            prompt = float(pricing.get("prompt", "0") or 0)
            completion = float(pricing.get("completion", "0") or 0)
        except (TypeError, ValueError):
            prompt = completion = 0.0
        if mid.endswith(":free") or (prompt == 0.0 and completion == 0.0):
            free_ids.add(mid)
        if is_watchlisted(mid):
            prices[mid] = {"prompt": prompt, "completion": completion}
    return free_ids, prices


def snapshot_modelsdev():
    """-> (free_ids: set["provider/model_id"], prices: {"provider/model_id": {"input":.., "output":..}})
    input/output are USD per 1M tokens (models.dev's native unit). Raises on any problem."""
    data = fetch_json("https://models.dev/api.json")
    free_ids, prices = set(), {}
    for prov_id, prov in data.items():
        for mid, m in (prov.get("models") or {}).items():
            full_id = f"{prov_id}/{mid}"
            cost = m.get("cost") or {}
            inp = cost.get("input")
            out = cost.get("output")
            if inp == 0 and out == 0:
                free_ids.add(full_id)
            if is_watchlisted(full_id) and (inp is not None or out is not None):
                prices[full_id] = {"input": inp or 0, "output": out or 0}
    return free_ids, prices


def load_prev():
    try:
        return json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    except Exception:
        return None


def price_drops(prev_prices, cur_prices, fields):
    """Yield (model_id, field, old, new, pct_drop) for any field that dropped >= PRICE_DROP_PCT.
    Ignores 0 -> 0 (no signal) and skips ids missing from either snapshot."""
    out = []
    for mid, cur in cur_prices.items():
        old = prev_prices.get(mid)
        if not old:
            continue
        for f in fields:
            o, n = old.get(f, 0), cur.get(f, 0)
            if o and n < o:
                pct = (o - n) / o * 100.0
                if pct >= PRICE_DROP_PCT:
                    out.append((mid, f, o, n, pct))
    return out


def build_diff(prev, cur, fetch_ok, fetch_note):
    lines = ["# Model-Quota Radar — diff", "",
             f"_Generated {datetime.datetime.now(datetime.timezone.utc).isoformat()} · "
             f"fetch_ok={fetch_ok}_", ""]
    if not fetch_ok:
        lines.append(f"⚠️ **Fetch failed — kept last known snapshot.** {fetch_note}")
        lines.append("")
        return "\n".join(lines), 0, 0

    if prev is None:
        lines.append("_First run — seeded snapshot, no prior state to diff against._")
        lines.append("")
        return "\n".join(lines), 0, 0

    prev_or_free = set(prev.get("openrouter", {}).get("free_ids", []))
    cur_or_free = set(cur["openrouter"]["free_ids"])
    prev_md_free = set(prev.get("modelsdev", {}).get("free_ids", []))
    cur_md_free = set(cur["modelsdev"]["free_ids"])

    new_or = sorted(cur_or_free - prev_or_free)
    removed_or = sorted(prev_or_free - cur_or_free)
    new_md = sorted(cur_md_free - prev_md_free)
    removed_md = sorted(prev_md_free - cur_md_free)

    or_drops = price_drops(prev.get("openrouter", {}).get("watchlist_prices", {}),
                            cur["openrouter"]["watchlist_prices"], ("prompt", "completion"))
    md_drops = price_drops(prev.get("modelsdev", {}).get("watchlist_prices", {}),
                            cur["modelsdev"]["watchlist_prices"], ("input", "output"))

    n_new_free = len(new_or) + len(new_md)
    n_price_drops = len(or_drops) + len(md_drops)

    if n_new_free == 0 and n_price_drops == 0 and not removed_or and not removed_md:
        lines.append("сигналов нет")
        lines.append("")
        return "\n".join(lines), 0, 0

    if new_or:
        lines.append(f"## New free models — OpenRouter ({len(new_or)})")
        lines.extend(f"- `{m}`" for m in new_or)
        lines.append("")
    if new_md:
        lines.append(f"## New free models — models.dev ({len(new_md)})")
        lines.extend(f"- `{m}`" for m in new_md)
        lines.append("")
    if or_drops or md_drops:
        lines.append(f"## Price drops >{PRICE_DROP_PCT:.0f}% (watchlist: {', '.join(WATCHLIST)})")
        for mid, f, o, n, pct in or_drops:
            lines.append(f"- OpenRouter `{mid}` {f}: {o:.8f} → {n:.8f} (-{pct:.0f}%)")
        for mid, f, o, n, pct in md_drops:
            lines.append(f"- models.dev `{mid}` {f}: ${o:.2f}/1M → ${n:.2f}/1M (-{pct:.0f}%)")
        lines.append("")
    if removed_or:
        lines.append(f"## Removed free models — OpenRouter ({len(removed_or)})")
        lines.extend(f"- `{m}`" for m in removed_or)
        lines.append("")
    if removed_md:
        lines.append(f"## Removed free models — models.dev ({len(removed_md)})")
        lines.extend(f"- `{m}`" for m in removed_md)
        lines.append("")

    return "\n".join(lines), n_new_free, n_price_drops


def log_run(rec):
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def main():
    prev = load_prev()
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat()

    try:
        or_free, or_prices = snapshot_openrouter()
        md_free, md_prices = snapshot_modelsdev()
        fetch_ok, fetch_note = True, ""
    except Exception as e:
        fetch_ok, fetch_note = False, f"{type(e).__name__}: {e}"
        or_free = or_prices = md_free = md_prices = None

    if fetch_ok:
        cur = {
            "ts": ts,
            "openrouter": {"free_ids": sorted(or_free), "watchlist_prices": or_prices},
            "modelsdev": {"free_ids": sorted(md_free), "watchlist_prices": md_prices},
        }
    else:
        cur = None

    diff_md, n_new_free, n_price_drops = build_diff(prev, cur, fetch_ok, fetch_note)
    DIFF_MD.parent.mkdir(parents=True, exist_ok=True)
    DIFF_MD.write_text(diff_md + "\n", encoding="utf-8")

    if fetch_ok:
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT.write_text(json.dumps(cur, indent=2) + "\n", encoding="utf-8")

    log_run({"ts": ts, "new_free": n_new_free, "price_drops": n_price_drops, "fetch_ok": fetch_ok})

    if "--md" in sys.argv:
        print(diff_md)
    else:
        print(f"model-quota-radar: fetch_ok={fetch_ok} new_free={n_new_free} "
              f"price_drops={n_price_drops} -> {DIFF_MD}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
