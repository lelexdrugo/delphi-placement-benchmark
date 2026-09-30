#requires -Version 7
<#
.SYNOPSIS
    Verify TCP reachability from public-cloud to the iperf3 NodePort on on-prem.

.DESCRIPTION
    Iter-2c targeted preflight for the public-cloud member only. Unlike
    its sibling `check-net-routes.ps1` which fans out across all three
    member clusters and requires direct LAN/VPN routes to each, this
    script probes only public-cloud — which IS reachable from the
    operator's PC (its kubeconfig context works without VPN).

    The script applies one ephemeral busybox Pod on public-cloud, runs
    `nc -zv <on-prem-node-ip> <NodePort>`, captures the result, and
    deletes the pod. The Pod write needs the operator's own access to
    public-cloud, so run it by hand, not from an automated session
    restricted to reads.

    Why this script exists: until iter-3 lands `pin-to-cluster`, the
    canonical Job-based-via-Karmada preflight cannot run unattended.
    This intermediate script proves the on-prem<-public-cloud route
    works today, which is the most representative single-member proof
    that the iperf3 NodePort can be reached from outside the on-prem
    network. Edge-1 and edge-2 reachability is verified later as part
    of the iter-3 Job-based preflight.

.PARAMETER OnPremNodeIp
    On-prem node IP reachable from public-cloud. Default 203.0.113.10,
    a documentation placeholder (RFC 5737): pass the address set as
    TARGET_HOST in experiments/specs/effectiveness.yaml.

.PARAMETER NodePort
    iperf3 NodePort. Default 31521 (matches experiments/iperf3-server/service.yaml).

.PARAMETER TimeoutSeconds
    Per-probe timeout. Default 10s.

.OUTPUTS
    experiments/runs/preflight/public-cloud-<timestamp>.json with
    fields {context, status, latency_ms, raw_output}.
    status is OK | UNREACHABLE | TIMEOUT | ERROR.

.EXAMPLE
    .\experiments\preflight\check-net-route-public-cloud.ps1 -OnPremNodeIp 203.0.113.10

    Probes from public-cloud to 203.0.113.10:31521. Returns exit 0
    if OK, 1 otherwise.
#>

[CmdletBinding()]
param(
    [Parameter()]
    [string]$OnPremNodeIp = "203.0.113.10",

    [Parameter()]
    [int]$NodePort = 31521,

    [Parameter()]
    [int]$TimeoutSeconds = 10
)

$ErrorActionPreference = 'Stop'

function Write-Step { param([string]$m) Write-Host ""; Write-Host "==> $m" -ForegroundColor Cyan }
function Write-Ok   { param([string]$m) Write-Host "[ok] $m" -ForegroundColor Green }
function Write-Warn { param([string]$m) Write-Warning $m }

$repoRoot = (& git rev-parse --show-toplevel 2>$null)
if (-not $repoRoot) {
    $repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
}
$outDir = Join-Path $repoRoot 'experiments\runs\preflight'
New-Item -ItemType Directory -Path $outDir -Force | Out-Null

$timestamp = (Get-Date -Format 'yyyyMMddTHHmmssZ')
$outFile = Join-Path $outDir "public-cloud-$timestamp.json"

$ctx = 'public-cloud'
$podName = "delphi-pc-probe-$([guid]::NewGuid().ToString('N').Substring(0,8))"

Write-Step "Preflight from public-cloud to $OnPremNodeIp`:$NodePort"

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
          # busybox `date` does not support %N (nanoseconds) on all builds;
          # using %s (seconds) is portable. Latency precision is therefore
          # coarse (whole seconds) — good enough for a binary reachability
          # check. The echo + exit MUST happen even when nc fails, so the
          # PowerShell parser sees PROBE_RC=<code>.
          start=`$(date +%s)
          nc -zv -w $TimeoutSeconds $OnPremNodeIp $NodePort
          rc=`$?
          end=`$(date +%s)
          delta=`$((end - start))
          if [ "`$delta" -lt 0 ]; then delta=0; fi
          ms=`$((delta * 1000))
          echo "PROBE_RC=`$rc PROBE_LATENCY_MS=`$ms"
          exit `$rc
"@

$tmpYaml = New-TemporaryFile
Set-Content -Path $tmpYaml -Value $podYaml -Encoding ascii

$status = 'ERROR'
$lat = $null
$logs = ''

try {
    kubectl --context $ctx apply -f $tmpYaml -n default | Out-Null
    if ($LASTEXITCODE -ne 0) {
        $logs = "kubectl apply failed (exit $LASTEXITCODE)"
    } else {
        $deadline = (Get-Date).AddSeconds($TimeoutSeconds + 20)
        $phase = ''
        while ((Get-Date) -lt $deadline) {
            $phase = kubectl --context $ctx get pod $podName -n default -o jsonpath='{.status.phase}' 2>$null
            if ($phase -in @('Succeeded', 'Failed')) { break }
            Start-Sleep -Seconds 1
        }
        $logs = kubectl --context $ctx logs $podName -n default 2>&1 | Out-String
        $rcMatch = [regex]::Match($logs, 'PROBE_RC=(\d+)')
        $latMatch = [regex]::Match($logs, 'PROBE_LATENCY_MS=(\d+)')
        $rc = if ($rcMatch.Success) { [int]$rcMatch.Groups[1].Value } else { -1 }
        $lat = if ($latMatch.Success) { [int]$latMatch.Groups[1].Value } else { $null }

        # Secondary positive signal: `nc -zv` prints "(host:port) open" on
        # success. If the wrapper exited before echoing PROBE_RC (e.g. an
        # arithmetic crash from an older script version), we can still
        # detect a successful probe from that line.
        $ncSuccessSignal = $logs -match '\bopen\b' -and ($logs -notmatch '\bclosed\b' -and $logs -notmatch '\brefused\b' -and $logs -notmatch 'no route to host')

        if ($phase -eq 'Succeeded' -and $rc -eq 0) {
            $status = 'OK'
        } elseif ($rcMatch.Success -and $rc -ne 0) {
            $status = 'UNREACHABLE'
        } elseif ($ncSuccessSignal) {
            # nc succeeded but the wrapper failed before reporting RC.
            $status = 'OK'
            if ($null -eq $lat) { $lat = 0 }
        } elseif ($phase -eq 'Failed') {
            $status = 'UNREACHABLE'
        } else {
            $status = 'TIMEOUT'
        }
    }
}
finally {
    kubectl --context $ctx delete pod $podName -n default --ignore-not-found --wait=false 2>$null | Out-Null
    Remove-Item -LiteralPath $tmpYaml -Force -ErrorAction SilentlyContinue
}

$report = [ordered]@{
    timestamp = (Get-Date).ToUniversalTime().ToString('o')
    on_prem_node_ip = $OnPremNodeIp
    node_port = $NodePort
    timeout_seconds = $TimeoutSeconds
    results = @(
        [pscustomobject]@{
            context = $ctx
            status = $status
            latency_ms = $lat
            raw_output = $logs.Trim()
        }
    )
}
($report | ConvertTo-Json -Depth 8) | Set-Content -Path $outFile -Encoding utf8
Write-Step "Report written to $outFile"

if ($status -eq 'OK') {
    Write-Ok "public-cloud reachability OK (latency ${lat}ms)"
    exit 0
} else {
    Write-Warn "public-cloud reachability $status — see $outFile"
    exit 1
}
