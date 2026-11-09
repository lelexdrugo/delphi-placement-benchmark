# experiments/orchestrator/analysis/

Post-run reducer for the DELPHI evaluation campaign. Reads a single
`experiments/runs/<id>/` directory and emits:

- `runs/<id>/analysis/<metric>.csv` — one CSV per metric;
- `runs/<id>/analysis/no-pollution.csv` — bi-directional invariant
  audit (forward + reverse leaks);
- `runs/<id>/analysis/summary.json` — aggregate per-baseline
  projection, read by `reduce_cells.py` and the plot generators.

This README documents the operator-level surface.

Each cell (policy x condition) is run k times. `reduce_cells.py` reduces
the k per-run `summary.json` files of a cell to the **median over the
runs**, with the min-max over the same runs; jobs are never pooled
across runs:

```powershell
python -m experiments.orchestrator.analysis.reduce_cells `
    --cells cells.json --out reduced.json
```

where `cells.json` maps a cell name to its run directories.

For figure generation against the `summary.json`s this analyzer
produces, see the sibling `plots/` subpackage. The plot CLI lives at the
orchestrator top level (`python -m experiments.orchestrator plots <name>
...`), not nested under `analysis`.

## Read-only contract

The analyzer **reads** PostgreSQL (via port-forward) and the
Kubernetes API (`kubectl get … -o json`). It never writes to either.

## CLI

```powershell
python -m experiments.orchestrator analysis `
    --run-dir experimentsuns\<id> `
    [--baseline static-karmada|heuristic-scorer|delphi-full] `
    [--registry experiments/specs/cluster-registry.yaml] `
    [--registry-variant <path-or-id>] `
    [--db-env experiments/db/.env] `
    [--no-db] [--no-kube] [--no-portforward] `
    [--skip-pollution-check]
```

`python -m experiments.orchestrator.cli analysis ...` is the same
command.

Offline, without a database or a cluster, a run is re-scored from its
own records (see *Persisted kube records* below):

```powershell
python -m experiments.orchestrator.cli analysis --run-dir <dir> --no-db --no-kube
```

Exit codes:
- `0` ok
- `1` input error (run-dir not found / malformed manifest or persisted
  record / multi-state run)
- `2` no-pollution invariant violated (forward or reverse leak)
- `3` DB connection failed and `--no-db` not set

**Single-state runs only.** A multi-state run (a run directory containing
`multistate.json`) is rejected with exit code 1. Analysis of multi-state
runs is not part of this release; the reported campaigns are
single-state.

## Module layout

```
analysis/
  __init__.py
  cli.py             argparse for the `analysis` subcommand
  runner.py          top composer; the only function the CLI invokes
  loaders.py         file IO only (manifest, submission-log, timeline, registry)
  db.py              psycopg + auto port-forward + named-query parser
  queries.sql        every SQL string the analyzer issues
  kube.py            kubectl --context <ctx> get -o json wrappers (read-only)
  models.py          frozen dataclasses
  validators.py      bi-directional no-pollution invariant
  writers.py         deterministic CSV + summary.json emission
  registry_diff.py   which registry cells an active variant perturbs
  reduce_cells.py    median and min-max over the k runs of each cell
  metrics/           one file per metric
    placement_success.py
    completion_time.py
    completion_decomposition.py  time_to_pod_spawn_s + execution_runtime_s
    decision_latency.py
    admission_overhead.py     pending: paired-run protocol not implemented
    telemetry_overhead.py     pending: paired-run protocol not implemented
    intent_compliance.py      two-tier formula (region as hard gate)
    cost_proxy.py             cost_value low=1.0 / medium=2.0 / high=3.0
    data_movement.py          (intent.region × cluster.role) → local/near/far
    traceability.py           joins clusters_score + reason from DB
    cordon_response.py        gated on cordon-timestamps.yaml
  plots/             figure generators (top-level `plots` subcommand)
  tests/
```

## Metrics reference

| Metric              | Source                                                        | Status                             |
|---------------------|---------------------------------------------------------------|------------------------------------|
| placement_success   | `submission-log.csv`                                          | full                               |
| completion_time     | `applied_at_utc` (`submission-log.csv`, else `manifest.actual_submit_timestamp_utc`) + `Job.status.completionTime` (`job-status.json`, else live `kubectl get job`) | full; offline-re-scorable from `job-status.json` |
| completion_decomposition | aggregated `Job.status` (`startTime` + `completionTime`) + `applied_at_utc` | `time_to_pod_spawn_s` (time to pod spawn: from submission to the Job's `status.startTime` at the Karmada entry point) + `execution_runtime_s`; `pending` when no `startTime` (no `job-status.json` and kube off / jobs GC'd). Also emits `cost_proxy_execution_based`. Offline-re-scorable from `job-status.json` |
| decision_latency    | `decision_requests` table (foreground, COMPLETED)             | full; **DB-only** (not in any run artifact) |
| admission_overhead  | paired optimize-propagation:true vs false                     | pending; paired-run protocol not implemented |
| telemetry_overhead  | paired sidecar-injected vs not                                | pending; paired-run protocol not implemented |
| intent_compliance   | offline join (PropagationPolicy + registry + intent label)    | full. Reported metric: `hard_pass_rate_completion_aware` (hard passes over every submitted foreground job; an incomplete job counts as non-compliant), with `soft_score_mean_completion_aware` and `n_submitted` / `n_completed` / `completion_rate`. The completed-only `hard_pass_rate` / `soft_score_mean` are kept as diagnostics; the per-job CSV stays completed-only. Placement source: `runs/<id>/placements.json` (persisted at collection time, see *Persisted kube records* below), else `manifest.json.assigned_cluster`, else the live PP. Without any of them (`--no-kube` on a run without `placements.json`) `selected_cluster` is unset and the metric is `insufficient_data` (completion-aware pair `None`) |
| cost_proxy          | `cost_value[cluster.cost] × runtime_seconds`                  | full; offline-re-scorable from `placements.json` + `job-status.json` |
| data_movement       | offline classification (intent.region × cluster.role)         | full                               |
| traceability        | `clusters_score` + `reason` from DB + outcomes                | full; DB-only                      |
| cordon_response     | `cordon-timestamps.yaml` + per-job submit time + PP creation time + PP target | Reported value: `during_cordon_strict_fraction_targeting_cordoned` (of the foreground jobs both submitted and decided inside the actual cordon window, the fraction whose decision names the cordoned member; lower is better). Only runs when `cordon-timestamps.yaml` is present. Figure: `plots cordon-avoidance` (PNG+PDF) |

`applied_at_utc` is when `kubectl apply` of the Job returned, taken on the
operator's clock (as is the cordon window), while the `Job.status` and
PropagationPolicy timestamps come from the control plane: the clocks
should be NTP-synchronised.

## Persisted kube records (`placements.json`, `job-status.json`)

Two things the analyzer reads through kubectl disappear when the campaign's
Jobs are deleted: each job's selected cluster (in its generated
`PropagationPolicy`, garbage-collected with the Job) and the aggregated
`Job.status.startTime` / `completionTime`. So that a run stays re-scorable
from its own records, the tail of `submit` calls
`collect.capture_kube_records`, which performs the analyzer's own read-only
lookups (`kube.get_propagation_policy`, `kube.get_job`) per applied
foreground job and writes two sibling artifacts:

```json
// runs/<id>/placements.json
{"schema_version": 1, "context": "...", "namespace": "...",
 "source": "PropagationPolicy.spec.placement.clusterAffinity.clusterNames[0]",
 "placements": {"<job>": {"selected_cluster": "edge-1", "pp_name": "pp-<job>",
                          "pp_generation": 1, "pp_created_at": "<RFC3339>",
                          "recorded_at": "<UTC ISO>"}}}
// runs/<id>/job-status.json
{"schema_version": 1, "context": "...", "namespace": "...",
 "source": "Job.status (aggregated, Karmada entrypoint)",
 "jobs": {"<job>": {"start_time": "<RFC3339>", "completion_time": "<RFC3339>",
                    "recorded_at": "<UTC ISO>"}}}
```

Only fields some metric consumes are stored: `startTime` feeds
`time_to_pod_spawn_s` / `execution_runtime_s`; `completionTime` feeds
`completion_time`, `cost_proxy` and the "completed" upgrade. Two files, not
one: each maps to one Kubernetes object, and they have different lifecycles
— a placement is final once the PP exists, whereas a `completionTime` can
legitimately arrive after the capture (a job that timed out in `submit`) and
be filled in by a later backfill.

`collect` then mirrors them into `manifest.json.jobs[].assigned_cluster` /
`completed_at`. To backfill a run whose Jobs still exist, run
`collect --run-id <id> --capture-kube-records` (plain `collect` stays
cluster-free). A recorded non-null value is never replaced; null values may
be filled.

Analyzer resolution per job and field: persisted artifact → manifest mirror
→ live object. A null persisted value falls back to the live one. When a
persisted and a live value both exist and differ, the persisted
(collection-time) value is scored and a note
(`placement disagreement for <job>: …` /
`job-status <field> disagreement for <job>: …`) is written to
`summary.json.notes` and stderr. The persisted `pp_created_at` /
`pp_generation` travel with the placement, so `cordon_response` is
re-scorable offline too. Nothing is invented: a completed job with no
placement is skipped (the completion-aware pair is `None`), and a job with
no persisted `startTime` stays out of the decomposition. A malformed
artifact is an input error (exit 1).

**Still DB-only.** `decision_latency` (the `decision_requests` lifecycle),
the `clusters_score` / `reason` columns of `traceability`, the no-pollution
invariant and join coverage come from PostgreSQL, and no run artifact
records them. `experiments/db/restore-seed.sh` drops and recreates the
whole DB before each measured run, so these rows do not survive the next
baseline's seed restore. For those metrics the only durable record is the
run's own `analysis/` CSVs.

## No-pollution invariant

Bi-directional check, scoped to **this run's** submission-log
job names — NOT a window-wide scan that would flag unrelated
calibration probes or background-load Jobs from other runs:

- **Forward**: any foreground job whose DB `kind != 'foreground'` is
  a leak (controller forwarded the role annotation wrong).
- **Reverse**: any background job whose DB `kind == 'foreground'` is
  a leak (background submission entered the agent-history surface).

The SQL mirrors the history filter of the placement middleware (not
published here), which reads `kind = 'foreground'`. **If the analyzer
detects a divergence, the middleware is wrong, not the validator.** Never
relax `queries.sql` to mask a real drift in the middleware.

## Idempotency

Re-running the analyzer on the same `--run-dir` (with the same
cluster + DB state at that moment) produces a byte-identical
`analysis/` directory. The runner's deterministic output ordering is
covered by `tests/test_runner_integration.py`.

## Database access

The analyzer reads PostgreSQL via a kubectl port-forward from
`experiments/db/port-forward.ps1` (or `.sh`). The helper is
idempotent — if a forward is already healthy, it is reused. Pass
`--no-portforward` if a port-forward is already established by hand.

Credentials live in `experiments/db/.env` (gitignored). See
[`experiments/db/.env.example`](../../db/.env.example) for the
expected keys, including the `PG_K8S_*` port-forward target.

## Testing

```powershell
python -m pytest experiments\orchestrator\analysis\tests -v
```

Test files include:
- `test_intent_compliance.py` — table-driven over (profile × cluster)
  pairs;
- `test_intent_compliance_completion_aware.py` — completion-aware
  edge cases on synthetic job records. Replaying measured run
  directories is not included, because those run directories are not
  published with this repository;
- `test_no_pollution.py` — synthetic injected leak triggers
  `PollutionDetected`;
- `test_runner_integration.py` — fixture run-dir + StubDB; byte-stable
  on re-run; a multi-state run is rejected;
- `test_traceability.py` — clusters_score / reason joined correctly;
- `test_cordon_response.py` — phase classification, the strict
  fraction, and runner gating;
- `test_reduce_cells.py` — median over runs, not over pooled jobs; a
  missing per-run value blocks the median;
- `test_registry_diff.py`, `test_registry_variants.py` — perturbed
  cells of the committed registry variants;
- `test_persisted_kube_records.py` — `placements.json` / `job-status.json`
  fallback: offline reproduces online (intent compliance, and the whole
  `analysis/` dir byte for byte incl. completion time / decomposition /
  decision latency / cost proxy), no artifact ⇒ unchanged, persisted-vs-live
  disagreement per field, late-completion fallback + backfill. Capture/collect
  side: `experiments/orchestrator/tests/test_kube_records_capture.py`.

## Out of scope

- Admission and telemetry overhead: the paired-run protocol is not
  implemented, so both metrics stay `pending`.
- Multi-state runs (`multistate.json`): rejected, see *CLI* above.
