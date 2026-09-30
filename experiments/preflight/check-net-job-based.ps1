#requires -Version 7
<#
.SYNOPSIS
    Karmada-propagated cross-cluster reachability check (iter-3 canonical).

.DESCRIPTION
    The architecturally correct alternative to `check-net-routes.ps1` and
    `check-net-route-public-cloud.ps1`: instead of writing a Pod to each
    member context directly (which requires LAN/VPN routes and write
    access on every member context), this script writes one
    `kind: Job` per target cluster to `unique-logical-entrypoint`. Each
    Job carries:

        metadata.annotations:
          optimize-propagation: "true"
          delphi.experiments/pin-to-cluster: "<target>"
          delphi.experiments/role: "background"

    so the decision-maker (post iter-3) resolves the placement directly
    to `<target>` with score 1.0, skipping NATS / the agent / history.
    Karmada then propagates the Job to that cluster. The Pod runs
    `nc -zv <on-prem-node-ip> <NodePort>` and the script reads the logs
    via the member context to verify reachability.

    Why this is preferable:
    - An automated session can run it (it writes only `kind: Job` on
      unique-logical-entrypoint, and reading logs from member contexts
      is read-only).
    - It exercises the entire production datapath
      (webhook -> controller -> decision-maker -> Karmada -> member),
      so a green probe also confirms that pin-to-cluster works.
    - Cleanup uses the experiments-cleanup carve-out
      (`kubectl --context unique-logical-entrypoint delete jobs -n delphi-experiments`).

    Pre-conditions:
    - `experiments/iperf3-server/` deployed on on-prem (NodePort 31521).
    - Decision-maker + controller running iter-3 image (pin-to-cluster
      handler present).
    - Namespace `delphi-experiments` exists on unique-logical-entrypoint.

.PARAMETER OnPremNodeIp
    On-prem node IP reachable from each member cluster. Default
    203.0.113.10, a documentation placeholder (RFC 5737): pass the address set
    as TARGET_HOST in experiments/specs/effectiveness.yaml.

.PARAMETER NodePort
    iperf3 NodePort. Default 31521.

.PARAMETER TargetClusters
    Member clusters to probe. Default: public-cloud, edge-1, edge-2.

.PARAMETER TimeoutSeconds
    Per-probe nc timeout. Default 10s.

.PARAMETER WaitDeadlineSeconds
    How long to wait for each Job to reach Complete/Failed. Default 300s.

.PARAMETER Namespace
    Where to create the probe Jobs. Default `delphi-experiments`.

.PARAMETER KeepJobs
    Skip cleanup at end. Use only for debugging.

.OUTPUTS
    experiments/runs/preflight/job-based-<timestamp>.json with one entry
    per target cluster:
        {context, job_name, status, latency_ms, raw_output}
    status is OK | UNREACHABLE | TIMEOUT | APPLY_FAILED | NO_LOGS.

.EXAMPLE
    .\experiments\preflight\check-net-job-based.ps1

    Probes from public-cloud, edge-1, edge-2 to 203.0.113.10:31521
    via Karmada propagation. Cleans up at the end.
#>

[CmdletBinding()]
param(
    [Parameter()]
    [string]$OnPremNodeIp = "203.0.113.10",

    [Parameter()]
    [int]$NodePort = 31521,

    [Parameter()]
    [string[]]$TargetClusters = @("public-cloud", "edge-1", "edge-2"),

    [Parameter()]
    [int]$TimeoutSeconds = 10,

    [Parameter()]
    [int]$WaitDeadlineSeconds = 300,

    [Parameter()]
    [string]$Namespace = "delphi-experiments",

    [Parameter()]
    [switch]$KeepJobs
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
$outFile = Join-Path $outDir "job-based-$timestamp.json"

function New-ProbeJobYaml {
    param(
        [string]$JobName,
        [string]$Cluster,
        [string]$Ns,
        [string]$NodeIp,
        [int]$Port,
        [int]$Timeout
    )
    return @"
apiVersion: batch/v1
kind: Job
metadata:
  name: $JobName
  namespace: $Ns
  annotations:
    optimize-propagation: "true"
    propagation-requirements: '{"latency":"any","cost":"any","region":"any","weight":1.0}'
    delphi.experiments/pin-to-cluster: "$Cluster"
    delphi.experiments/role: "background"
    delphi.experiments/spec-id: "preflight-net"
    delphi.experiments/run-id: "preflight-$timestamp"
  labels:
    app.kubernetes.io/part-of: delphi-experiments
    delphi.experiments/role: "background"
    delphi.experiments/pin-to-cluster: "$Cluster"
spec:
  backoffLimit: 0
  activeDeadlineSeconds: $($Timeout + 60)
  template:
    metadata:
      labels:
        app.kubernetes.io/part-of: delphi-experiments
        delphi.experiments/role: "background"
        delphi.experiments/pin-to-cluster: "$Cluster"
    spec:
      restartPolicy: Never
      containers:
        - name: probe
          image: busybox:1.36
          command: ["sh","-c"]
          args:
            - |
              # Single-attempt nc probe. Latency precision is coarse (seconds)
              # because busybox `date +%s` is the portable choice. Good enough
              # for binary reachability.
              start=`$(date +%s)
              nc -zv -w $Timeout $NodeIp $Port
              rc=`$?
              end=`$(date +%s)
              delta=`$((end - start))
              if [ "`$delta" -lt 0 ]; then delta=0; fi
              ms=`$((delta * 1000))
              echo "PROBE_RC=`$rc PROBE_LATENCY_MS=`$ms"
              exit `$rc
"@
}

function Wait-JobTerminal {
    param(
        [string]$Ctx,
        [string]$Ns,
        [string]$Name,
        [int]$DeadlineSeconds
    )
    $deadline = (Get-Date).AddSeconds($DeadlineSeconds)
    while ((Get-Date) -lt $deadline) {
        $json = kubectl --context $Ctx get job $Name -n $Ns -o json 2>$null
        if ($LASTEXITCODE -eq 0 -and $json) {
            $obj = $json | ConvertFrom-Json
            foreach ($cond in @($obj.status.conditions)) {
                if ($cond -and $cond.status -eq 'True' -and ($cond.type -eq 'Complete' -or $cond.type -eq 'Failed')) {
                    return $cond.type
                }
            }
        }
        Start-Sleep -Seconds 5
    }
    return 'TimedOut'
}

function Read-ProbeLogs {
    param(
        [string]$Ctx,
        [string]$Ns,
        [string]$JobName
    )
    # Find the propagated Pod for this Job on the target cluster.
    $pod = kubectl --context $Ctx get pods -n $Ns -l job-name=$JobName -o jsonpath='{.items[0].metadata.name}' 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $pod) {
        return $null, 'NO_POD'
    }
    $logs = kubectl --context $Ctx logs $pod -n $Ns -c probe --tail=200 2>&1 | Out-String
    return $logs, $null
}

$results = @()
$jobsCreated = @()

Write-Step "Preflight (Karmada-propagated) -> $OnPremNodeIp`:$NodePort across $($TargetClusters.Count) member(s)"

foreach ($cluster in $TargetClusters) {
    $shortCluster = ($cluster -replace '[^a-z0-9-]', '-')
    if ($shortCluster.Length -gt 32) { $shortCluster = $shortCluster.Substring(0, 32).TrimEnd('-') }
    $jobName = "delphi-pf-net-$shortCluster-$([guid]::NewGuid().ToString('N').Substring(0,6))"
    $yaml = New-ProbeJobYaml `
        -JobName $jobName -Cluster $cluster -Ns $Namespace `
        -NodeIp $OnPremNodeIp -Port $NodePort -Timeout $TimeoutSeconds

    $tmp = New-TemporaryFile
    Set-Content -Path $tmp -Value $yaml -Encoding ascii

    $status = 'ERROR'
    $lat = $null
    $logs = ''

    Write-Step "applying probe Job $jobName (pin -> $cluster)"
    kubectl --context unique-logical-entrypoint apply -f $tmp | Out-Null
    if ($LASTEXITCODE -ne 0) {
        $status = 'APPLY_FAILED'
        $results += [pscustomobject]@{
            context = $cluster
            job_name = $jobName
            status = $status
            latency_ms = $null
            raw_output = "kubectl apply failed (exit $LASTEXITCODE)"
        }
        Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
        continue
    }
    $jobsCreated += $jobName

    $finalType = Wait-JobTerminal `
        -Ctx $cluster -Ns $Namespace -Name $jobName `
        -DeadlineSeconds $WaitDeadlineSeconds

    $logResult = Read-ProbeLogs -Ctx $cluster -Ns $Namespace -JobName $jobName
    $logs = $logResult[0]
    $noPodReason = $logResult[1]

    if ($noPodReason) {
        $status = 'NO_LOGS'
    } else {
        $rcMatch = [regex]::Match($logs, 'PROBE_RC=(\d+)')
        $latMatch = [regex]::Match($logs, 'PROBE_LATENCY_MS=(\d+)')
        $rc = if ($rcMatch.Success) { [int]$rcMatch.Groups[1].Value } else { -1 }
        $lat = if ($latMatch.Success) { [int]$latMatch.Groups[1].Value } else { $null }
        # Secondary "open" signal in case nc succeeded but the wrapper crashed.
        $ncSuccessSignal = $logs -match '\bopen\b' -and ($logs -notmatch '\bclosed\b' -and $logs -notmatch '\brefused\b' -and $logs -notmatch 'no route to host')

        if ($finalType -eq 'Complete' -and $rc -eq 0) {
            $status = 'OK'
        } elseif ($rcMatch.Success -and $rc -ne 0) {
            $status = 'UNREACHABLE'
        } elseif ($ncSuccessSignal) {
            $status = 'OK'
            if ($null -eq $lat) { $lat = 0 }
        } elseif ($finalType -eq 'Failed') {
            $status = 'UNREACHABLE'
        } elseif ($finalType -eq 'TimedOut') {
            $status = 'TIMEOUT'
        }
    }

    $results += [pscustomobject]@{
        context    = $cluster
        job_name   = $jobName
        status     = $status
        latency_ms = $lat
        raw_output = ($logs ?? '').Trim()
    }
    Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
}

if (-not $KeepJobs -and $jobsCreated.Count -gt 0) {
    Write-Step "cleaning up probe Jobs in $Namespace"
    foreach ($name in $jobsCreated) {
        # Uses the experiments-cleanup carve-out (see 55-cluster-safety.md).
        kubectl --context unique-logical-entrypoint delete job $name -n $Namespace --ignore-not-found | Out-Null
    }
}

$report = [ordered]@{
    timestamp       = (Get-Date).ToUniversalTime().ToString('o')
    on_prem_node_ip = $OnPremNodeIp
    node_port       = $NodePort
    namespace       = $Namespace
    target_clusters = $TargetClusters
    results         = $results
}
($report | ConvertTo-Json -Depth 8) | Set-Content -Path $outFile -Encoding utf8
Write-Step "Report written to $outFile"

$allOk = ($results | Where-Object { $_.status -ne 'OK' }).Count -eq 0
foreach ($r in $results) {
    if ($r.status -eq 'OK') {
        Write-Ok "$($r.context): OK ($($r.latency_ms)ms)"
    } else {
        Write-Warn "$($r.context): $($r.status) — see $outFile"
    }
}

if ($allOk) { exit 0 } else { exit 1 }
