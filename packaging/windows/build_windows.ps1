<#
.SYNOPSIS
    Build, test and package AXIOM Operator for Windows x64.

.DESCRIPTION
    Runs on a Windows x64 machine or a GitHub Actions windows-latest runner.
    Every step is explicit; the build fails if it cannot do what it claims.

      1. verify the source commit matches what was asked for, tree clean
      2. install runtime + pinned build requirements
      3. run focused + full Python and both JavaScript suites (all hard gates)
      4. freeze the application with PyInstaller (onefile AXIOM-Chart.exe)
      5. smoke-test the frozen executable: /terminal, legacy /chart, assets,
         token gate, disconnected Vantage default, DEMO data and paper controls
      6. fail-closed checks: the exe must refuse non-paper configurations
      7. build the Inno Setup installer
      8. write SOURCE_COMMIT.txt, SHA256SUMS.txt and BUILD_METADATA.json

.PARAMETER ExpectedCommit
    The full 40-character commit this build must be made from.

.PARAMETER Version
    Version label used for the installer and the artifact names.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ExpectedCommit,
    [Parameter(Mandatory = $true)][string]$Version
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
# Expected non-zero exits (the fail-closed cases) must not throw.
$PSNativeCommandUseErrorActionPreference = $false

function Step($text) { Write-Host "`n=== $text ===" -ForegroundColor Cyan }
function Fail($text) { Write-Host "`nBUILD FAILED: $text" -ForegroundColor Red; exit 1 }

# The script lives in packaging/windows/; the repository root is two levels up.
$Kit = $PSScriptRoot
$RepoRoot = (Resolve-Path (Join-Path $Kit '..\..')).Path
Set-Location $RepoRoot

$Clean = { param($p) if ($p) { ($p | Out-String).Trim() } }

# ---------------------------------------------------------------- 1. identity
Step '1. Verify the source commit'
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Fail 'git is required' }
$Head = & git rev-parse HEAD
$Head = $Head.Trim()
if ($Head -ne $ExpectedCommit) { Fail "source is $Head but this build was asked for $ExpectedCommit" }
$Dirty = (& git status --porcelain | Out-String).Trim()
if ($Dirty) { Fail "working tree is not clean:`n$Dirty" }
Write-Host "commit  $Head"
Write-Host "subject $((& git log -1 --format=%s).Trim())"

# ------------------------------------------------------------ 2. environment
Step '2. Build environment'
if (-not (Get-Command python -ErrorAction SilentlyContinue)) { Fail 'python is not on PATH' }
$PyVersion = (& python -c "import sys;print(sys.version.split()[0])").Trim()
$PyArch = (& python -c "import platform;print(platform.machine())").Trim()
if ($PyArch -notmatch 'AMD64|x86_64') { Fail "python must be x64, found $PyArch" }
Write-Host "python $PyVersion ($PyArch)"
Write-Host ([System.Environment]::OSVersion.VersionString)

Step '2a. Install requirements'
& python -m pip install --upgrade pip --quiet
& python -m pip install --quiet -r requirements.txt -r (Join-Path $Kit 'requirements-build.txt')
if ($LASTEXITCODE -ne 0) { Fail 'pip install failed' }
$PipFreeze = @(& python -m pip freeze)

# ------------------------------------------------------------------ 3. tests
Step '3. Focused Python test suites (hard gate)'
& python -m pytest -q tests/test_vantage_connector.py tests/test_vantage_api.py tests/test_vantage_dpapi_windows.py tests/test_us_equities_paper.py tests/test_us_chart_integration.py tests/test_chart_terminal.py 2>&1 | Tee-Object -FilePath (Join-Path $Kit 'pytest-focused.log')
$FocusedExit = $LASTEXITCODE
if ($FocusedExit -ne 0) { Fail 'focused Vantage/US/chart suites failed on Windows' }
Write-Host 'focused suites passed, including real DPAPI round-trips with dummy values'

Step '3a. JavaScript operator suites (hard gate)'
if (-not (Get-Command node -ErrorAction SilentlyContinue)) { Fail 'Node.js is required for UI tests' }
if (-not (Get-Command npm -ErrorAction SilentlyContinue)) { Fail 'npm is required for pinned UI test dependencies' }
& npm ci --ignore-scripts --no-audit --no-fund
if ($LASTEXITCODE -ne 0) { Fail 'npm ci failed' }
& node --check app/static/chart/chart.js
if ($LASTEXITCODE -ne 0) { Fail 'legacy chart JavaScript syntax failed' }
& node --check app/static/terminal/terminal.js
if ($LASTEXITCODE -ne 0) { Fail 'operator JavaScript syntax failed' }
& node tests/js/chart_smoke.mjs 2>&1 | Tee-Object -FilePath (Join-Path $Kit 'js-chart.log')
if ($LASTEXITCODE -ne 0) { Fail 'legacy chart JavaScript harness failed' }
& node tests/js/terminal_smoke.mjs 2>&1 | Tee-Object -FilePath (Join-Path $Kit 'js-terminal.log')
if ($LASTEXITCODE -ne 0) { Fail 'operator JavaScript interaction harness failed' }

Step '3b. Full Python test suite (hard gate)'
& python -m pytest -q 2>&1 | Tee-Object -FilePath (Join-Path $Kit 'pytest-full.log')
$FullExit = $LASTEXITCODE
$FullTail = (Get-Content (Join-Path $Kit 'pytest-full.log') -Tail 30) -join "`n"
$FullFailures = @(Get-Content (Join-Path $Kit 'pytest-full.log') |
    Where-Object { $_ -match '^(FAILED|ERROR) ' } |
    ForEach-Object { $_.Trim() })
$FullSummary = 'unknown'
if ($FullTail -match '(\d+) passed') { $FullSummary = "$($Matches[1]) passed" }
if ($FullTail -match '(\d+) failed') { $FullSummary = "$FullSummary, $($Matches[1]) failed" }
if ($FullTail -match '(\d+) skipped') { $FullSummary = "$FullSummary, $($Matches[1]) skipped" }
if ($FullExit -ne 0) {
    foreach ($f in $FullFailures) { Write-Host "  failing| $f" -ForegroundColor Red }
    Fail "full Python suite failed on Windows: $FullSummary"
}
Write-Host "full suite passed ($FullSummary)"

# ------------------------------------------------------------------ 4. freeze
Step '4. Freeze with PyInstaller'
$Dist = Join-Path $Kit 'dist'
$BuildTmp = Join-Path $Kit 'build'
foreach ($p in @($Dist, $BuildTmp)) { if (Test-Path $p) { Remove-Item -Recurse -Force $p } }
Push-Location $Kit
try {
    & python -m PyInstaller --noconfirm --clean axiom.spec
    if ($LASTEXITCODE -ne 0) { Fail 'PyInstaller failed' }
}
finally { Pop-Location }
$Exe = Join-Path $Dist 'AXIOM-Chart.exe'
if (-not (Test-Path $Exe)) { Fail "expected $Exe" }
$ExeItem = Get-Item $Exe
Write-Host ("built {0} ({1:N1} MB)" -f $ExeItem.Name, ($ExeItem.Length / 1MB))

# ------------------------------------------------------- 5. smoke the build
Step '5. Smoke-test the frozen executable on this machine'
$Port = 8123
$SmokeHome = Join-Path $env:TEMP ("axiom-smoke-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $SmokeHome | Out-Null
$env:AXIOM_DATA_DIR = $SmokeHome
$env:AXIOM_PORT = "$Port"
$Base = "http://127.0.0.1:$Port"
$Log = New-Object System.Collections.Generic.List[string]
function Note($line) { $Log.Add($line); Write-Host "  $line" }
function Check($ok, $label) {
    if ($ok) { Note "PASS  $label" } else { Note "FAIL  $label" }
    return [bool]$ok
}

$SmokeOut = Join-Path $SmokeHome 'launcher-stdout.log'
$SmokeErr = Join-Path $SmokeHome 'launcher-stderr.log'
$Proc = Start-Process -FilePath $Exe -ArgumentList '--no-browser' -PassThru -WindowStyle Hidden -RedirectStandardOutput $SmokeOut -RedirectStandardError $SmokeErr
$Ready = $false
for ($i = 0; $i -lt 90; $i++) {
    Start-Sleep -Milliseconds 500
    if ($Proc.HasExited) { break }
    try { $null = Invoke-WebRequest -UseBasicParsing "$Base/health" -TimeoutSec 2; $Ready = $true; break } catch { }
}
try {
    if (-not $Ready) { Fail 'the packaged app never answered /health' }

    $Ok = $true
    $Ok = (Check $true 'launch: API answered /health on loopback') -and $Ok

    $Chart = Invoke-WebRequest -UseBasicParsing "$Base/chart" -TimeoutSec 10
    $Ok = (Check ($Chart.StatusCode -eq 200) '/chart returns 200') -and $Ok
    $Ok = (Check ($Chart.Content -match 'PAPER') 'chart page displays the PAPER badge') -and $Ok

    $Terminal = Invoke-WebRequest -UseBasicParsing "$Base/terminal" -TimeoutSec 10
    $Ok = (Check ($Terminal.StatusCode -eq 200 -and $Terminal.Content -match 'VANTAGE LIVE' -and $Terminal.Content -match 'AXIOM PAPER') '/terminal returns all explicit execution modes') -and $Ok
    $TerminalJs = Invoke-WebRequest -UseBasicParsing "$Base/static/terminal/terminal.js" -TimeoutSec 10
    $Ok = (Check ($TerminalJs.StatusCode -eq 200 -and $TerminalJs.Content -match 'submitVantageReview') 'operator terminal.js served from the bundle') -and $Ok
    $TerminalCss = Invoke-WebRequest -UseBasicParsing "$Base/static/terminal/terminal.css" -TimeoutSec 10
    $Ok = (Check ($TerminalCss.StatusCode -eq 200) 'operator terminal.css served from the bundle') -and $Ok
    $Manual = Invoke-WebRequest -UseBasicParsing "$Base/manual" -TimeoutSec 10
    $Ok = (Check ($Manual.StatusCode -eq 200 -and $Manual.Content -match 'Vantage' -and $Manual.Content -match 'Arm') 'bundled user manual covers Vantage lifecycle') -and $Ok

    $Js = Invoke-WebRequest -UseBasicParsing "$Base/static/chart/chart.js" -TimeoutSec 10
    $Ok = (Check ($Js.StatusCode -eq 200 -and $Js.Content -match 'markerPoint') 'chart.js served from the bundle') -and $Ok
    $Css = Invoke-WebRequest -UseBasicParsing "$Base/static/chart/chart.css" -TimeoutSec 10
    $Ok = (Check ($Css.StatusCode -eq 200) 'chart.css served from the bundle') -and $Ok

    # Token gate.
    $Anon = 0
    try { $null = Invoke-WebRequest -UseBasicParsing "$Base/chart/data?symbol=DEMO&timeframe=1D" -TimeoutSec 5 }
    catch { $Anon = [int]$_.Exception.Response.StatusCode }
    $Ok = (Check ($Anon -eq 401) "data route refuses anonymous request ($Anon)") -and $Ok
    $AnonTrading = 0
    try { $null = Invoke-WebRequest -UseBasicParsing "$Base/api/trading/status" -TimeoutSec 5 }
    catch { $AnonTrading = [int]$_.Exception.Response.StatusCode }
    $Ok = (Check ($AnonTrading -eq 401) "Vantage status refuses anonymous request ($AnonTrading)") -and $Ok

    $Cfg = Get-Content (Join-Path $SmokeHome 'axiom.env')
    $Token = (($Cfg | Select-String '^API_TOKEN=').Line) -replace '^API_TOKEN=', ''
    $Headers = @{ Authorization = "Bearer $Token" }

    $Trading = Invoke-RestMethod -Headers $Headers "$Base/api/trading/status"
    $Ok = (Check ($Trading.mode -eq 'AXIOM_PAPER' -and -not $Trading.armed -and -not $Trading.active) 'operator starts in independent PAPER, disarmed') -and $Ok
    $Capabilities = Invoke-RestMethod -Headers $Headers "$Base/api/trading/connection"
    $Ok = (Check ($Capabilities.secret_store -match 'Windows DPAPI') 'Windows DPAPI protected storage is available') -and $Ok
    $Ok = (Check ($Capabilities.mt5_package_available -eq $true) 'official MetaTrader5 module is present in the frozen build') -and $Ok
    $DemoBody = @{ mode = 'VANTAGE_DEMO' } | ConvertTo-Json
    $Demo = Invoke-RestMethod -Method Post -Headers $Headers -ContentType 'application/json' -Body $DemoBody "$Base/api/trading/session"
    $Ok = (Check ($Demo.mode -eq 'VANTAGE_DEMO' -and -not $Demo.connected -and -not $Demo.armed -and -not $Demo.active) 'selecting DEMO never auto-connects or activates') -and $Ok
    $Disconnected = Invoke-RestMethod -Headers $Headers "$Base/api/trading/workspace"
    $Ok = (Check ($Disconnected.broker_state_available -eq $false -and $null -eq $Disconnected.reconciled_at) 'disconnected workspace is explicitly not a flat-account snapshot') -and $Ok
    $PaperBody = @{ mode = 'AXIOM_PAPER' } | ConvertTo-Json
    $null = Invoke-RestMethod -Method Post -Headers $Headers -ContentType 'application/json' -Body $PaperBody "$Base/api/trading/session"

    $Symbols = Invoke-RestMethod -Headers $Headers "$Base/chart/symbols"
    $Ok = (Check ($Symbols.symbols.Count -gt 0) "symbols listed ($($Symbols.symbols.Count))") -and $Ok

    $Payload = Invoke-RestMethod -Headers $Headers "$Base/chart/data?symbol=DEMO&timeframe=1D&limit=60"
    $Ok = (Check ($Payload.count -ge 60) "DEMO preview drew $($Payload.count) bars from bundled data") -and $Ok
    $Ok = (Check ($Payload.market_data -eq $false -and $Payload.data_feed_label -match 'NOT MARKET DATA') "DEMO bars labelled '$($Payload.data_feed_label)'") -and $Ok

    # Paper session start/stop.
    $Started = Invoke-RestMethod -Method Post -Headers $Headers "$Base/api/paper/start-default"
    $Ok = (Check ($Started.mode -eq 'PAPER' -and $Started.live_trading -eq $false) 'paper session start reported PAPER / live_trading=false') -and $Ok
    Start-Sleep -Seconds 3
    $Status = Invoke-RestMethod -Headers $Headers "$Base/paper/status"
    $Ok = (Check ($Status.state -eq 'RUNNING' -and $Status.paper_mode -eq $true) "session RUNNING, $($Status.processed_bars) bars processed, paper_mode=true") -and $Ok
    $Post = Invoke-RestMethod -Headers $Headers "$Base/chart/data?symbol=DEMO&timeframe=1D&limit=60"
    $Ok = (Check ($Post.source -eq 'engine') "chart then drew bars the engine processed (source=$($Post.source))") -and $Ok
    $Stopped = Invoke-RestMethod -Method Post -Headers $Headers "$Base/paper/stop"
    $Ok = (Check ($Stopped.state -eq 'STOPPED') 'paper session stopped') -and $Ok

    # Manual order path must be the local simulator.
    $Ticket = Invoke-RestMethod -Headers $Headers "$Base/paper/ticket?symbol=DEMO"
    $Ok = (Check ($Ticket.paper_only -eq $true -and $Ticket.live_trading -eq $false) 'ticket reports paper_only=true, live_trading=false') -and $Ok

    # Missing data must be explained, not invented.
    $Missing = 0
    try { $null = Invoke-WebRequest -UseBasicParsing -Headers $Headers "$Base/chart/data?symbol=NOPE&timeframe=1D" -TimeoutSec 5 }
    catch { $Missing = [int]$_.Exception.Response.StatusCode }
    $Ok = (Check ($Missing -eq 422) "unknown symbol refused with an explanation ($Missing)") -and $Ok

    if (-not $Ok) { Fail 'one or more smoke checks failed; see the log above' }
}
finally {
    if (-not $Proc.HasExited) { $Proc.Kill(); $Proc.WaitForExit(5000) }
    # A onefile bootloader can have a same-named child. Stop only processes
    # whose executable is this build, never unrelated AXIOM installations.
    Get-Process -Name 'AXIOM-Chart' -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -eq $Exe } |
        ForEach-Object { try { $_.Kill(); $_.WaitForExit(5000) } catch { } }
}
$LauncherOutput = @((Get-Content $SmokeOut -Raw -ErrorAction SilentlyContinue), (Get-Content $SmokeErr -Raw -ErrorAction SilentlyContinue)) -join "`n"
if ($Token -and $LauncherOutput -match [regex]::Escape($Token)) { Fail 'launcher output exposed the local API token' }
Note 'PASS  launcher output does not expose the local API token'

Step '6. Fail-closed checks'
function Test-Refuses([string]$Name, [hashtable]$Overrides) {
    $dir = Join-Path $env:TEMP ("axiom-refuse-" + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $dir | Out-Null
    $cfg = @('MODE=PAPER', 'LIVE_TRADING=false', 'BROKER=LOCAL_PAPER', 'API_TOKEN=smoke-token')
    foreach ($k in $Overrides.Keys) {
        $cfg = @($cfg | Where-Object { $_ -notlike "$k=*" })
        $cfg += "$k=$($Overrides[$k])"
    }
    Set-Content -Path (Join-Path $dir 'axiom.env') -Value $cfg
    $env:AXIOM_DATA_DIR = $dir
    $env:AXIOM_PORT = '8124'
    $out = (& $Exe --no-browser 2>&1 | Out-String)
    $code = $LASTEXITCODE
    if ($code -eq 0) { Note "FAIL  $Name was accepted but must be refused"; Fail "$Name not refused" }
    Note "PASS  refused $Name (exit $code)"
}
Test-Refuses 'MODE=LIVE' @{ MODE = 'LIVE' }
Test-Refuses 'LIVE_TRADING=true' @{ LIVE_TRADING = 'true' }
Test-Refuses 'BROKER=UPSTOX_SANDBOX' @{ BROKER = 'UPSTOX_SANDBOX' }
Test-Refuses 'alpaca without credentials' @{ DATA_SOURCE = 'alpaca'; MARKET = 'us'; CURRENCY = 'USD'; MARKET_CALENDAR = 'us-equities' }
Test-Refuses 'alpaca without limits' @{ DATA_SOURCE = 'alpaca'; MARKET = 'us'; CURRENCY = 'USD'; MARKET_CALENDAR = 'us-equities'; ALPACA_API_KEY = 'x'; ALPACA_API_SECRET = 'y'; ALPACA_SYMBOLS = 'AAPL'; DEFAULT_TIMEFRAME = '1m' }
Test-Refuses 'empty API_TOKEN' @{ API_TOKEN = '' }

# --------------------------------------------------------------- 7. installer
Step '7. Build the installer (Inno Setup 6)'
$Iscc = @(
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if (-not $Iscc) {
    $cmd = Get-Command iscc.exe -ErrorAction SilentlyContinue
    if ($cmd) { $Iscc = $cmd.Source }
}
if (-not $Iscc) {
    Write-Host 'Inno Setup 6 not found; attempting winget/choco install'
    if (Get-Command choco -ErrorAction SilentlyContinue) {
        & choco install innosetup --no-progress -y | Out-Host
    } elseif (Get-Command winget -ErrorAction SilentlyContinue) {
        & winget install --id JRSoftware.InnoSetup --silent --accept-package-agreements --accept-source-agreements | Out-Host
    }
    $Iscc = @(
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
    ) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
    if (-not $Iscc) { Fail 'Inno Setup 6 (ISCC.exe) is unavailable' }
}
Write-Host "using $Iscc"

# The commit identifier travels inside the installed program.
Set-Content -Path (Join-Path $Dist 'SOURCE_COMMIT.txt') -Encoding ascii -Value @(
    "commit:   $Head"
    "subject:  $((& git log -1 --format=%s).Trim())"
    "date:     $((& git log -1 --format=%cI).Trim())"
    "version:  $Version"
    "built:    $((Get-Date).ToUniversalTime().ToString('o'))"
)

Push-Location $Kit
try {
    & $Iscc "/DMyAppVersion=$Version" axiom.iss
    if ($LASTEXITCODE -ne 0) { Fail 'Inno Setup failed' }
}
finally { Pop-Location }
$Setup = Get-ChildItem $Dist -Filter '*-setup.exe' | Select-Object -First 1
if (-not $Setup) { Fail 'installer was not produced' }
Write-Host ("built {0} ({1:N1} MB)" -f $Setup.Name, ($Setup.Length / 1MB))

Step '8. Checksums and build metadata'
$Log | Set-Content -Path (Join-Path $Dist 'install-test-log.txt') -Encoding utf8

$Artifacts = @()
foreach ($f in @($Exe, $Setup.FullName)) {
    $item = Get-Item $f
    $Artifacts += [ordered]@{
        name   = $item.Name
        bytes  = $item.Length
        sha256 = (Get-FileHash -Algorithm SHA256 $item.FullName).Hash.ToLower()
    }
}
$Meta = [ordered]@{
    schema        = 'axiom.build-metadata/1'
    version       = $Version
    commit        = $Head
    commit_subject = (& git log -1 --format=%s).Trim()
    commit_date   = (& git log -1 --format=%cI).Trim()
    built_utc     = (Get-Date).ToUniversalTime().ToString('o')
    platform      = 'Windows x64'
    build_environment = [ordered]@{
        os          = [System.Environment]::OSVersion.VersionString
        runner_image = $env:ImageOS
        python      = $PyVersion
        python_arch = $PyArch
        powershell  = $PSVersionTable.PSVersion.ToString()
        workflow_run = $env:GITHUB_RUN_ID
        workflow_ref = $env:GITHUB_WORKFLOW_REF
    }
    commands      = [ordered]@{
        focused_tests = 'python -m pytest -q tests/test_vantage_connector.py tests/test_vantage_api.py tests/test_vantage_dpapi_windows.py tests/test_us_equities_paper.py tests/test_us_chart_integration.py tests/test_chart_terminal.py'
        full_tests    = 'python -m pytest -q'
        javascript_tests = 'node tests/js/chart_smoke.mjs; node tests/js/terminal_smoke.mjs'
        freeze        = 'python -m PyInstaller --noconfirm --clean axiom.spec'
        installer     = "ISCC.exe /DMyAppVersion=$Version axiom.iss"
    }
    test_results  = [ordered]@{
        focused_windows = 'passed'
        full_windows    = $(if ($FullExit -eq 0) { 'passed' } else { 'failed' })
        full_summary    = $FullSummary
        full_failures   = @($FullFailures)
        javascript      = 'chart smoke + operator interaction passed'
        dpapi           = 'real Windows DPAPI round-trips passed with dummy values only'
        fake_mt5        = 'connector/service/API tests passed; no broker account contacted'
        vantage_demo    = 'NOT TESTED - no Vantage demo account available to CI'
        vantage_live    = 'NOT TESTED - live login/orders prohibited in CI'
        smoke_checks    = @($Log | Where-Object { $_ -match '^(PASS|FAIL)' })
        smoke_failed    = @($Log | Where-Object { $_ -match '^FAIL' }).Count
    }
    execution_safety = [ordered]@{
        default_mode      = 'AXIOM_PAPER; Vantage disconnected and disarmed'
        legacy_process    = 'MODE=PAPER, LIVE_TRADING=false, BROKER=LOCAL_PAPER enforced for the legacy engine'
        paper_order_path  = 'POST /paper/order -> _require_local_paper() -> in-process PaperBroker (simulated fills)'
        vantage_order_path = 'authenticated review -> durable intent -> one official MT5 request -> broker reconciliation'
        live_routing      = 'present but blocked until exact identity, readiness, Arm, separate Activate and final review'
        automation        = 'Vantage automation unavailable and OFF'
        fail_closed_cases = @('MODE=LIVE', 'LIVE_TRADING=true', 'BROKER=UPSTOX_SANDBOX',
                              'alpaca without credentials', 'alpaca without limits', 'empty API_TOKEN')
    }
    signing       = [ordered]@{
        status = 'UNSIGNED'
        detail = 'No code-signing certificate was configured; installer and executable are unsigned.'
    }
    artifacts     = $Artifacts
    pip_freeze    = $PipFreeze
}
$Meta | ConvertTo-Json -Depth 8 | Set-Content -Path (Join-Path $Dist 'BUILD_METADATA.json') -Encoding utf8

$SumFiles = @($Exe, $Setup.FullName, (Join-Path $Dist 'BUILD_METADATA.json')) +
            @(Get-ChildItem $Dist -Filter '*.exe' | ForEach-Object { $_.FullName }) | Select-Object -Unique
$SumFiles | ForEach-Object {
    "{0}  {1}" -f (Get-FileHash -Algorithm SHA256 $_).Hash.ToLower(), (Split-Path $_ -Leaf)
} | Set-Content -Path (Join-Path $Dist 'SHA256SUMS.txt') -Encoding ascii

Step 'Done'
Get-ChildItem $Dist -File | ForEach-Object { "{0,-56} {1,12:N1} KB" -f $_.Name, ($_.Length / 1KB) }
Get-Content (Join-Path $Dist 'SHA256SUMS.txt')
