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

.PARAMETER StateDir
Absolute shared state root used by paper-day, scheduler, and engine workers.

.PARAMETER Mandate
MANAGE_ONLY preserves the legacy management-only session. FULL requires the
hash-pinned scheduler policy, catalog, config, schedule artifact, and
configuration fingerprint below.
#>
[CmdletBinding()]
param(
    [int]$TimeoutSeconds = 180,
    [string]$ScheduleConfig,
    [string]$ScheduleConfigSha256,
    [string]$StateDir,
    [ValidateSet("MANAGE_ONLY", "FULL")]
    [string]$Mandate = "MANAGE_ONLY",
    [string]$PolicySha256,
    [string]$CatalogSha256,
    [string]$ConfigSha256,
    [string]$ConfigurationFingerprint
)

$ErrorActionPreference = "Stop"

if ($env:OS -ne "Windows_NT") {
    Write-Error "start-paper-day.ps1 supports Windows only (found '$env:OS')."
    exit 20
}

$repo = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repo "engine\.venv\Scripts\python.exe"
$defaultStateDir = Join-Path $repo "engine\.engine"
if ([string]::IsNullOrWhiteSpace($StateDir)) {
    $StateDir = $defaultStateDir
}
if (-not [IO.Path]::IsPathFullyQualified($StateDir)) {
    Write-Error "-StateDir must be an absolute path: $StateDir"
    exit 20
}
if ($Mandate -eq "FULL" -and (
        [string]::IsNullOrWhiteSpace($PolicySha256) -or
        [string]::IsNullOrWhiteSpace($CatalogSha256) -or
        [string]::IsNullOrWhiteSpace($ConfigSha256) -or
        [string]::IsNullOrWhiteSpace($ScheduleConfig) -or
        [string]::IsNullOrWhiteSpace($ScheduleConfigSha256) -or
        [string]::IsNullOrWhiteSpace($ConfigurationFingerprint))) {
    Write-Error "FULL requires -ScheduleConfig, -ScheduleConfigSha256, -PolicySha256, -CatalogSha256, -ConfigSha256, and -ConfigurationFingerprint."
    exit 20
}

foreach ($required in @(
        @{ Path = (Join-Path $repo "engine\src\engine\paperday.py"); What = "engine repository" },
        @{ Path = $python; What = "engine virtual environment (run: cd engine; uv sync)" }
    )) {
    if (-not (Test-Path $required.Path)) {
        Write-Error "Missing $($required.What): $($required.Path)"
        exit 20
    }
}

$controllerArgs = @(
    "--timeout", $TimeoutSeconds,
    "--state-dir", $StateDir,
    "--mandate", $Mandate
)
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
foreach ($optional in @(
        @{ Flag = "--policy-sha256"; Value = $PolicySha256 },
        @{ Flag = "--catalog-sha256"; Value = $CatalogSha256 },
        @{ Flag = "--config-sha256"; Value = $ConfigSha256 },
        @{ Flag = "--configuration-fingerprint"; Value = $ConfigurationFingerprint }
    )) {
    if (-not [string]::IsNullOrWhiteSpace($optional.Value)) {
        $controllerArgs += @($optional.Flag, $optional.Value)
    }
}

& $python -c "import sys; from engine.paperday import main_start; sys.exit(main_start(sys.argv[1:]))" @controllerArgs
exit $LASTEXITCODE
