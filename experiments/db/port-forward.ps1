#requires -Version 7
<#
.SYNOPSIS
    Idempotent kubectl port-forward to the decision-maker Postgres.

.DESCRIPTION
    Iter-4a rewrite. The previous foreground-only script is replaced
    with an idempotent background runner that the analyzer subpackage
    (experiments/orchestrator/analysis/db.py) calls automatically on
    startup.

    Behaviour:
      - Reads PG_K8S_CONTEXT / PG_K8S_NAMESPACE / PG_K8S_SERVICE /
        PG_K8S_PORT / PG_LOCAL_PORT from experiments/db/.env if
        present, falling back to safe defaults.
      - If a port-forward is already healthy (PID file present and
        process alive AND local port answers TCP connect), exits 0
        without doing anything.
      - Otherwise spawns
        `kubectl --context <ctx> port-forward -n <ns> svc/<svc>
        <local>:<remote>` in the background and writes the PID to
        experiments/db/.port-forward.pid.
      - Use `-Stop` to kill any held PID.
      - Use `-Foreground` to behave like the old script (block in
        the current console). Mostly for operator debugging.

.PARAMETER Stop
    Kill the held port-forward PID and remove the PID file. Exits 0
    if nothing was running.

.PARAMETER Foreground
    Run in the current console instead of background. Mutually
    exclusive with -Stop.

.PARAMETER LocalPort
    Override PG_LOCAL_PORT. Default: env value or 5432.

.PARAMETER WaitSeconds
    How long to wait for the new port-forward to come up before
    declaring failure. Default 10.

.EXAMPLE
    .\experiments\db\port-forward.ps1
    # Background. Idempotent. PID at experiments\db\.port-forward.pid.

.EXAMPLE
    .\experiments\db\port-forward.ps1 -Stop
    # Stop the held port-forward.

.EXAMPLE
    .\experiments\db\port-forward.ps1 -Foreground
    # Block (legacy behaviour).
#>
[CmdletBinding()]
param(
    [switch]$Stop,
    [switch]$Foreground,
    [int]$LocalPort = 0,
    [int]$WaitSeconds = 10
)

$ErrorActionPreference = 'Stop'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$EnvFile   = Join-Path $ScriptDir '.env'
$PidFile   = Join-Path $ScriptDir '.port-forward.pid'

# --- Load .env if present (KEY=VALUE per line; comments ignored) ----------
$envVars = @{}
if (Test-Path $EnvFile) {
    Get-Content $EnvFile | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith('#') -and $line.Contains('=')) {
            $k, $v = $line -split '=', 2
            $envVars[$k.Trim()] = $v.Trim()
        }
    }
}

$ctx       = if ($envVars['PG_K8S_CONTEXT'])   { $envVars['PG_K8S_CONTEXT'] }   else { 'on-prem' }
$ns        = if ($envVars['PG_K8S_NAMESPACE']) { $envVars['PG_K8S_NAMESPACE'] } else { 'karmada-system' }
$svc       = if ($envVars['PG_K8S_SERVICE'])   { $envVars['PG_K8S_SERVICE'] }   else { 'postgres' }
$remote    = if ($envVars['PG_K8S_PORT'])      { [int]$envVars['PG_K8S_PORT'] } else { 5432 }
$effective = if ($LocalPort -gt 0) { $LocalPort } elseif ($envVars['PG_LOCAL_PORT']) { [int]$envVars['PG_LOCAL_PORT'] } else { 5432 }

function Test-PortOpen([int]$Port, [int]$TimeoutMs = 500) {
    $client = $null
    try {
        $client = [System.Net.Sockets.TcpClient]::new()
        $iar    = $client.BeginConnect('127.0.0.1', $Port, $null, $null)
        if (-not $iar.AsyncWaitHandle.WaitOne($TimeoutMs)) { return $false }
        $client.EndConnect($iar)
        return $true
    } catch { return $false }
    finally { if ($client) { $client.Dispose() } }
}

function Read-HeldPid {
    if (-not (Test-Path $PidFile)) { return $null }
    $raw = (Get-Content $PidFile -Raw).Trim()
    if (-not $raw) { return $null }
    return [int]$raw
}

function Test-PidAlive([int]$Id) {
    try { Get-Process -Id $Id -ErrorAction Stop | Out-Null; return $true }
    catch { return $false }
}

# --- Stop ------------------------------------------------------------------
if ($Stop) {
    $heldPid = Read-HeldPid
    if (-not $heldPid) {
        Write-Host '==> no held port-forward (no PID file)' -ForegroundColor DarkGray
        if (Test-Path $PidFile) { Remove-Item $PidFile -Force }
        exit 0
    }
    if (Test-PidAlive $heldPid) {
        Write-Host "==> stopping kubectl port-forward (PID $heldPid)" -ForegroundColor Cyan
        Stop-Process -Id $heldPid -Force -ErrorAction SilentlyContinue
    } else {
        Write-Host "==> held PID $heldPid is not running; cleaning up PID file" -ForegroundColor DarkGray
    }
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
    exit 0
}

# --- Foreground (legacy) ---------------------------------------------------
if ($Foreground) {
    Write-Host "==> foreground port-forward svc/$svc -n $ns on $ctx (${effective}:${remote})" -ForegroundColor Cyan
    Write-Host '    Press Ctrl+C to terminate.'
    kubectl --context $ctx port-forward -n $ns "svc/$svc" "${effective}:${remote}"
    exit $LASTEXITCODE
}

# --- Background, idempotent -----------------------------------------------
$heldPid = Read-HeldPid
if ($heldPid -and (Test-PidAlive $heldPid) -and (Test-PortOpen $effective)) {
    Write-Host "==> port-forward already running (PID $heldPid, localhost:$effective)" -ForegroundColor Green
    exit 0
}

# Either PID file is stale, process dead, or port not answering — clean up + restart.
if ($heldPid -and (Test-PidAlive $heldPid)) {
    Write-Host "==> stale state: PID $heldPid alive but port $effective not answering; restarting" -ForegroundColor Yellow
    Stop-Process -Id $heldPid -Force -ErrorAction SilentlyContinue
}
if (Test-Path $PidFile) { Remove-Item $PidFile -Force -ErrorAction SilentlyContinue }

Write-Host "==> starting port-forward svc/$svc -n $ns on $ctx (localhost:$effective -> :$remote)" -ForegroundColor Cyan
$proc = Start-Process -FilePath 'kubectl' `
    -ArgumentList @('--context', $ctx, 'port-forward', '-n', $ns, "svc/$svc", "${effective}:${remote}") `
    -PassThru -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $ScriptDir '.port-forward.out') `
    -RedirectStandardError  (Join-Path $ScriptDir '.port-forward.err')

if (-not $proc) {
    Write-Host '==> Start-Process failed' -ForegroundColor Red
    exit 1
}
Set-Content -Path $PidFile -Value $proc.Id -Encoding ascii
Write-Host "    spawned PID $($proc.Id); waiting up to ${WaitSeconds}s for localhost:$effective ..."

$deadline = (Get-Date).AddSeconds($WaitSeconds)
while ((Get-Date) -lt $deadline) {
    if (-not (Test-PidAlive $proc.Id)) {
        Write-Host '==> kubectl exited prematurely; see .port-forward.err' -ForegroundColor Red
        if (Test-Path $PidFile) { Remove-Item $PidFile -Force -ErrorAction SilentlyContinue }
        exit 1
    }
    if (Test-PortOpen $effective) {
        Write-Host '==> ready' -ForegroundColor Green
        exit 0
    }
    Start-Sleep -Milliseconds 250
}

Write-Host "==> timed out after ${WaitSeconds}s waiting for localhost:$effective" -ForegroundColor Red
Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
if (Test-Path $PidFile) { Remove-Item $PidFile -Force -ErrorAction SilentlyContinue }
exit 1
