<#
.SYNOPSIS
Stop and start one paper-day authority without changing its state root.

.DESCRIPTION
The stop must be clean before the replacement starts. A dirty stop leaves the
gate closed and returns without starting a second controller, forcing explicit
broker reconciliation instead of hiding an ambiguous scheduler outcome.
#>
[CmdletBinding()]
param(
    [int]$TimeoutSeconds = 180,
    [string]$StateDir,
    [string]$ScheduleConfig,
    [string]$ScheduleConfigSha256,
    [ValidateSet("MANAGE_ONLY", "FULL")]
    [string]$Mandate = "MANAGE_ONLY",
    [string]$PolicySha256,
    [string]$CatalogSha256,
    [string]$ConfigSha256,
    [string]$ConfigurationFingerprint
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($StateDir)) {
    $StateDir = Join-Path $repo "engine\.engine"
}
if (-not [IO.Path]::IsPathFullyQualified($StateDir)) {
    Write-Error "-StateDir must be an absolute path: $StateDir"
    exit 10
}

& (Join-Path $PSScriptRoot "stop-paper-day.ps1") `
    -TimeoutSeconds $TimeoutSeconds -StateDir $StateDir
if ($LASTEXITCODE -ne 0) {
    Write-Error "Paper-day stop was dirty; refusing automatic restart."
    exit $LASTEXITCODE
}

$startArgs = @{
    TimeoutSeconds = $TimeoutSeconds
    StateDir = $StateDir
    Mandate = $Mandate
}
foreach ($name in @(
        "ScheduleConfig", "ScheduleConfigSha256", "PolicySha256", "CatalogSha256",
        "ConfigSha256", "ConfigurationFingerprint"
    )) {
    $value = Get-Variable -Name $name -ValueOnly
    if (-not [string]::IsNullOrWhiteSpace($value)) {
        $startArgs[$name] = $value
    }
}
& (Join-Path $PSScriptRoot "start-paper-day.ps1") @startArgs
exit $LASTEXITCODE
