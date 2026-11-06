<#
.SYNOPSIS
  Official Windows PowerShell source launcher for AXIOM Operator (/terminal).

.DESCRIPTION
  Runs AXIOM directly from the cloned source repository without building or
  running any installer, works from both the repository root and any other
  working directory without manual PYTHONPATH changes, preserves existing
  %LOCALAPPDATA%\AXIOM config/credentials/databases, and keeps all live
  execution gates disabled and disarmed by default.

.EXAMPLE
  # First-time setup (creates .venv and installs locked runtime dependencies):
  powershell -ExecutionPolicy Bypass -File .\Start-Axiom.ps1 -Setup

  # Normal source launch from repo root:
  powershell -ExecutionPolicy Bypass -File .\Start-Axiom.ps1

  # Launch from ANY other current directory:
  powershell -ExecutionPolicy Bypass -File "C:\path\to\AXIOM\Start-Axiom.ps1"

  # Non-interactive smoke check (serves /terminal + /health locally, 0 broker calls):
  powershell -ExecutionPolicy Bypass -File .\Start-Axiom.ps1 -SmokeCheck
#>
[CmdletBinding()]
param(
    [switch]$Setup,
    [switch]$CheckDeps,
    # `-Smoke` is the documented spelling (README, USER_MANUAL, OWNER_SETUP_GUIDE);
    # `-SmokeCheck` remains accepted for older scripts and notes.
    [Alias("Smoke")]
    [switch]$SmokeCheck,
    [switch]$NoBrowser,
    [int]$Port = 0
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-Diagnostic([string]$Message) {
    Write-Host "[AXIOM] $Message"
}

$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Definition
$VenvWinPy = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$VenvPosixPy = Join-Path $RepoRoot ".venv/bin/python"
$LauncherPy = Join-Path $RepoRoot "axiom_launcher.py"
$LockFile = Join-Path $RepoRoot "requirements-lock.txt"

function Resolve-BootstrapPython {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        return @("py", "-3.11")
    }
    if (Get-Command python -ErrorAction SilentlyContinue) {
        return @("python")
    }
    if (Get-Command python3 -ErrorAction SilentlyContinue) {
        return @("python3")
    }
    throw "Python 3.11+ was not found on PATH. Install CPython 3.11 x64 from python.org and re-run with -Setup."
}

$PythonExe = $null
if (Test-Path -LiteralPath $VenvWinPy) {
    $PythonExe = $VenvWinPy
} elseif (Test-Path -LiteralPath $VenvPosixPy) {
    $PythonExe = $VenvPosixPy
}

Write-Diagnostic "repo      $RepoRoot"
if ($PythonExe) {
    # Fail with a clear message when a stale/broken interpreter is present
    # instead of a wall of import errors from the launcher.
    & $PythonExe -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 3)" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Diagnostic "the interpreter at $PythonExe is unusable or older than 3.11 (exit $LASTEXITCODE). Delete .venv and re-run with -Setup."
        exit 3
    }
    Write-Diagnostic "python    $PythonExe"
} else {
    Write-Diagnostic "python    not installed yet (run with -Setup)"
}

if ($Setup) {
    if (-not $PythonExe) {
        Write-Host "[AXIOM] Creating local virtual environment at $RepoRoot\.venv ..."
        $boot = @(Resolve-BootstrapPython)
        if ($boot.Count -gt 1) {
            & $boot[0] $boot[1..($boot.Count - 1)] -m venv (Join-Path $RepoRoot ".venv")
        } else {
            & $boot[0] -m venv (Join-Path $RepoRoot ".venv")
        }
        if ($LASTEXITCODE -ne 0) {
            exit $LASTEXITCODE
        }
        if (Test-Path -LiteralPath $VenvWinPy) {
            $PythonExe = $VenvWinPy
        } else {
            $PythonExe = $VenvPosixPy
        }
    }
    if (-not (Test-Path -LiteralPath $LockFile)) {
        Write-Diagnostic "requirements-lock.txt is missing at $LockFile; this is not a complete AXIOM checkout."
        exit 4
    }
    Write-Host "[AXIOM] Installing pinned runtime dependencies from requirements-lock.txt ..."
    & $PythonExe -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & $PythonExe -m pip install -r $LockFile
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & $PythonExe $LauncherPy --register-venv
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

if (-not $PythonExe) {
    Write-Error (
        "Virtual environment not found at '$VenvWinPy'.`n" +
        "Run first-time source setup with:`n" +
        "  powershell -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Setup"
    )
    exit 2
}

$ForwardArgs = New-Object System.Collections.Generic.List[string]
if ($CheckDeps) {
    $ForwardArgs.Add("--check-deps")
}
if ($SmokeCheck) {
    # A smoke run is non-interactive by definition: never open a browser.
    $ForwardArgs.Add("--smoke")
    $NoBrowser = $true
    Write-Diagnostic "mode      smoke (isolated temp data dir, PAPER only, 0 broker orders)"
}
if ($NoBrowser) {
    $ForwardArgs.Add("--no-browser")
}
if ($Port -gt 0) {
    $ForwardArgs.Add("--port")
    $ForwardArgs.Add("$Port")
}

try {
    & $PythonExe $LauncherPy @ForwardArgs
} catch {
    Write-Diagnostic "launcher failed: $($_.Exception.Message)"
    Write-Diagnostic "re-run with -Setup if dependencies are missing; configuration and credentials live in %LOCALAPPDATA%\AXIOM and are never modified by this script."
    exit 1
}
$Exit = $LASTEXITCODE
if ($Exit -ne 0) {
    Write-Diagnostic "AXIOM exited with status $Exit (2 = refused configuration/missing dependency, 1 = smoke failure or unexpected error)."
}
exit $Exit
