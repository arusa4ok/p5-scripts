-- Retire the pages that exist twice: once in the `default` junk-drawer source and
-- once in the project source that actually owns them.
--
-- WHY THEY EXIST: gbrain's write path carries a pervasive `sourceId ?? 'default'`
-- fallback, so anything written without an explicit source lands in `default`.
-- Audit: p5-loop/gbrain-default-source-audit-2026-08-13.md
--
-- WHAT THIS IS NOT: a delete. Every inbound edge is repointed at the surviving
-- copy first, and the pages are SOFT-deleted (`deleted_at`), so the whole thing
-- is reversible without the dump until someone runs `purge_deleted_pages`.
--
-- SCOPE, measured rather than assumed (2026-08-13):
--   826 same-slug pairs exist, but only 516 are byte-identical in `compiled_truth`.
--   `content_hash` is NOT usable for this comparison — it agreed on only 85 of the
--   516 identical-text pairs, because it is computed over more than the text.
--   Those 516 pairs are 484 distinct default pages; 7 of them match MORE THAN ONE
--   project page and are deliberately LEFT ALONE — picking one of seven targets is
--   the guesswork this cleanup exists to end. 477 unambiguous pages are retired.
--
-- Run inside a transaction and read the verification output before COMMIT.
--   psql -d gbrain -f scripts/p5/dedupe-default-source.sql

BEGIN;

-- The mapping. `having count(distinct o.id) = 1` is what excludes the ambiguous 7.
CREATE TEMP TABLE dupe_map ON COMMIT DROP AS
SELECT d.id AS dup_id, min(o.id) AS keep_id
FROM pages d
JOIN pages o ON d.slug = o.slug AND o.source_id <> 'default'
WHERE d.source_id = 'default'
  AND d.deleted_at IS NULL AND o.deleted_at IS NULL
  AND d.compiled_truth = o.compiled_truth
GROUP BY d.id
HAVING count(DISTINCT o.id) = 1;

-- 1. Inbound edges the survivor ALREADY has. Repointing these would create a
--    duplicate edge, so they are dropped rather than moved. Measured: 1757.
DELETE FROM links l
USING dupe_map m
WHERE l.to_page_id = m.dup_id
  AND EXISTS (
    SELECT 1 FROM links x
    WHERE x.from_page_id = l.from_page_id
      AND x.to_page_id = m.keep_id
      AND x.link_type IS NOT DISTINCT FROM l.link_type);

-- 2. The remaining inbound edges move to the survivor. This is the step that
--    makes the retirement non-destructive to the graph. Measured: 1698.
UPDATE links l SET to_page_id = m.keep_id
FROM dupe_map m WHERE l.to_page_id = m.dup_id;

-- 3. Outbound edges of the retired page are redundant: the survivor has identical
--    text, so link extraction already produced the same edges from it. Measured: 2803.
DELETE FROM links l USING dupe_map m WHERE l.from_page_id = m.dup_id;

-- 4. Chunks of a hidden page are dead weight in the embedding pool. Measured: 3585.
DELETE FROM content_chunks c USING dupe_map m WHERE c.page_id = m.dup_id;

-- 5. Soft delete. NOT a purge — `deleted_at` is what makes this reversible.
UPDATE pages p SET deleted_at = now()
FROM dupe_map m WHERE p.id = m.dup_id;

-- ---- verification: read this before COMMIT ----
SELECT 'pages retired'              AS check, count(*) FROM pages p JOIN dupe_map m ON p.id = m.dup_id WHERE p.deleted_at IS NOT NULL
UNION ALL
SELECT 'DANGLING inbound (must be 0)', count(*) FROM links l JOIN dupe_map m ON l.to_page_id   = m.dup_id
UNION ALL
SELECT 'DANGLING outbound (must be 0)', count(*) FROM links l JOIN dupe_map m ON l.from_page_id = m.dup_id
UNION ALL
SELECT 'orphan chunks (must be 0)',    count(*) FROM content_chunks c JOIN dupe_map m ON c.page_id = m.dup_id
UNION ALL
SELECT 'survivors still live (must equal pages retired)', count(DISTINCT m.keep_id)
  FROM dupe_map m JOIN pages p ON p.id = m.keep_id WHERE p.deleted_at IS NULL
UNION ALL
SELECT 'ambiguous, deliberately untouched', count(*) FROM (
  SELECT d.id FROM pages d JOIN pages o ON d.slug = o.slug AND o.source_id <> 'default'
  WHERE d.source_id = 'default' AND d.deleted_at IS NULL AND o.deleted_at IS NULL
    AND d.compiled_truth = o.compiled_truth
  GROUP BY d.id HAVING count(DISTINCT o.id) > 1) t;

-- COMMIT;   <- run manually after reading the verification block
