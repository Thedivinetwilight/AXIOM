<#
.SYNOPSIS
    Build, test and package AXIOM Operator for Windows x64.

.DESCRIPTION
    Runs on a Windows x64 machine or a GitHub Actions windows-latest runner.
    Every step is explicit; the build fails if it cannot do what it claims.

      1. verify the source commit matches what was asked for, tree clean
      2. install runtime + pinned build requirements
      3. run focused + full Python and all three JavaScript harnesses (all hard gates)
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
. (Join-Path $PSScriptRoot 'PaperSmoke.ps1')

function Step($text) { Write-Host "`n=== $text ===" -ForegroundColor Cyan }
function Fail($text) {
    Write-Host "`nBUILD FAILED: $text" -ForegroundColor Red
    Write-Host "::error title=AXIOM Windows build gate::$text"
    exit 1
}

# The script lives in packaging/windows/; the repository root is two levels up.
$Kit = $PSScriptRoot
$RepoRoot = (Resolve-Path (Join-Path $Kit '..\..')).Path
$PayloadGuard = Join-Path $Kit 'payload_guard.py'

trap {
    # Named Fail gates already have annotations. Capture unexpected strict-mode,
    # Cmdlet and HTTP failures too, otherwise a green test suite can hide the
    # actual packaging/smoke error when hosted raw logs are unavailable.
    $message = $_.Exception.Message
    $position = $_.InvocationInfo.PositionMessage
    Set-Content -Path (Join-Path $Kit 'build-error.txt') -Value @($message, $position) -Encoding utf8
    $smokeVariable = Get-Variable -Name SmokeHome -ErrorAction SilentlyContinue
    if ($smokeVariable -and $smokeVariable.Value) {
        foreach ($name in @('launcher-stdout.log', 'launcher-stderr.log')) {
            $source = Join-Path $smokeVariable.Value $name
            if (Test-Path $source) { Copy-Item $source (Join-Path $Kit "frozen-$name") -Force }
        }
    }
    Write-Host "::error title=AXIOM Windows build error::$message"
    exit 1
}

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

# Bind the eight reviewed a5 blocker-fix files to this exact build. Test files
# remain source evidence, not production payloads; runtime files are exercised
# below from the frozen onefile/onedir/installed/portable executables.
$BlockerFixPaths = @(
    'app/autonomy/models.py', 'app/autonomy/upstox.py', 'app/autonomy/store.py',
    'app/autonomy/service.py', 'app/static/autonomy/autonomy.js', 'docs/AUTONOMY.md',
    'tests/js/autonomy_smoke.mjs', 'tests/test_autonomy_release_blockers.py'
)
$BlockerFixFiles = @($BlockerFixPaths | ForEach-Object {
    $sourcePath = Join-Path $RepoRoot $_
    if (-not (Test-Path $sourcePath)) { Fail "missing reviewed blocker-fix file $_" }
    [ordered]@{
        path = $_
        sha256 = (Get-FileHash -Algorithm SHA256 $sourcePath).Hash.ToLower()
        production_payload = ($_ -notlike 'tests/*')
    }
})
if ($BlockerFixFiles.Count -ne 8) { Fail 'the blocker-fix source manifest must contain eight files' }

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
& python -m pytest -q tests/test_vantage_connector.py tests/test_vantage_api.py tests/test_mt5_market_data.py tests/test_terminal_performance_listed.py tests/test_vantage_dpapi_windows.py tests/test_us_equities_paper.py tests/test_us_chart_integration.py tests/test_chart_terminal.py tests/test_paper_replay_audit.py tests/test_windows_app_control_packaging.py tests/test_research_candidate_cli.py tests/test_kronos_setup.py tests/test_autonomy_foundation.py tests/test_autonomy_service.py tests/test_autonomy_research.py tests/test_autonomy_api.py tests/test_autonomy_shakedown.py tests/test_autonomy_release_blockers.py 2>&1 | Tee-Object -FilePath (Join-Path $Kit 'pytest-focused.log')
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
& node --check app/static/autonomy/autonomy.js
if ($LASTEXITCODE -ne 0) { Fail 'autonomy JavaScript syntax failed' }
& node tests/js/autonomy_smoke.mjs 2>&1 | Tee-Object -FilePath (Join-Path $Kit 'js-autonomy.log')
if ($LASTEXITCODE -ne 0) { Fail 'autonomy JavaScript interaction harness failed' }
& node tests/js/chart_smoke.mjs 2>&1 | Tee-Object -FilePath (Join-Path $Kit 'js-chart.log')
if ($LASTEXITCODE -ne 0) { Fail 'legacy chart JavaScript harness failed' }
& node tests/js/terminal_smoke.mjs 2>&1 | Tee-Object -FilePath (Join-Path $Kit 'js-terminal.log')
if ($LASTEXITCODE -ne 0) { Fail 'operator JavaScript interaction harness failed' }
& node tests/js/multimarket_terminal_smoke.mjs 2>&1 | Tee-Object -FilePath (Join-Path $Kit 'js-multimarket.log')
if ($LASTEXITCODE -ne 0) { Fail 'multi-market desktop JavaScript interaction harness failed' }

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

Step '3c. Exact-commit fake-provider performance benchmarks (hard gate)'
$BenchmarkOutput = Join-Path $Kit 'TERMINAL_BENCHMARK.json'
$UiBenchmarkOutput = Join-Path $Kit 'TERMINAL_UI_BENCHMARK.json'
& python scripts/benchmark_terminal.py --output $BenchmarkOutput 2>&1 | Tee-Object -FilePath (Join-Path $Kit 'benchmark-python.log')
if ($LASTEXITCODE -ne 0) {
    $benchTail = ((Get-Content (Join-Path $Kit 'benchmark-python.log') -Tail 12 -ErrorAction SilentlyContinue) -join ' || ')
    Fail "fake-provider backend benchmark failed on Windows :: $benchTail"
}
$env:AXIOM_UI_BENCHMARK_OUTPUT = $UiBenchmarkOutput
& node tests/js/terminal_smoke.mjs 2>&1 | Tee-Object -FilePath (Join-Path $Kit 'js-terminal-benchmark.log')
$BenchmarkUiExit = $LASTEXITCODE
Remove-Item Env:AXIOM_UI_BENCHMARK_OUTPUT -ErrorAction SilentlyContinue
if ($BenchmarkUiExit -ne 0) { Fail 'operator UI benchmark harness failed on Windows' }
$Benchmark = Get-Content (Join-Path $Kit 'TERMINAL_BENCHMARK.json') -Raw | ConvertFrom-Json
$UiBenchmark = Get-Content (Join-Path $Kit 'TERMINAL_UI_BENCHMARK.json') -Raw | ConvertFrom-Json
if ($Benchmark.commit -ne $Head -or $UiBenchmark.commit -ne $Head) { Fail 'benchmark source commit does not match the requested build' }
if ($Benchmark.working_tree_dirty_at_measurement -or $UiBenchmark.working_tree_dirty_at_measurement) { Fail 'benchmarks must run from the exact clean source tree' }
if ($Benchmark.assertions.order_send_calls -ne $Benchmark.assertions.expected_order_send_calls) { Fail 'benchmark order_send count mismatch' }
Write-Host 'exact-commit fake-only backend and UI benchmarks passed; no real DEMO/LIVE latency measured'

Step '3d. Explicit DEMO source payload allowlist (hard gate)'
& python $PayloadGuard source $RepoRoot
if ($LASTEXITCODE -ne 0) { Fail 'reviewed DEMO source payload validation failed' }

# ------------------------------------------------------------------ 4. freeze
Step '3e. Write the machine-readable build identity (hard gate)'
$IdentityPath = Join-Path $RepoRoot 'app\_build_identity.json'
$IdentitySourcePaths = @(
    'app/static/autonomy/autonomy.js', 'app/autonomy/upstox.py',
    'app/autonomy/models.py', 'app/autonomy/service.py', 'app/api/routes/autonomy.py'
)
$IdentityHashes = [ordered]@{}
foreach ($rel in $IdentitySourcePaths) {
    $abs = Join-Path $RepoRoot ($rel -replace '/', '\')
    if (-not (Test-Path $abs)) { Fail "build identity source missing: $rel" }
    $IdentityHashes[$rel] = (Get-FileHash -Algorithm SHA256 $abs).Hash.ToLower()
}
$Identity = [ordered]@{
    commit        = $Head
    commit_subject = (& git log -1 --format=%s).Trim()
    commit_date   = (& git log -1 --format=%cI).Trim()
    version       = $Version
    built_utc     = (Get-Date).ToUniversalTime().ToString('o')
    source_hashes = $IdentityHashes
}
$Identity | ConvertTo-Json -Depth 4 | Set-Content -Path $IdentityPath -Encoding utf8
Write-Host "build identity bound to commit $Head with $($IdentityHashes.Count) source hashes"

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

Step '4a. Inspect every legacy onefile CArchive payload (hard gate)'
& python $PayloadGuard onefile $Exe
if ($LASTEXITCODE -ne 0) { Fail 'legacy onefile contains forbidden database/user-state/weight payloads' }

Step '4b. Freeze the no-TEMP onedir runtime'
$OnedirDist = Join-Path $Kit 'dist-onedir'
$OnedirBuild = Join-Path $Kit 'build-onedir'
foreach ($p in @($OnedirDist, $OnedirBuild)) { if (Test-Path $p) { Remove-Item -Recurse -Force $p } }
$env:AXIOM_ONEDIR = '1'
Push-Location $Kit
try {
    & python -m PyInstaller --noconfirm --clean --distpath $OnedirDist --workpath $OnedirBuild axiom.spec
    if ($LASTEXITCODE -ne 0) { Fail 'PyInstaller onedir build failed' }
}
finally {
    Pop-Location
    Remove-Item Env:AXIOM_ONEDIR -ErrorAction SilentlyContinue
}
$OnedirRoot = Join-Path $OnedirDist 'AXIOM-Operator'
$OnedirExe = Join-Path $OnedirRoot 'AXIOM-Chart.exe'
if (-not (Test-Path $OnedirExe)) { Fail 'expected onedir AXIOM-Chart.exe' }
if (-not (Test-Path (Join-Path $OnedirRoot '_internal'))) { Fail 'onedir _internal dependency directory is missing' }
Write-Host 'onedir runtime built; it does not use PyInstaller onefile TEMP extraction'

Step '4c. Inspect every onedir payload file (hard gate)'
& python $PayloadGuard tree $OnedirRoot
if ($LASTEXITCODE -ne 0) { Fail 'onedir runtime contains forbidden database/user-state/weight payloads' }

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
    $Ok = (Check ($Terminal.StatusCode -eq 200 -and $Terminal.Content -match 'VANTAGE LIVE' -and $Terminal.Content -match 'LOCAL PAPER') '/terminal returns all explicit execution modes') -and $Ok
    $Ok = (Check ($Terminal.Content -match 'execution-boundary' -and $Terminal.Content -match 'watchlist-only' -and $Terminal.Content -match 'chart-indicator' -and $Terminal.Content -match 'drawing-tool' -and $Terminal.Content -match 'performance-summary') 'redesigned boundary, watchlist, chart tools and diagnostics are bundled') -and $Ok
    $TerminalJs = Invoke-WebRequest -UseBasicParsing "$Base/static/terminal/terminal.js" -TimeoutSec 10
    $Ok = (Check ($TerminalJs.StatusCode -eq 200 -and $TerminalJs.Content -match 'submitVantageReview' -and $TerminalJs.Content -match 'bindChartCanvas' -and $TerminalJs.Content -match 'renderTicketEstimate') 'operator terminal.js and reviewed ticket/chart interactions served from the bundle') -and $Ok
    $TerminalCss = Invoke-WebRequest -UseBasicParsing "$Base/static/terminal/terminal.css" -TimeoutSec 10
    $Ok = (Check ($TerminalCss.StatusCode -eq 200) 'operator terminal.css served from the bundle') -and $Ok
    $Manual = Invoke-WebRequest -UseBasicParsing "$Base/manual" -TimeoutSec 10
    $Ok = (Check ($Manual.StatusCode -eq 200 -and $Manual.Content -match 'Vantage' -and $Manual.Content -match 'Arm' -and $Manual.Content -match 'Owner-run Vantage DEMO execution rehearsal' -and $Manual.Content -match 'LIVE-readiness checklist') 'bundled user manual covers lifecycle, owner DEMO verification and LIVE checklist') -and $Ok
    $Autonomy = Invoke-WebRequest -UseBasicParsing "$Base/autonomy" -TimeoutSec 10
    $Ok = (Check ($Autonomy.StatusCode -eq 200 -and $Autonomy.Content -match 'PAPER' -and $Autonomy.Content -match 'UPSTOX_LIVE' -and $Autonomy.Content -match 'static/autonomy/autonomy.js') 'autonomy page and explicit execution boundaries are bundled') -and $Ok
    $AutonomyJs = Invoke-WebRequest -UseBasicParsing "$Base/static/autonomy/autonomy.js" -TimeoutSec 10
    $Ok = (Check ($AutonomyJs.StatusCode -eq 200 -and $AutonomyJs.Content -match 'reconcile' -and $AutonomyJs.Content -match 'activate') 'autonomy JavaScript served from the bundle') -and $Ok
    $Ok = (Check ($AutonomyJs.Content -match 'BOD rows') 'served autonomy.js carries the BOD parse diagnostics') -and $Ok
    # Prove the frozen bundle serves exactly this commit's files: compare the
    # served bytes against the staged source tree and the embedded identity.
    $ServedJsPath = Join-Path $SmokeHome 'served-autonomy.js'
    Invoke-WebRequest -UseBasicParsing "$Base/static/autonomy/autonomy.js" -OutFile $ServedJsPath -TimeoutSec 10
    $ServedJsHash = (Get-FileHash -Algorithm SHA256 $ServedJsPath).Hash.ToLower()
    $ExpectedJsHash = (Get-FileHash -Algorithm SHA256 (Join-Path $RepoRoot 'app\static\autonomy\autonomy.js')).Hash.ToLower()
    $Ok = (Check ($ServedJsHash -eq $ExpectedJsHash) 'served autonomy.js bytes match the staged source exactly') -and $Ok
    $BuildInfo = Invoke-WebRequest -UseBasicParsing "$Base/health" -TimeoutSec 10
    $Build = ($BuildInfo.Content | ConvertFrom-Json).build
    $Ok = (Check ($Build.commit -eq $Head) "frozen build identity names the exact commit $($Head.Substring(0, 10))") -and $Ok
    $Ok = (Check ($Build.frozen -eq $true) 'the running app reports its frozen package identity') -and $Ok
    foreach ($rel in $IdentitySourcePaths) {
        $expected = $IdentityHashes[$rel]
        $actual = $Build.source_hashes.$rel
        $Ok = (Check ($actual -eq $expected) "build identity source hash matches: $rel") -and $Ok
    }
    $ChartRuntime = Invoke-WebRequest -UseBasicParsing "$Base/static/vendor/lightweight-charts/lightweight-charts.standalone.production.js" -TimeoutSec 10
    $Ok = (Check ($ChartRuntime.StatusCode -eq 200) 'pinned display-only Lightweight Charts runtime is bundled') -and $Ok
    $AutonomyManual = Invoke-WebRequest -UseBasicParsing "$Base/autonomy/manual" -TimeoutSec 10
    $Ok = (Check ($AutonomyManual.StatusCode -eq 200 -and $AutonomyManual.Content -match 'not a live-readiness certificate') 'autonomy operator manual is bundled with honest limitations') -and $Ok

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
    $AnonMarketData = 0
    try { $null = Invoke-WebRequest -UseBasicParsing "$Base/api/market-data/status" -TimeoutSec 5 }
    catch { $AnonMarketData = [int]$_.Exception.Response.StatusCode }
    $Ok = (Check ($AnonMarketData -eq 401) "independent market-data status refuses anonymous request ($AnonMarketData)") -and $Ok
    $AnonPerformance = 0
    try { $null = Invoke-WebRequest -UseBasicParsing "$Base/api/trading/performance" -TimeoutSec 5 }
    catch { $AnonPerformance = [int]$_.Exception.Response.StatusCode }
    $Ok = (Check ($AnonPerformance -eq 401) "performance diagnostics refuse anonymous request ($AnonPerformance)") -and $Ok
    $AnonListed = 0
    try { $null = Invoke-WebRequest -UseBasicParsing "$Base/api/listed-assets/status" -TimeoutSec 5 }
    catch { $AnonListed = [int]$_.Exception.Response.StatusCode }
    $Ok = (Check ($AnonListed -eq 401) "listed-asset status refuses anonymous request ($AnonListed)") -and $Ok
    $AnonAutonomy = 0
    try { $null = Invoke-WebRequest -UseBasicParsing "$Base/api/autonomy/status" -TimeoutSec 5 }
    catch { $AnonAutonomy = [int]$_.Exception.Response.StatusCode }
    $Ok = (Check ($AnonAutonomy -eq 401) "autonomy status refuses anonymous request ($AnonAutonomy)") -and $Ok

    $Cfg = Get-Content (Join-Path $SmokeHome 'axiom.env')
    $Token = (($Cfg | Select-String '^API_TOKEN=').Line) -replace '^API_TOKEN=', ''
    $Headers = @{ Authorization = "Bearer $Token" }
    $AutonomyStatus = Invoke-RestMethod -Headers $Headers "$Base/api/autonomy/status"
    $Ok = (Check ($AutonomyStatus.automatic_arm_or_activate -eq $false -and $AutonomyStatus.sessions.Count -eq 0 -and $AutonomyStatus.kronos.weights_bundled -eq $false) 'autonomy starts with no sessions, no automatic approval and no bundled Kronos weights') -and $Ok

    $Trading = Invoke-RestMethod -Headers $Headers "$Base/api/trading/status"
    $Ok = (Check ($Trading.mode -eq 'AXIOM_PAPER' -and -not $Trading.armed -and -not $Trading.active) 'operator starts in independent PAPER, disarmed') -and $Ok
    $Performance = Invoke-RestMethod -Headers $Headers "$Base/api/trading/performance"
    $Ok = (Check ($Performance.sample_capacity_per_metric -eq 512 -and $Performance.note -match 'no universal or zero-latency') 'performance diagnostics are bounded and make no zero-latency claim') -and $Ok
    $Listed = Invoke-RestMethod -Headers $Headers "$Base/api/listed-assets/status"
    $Ok = (Check (-not $Listed.execution_supported -and -not $Listed.mt5_routing_allowed -and -not $Listed.cfd_substitution_allowed) 'listed execution and MT5/CFD substitution are disabled') -and $Ok
    $ListedBody = @{ symbol = 'AAPL'; exchange = 'NASDAQ'; currency = 'USD'; product_type = 'LISTED_SHARE' } | ConvertTo-Json
    $ListedPolicy = Invoke-RestMethod -Method Post -Headers $Headers -ContentType 'application/json' -Body $ListedBody "$Base/api/listed-assets/routing-policy"
    $Ok = (Check (-not $ListedPolicy.order_created -and -not $ListedPolicy.vantage_mt5_allowed -and -not $ListedPolicy.execution.allowed) 'listed policy check is read-only and never routes to MT5') -and $Ok
    $Capabilities = Invoke-RestMethod -Headers $Headers "$Base/api/trading/connection"
    $Ok = (Check ($Capabilities.secret_store -match 'Windows DPAPI') 'Windows DPAPI protected storage is available') -and $Ok
    $Ok = (Check ($Capabilities.mt5_package_available -eq $true) 'official MetaTrader5 module is present in the frozen build') -and $Ok
    $MarketData = Invoke-RestMethod -Headers $Headers "$Base/api/market-data/status"
    $Ok = (Check ($MarketData.execution_mode -eq 'AXIOM_PAPER' -and $MarketData.read_only -eq $true -and -not $MarketData.connected -and -not $MarketData.orders_modified -and -not $MarketData.live_trading) 'independent MT5 market data starts disconnected, read-only and paper-only') -and $Ok
    $MarketConnect = 0
    try { $null = Invoke-WebRequest -UseBasicParsing -Method Post -Headers $Headers "$Base/api/market-data/connect" -TimeoutSec 5 }
    catch { $MarketConnect = [int]$_.Exception.Response.StatusCode }
    $Ok = (Check ($MarketConnect -eq 409) "market-data connect without a DEMO profile fails closed ($MarketConnect)") -and $Ok
    $AfterMarketRefusal = Invoke-RestMethod -Headers $Headers "$Base/api/trading/status"
    $Ok = (Check ($AfterMarketRefusal.mode -eq 'AXIOM_PAPER' -and -not $AfterMarketRefusal.active) 'failed market-data connect never changes execution or activates') -and $Ok
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

    $Ok = (Check (Test-AxiomPaperBlockerSmoke $Exe) 'onefile frozen PAPER blocker fixes: candles, null event, persistence and market close') -and $Ok
    $Ok = (Check (Test-AxiomPaperBlockerSmoke $OnedirExe) 'onedir frozen PAPER blocker fixes: candles, null event, persistence and market close') -and $Ok
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
$Setup = Get-ChildItem $Dist -Filter '*-no-temp-setup.exe' | Select-Object -First 1
if (-not $Setup) { Fail 'loaderless installer was not produced' }
$SetupParts = @()
foreach ($index in @(0, 1)) {
    $part = Join-Path $Dist ($Setup.BaseName + "-$index.bin")
    if (-not (Test-Path $part)) { Fail "loaderless installer sidecar is missing: $part" }
    $SetupParts += $part
}
Write-Host ("built loaderless {0} with {1} required BIN sidecars" -f $Setup.Name, $SetupParts.Count)

Step '7b. Package the loaderless installer and onedir portable ZIPs'
$Staging = Join-Path $Kit 'package-staging'
if (Test-Path $Staging) { Remove-Item -Recurse -Force $Staging }
$InstallStage = Join-Path $Staging 'no-temp-install'
$PortableStage = Join-Path $Staging 'portable'
$PortableApp = Join-Path $PortableStage 'AXIOM-Operator'
New-Item -ItemType Directory -Force -Path $InstallStage, $PortableStage | Out-Null
Copy-Item $Setup.FullName $InstallStage
foreach ($part in $SetupParts) { Copy-Item $part $InstallStage }
Copy-Item -Recurse $OnedirRoot $PortableApp
$Docs = @(
    'US_EQUITIES_PAPER.md', 'VANTAGE_NEXT_VERSION_PLAN.md', 'TERMINAL_REDESIGN_PLAN.md',
    'TERMINAL_PERFORMANCE.md', 'LISTED_ASSETS.md', 'WINDOWS_APP_CONTROL.md',
    'RESEARCH_CANDIDATES.md'
)
foreach ($destination in @($InstallStage, $PortableApp)) {
    Copy-Item (Join-Path $Dist 'SOURCE_COMMIT.txt') $destination
    Copy-Item (Join-Path $Kit 'WINDOWS_INSTALL_MANUAL.md') $destination
    Copy-Item (Join-Path $RepoRoot 'USER_MANUAL.md') $destination
    $docDir = Join-Path $destination 'docs'
    New-Item -ItemType Directory -Force -Path $docDir | Out-Null
    foreach ($doc in $Docs) { Copy-Item (Join-Path $RepoRoot "docs\$doc") $docDir }
    Copy-Item (Join-Path $Kit 'TERMINAL_BENCHMARK.json') $docDir
    Copy-Item (Join-Path $Kit 'TERMINAL_UI_BENCHMARK.json') $docDir
}
$InstallReadme = @"
AXIOM Operator $Version - loaderless installer workaround

Extract this ENTIRE ZIP before running setup. Keep the no-temp-setup.exe and
both matching -0.bin/-1.bin files in the same folder. Run only the EXE after
all hashes have been verified. UseSetupLdr=no prevents Inno Setup from copying
its setup program to TEMP. The installed runtime is onedir and avoids
PyInstaller onefile TEMP extraction.

The package is still UNSIGNED. It does not disable or bypass Windows policy,
and an enforced Smart App Control/WDAC policy can still reject unsigned code.
Trusted signing or administrator approval is required if the device still
blocks it. No DEMO or LIVE account is contacted by installation.
"@
Set-Content -Path (Join-Path $InstallStage 'README-FIRST.txt') -Value $InstallReadme -Encoding utf8
$PortableReadme = @"
AXIOM Operator $Version - onedir portable workaround

Extract the ENTIRE ZIP, then run AXIOM-Chart.exe inside AXIOM-Operator.
Keep _internal beside the executable; do not copy the EXE out of this folder.
This runtime avoids PyInstaller onefile TEMP extraction. It is UNSIGNED and
Windows Application Control can still reject it. No security feature is
changed or disabled by this package.
"@
Set-Content -Path (Join-Path $PortableApp 'README-FIRST.txt') -Value $PortableReadme -Encoding utf8
$InstallZip = Join-Path $Dist "AXIOM-Operator-$Version-windows-x64-no-temp-install.zip"
$PortableZip = Join-Path $Dist "AXIOM-Operator-$Version-windows-x64-portable.zip"
Compress-Archive -Path (Join-Path $InstallStage '*') -DestinationPath $InstallZip -CompressionLevel Optimal
Compress-Archive -Path $PortableApp -DestinationPath $PortableZip -CompressionLevel Optimal
Add-Type -AssemblyName System.IO.Compression.FileSystem
foreach ($check in @(
    @{ Path = $InstallZip; Required = @($Setup.Name, ($Setup.BaseName + '-0.bin'), ($Setup.BaseName + '-1.bin'), 'README-FIRST.txt') },
    @{ Path = $PortableZip; Required = @('AXIOM-Operator/AXIOM-Chart.exe', 'AXIOM-Operator/README-FIRST.txt') }
)) {
    $archive = [System.IO.Compression.ZipFile]::OpenRead($check.Path)
    try {
        $names = @($archive.Entries | ForEach-Object { $_.FullName.Replace('\', '/') })
        foreach ($required in $check.Required) {
            if ($names -notcontains $required) { Fail "ZIP $($check.Path) is missing $required" }
        }
        if ($check.Path -eq $PortableZip -and -not ($names | Where-Object { $_ -like 'AXIOM-Operator/_internal/*' } | Select-Object -First 1)) {
            Fail 'portable ZIP has no _internal runtime dependencies'
        }
    }
    finally { $archive.Dispose() }
}
Write-Host 'loaderless installer ZIP and onedir portable ZIP structure verified'

Step '7c. Inspect final installer and portable ZIP payloads (hard gate)'
foreach ($package in @($InstallZip, $PortableZip)) {
    & python $PayloadGuard zip $package
    if ($LASTEXITCODE -ne 0) { Fail "final ZIP contains forbidden database/user-state/weight payloads: $package" }
}

Step '8. Checksums and build metadata'
$Log | Set-Content -Path (Join-Path $Dist 'install-test-log.txt') -Encoding utf8

$Artifacts = @()
foreach ($f in @($Exe, $InstallZip, $PortableZip)) {
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
    blocker_fix_files = $BlockerFixFiles
    build_identity = $Identity
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
        focused_tests = 'python -m pytest -q tests/test_vantage_connector.py tests/test_vantage_api.py tests/test_mt5_market_data.py tests/test_terminal_performance_listed.py tests/test_vantage_dpapi_windows.py tests/test_us_equities_paper.py tests/test_us_chart_integration.py tests/test_chart_terminal.py tests/test_paper_replay_audit.py tests/test_windows_app_control_packaging.py tests/test_research_candidate_cli.py tests/test_kronos_setup.py tests/test_autonomy_foundation.py tests/test_autonomy_service.py tests/test_autonomy_research.py tests/test_autonomy_api.py tests/test_autonomy_shakedown.py tests/test_autonomy_release_blockers.py'
        full_tests    = 'python -m pytest -q'
        javascript_tests = 'node --check app/static/chart/chart.js; node --check app/static/terminal/terminal.js; node --check app/static/autonomy/autonomy.js; node tests/js/autonomy_smoke.mjs; node tests/js/chart_smoke.mjs; node tests/js/terminal_smoke.mjs; node tests/js/multimarket_terminal_smoke.mjs'
        performance_benchmarks = "python scripts/benchmark_terminal.py --output `"$BenchmarkOutput`"; `$env:AXIOM_UI_BENCHMARK_OUTPUT = `"$UiBenchmarkOutput`"; node tests/js/terminal_smoke.mjs"
        payload_hygiene = "python `"$PayloadGuard`" source `"$RepoRoot`"; python `"$PayloadGuard`" onefile `"$Exe`"; python `"$PayloadGuard`" tree `"$OnedirRoot`"; python `"$PayloadGuard`" zip `"$InstallZip`"; python `"$PayloadGuard`" zip `"$PortableZip`""
        freeze        = 'python -m PyInstaller --noconfirm --clean axiom.spec'
        freeze_onedir = '$env:AXIOM_ONEDIR = ''1''; python -m PyInstaller --noconfirm --clean --distpath dist-onedir --workpath build-onedir axiom.spec'
        installer     = "`"$Iscc`" `"/DMyAppVersion=$Version`" axiom.iss (UseSetupLdr=no; onedir runtime)"
        installer_test = './install_test.ps1 -DistDir dist -SetupPath <extracted-no-temp-setup.exe> (workflow hard gate; includes upgrade and uninstall)'
        portable_test = './portable_test.ps1 -PackagePath <release-portable.zip> -ExpectedCommit <exact-commit> (workflow hard gate)'
    }
    test_results  = [ordered]@{
        focused_windows = 'passed'
        full_windows    = $(if ($FullExit -eq 0) { 'passed' } else { 'failed' })
        full_summary    = $FullSummary
        full_failures   = @($FullFailures)
        javascript      = 'chart + terminal + autonomy syntax checks and all three interaction harnesses passed with local DOM/fetch fakes'
        payload_hygiene = 'passed: explicit DEMO allowlist plus onefile CArchive, onedir tree, installer ZIP and portable ZIP forbidden-name/SQLite-header checks; exact clean source and no broker credentials supplied'
        autonomy        = 'autonomy Python/API/fault/research/shakedown/blocker suites and frozen safe-default/manual/asset gates passed; onefile and onedir PAPER diagnostics exercised candle ordering, null-event status, persistence halt and market-close cleanup with zero broker order calls; no Upstox account, external order or complete-session shakedown tested; genuine Kronos inference is a separate installed-app hard gate'
        blocker_fix_smoke = 'onefile + onedir passed; installed + upgraded + portable results are appended by the separate workflow acceptance gates; synthetic temporary PAPER state only'
        dpapi           = 'real Windows DPAPI round-trips passed with dummy values only'
        fake_mt5        = 'connector/service/API tests passed; no broker account contacted'
        read_only_mt5   = 'fake read-only market-data provider, fail-closed and no-order-reachability tests passed'
        research_candidates = 'chronological warmup, positive walk-forward, zero-PF scoring, finite router, robustness and per-instrument CLI regressions passed on local/synthetic fixtures; no real-market profitability or execution evidence'
        performance     = 'exact-commit Windows fake-provider backend + fake-canvas UI benchmarks passed; see TERMINAL_BENCHMARK.json and TERMINAL_UI_BENCHMARK.json; no real DEMO/LIVE latency measured'
        listed_assets   = 'authenticated listed identity/policy tests passed; no listed execution provider and no MT5/CFD substitution'
        app_control_workaround = 'loaderless installer and onedir ZIPs built; portable ZIP acceptance runs in the workflow; enforced on-device Smart App Control remains unverified'
        vantage_demo    = 'NOT TESTED - no Vantage demo account available to CI'
        demo_market_data = 'NOT TESTED with a real terminal/account; no real quote, bar or local paper fill was observed'
        vantage_live    = 'NOT TESTED - live login/orders prohibited in CI'
        smoke_checks    = @($Log | Where-Object { $_ -match '^(PASS|FAIL)' })
        smoke_failed    = @($Log | Where-Object { $_ -match '^FAIL' }).Count
    }
    execution_safety = [ordered]@{
        default_mode      = 'AXIOM_PAPER; Vantage disconnected and disarmed'
        legacy_process    = 'MODE=PAPER, LIVE_TRADING=false, BROKER=LOCAL_PAPER enforced for the legacy engine'
        paper_order_path  = 'POST /paper/order -> _require_local_paper() -> in-process PaperBroker (simulated fills)'
        market_data_path  = 'VANTAGE_DEMO DPAPI credential -> isolated read-only MT5 gateway -> fresh validated quotes/closed bars; no execution gateway'
        mt5_paper_scope   = 'manual_only; exact account-discovered symbols; configured timeframe; explicit units, not MT5 lots; local fills only'
        vantage_order_path = 'authenticated review -> durable intent -> one official MT5 request -> broker reconciliation'
        live_routing      = 'present but blocked until exact identity, readiness, Arm, separate Activate and final review'
        automation        = 'Vantage automation unavailable and OFF'
        autonomy_paper    = 'live-market local simulation with no broker order/cancel/modify route'
        autonomy_live     = 'UPSTOX_LIVE disabled by default; fresh daily authorization/risk and separate operator Arm/Activate required; no real account or order tested'
        fail_closed_cases = @('MODE=LIVE', 'LIVE_TRADING=true', 'BROKER=UPSTOX_SANDBOX',
                              'alpaca without credentials', 'alpaca without limits', 'empty API_TOKEN')
    }
    packaging = [ordered]@{
        installer_mode = 'Inno Setup UseSetupLdr=no; setup.exe requires matching -0.bin and -1.bin beside it'
        installed_runtime = 'PyInstaller onedir; no onefile _MEI TEMP extraction'
        portable_runtime = 'extract entire onedir ZIP and keep AXIOM-Chart.exe beside _internal'
        legacy_onefile = 'AXIOM-Chart.exe retained for compatibility; it can use TEMP extraction'
        app_control_boundary = 'UNSIGNED workaround only; no policy is disabled or bypassed and enforced on-device acceptance is not claimed'
        demo_data = 'eight explicit reviewed synthetic DEMO CSV/JSON fixtures; recursive data/ collection is prohibited'
        payload_exclusions = 'credentials/environment files, SQLite databases and sidecars, autonomy/cache/results state, local logs/locks, and model-weight files'
        setup_sidecars = @($SetupParts | ForEach-Object { Split-Path $_ -Leaf })
    }
    signing       = [ordered]@{
        status = 'UNSIGNED'
        detail = 'No code-signing certificate was configured; all installer, portable and runtime payloads are unsigned.'
    }
    artifacts     = $Artifacts
    pip_freeze    = $PipFreeze
}
$Meta | ConvertTo-Json -Depth 8 | Set-Content -Path (Join-Path $Dist 'BUILD_METADATA.json') -Encoding utf8

$SumFiles = @($Exe, $InstallZip, $PortableZip, (Join-Path $Dist 'BUILD_METADATA.json'))
$SumFiles | ForEach-Object {
    "{0}  {1}" -f (Get-FileHash -Algorithm SHA256 $_).Hash.ToLower(), (Split-Path $_ -Leaf)
} | Set-Content -Path (Join-Path $Dist 'SHA256SUMS.txt') -Encoding ascii

Remove-Item -Force $IdentityPath -ErrorAction SilentlyContinue

Step 'Done'
Get-ChildItem $Dist -File | ForEach-Object { "{0,-56} {1,12:N1} KB" -f $_.Name, ($_.Length / 1KB) }
Get-Content (Join-Path $Dist 'SHA256SUMS.txt')
