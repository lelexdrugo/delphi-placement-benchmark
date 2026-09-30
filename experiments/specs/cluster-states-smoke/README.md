# cluster-states-smoke

Smoke variants of the production cluster-state specs under
`../cluster-states/`. The `state_id` fields are kept identical so K8s
labels (`delphi.experiments/state-id: <state_id>`) and the analyzer's
per-state grouping behave exactly like the production runs — only the
timing knobs are compressed.

Compression rules (apply to every file here):

- `warmup_seconds`: production 60-90 → smoke 20 (or kept 0 for idle).
  Enough to let the K8s scheduler reconcile background-load pods on
  their pinned clusters before the foreground stream starts.
- `stabilisation_check`: production `true` → smoke `false`. The
  stabilisation poll's 300 s timeout dominates a 12-minute smoke
  budget; we accept "background-load Jobs are Running" as good enough
  for pipeline validation.
- `background_jobs[*].parameters.DURATION`: production `"5400s"`
  (90 min) → smoke `"300s"` (5 min). The smoke per-state window is
  ~140 s; 300 s gives a 2× safety margin so the background-load
  outlives the foreground without leaking past `inter_state_cooldown`.
- Background-load resource envelopes are **unchanged**. The smoke
  must still exercise the pin-to-cluster decision-maker handler and
  the no-pollution invariant, which both depend on the same Job
  shape as production.

Use:

```powershell
.venv\Scripts\python.exe -m experiments.orchestrator render-bootstrap `
    --spec experiments\specs\bootstrap-smoke.yaml `
    --cluster-states-dir experiments\specs\cluster-states-smoke `
    --run-id phase-a-smoke-<utc-ts>
```

When NOT to use this: any real Phase-A seed run, any Phase-B
effectiveness run, any timing-sensitive measurement. Smoke is for
pipeline validation only.
