<#
.SYNOPSIS
Stop the IBKR paper-trading day. Idempotent.

.DESCRIPTION
Closes the entry gate FIRST (no new proposals from that instant), cancels
working entry orders, settles outstanding handoffs, reconciles and marks,
persists the session summary, asks the reviewer to stop, terminates the
builder watcher (only after verifying the PID still belongs to it), and
releases the session lock. Filled positions are always preserved.

Prints PAPER_DAY_STOPPED (exit 0 clean, exit 10 dirty -- details above it).

.PARAMETER TimeoutSeconds
How long to wait for REVIEWER_STOPPED (default 180).

.PARAMETER StateDir
Absolute shared state root used by the session being stopped.
#>
[CmdletBinding()]
param(
    [int]$TimeoutSeconds = 180,
    [string]$StateDir
)

$ErrorActionPreference = "Stop"

if ($env:OS -ne "Windows_NT") {
    Write-Error "stop-paper-day.ps1 supports Windows only (found '$env:OS')."
    exit 10
}

$repo = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repo "engine\.venv\Scripts\python.exe"
$defaultStateDir = Join-Path $repo "engine\.engine"
if ([string]::IsNullOrWhiteSpace($StateDir)) {
    $StateDir = $defaultStateDir
}
if (-not [IO.Path]::IsPathFullyQualified($StateDir)) {
    Write-Error "-StateDir must be an absolute path: $StateDir"
    exit 10
}

if (-not (Test-Path $python)) {
    Write-Error "Missing engine virtual environment: $python"
    exit 10
}

& $python -c "import sys; from engine.paperday import main_stop; sys.exit(main_stop(sys.argv[1:]))" `
    --timeout $TimeoutSeconds --state-dir $StateDir
exit $LASTEXITCODE
