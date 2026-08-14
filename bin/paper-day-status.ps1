<#
.SYNOPSIS
Read-only status of the paper-trading day.

.DESCRIPTION
Prints broker reachability, paper/live environment, both watchers with PIDs
and health, verifier readiness, handoff queues, the entry gate, open
positions, working orders (via reconciliation records), reservations, marks,
today's order count, the last successful autonomous verification and the last
clean shutdown. Connects no broker API client; safe to run beside a live
session.

.PARAMETER StateDir
Absolute shared state root to inspect.
#>
[CmdletBinding()]
param(
    [string]$StateDir
)

$ErrorActionPreference = "Stop"

$repo = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repo "engine\.venv\Scripts\python.exe"
$defaultStateDir = Join-Path $repo "engine\.engine"
if ([string]::IsNullOrWhiteSpace($StateDir)) {
    $StateDir = $defaultStateDir
}
if (-not [IO.Path]::IsPathFullyQualified($StateDir)) {
    Write-Error "-StateDir must be an absolute path: $StateDir"
    exit 1
}

if (-not (Test-Path $python)) {
    Write-Error "Missing engine virtual environment: $python"
    exit 1
}

& $python -c "import sys; from engine.paperday import main_status; sys.exit(main_status(sys.argv[1:]))" `
    --state-dir $StateDir
exit $LASTEXITCODE
