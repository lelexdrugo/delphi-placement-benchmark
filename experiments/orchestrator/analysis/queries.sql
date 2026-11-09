-- Named SQL queries consumed by experiments/orchestrator/analysis/db.py.
-- One block per query, separated by a `-- name: <query_id>` comment.
-- The parser is intentionally tiny (regex on the name marker) — do NOT
-- use a complex SQL syntax that breaks naive splitting.
--
-- These queries mirror the placement middleware's history filter
-- (kind = 'foreground'; the middleware is not published here), so that a
-- divergence in the middleware surfaces as a no-pollution failure in the
-- analyzer. Never relax the analyzer's filters to mask a real drift in
-- the middleware.
--
-- Placeholders use psycopg3 DBAPI style (%s), not libpq native ($N): the
-- analyzer executes through psycopg3 cursors.

-- name: decision_latency_foreground
-- Return (created_at, last_attempt_at, latency_ms) for foreground requests
-- in the window. Not issued by the current metrics: decision_latency reads
-- the same timestamps from request_for_jobs.
SELECT job_name,
       created_at,
       last_attempt_at,
       EXTRACT(EPOCH FROM (last_attempt_at - created_at)) * 1000.0 AS latency_ms
FROM   decision_requests
WHERE  status      = 'COMPLETED'
  AND  kind        = 'foreground'
  AND  job_name    = ANY(%s::text[]);

-- name: background_audit
-- Auditability check: every background request in the window IS persisted
-- (inverse of the pollution invariant — we should never silently drop
-- background rows). Not issued by the current metrics.
SELECT count(*) AS persisted_count
FROM   decision_requests
WHERE  kind        = 'background'
  AND  created_at  BETWEEN %s AND %s;

-- name: no_pollution_invariant
-- Given a list of FOREGROUND job names from this run's submission-log,
-- return any DB row whose name is in that list but whose `kind` column
-- is NOT 'foreground'. A non-empty result means a foreground submission
-- was persisted with the wrong kind — a controller-side role-forwarding
-- bug that would let the row escape the agent-history filter.
--
-- This is the actual integrity check, NOT "anything non-foreground in
-- the window". Background-load Jobs (cluster-state experiments) and
-- calibration probes legitimately persist with
-- kind='background' and must NOT be flagged as leaks just because they
-- live in the time window.
SELECT id,
       job_name,
       kind,
       status,
       created_at
FROM   decision_requests
WHERE  job_name = ANY(%s::text[])
  AND  kind    <> 'foreground';

-- name: no_reverse_pollution_invariant
-- Given a list of BACKGROUND job names from this run's submission-log
-- (background-load Jobs in cluster-state experiments, calibration
-- probes, preflight Jobs), return any DB row whose name is in that
-- list but whose `kind` column IS 'foreground'. A non-empty result
-- means a background submission accidentally entered the agent-history
-- surface — the exact data-pollution failure the no-pollution invariant
-- exists to catch. The middleware's history filter reads
-- `WHERE kind = 'foreground'`; if a background row was mis-labelled as
-- foreground, the agent would see it via MCP history.
SELECT id,
       job_name,
       kind,
       status,
       created_at
FROM   decision_requests
WHERE  job_name = ANY(%s::text[])
  AND  kind     = 'foreground';

-- name: request_for_jobs
-- Return the full DB projection (status, kind, clusters_score, reason,
-- timestamps) for the foreground jobs in this run. Drives traceability,
-- decision_latency and the join-coverage check.
-- clusters_score and reason live inside the `result` JSONB column (not as
-- top-level columns); we extract them here so the analyzer doesn't need
-- to know the storage shape. Adjust the JSON path if decision-maker ever
-- changes how the result blob is laid out.
SELECT id,
       job_name,
       status,
       kind,
       created_at,
       last_attempt_at,
       result -> 'clusters_score' AS clusters_score,
       result ->> 'reason'        AS reason
FROM   decision_requests
WHERE  job_name = ANY(%s::text[]);

-- name: feedback_count_in_window
-- Count feedback rows in the run window. Not issued by the current
-- metrics.
SELECT count(*) AS feedback_rows
FROM   feedback
WHERE  created_at BETWEEN %s AND %s;
