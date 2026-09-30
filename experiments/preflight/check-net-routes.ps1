#requires -Version 7
<#
.SYNOPSIS
    Verify cross-cluster TCP reachability to the iperf3 target.

.DESCRIPTION
    Iter-2a best-effort reachability check. Probes whether pods
    running on public-cloud / edge-1 / edge-2 can reach the iperf3
    server NodePort on the on-prem node IP.

    IMPORTANT LIMITATION (lab topology).
    The DELPHI lab joins edge-1 and edge-2 to Karmada in pull mode.
    Their kubeconfig contexts work as write targets only when the
    operator has direct network reachability to those clusters (e.g.,
    same LAN / VPN). The canonical, production-equivalent path to
    place a workload on a specific member cluster is via Karmada
    propagation from `unique-logical-entrypoint`, which requires the
    `delphi.experiments/pin-to-cluster` annotation (decision-maker
    feature landing in iter-3).

    This script therefore works only when the operator has direct
    routes. It is NOT the architecturally-canonical reachability
    test for the cloud-continuum setup. The Job-based, Karmada-
    propagated reachability test lands as part of iter-3 once
    pin-to-cluster is implemented; see experiments/ITERATION-03.md.

    OPERATOR ONLY: THIS SCRIPT WRITES ON MEMBER CLUSTERS.

    It writes one ephemeral Pod per member context to probe TCP
    connectivity, so it is intended exclusively for the human
    operator, run from their own shell after iperf3-server has been
    deployed on on-prem, and not from an automated session.

.PARAMETER OnPremNodeIp
    The INTERNAL-IP of the on-prem node. Discover with:
        kubectl --context on-prem get nodes -o wide

.PARAMETER NodePort
    The iperf3 NodePort. Default 31521 (matches experiments/iperf3-server/service.yaml).

.PARAMETER TimeoutSeconds
    Per-probe timeout. Default 10s.

.OUTPUTS
    A JSON file at experiments/runs/preflight/<timestamp>.json with
    one entry per member context: {context, status, latency_ms, raw_output}.
    Status is OK | UNREACHABLE | TIMEOUT | ERROR.

.EXAMPLE
    .\experiments\preflight\check-net-routes.ps1 -OnPremNodeIp <on-prem-node-ip>

    Probes from edge-1, edge-2, public-cloud to <on-prem-node-ip>:31521.
    Run only after applying experiments/iperf3-server/ on on-prem and
    confirming the iperf3-server pod is Ready.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$OnPremNodeIp,

    [Parameter()]
    [int]$NodePort = 31521,

    [Parameter()]
    [int]$TimeoutSeconds = 10,

    [Parameter()]
    [string[]]$MemberContexts = @('edge-1', 'edge-2', 'public-cloud')
)

$ErrorActionPreference = 'Stop'

function Write-Step { param([string]$m) Write-Host ""; Write-Host "==> $m" -ForegroundColor Cyan }
function Write-Ok   { param([string]$m) Write-Host "[ok] $m" -ForegroundColor Green }
function Write-Warn { param([string]$m) Write-Warning $m }

# Resolve repo root.
$repoRoot = (& git rev-parse --show-toplevel 2>$null)
if (-not $repoRoot) {
    $repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
}
$outDir = Join-Path $repoRoot 'experiments\runs\preflight'
New-Item -ItemType Directory -Path $outDir -Force | Out-Null

$timestamp = (Get-Date -Format 'yyyyMMddTHHmmssZ')
$outFile = Join-Path $outDir "preflight-$timestamp.json"

Write-Step "Preflight cross-cluster reachability to $OnPremNodeIp`:$NodePort"
Write-Host "Member contexts: $($MemberContexts -join ', ')"
Write-Host "Per-probe timeout: ${TimeoutSeconds}s"

# Sanity: contexts must all be in the allowlist.
$allowed = @('on-prem','public-cloud','unique-logical-entrypoint','edge-1','edge-2')
foreach ($ctx in $MemberContexts) {
    if ($allowed -notcontains $ctx) {
        throw "Context '$ctx' is not in the allowlist ($($allowed -join ', '))."
    }
    if ($ctx -eq 'on-prem' -or $ctx -eq 'unique-logical-entrypoint') {
        throw "Probe must target a member cluster, not '$ctx'."
    }
}

$results = @()

foreach ($ctx in $MemberContexts) {
    Write-Step "Probing from $ctx"
    $podName = "delphi-preflight-$([guid]::NewGuid().ToString('N').Substring(0,8))"

    # Inline pod that runs once. busybox 'nc -zv' returns exit 0 on success.
    # Image is widely available; pull is from upstream public registry.
    # The pod self-deletes via restartPolicy: Never + the kubectl --rm flag
    # below, but as a safety belt we also bound it with activeDeadlineSeconds.
    $podYaml = @"
apiVersion: v1
kind: Pod
metadata:
  name: $podName
  labels:
    app.kubernetes.io/part-of: delphi-experiments
    delphi.experiments/role: preflight-probe
spec:
  restartPolicy: Never
  activeDeadlineSeconds: $($TimeoutSeconds + 10)
  containers:
    - name: probe
      image: busybox:1.36
      command: ["sh","-c"]
      args:
        - |
          start=`$(date +%s%N)
          nc -zv -w $TimeoutSeconds $OnPremNodeIp $NodePort
          rc=`$?
          end=`$(date +%s%N)
          ms=`$(( (end - start) / 1000000 ))
          echo "PROBE_RC=`$rc PROBE_LATENCY_MS=`$ms"
          exit `$rc
"@

    $tmpYaml = New-TemporaryFile
    Set-Content -Path $tmpYaml -Value $podYaml -Encoding ascii

    try {
        # Apply pod. This write on a member cluster is what makes the
        # script operator only.
        kubectl --context $ctx apply -f $tmpYaml -n default | Out-Null
        if ($LASTEXITCODE -ne 0) {
            $results += [pscustomobject]@{
                context = $ctx
                status = 'ERROR'
                latency_ms = $null
                raw_output = "kubectl apply failed (exit $LASTEXITCODE)"
            }
            continue
        }

        # Wait for completion.
        $deadline = (Get-Date).AddSeconds($TimeoutSeconds + 20)
        $phase = ''
        while ((Get-Date) -lt $deadline) {
            $phase = kubectl --context $ctx get pod $podName -n default -o jsonpath='{.status.phase}' 2>$null
            if ($phase -in @('Succeeded','Failed')) { break }
            Start-Sleep -Seconds 1
        }

        $logs = kubectl --context $ctx logs $podName -n default 2>&1 | Out-String
        $rcMatch = [regex]::Match($logs, 'PROBE_RC=(\d+)')
        $latMatch = [regex]::Match($logs, 'PROBE_LATENCY_MS=(\d+)')
        $rc = if ($rcMatch.Success) { [int]$rcMatch.Groups[1].Value } else { -1 }
        $lat = if ($latMatch.Success) { [int]$latMatch.Groups[1].Value } else { $null }

        $status = if ($phase -eq 'Succeeded' -and $rc -eq 0) { 'OK' }
                  elseif ($phase -eq 'Failed' -or $rc -ne 0) { 'UNREACHABLE' }
                  else { 'TIMEOUT' }

        $results += [pscustomobject]@{
            context = $ctx
            status = $status
            latency_ms = $lat
            raw_output = $logs.Trim()
        }

        if ($status -eq 'OK') { Write-Ok "$ctx OK (latency ${lat}ms)" }
        else { Write-Warn "$ctx $status" }
    }
    finally {
        # Operator-side cleanup of the ephemeral pod. Best-effort; the
        # activeDeadlineSeconds safety net guarantees death even if this
        # cleanup is missed.
        kubectl --context $ctx delete pod $podName -n default --ignore-not-found --wait=false 2>$null | Out-Null
        Remove-Item -LiteralPath $tmpYaml -Force -ErrorAction SilentlyContinue
    }
}

# Write the JSON report.
$report = [ordered]@{
    timestamp = (Get-Date).ToUniversalTime().ToString('o')
    on_prem_node_ip = $OnPremNodeIp
    node_port = $NodePort
    timeout_seconds = $TimeoutSeconds
    results = $results
}
($report | ConvertTo-Json -Depth 8) | Set-Content -Path $outFile -Encoding utf8
Write-Step "Report written to $outFile"

# Exit code reflects worst-case outcome: 0 if all OK, 1 if any UNREACHABLE/TIMEOUT/ERROR.
$bad = @($results | Where-Object { $_.status -ne 'OK' })
if ($bad.Count -gt 0) {
    Write-Warn "Preflight failed on: $($bad.context -join ', ')"
    exit 1
}
Write-Ok "All members reachable"
exit 0
