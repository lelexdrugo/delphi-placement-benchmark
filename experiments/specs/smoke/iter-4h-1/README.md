# iter-4h-1 smoke manifests

Six hand-authored Job manifests, one per cell of the iter-4h-1 smoke
campaign (per [iter-4h-1-workmode-pivot.md][plan] § Q1=B). Each cell
pins to a single target cluster via
`delphi.experiments/pin-to-cluster`, exercising the iter-4h-1
task-bound stressor invocation against that architecture's hardware.

[plan]: ../../../plans/iter-4h-1-workmode-pivot.md

## Why hand-authored

These manifests bypass the renderer (per Q3=C). The renderer and the
three Jinja templates carry no `pin_to_cluster` conditional. The
smoke is a one-off, six-Job, operator-supervised batch; hand-authored
manifests make it self-contained and the pin annotation literally
visible in the file.

The K values (`CPU_OPS`, `MEMORY_OPS`) and `BURST_SIZE` here MUST
match the placeholders in
[`experiments/specs/effectiveness-taskbound.yaml`][spec]. If a future
commit tunes K in the spec, edit all six manifests in the same
commit.

[spec]: ../../effectiveness-taskbound.yaml

## Cell roster (Q1=B)

| Cell | Mode          | Pin            | K / size           | Purpose |
|------|---------------|----------------|--------------------|---------|
| 1    | cpu-ops       | on-prem        | CPU_OPS=1000000    | amd64 anchor for CPU K order-of-magnitude. |
| 2    | cpu-ops       | public-cloud   | CPU_OPS=1000000    | amd64 second anchor (different HW profile). |
| 3    | cpu-ops       | edge-1         | CPU_OPS=1000000    | arm64 anchor for CPU. |
| 4    | memory-ops    | on-prem        | MEMORY_OPS=10000   | amd64 anchor for memory K. |
| 5    | memory-ops    | edge-1         | MEMORY_OPS=10000   | arm64 anchor for memory. |
| 6    | network-burst | on-prem        | BURST_SIZE=200M    | confirms iperf3 is no longer wall-clock-clipped. |

## How to use

Each cell is a `.yaml.tmpl` with two `${...}` placeholders that
must be substituted before apply. Both come from the run-id's
`images.json` produced by `prepare-images.sh --mode build`.

Bash:

```bash
# 1) Build / refresh the multi-arch images and write images.json:
bash experiments/runtime/prepare-images.sh \
    --mode build \
    --run-id iter-4h-1-smoke-$(date -u +%Y%m%dT%H%M%SZ)

# 2) Pin RUN_ID to whatever you passed above.
export RUN_ID=iter-4h-1-smoke-20260526T140000Z   # adjust
export SMOKE_IMAGE_STRESS_NG=$(jq -r .stress_ng experiments/runs/$RUN_ID/images.json)
export SMOKE_IMAGE_NET_STRESSER=$(jq -r .net_stresser experiments/runs/$RUN_ID/images.json)

# 3) Apply one cell (recommended for the first pass):
envsubst < experiments/specs/smoke/iter-4h-1/cell-1-cpu-on-prem.yaml.tmpl | \
    kubectl --context unique-logical-entrypoint apply -f -

# Or all six (after smoke-watching the first one):
for cell in experiments/specs/smoke/iter-4h-1/cell-*.yaml.tmpl; do
  envsubst < "$cell" | kubectl --context unique-logical-entrypoint apply -f -
done
```

PowerShell (Windows):

```powershell
bash experiments/runtime/prepare-images.sh `
    --mode build --run-id iter-4h-1-smoke-$(Get-Date -UFormat %Y%m%dT%H%M%SZ)

$env:RUN_ID = "iter-4h-1-smoke-20260526T140000Z"
$images = Get-Content experiments/runs/$env:RUN_ID/images.json | ConvertFrom-Json
$env:SMOKE_IMAGE_STRESS_NG    = $images.stress_ng
$env:SMOKE_IMAGE_NET_STRESSER = $images.net_stresser

# envsubst comes from Git for Windows under
#   C:\Program Files\Git\usr\bin\envsubst.exe
# PowerShell-native substitute if absent:
function Invoke-EnvSubst($path) {
    $body = Get-Content $path -Raw
    foreach ($v in 'RUN_ID','SMOKE_IMAGE_STRESS_NG','SMOKE_IMAGE_NET_STRESSER') {
        $body = $body.Replace("`${$v}", [Environment]::GetEnvironmentVariable($v))
    }
    return $body
}
Invoke-EnvSubst experiments/specs/smoke/iter-4h-1/cell-1-cpu-on-prem.yaml.tmpl | `
    kubectl --context unique-logical-entrypoint apply -f -
```

## Watching and recording

Per cell, poll for completion or failure (see the plan § Operator-driven
smoke campaign step 4) and record `wall_clock`, `exit_code`, and notes
into `experiments/runs/iter-4h-1-smoke/smoke-results.md`.

## Cleanup

The development setup's safety rules allow the operator
to delete Jobs in the `delphi-experiments` namespace through
`unique-logical-entrypoint`:

```bash
kubectl --context unique-logical-entrypoint delete jobs \
    -l delphi.experiments/spec-id=iter-4h-1-smoke \
    -n delphi-experiments
```

This is operator-only. Automated sessions do not invoke `kubectl delete`.
