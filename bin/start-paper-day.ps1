<#
.SYNOPSIS
Start the IBKR paper-trading day. Idempotent.

.DESCRIPTION
Thin wrapper: verifies the local toolchain, then delegates every decision to
engine.paperday (Python), which prints its checks and exactly one final state:

  PAPER_DAY_READY     (exit 0)   entry gate OPEN
  PAPER_DAY_DEGRADED  (exit 10)  management only; armed entries refuse
  PAPER_DAY_BLOCKED   (exit 20)  book not trustworthy; entry gate CLOSED

Re-running is safe: a healthy session is re-verified, a stale one is recovered.

.PARAMETER TimeoutSeconds
How long to wait for the reviewer's liveness reply (default 180).
#>
[CmdletBinding()]
param(
    [int]$TimeoutSeconds = 180,
    [string]$ScheduleConfig,
    [string]$ScheduleConfigSha256
)

$ErrorActionPreference = "Stop"

if ($env:OS -ne "Windows_NT") {
    Write-Error "start-paper-day.ps1 supports Windows only (found '$env:OS')."
    exit 20
}

$repo = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repo "engine\.venv\Scripts\python.exe"

foreach ($required in @(
        @{ Path = (Join-Path $repo "engine\src\engine\paperday.py"); What = "engine repository" },
        @{ Path = $python; What = "engine virtual environment (run: cd engine; uv sync)" }
    )) {
    if (-not (Test-Path $required.Path)) {
        Write-Error "Missing $($required.What): $($required.Path)"
        exit 20
    }
}

$controllerArgs = @("--timeout", $TimeoutSeconds)
if (($null -eq $ScheduleConfig) -xor ($null -eq $ScheduleConfigSha256)) {
    Write-Error "-ScheduleConfig and -ScheduleConfigSha256 must be supplied together."
    exit 20
}
if ($ScheduleConfig) {
    $controllerArgs += @(
        "--schedule-config", $ScheduleConfig,
        "--schedule-config-sha256", $ScheduleConfigSha256
    )
}

& $python -c "import sys; from engine.paperday import main_start; sys.exit(main_start(sys.argv[1:]))" @controllerArgs
exit $LASTEXITCODE
