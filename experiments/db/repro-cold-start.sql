-- repro-cold-start.sql — iter-4d.0c reproduction of the MCP
-- history-retrieval cold-start observed on iter-4d's DELPHI-Full run
-- (40/40 jobs reported empty history despite a 144-fg Phase-A seed).
--
-- This is a SELECT-only diagnostic script. It MUST NOT mutate any
-- table. It runs idempotently as `psql ... -f repro-cold-start.sql`
-- (or as a single transaction in any client). The audit writeup at
-- experiments/runs/iter-4d-summary/history-retrieval-audit.md
-- cross-references each labelled section below.
--
-- Prerequisites:
--   1. `pwsh experiments/db/port-forward.ps1` (idempotent) — opens
--      tcp/<PG_LOCAL_PORT> against svc/postgres -n karmada-system on
--      the on-prem context. Default PG_LOCAL_PORT=5432.
--   2. experiments/db/.env populated (gitignored). The README in this
--      directory documents the keys.
--   3. The DB still has iter-4d's run + the Phase-A seed restored
--      before each baseline (see cross-baseline-comparison.json).
--
-- Sample job: balanced/cpu intent, run-id
-- `effectiveness-delphi-full-20260525T044028Z`, job index 005. Both
-- the seed-side mismatch and the within-run skeleton finding reproduce
-- on this sample; the audit writeup names whether the finding
-- generalises to all 40 rows.
\set sample_job 'delphi-effectiveness-delphi-full-20260525t044028z-005-cpu'
\set late_sample_job 'delphi-effectiveness-delphi-full-20260525t044028z-038-cpu'

-- ---------------------------------------------------------------------------
-- [A] Snapshot composition. Should be 144 fg + 12 bg + iter-4d's writes.
-- ---------------------------------------------------------------------------
\echo '== [A] decision_requests by (kind, status) =='
SELECT kind, status, COUNT(*) AS n
FROM decision_requests
GROUP BY kind, status
ORDER BY kind, status;

-- ---------------------------------------------------------------------------
-- [B] The iter-4d sample row. Inspect `image`, `job_signature`,
--     `propagation_requirements::text`. Notice (i) the run-id-tagged
--     image and (ii) the JSONB-normalized text form of
--     propagation_requirements: keys reordered by (length, alpha) and
--     `weight: 1.0` rendered as `weight: 1`.
-- ---------------------------------------------------------------------------
\echo '== [B] iter-4d sample (decision_requests row) =='
SELECT job_name,
       image,
       job_signature,
       command_hash,
       propagation_requirements::text AS prop_reqs_jsonb_text,
       kind,
       status,
       created_at,
       updated_at
FROM decision_requests
WHERE job_name = :'sample_job'
LIMIT 1;

-- ---------------------------------------------------------------------------
-- [C] A representative Phase-A seed row (cpu, foreground, completed).
--     Compare `image` and `job_signature` with [B]; the image-tag
--     suffix differs by design (iter-4c.2 immutable per-experiment
--     tag), so the seed signature can never equal an iter-4d
--     signature.
-- ---------------------------------------------------------------------------
\echo '== [C] Phase-A seed sample (cpu/foreground/completed) =='
SELECT job_name,
       image,
       job_signature,
       command_hash,
       propagation_requirements::text AS prop_reqs_jsonb_text,
       created_at
FROM decision_requests
WHERE job_name LIKE 'delphi-bootstrap-phase-a-%-cpu-idle'
  AND kind = 'foreground'
  AND status = 'COMPLETED'
ORDER BY created_at ASC
LIMIT 1;

-- ---------------------------------------------------------------------------
-- [D] EXACT-HISTORY QUERY THE MCP TOOL WOULD ISSUE for the sample's
--     signature. Mirrors `get_job_history` in
--     code/delphi-system/decision-maker/internal/repository/decision_repository.go:330-341.
--
--     For sample_job (job index 005): returned 0 rows at decision time
--     because no prior iter-4d sibling had reached COMPLETED yet AND
--     Phase-A's image-tag-different rows are unreachable. Today the
--     same query returns 5 rows (all later iter-4d siblings finished
--     after 005's decision was written).
-- ---------------------------------------------------------------------------
\echo '== [D] get_job_history exact-signature query (current DB state) =='
SELECT id, job_name, job_signature, created_at, updated_at
FROM decision_requests
WHERE status = 'COMPLETED' AND kind = 'foreground'
  AND job_signature = (
    SELECT job_signature FROM decision_requests WHERE job_name = :'sample_job' LIMIT 1
  )
ORDER BY created_at DESC
LIMIT 5;

-- ---------------------------------------------------------------------------
-- [E] HISTORICAL SIBLINGS THAT WERE COMPLETED *BEFORE* THE SAMPLE'S
--     decision was written. This is what `get_job_history` would have
--     returned at decision time for `sample_job`.
-- ---------------------------------------------------------------------------
\echo '== [E] siblings COMPLETED before sample_job.updated_at (=decision-write moment) =='
SELECT job_name, status, created_at, updated_at
FROM decision_requests
WHERE job_signature = (SELECT job_signature FROM decision_requests WHERE job_name = :'sample_job' LIMIT 1)
  AND status = 'COMPLETED'
  AND kind = 'foreground'
  AND updated_at < (SELECT updated_at FROM decision_requests WHERE job_name = :'sample_job' LIMIT 1)
ORDER BY updated_at DESC;

-- ---------------------------------------------------------------------------
-- [F] Same at-decision-time check for the LATE sample (job 038-cpu).
--     Demonstrates that late-in-run jobs DO have prior within-run
--     siblings COMPLETED at decision time, yet still report cold-start
--     in their traceability rationale. This is the empirical evidence
--     that the symptom cannot be explained by signature mismatch alone
--     and points to mechanism M2 (the metrics-join schema mismatch
--     analysed in [H]).
-- ---------------------------------------------------------------------------
\echo '== [F] siblings COMPLETED before late_sample_job.updated_at =='
SELECT job_name, status, created_at, updated_at
FROM decision_requests
WHERE job_signature = (SELECT job_signature FROM decision_requests WHERE job_name = :'late_sample_job' LIMIT 1)
  AND status = 'COMPLETED'
  AND kind = 'foreground'
  AND updated_at < (SELECT updated_at FROM decision_requests WHERE job_name = :'late_sample_job' LIMIT 1)
ORDER BY updated_at DESC;

-- ---------------------------------------------------------------------------
-- [G] Per-image distribution of foreground+completed rows for the
--     same propagation_requirements value as the sample.
--     Shows the per-run-id tagging: Phase-A vs iter-4d tags hold rows
--     for the SAME logical intent profile but the MCP query filters by
--     `image = $` exact equality, so the cross-run rows are
--     unreachable (mechanism M1).
-- ---------------------------------------------------------------------------
\echo '== [G] per-image counts for the sample propagation_requirements =='
SELECT image, COUNT(*) AS n
FROM decision_requests
WHERE status = 'COMPLETED' AND kind = 'foreground'
  AND propagation_requirements @> (
    SELECT propagation_requirements FROM decision_requests WHERE job_name = :'sample_job' LIMIT 1
  )
GROUP BY image
ORDER BY n DESC;

-- ---------------------------------------------------------------------------
-- [H] THE METRICS JOIN — the dominant root cause for the
--     "skeleton object" rationale. The MCP tool's per-row metrics
--     pass (decision_repository.go:411-414) joins
--
--         WHERE job_name = $1 AND namespace = $2 AND image = $3
--           AND preferences = $4
--           AND sample_time >= $5 AND sample_time <= $6
--
--     with $4 = `string(propReqBytes)` (JSONB cast to text, PG-
--     normalized key order + numeric form) and $5..$6 = the decision
--     row's [created_at, updated_at] window. Both are mismatched on
--     iter-4d data:
--
--       M2  job_metrics.preferences is the RAW annotation text the
--           webhook injected as PREFERENCES env var into the sidecar
--           (controller/webhooks/job_webhook.go:231,242). The
--           sidecar publishes it verbatim; nats-listener persists it
--           verbatim. So that column holds the YAML-author's key
--           order (latency, cost, region, weight) with weight as a
--           float (1.0). decision_requests.propagation_requirements
--           is JSONB, so its `::text` cast renders the PG-normalized
--           form (cost, region, weight, latency; 1 not 1.0). The
--           text-equality join always returns zero rows.
--
--       M3  job_start_time and the first sample_time arrive *after*
--           the decision row's updated_at (the decision finishes
--           BEFORE the controller materializes the policy, Karmada
--           propagates, the pod starts, and the sidecar publishes
--           its first sample). So even if $4 matched, the
--           sample_time window [created_at, updated_at] excludes
--           all of the metrics for the joined job.
-- ---------------------------------------------------------------------------
\echo '== [H1] decision_requests vs job_metrics preferences string for sample_job =='
SELECT
    'decision_requests.propagation_requirements::text' AS source,
    (SELECT propagation_requirements::text FROM decision_requests WHERE job_name = :'sample_job' LIMIT 1) AS value
UNION ALL
SELECT
    'job_metrics.preferences (distinct)',
    (SELECT DISTINCT preferences FROM job_metrics WHERE job_name = :'sample_job' LIMIT 1);

\echo '== [H2] MCP metrics-join count (preferences=text AND sample_time window) =='
WITH req AS (
    SELECT image, namespace, propagation_requirements::text AS prefs_text,
           created_at, updated_at
    FROM decision_requests
    WHERE job_name = :'sample_job'
    LIMIT 1
)
SELECT COUNT(*) AS rows_joined
FROM job_metrics jm, req r
WHERE jm.job_name = :'sample_job'
  AND jm.namespace = r.namespace
  AND jm.image     = r.image
  AND jm.preferences = r.prefs_text
  AND jm.sample_time >= r.created_at
  AND jm.sample_time <= r.updated_at;

\echo '== [H3] same join, dropping the sample_time window =='
WITH req AS (
    SELECT image, namespace, propagation_requirements::text AS prefs_text
    FROM decision_requests
    WHERE job_name = :'sample_job'
    LIMIT 1
)
SELECT COUNT(*) AS rows_joined_no_window
FROM job_metrics jm, req r
WHERE jm.job_name = :'sample_job'
  AND jm.namespace = r.namespace
  AND jm.image     = r.image
  AND jm.preferences = r.prefs_text;

\echo '== [H4] same join, dropping preferences (sample_time window still applied) =='
WITH req AS (
    SELECT image, namespace, created_at, updated_at
    FROM decision_requests
    WHERE job_name = :'sample_job'
    LIMIT 1
)
SELECT COUNT(*) AS rows_joined_no_prefs
FROM job_metrics jm, req r
WHERE jm.job_name = :'sample_job'
  AND jm.namespace = r.namespace
  AND jm.image     = r.image
  AND jm.sample_time >= r.created_at
  AND jm.sample_time <= r.updated_at;

\echo '== [H5] same join, dropping BOTH preferences and the time window =='
SELECT COUNT(*) AS rows_joined_loose
FROM job_metrics jm
WHERE jm.job_name = :'sample_job'
  AND jm.namespace = 'delphi-experiments'
  AND jm.image     = (SELECT image FROM decision_requests WHERE job_name = :'sample_job' LIMIT 1);

\echo '== [H6] actual sample_time range vs decision_request window =='
SELECT
    (SELECT MIN(sample_time) FROM job_metrics WHERE job_name = :'sample_job') AS first_sample,
    (SELECT MAX(sample_time) FROM job_metrics WHERE job_name = :'sample_job') AS last_sample,
    (SELECT created_at FROM decision_requests WHERE job_name = :'sample_job' LIMIT 1) AS decreq_created_at,
    (SELECT updated_at FROM decision_requests WHERE job_name = :'sample_job' LIMIT 1) AS decreq_updated_at;

-- ---------------------------------------------------------------------------
-- [I] WOULD-MATCH SQL. If the MCP query relaxed the image filter to
--     the repo (drop the tag), AND if `preferences` used JSONB
--     containment instead of text equality, AND if the time window
--     were the workload runtime instead of the decision-request
--     window — how many rows would the sample's history retrieve?
--
--     This is the upper bound iter-4d.3 can target IF the Go bugs in
--     [H] are fixed and the seed shares the workload image repo with
--     the campaign. With the seed unchanged but the Go fixes landed,
--     this query should show >> 0 rows of telemetry.
-- ---------------------------------------------------------------------------
\echo '== [I] would-match foreground rows under relaxed image + JSONB containment =='
WITH req AS (
    SELECT split_part(image, ':', 1) AS repo,
           command_hash,
           propagation_requirements
    FROM decision_requests
    WHERE job_name = :'sample_job'
    LIMIT 1
)
SELECT COUNT(*) AS would_match_decision_requests
FROM decision_requests dr, req r
WHERE dr.status = 'COMPLETED' AND dr.kind = 'foreground'
  AND split_part(dr.image, ':', 1) = r.repo
  AND dr.command_hash               = r.command_hash
  AND dr.propagation_requirements @> r.propagation_requirements;

\echo '== [I2] would-match per-job metrics under JSONB containment join (no time window) =='
WITH req AS (
    SELECT split_part(image, ':', 1) AS repo,
           command_hash,
           propagation_requirements
    FROM decision_requests
    WHERE job_name = :'sample_job'
    LIMIT 1
)
SELECT COUNT(*) AS would_match_metric_samples
FROM job_metrics jm,
     decision_requests dr,
     req r
WHERE dr.status = 'COMPLETED' AND dr.kind = 'foreground'
  AND split_part(dr.image, ':', 1) = r.repo
  AND dr.command_hash               = r.command_hash
  AND dr.propagation_requirements @> r.propagation_requirements
  AND jm.job_name  = dr.job_name
  AND jm.namespace = dr.namespace
  AND jm.preferences::jsonb @> r.propagation_requirements;
