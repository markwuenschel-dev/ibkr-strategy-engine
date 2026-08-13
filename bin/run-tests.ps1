<#
.SYNOPSIS
Run the engine test suite without touching a live paper-day runtime.

.DESCRIPTION
Two things this wrapper exists to prevent, both of which have actually happened:

1. `uv run pytest` -- the bare form -- resolves the environment WITHOUT the dev
   extra, so uv tears down and rebuilds `engine\.venv`, dropping pytest and the
   editable install of `engine` itself. That venv is not only the test
   environment: it is the interpreter `bin\start-paper-day.ps1` launches, the one
   `spawn_detached` hands to the collab watchers, and the binary behind every
   manual `options-run` / `options-mark`. Destroying it mid-session breaks the
   live trading loop, not just the tests. This wrapper always passes
   `--extra dev`, matching CI (.github/workflows/ci.yml).

2. Running the suite at all in a checkout that owns a live session. Even with the
   right flags, a resync can replace the interpreter underneath a running
   controller. So this refuses outright when a paper-day session lock is held
   here, and points you at a worktree instead -- a worktree has its own `.venv`,
   which makes the isolation structural rather than a thing to remember.

`pytest -q` is also wrong here: `addopts` already sets `-q`, so an extra `-q`
yields `-qq` and silently suppresses the summary line. Pass arguments through
this script and you get the counts.

.PARAMETER Force
Run even if a session lock is present. For the case where the lock is a corpse
and you have already confirmed no controller is alive.

.EXAMPLE
.\bin\run-tests.ps1
.\bin\run-tests.ps1 tests/test_paperday.py
.\bin\run-tests.ps1 -k two_sided
#>
[CmdletBinding()]
param(
    [switch]$Force,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$PytestArgs
)

$ErrorActionPreference = "Stop"

$repo = Split-Path -Parent $PSScriptRoot
$engine = Join-Path $repo "engine"

if (-not (Test-Path (Join-Path $engine "pyproject.toml"))) {
    Write-Error "Not an engine checkout: $engine"
    exit 2
}

$lock = Join-Path $engine ".engine\paperday\session.lock"
if ((Test-Path $lock) -and (-not $Force)) {
    $held = Get-Content $lock -Raw
    Write-Host ""
    Write-Host "REFUSING: a paper-day session is live in this checkout." -ForegroundColor Red
    Write-Host $held
    Write-Host "Running the suite here can replace engine\.venv underneath the"
    Write-Host "controller and its watchers. Use an isolated worktree instead:"
    Write-Host ""
    Write-Host "  git worktree add -b <lane>/<topic> ..\ibkr-strategy-engine-<lane> HEAD"
    Write-Host "  cd ..\ibkr-strategy-engine-<lane>; .\bin\run-tests.ps1"
    Write-Host ""
    Write-Host "Or stop the session first (.\bin\stop-paper-day.ps1), or pass -Force"
    Write-Host "if you have confirmed the lock is stale."
    exit 3
}

Push-Location $engine
try {
    & uv run --extra dev pytest @PytestArgs
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
