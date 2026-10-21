#!/usr/bin/env python3
"""Ledger-to-graph materializer (cc-stack P5) — the idempotent, $0 DR reconstructor
that turns the decision ledger into a FILE-BACKED, queryable gbrain graph.

SYSTEM OF RECORD = the JSONL ledger (~/.claude/decisions/*.jsonl, git-tracked).
CANONICAL GBRAIN LAYER = markdown FILES in a git source (~/gbrain-sources/decisions).
DERIVED CACHE = the gbrain DB, rebuilt from those files.

Why files, not `gbrain put` (grounded against the RUNNING build v0.42.56, 2026-07-05):
  gbrain's own system-of-record doc mandates FS-canonical markdown for any knowledge
  category (docs/architecture/system-of-record.md); DB-only pages do not survive a
  destructive rebuild. So each decision is a real .md file; the DB is regenerated from
  it via `gbrain sync --source decisions`.

The two edge channels (both grounded in v0.42 source, deep-read 2026-07-05):
  1. supersedes_dec (decision -> decision): NO markdown/frontmatter/wikilink channel can
     produce this TYPED edge on v0.42 — `inferLinkType` is hardcoded verbs (pack-unaware),
     the NER pass excludes `decision` from its gazetteer + target-type map
     (by-mention.ts LINKABLE_ENTITY_TYPES, extract-ner.ts buildTargetTypeMap), and pack
     `frontmatter_links` for custom types is dead code (frontmatterLinkTypeFromPack wired
     to nothing). The ONLY channel for a custom typed edge is the `link`/add_link op
     (operations.ts:1960-1999), which writes a custom link_source that reconciliation and
     `extract --stale` NEVER touch. Not FS-backed -> DR-safety is provided by THIS script:
     re-run it after any rebuild and the 6 edges are restored in seconds, $0.
     CLI flag is `--link_type` (NOT `--type`; `--type` is silently ignored).
  2. A body wikilink `[[decisions/<older>]]` is ALSO written — it yields a file-backed
     `mentions` edge (DR-safe, regenerates from text on rebuild) as a durable backstop, so
     even without the script the supersession is visible in the graph (just untyped).

Idempotent: files overwritten in place; `gbrain link` is ON CONFLICT DO UPDATE on
(from, to, link_type); `sources add`/`sync` skip-unchanged. Safe to run any time.

Usage:  python3 ledger-to-graph.py            # materialize files + sync + typed edges
        python3 ledger-to-graph.py --dry-run   # write files only, no gbrain mutation
"""
import json, os, sys, glob, subprocess

DEC_DIR     = os.path.expanduser("~/.claude/decisions")
SOURCE_ID   = "decisions"
SOURCE_DIR  = os.path.expanduser("~/gbrain-sources/decisions")
PAGES_SUBDIR= "decisions"                      # slug prefix: decisions/<id>
LINK_TYPE   = "supersedes_dec"
LINK_SOURCE = "ledger-parse"
DRY_RUN     = "--dry-run" in sys.argv
HOME        = os.path.expanduser("~")


def dec_slug(rid):
    """decisions/<id>, lowercased. gbrain lowercases slugs (utils.ts validateSlug);
    keep the exact original id in frontmatter `id:` for traceability."""
    return f"{PAGES_SUBDIR}/{rid.lower()}"


def load():
    rows, bad = [], 0
    for path in sorted(glob.glob(os.path.join(DEC_DIR, "*.jsonl"))):
        for line in open(path, encoding="utf-8", errors="replace"):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                bad += 1
    return rows, bad


def dedup(rows):
    seen, uniq = set(), []
    for r in rows:
        rid = r.get("id")
        if rid and rid in seen:
            continue
        if rid:
            seen.add(rid)
        uniq.append(r)
    return uniq


def yaml_str(v):
    return '"' + str(v).replace("\\", "").replace('"', "'") + '"'


def page_content(r):
    """Frontmatter (type: decision explicit — beats path inference) + one-line body.
    Body carries a `Supersedes [[decisions/<older>]]` wikilink when applicable: a
    file-backed `mentions` backstop edge that survives rebuild from text alone."""
    rid   = r.get("id", "")
    title = str(r.get("title") or rid)
    fm = [
        "---",
        f"title: {yaml_str(title)}",
        "type: decision",
        f"id: {yaml_str(rid)}",
        f"dec_type: {yaml_str(r.get('type', ''))}",
        f"confidence: {yaml_str(r.get('confidence', ''))}",
        f"scope: {yaml_str(r.get('scope', ''))}",
        f"project: {yaml_str(r.get('project', ''))}",
    ]
    tags = r.get("tags") or []
    if tags:
        fm.append("tags: [" + ", ".join(yaml_str(t) for t in tags) + "]")
    fm.append("---")
    body = str(title).replace("\n", " ").strip() + "."
    sup = r.get("supersedes")
    if sup:
        body += f"\n\nSupersedes [[{dec_slug(sup)}]]."
    return "\n".join(fm) + "\n\n" + body + "\n"


def run(cmd, cwd=HOME):
    if DRY_RUN:
        return True, "(dry-run)"
    p = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
    return p.returncode == 0, (p.stdout + p.stderr).strip()


def ensure_source():
    if DRY_RUN:
        return
    ok, out = run(["gbrain", "sources", "list"])
    # Match the source id as a standalone first-column token — NOT a substring
    # (substring false-positives on e.g. default's local_path .../decisions-pilot).
    registered = any(
        ln.split() and ln.split()[0] == SOURCE_ID for ln in out.splitlines()
    )
    if not registered:
        run(["gbrain", "sources", "add", SOURCE_ID, "--path", SOURCE_DIR, "--federated"])


def git_commit():
    if DRY_RUN:
        return
    if not os.path.isdir(os.path.join(SOURCE_DIR, ".git")):
        run(["git", "init", "-q"], cwd=SOURCE_DIR)
    run(["git", "add", "-A"], cwd=SOURCE_DIR)
    run(["git", "-c", "user.email=ledger@local", "-c", "user.name=ledger",
         "commit", "-qm", "materialize decision ledger"], cwd=SOURCE_DIR)


def main():
    rows, bad = load()
    rows = dedup(rows)
    if not rows:
        print("(decision ledger empty — nothing to materialize)")
        return

    pages_dir = os.path.join(SOURCE_DIR, PAGES_SUBDIR)
    os.makedirs(pages_dir, exist_ok=True)
    written = 0
    for r in rows:
        rid = r.get("id")
        if not rid:
            continue
        with open(os.path.join(pages_dir, f"{rid.lower()}.md"), "w", encoding="utf-8") as fh:
            fh.write(page_content(r))
        written += 1

    edges = [(r["id"], r["supersedes"]) for r in rows
             if r.get("supersedes") and r.get("id")]

    if not DRY_RUN:
        git_commit()
        ensure_source()
        run(["gbrain", "sync", "--source", SOURCE_ID])
        run(["gbrain", "extract", "--stale"])
        for newer, older in edges:
            run(["gbrain", "link",
                 "--from", dec_slug(newer), "--to", dec_slug(older),
                 "--link_type", LINK_TYPE, "--link_source", LINK_SOURCE])

    print("=" * 64)
    print(f"  LEDGER -> GRAPH ({SOURCE_ID}) — {written} decision files"
          + (f", {bad} malformed lines skipped" if bad else ""))
    print("=" * 64)
    print(f"Files written:        {written}  -> {pages_dir}")
    print(f"Typed edges (supersedes_dec via link op): {len(edges)}")
    for n, o in edges:
        print(f"  {dec_slug(n)} -> {dec_slug(o)}")
    if DRY_RUN:
        print("\n(dry-run — files written, NO gbrain mutation, NO git commit)")


if __name__ == "__main__":
    main()
