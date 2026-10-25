<#
    Install, launch, exercise and uninstall the AXIOM chart terminal.

    This script is the on-machine acceptance test for the Windows installer. It
    runs on a GitHub Windows runner (or any Windows 10/11 x64 machine) and
    proves, against the *installed* copy - not the build output directory - that

      1. the installer runs silently and places the application where asked,
      2. the installed executable starts and answers /health on loopback,
      3. /terminal and legacy /chart return 200 with honest mode labels,
      4. both UIs' static assets and the manual are inside the package,
      5. protected data routes refuse anonymous calls and startup is disarmed,
      6. an upgrade preserves operator data and the local token,
      7. the uninstaller removes program files but keeps operator data.

    Broker-safe: the test uses a throw-away data directory under %TEMP%, the
    bundled DEMO sample data and a locally generated token. No broker is
    contacted and no broker credential is read or written.

    Every phase is announced twice: as a line in the log file, and as a GitHub
    Actions notice annotation, so a failure is attributable even when the raw
    step log cannot be retrieved. The full log is written to
    dist\installer-test-log.txt, and a failure also writes
    dist\INSTALL_TEST_ERROR.txt.

    Usage (from packaging/windows):
        pwsh -File install_test.ps1 -DistDir dist
#>

[CmdletBinding()]
param(
    [string]$DistDir = 'dist',
    [string]$SetupPath = '',
    [int]$Port = 0
)

$ErrorActionPreference = 'Stop'

$script:Phase = 'start'
$script:LogPath = Join-Path $DistDir 'installer-test-log.txt'
$script:ErrorPath = Join-Path $DistDir 'INSTALL_TEST_ERROR.txt'
$script:Failures = New-Object System.Collections.Generic.List[string]

function Say {
    param([string]$Message)
    Write-Host $Message
    Add-Content -Path $script:LogPath -Value $Message -Encoding utf8
}

function Note {
    # A notice annotation is the only channel that survives when the hosted
    # runner log is not retrievable, so every phase announces itself here.
    param([string]$Message)
    Say $Message
    Write-Host ('::notice title=install-test::{0}' -f $Message)
}

function Step {
    param([string]$Phase, [string]$Message)
    $script:Phase = $Phase
    Note ('[{0}] {1}' -f $Phase, $Message)
}

function Check {
    param([bool]$Condition, [string]$Description)
    if ($Condition) {
        Say ('PASS  {0}' -f $Description)
    } else {
        Say ('FAIL  {0}' -f $Description)
        $script:Failures.Add($Description)
    }
    return $Condition
}

function Get-AppProcesses {
    # A PyInstaller onefile executable runs a child process of the same name, so
    # stopping only the process we started would leave the application, and its
    # lock on the installed executable, alive.
    return @(Get-Process -Name 'AXIOM-Chart' -ErrorAction SilentlyContinue)
}

function Stop-App {
    $running = Get-AppProcesses
    foreach ($proc in $running) {
        try {
            $proc.Kill()
            $proc.WaitForExit(10000) | Out-Null
        } catch {
            Say ('  could not stop pid {0}: {1}' -f $proc.Id, $_.Exception.Message)
        }
    }
    for ($i = 0; $i -lt 80; $i++) {
        if (@(Get-AppProcesses).Count -eq 0) { break }
        Start-Sleep -Milliseconds 250
    }
    return (@(Get-AppProcesses).Count -eq 0)
}

function Get-FreePort {
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
    $listener.Start()
    $port = ([System.Net.IPEndPoint]$listener.LocalEndpoint).Port
    $listener.Stop()
    return $port
}

Set-Content -Path $script:LogPath -Value ('AXIOM installer test  ' + (Get-Date -Format o)) -Encoding utf8
Remove-Item -Path $script:ErrorPath -ErrorAction SilentlyContinue

$app = $null
$installedExe = $null
$installDir = $null
$dataDir = $null

try {
    # ---------------------------------------------------------------- phase 1
    Step 'install' 'locating the installer'
    if (-not (Test-Path $DistDir)) { throw "the dist directory '$DistDir' does not exist" }
    if ($SetupPath) {
        $setupItem = Get-Item $SetupPath
    } else {
        $setupItem = Get-ChildItem -Path $DistDir -Filter '*-no-temp-setup.exe' | Select-Object -First 1
    }
    if (-not $setupItem) { throw "no *-no-temp-setup.exe was found in $DistDir" }
    $setup = $setupItem.FullName
    Say ('installer: {0} ({1:N1} MB)' -f $setup, ($setupItem.Length / 1MB))
    Check ($setupItem.Name -match '-no-temp-setup\.exe$') 'acceptance uses the loaderless no-TEMP installer'
    foreach ($index in @(0, 1)) {
        $sidecar = Join-Path $setupItem.DirectoryName ($setupItem.BaseName + "-$index.bin")
        Check (Test-Path $sidecar) "required installer sidecar exists: $($setupItem.BaseName)-$index.bin"
        if (-not (Test-Path $sidecar)) { throw "refusing to run an incomplete loaderless installer; missing $sidecar" }
    }
    Check (Test-Path (Join-Path $DistDir 'AXIOM-Chart.exe')) 'legacy onefile remains available but is not the installed runtime'

    $installDir = Join-Path $env:LOCALAPPDATA 'Programs\AXIOM-Chart'
    $installedExe = Join-Path $installDir 'AXIOM-Chart.exe'
    $installLog = Join-Path $env:TEMP 'axiom-install.log'
    Say ('install dir: {0}' -f $installDir)
    Say ('install log: {0}' -f $installLog)
    if (Test-Path $installDir) {
        Say 'a previous install directory is present; leaving it to the installer to upgrade'
    }

    # ---------------------------------------------------------------- phase 2
    Step 'install' 'running the installer silently'
    $setupArgs = @(
        '/VERYSILENT'
        '/SUPPRESSMSGBOXES'
        '/NORESTART'
        ('/DIR="{0}"' -f $installDir)
        ('/LOG="{0}"' -f $installLog)
    )
    Say ('arguments: {0}' -f ($setupArgs -join ' '))
    $setupProc = Start-Process -FilePath $setup -ArgumentList $setupArgs -PassThru -Wait
    Say ('installer exit code: {0}' -f $setupProc.ExitCode)
    if ($setupProc.ExitCode -ne 0) {
        if (Test-Path $installLog) {
            Say '--- installer log (tail)'
            Get-Content -Path $installLog -Tail 80 | ForEach-Object { Say ('  inno| {0}' -f $_) }
        } else {
            Say 'the installer produced no log file'
        }
        throw ('the installer exited with code {0}' -f $setupProc.ExitCode)
    }
    Check ($setupProc.ExitCode -eq 0) 'silent install reported success'

    # ---------------------------------------------------------------- phase 3
    Step 'installed files' 'verifying what the installer placed on disk'
    for ($i = 0; $i -lt 120 -and -not (Test-Path $installedExe); $i++) { Start-Sleep -Milliseconds 500 }
    if (-not (Test-Path $installedExe)) {
        if (Test-Path $installDir) {
            Say ('contents of {0}:' -f $installDir)
            Get-ChildItem -Path $installDir | ForEach-Object { Say ('  found| {0}' -f $_.Name) }
        } else {
            Say ('{0} does not exist' -f $installDir)
        }
        Get-ChildItem -Path $env:LOCALAPPDATA -Recurse -Depth 3 -Filter 'AXIOM-Chart.exe' -ErrorAction SilentlyContinue |
            Select-Object -First 3 | ForEach-Object { Say ('  elsewhere| {0}' -f $_.FullName) }
        throw ('AXIOM-Chart.exe was not installed into {0}' -f $installDir)
    }
    if (Test-Path $installLog) {
        Get-Content -Path $installLog -Tail 12 | ForEach-Object { Say ('  inno| {0}' -f $_) }
    }
    Check (Test-Path $installedExe) ('the installer placed AXIOM-Chart.exe in {0}' -f $installDir)
    Check (Test-Path (Join-Path $installDir 'WINDOWS_INSTALL_MANUAL.md')) 'the user manual was installed next to the executable'
    Check (Test-Path (Join-Path $installDir 'SOURCE_COMMIT.txt')) 'the source commit record was installed'
    Check (Test-Path (Join-Path $installDir '_internal')) 'loaderless installer deployed the onedir _internal runtime beside the executable'
    foreach ($doc in @('docs\LISTED_ASSETS.md', 'docs\TERMINAL_PERFORMANCE.md', 'docs\TERMINAL_BENCHMARK.json', 'docs\TERMINAL_UI_BENCHMARK.json', 'docs\WINDOWS_APP_CONTROL.md')) {
        Check (Test-Path (Join-Path $installDir $doc)) ('installer included ' + $doc)
    }
    $uninstaller = Join-Path $installDir 'unins000.exe'
    Check (Test-Path $uninstaller) 'the uninstaller was installed'

    # ---------------------------------------------------------------- phase 4
    Step 'launch' 'starting the installed copy and waiting for /health'
    $dataDir = Join-Path $env:TEMP ('axiom-installed-' + [guid]::NewGuid().ToString('N'))
    if ($Port -le 0) { $Port = Get-FreePort }
    $base = 'http://127.0.0.1:{0}' -f $Port
    Say ('data dir: {0}' -f $dataDir)
    Say ('port:     {0}' -f $Port)
    $env:AXIOM_DATA_DIR = $dataDir
    $env:AXIOM_PORT = ('{0}' -f $Port)
    $env:MODE = 'PAPER'
    $env:LIVE_TRADING = 'false'
    $env:BROKER = 'LOCAL_PAPER'

    $outLog = Join-Path $env:TEMP 'axiom-installed-stdout.log'
    $errLog = Join-Path $env:TEMP 'axiom-installed-stderr.log'
    Remove-Item -Path $outLog, $errLog -ErrorAction SilentlyContinue
    $app = Start-Process -FilePath $installedExe -ArgumentList '--no-browser' -PassThru -RedirectStandardOutput $outLog -RedirectStandardError $errLog

    $healthy = $false
    for ($i = 0; $i -lt 240; $i++) {
        Start-Sleep -Milliseconds 500
        if ($app.HasExited) { break }
        try {
            $null = Invoke-WebRequest -UseBasicParsing ('{0}/health' -f $base) -TimeoutSec 2
            $healthy = $true
            break
        } catch { }
    }
    if (-not $healthy) {
        if ($app.HasExited) { Say ('the application exited with code {0}' -f $app.ExitCode) }
        else { Say 'the application is still running but never answered' }
        foreach ($f in @($outLog, $errLog)) {
            if (Test-Path $f) {
                Say ('--- {0}' -f $f)
                Get-Content -Path $f -Tail 60 | ForEach-Object { Say ('  app| {0}' -f $_) }
            }
        }
        if (-not $app.HasExited) { $app.Kill() }
        throw ('the installed application never answered {0}/health' -f $base)
    }
    Check $healthy 'the installed copy answered /health on loopback'

    # ---------------------------------------------------------------- phase 5
    Step 'operator-and-chart' 'checking installed pages, assets and token gates'
    $terminal = Invoke-WebRequest -UseBasicParsing ('{0}/terminal' -f $base) -TimeoutSec 20
    Check ($terminal.StatusCode -eq 200) ('/terminal returns 200 (got {0})' -f $terminal.StatusCode)
    Check ($terminal.Content -match 'LOCAL PAPER' -and $terminal.Content -match 'VANTAGE LIVE') '/terminal names all explicit execution modes'
    Check ($terminal.Content -match 'execution-boundary' -and $terminal.Content -match 'watchlist-only' -and $terminal.Content -match 'drawing-tool' -and $terminal.Content -match 'performance-summary') 'installed terminal includes boundary, watchlist, drawing and diagnostics controls'
    $terminalJs = Invoke-WebRequest -UseBasicParsing ('{0}/static/terminal/terminal.js' -f $base) -TimeoutSec 20
    Check ($terminalJs.StatusCode -eq 200 -and $terminalJs.Content -match 'submitVantageReview' -and $terminalJs.Content -match 'bindChartCanvas' -and $terminalJs.Content -match 'renderTicketEstimate') 'operator JavaScript and chart/ticket interactions are served from the installed package'
    $terminalCss = Invoke-WebRequest -UseBasicParsing ('{0}/static/terminal/terminal.css' -f $base) -TimeoutSec 20
    Check ($terminalCss.StatusCode -eq 200) 'operator CSS is served from the installed package'
    $manual = Invoke-WebRequest -UseBasicParsing ('{0}/manual' -f $base) -TimeoutSec 20
    Check ($manual.StatusCode -eq 200 -and $manual.Content -match 'Vantage' -and $manual.Content -match 'Activate') 'installed manual covers Vantage and Activate'

    $chart = Invoke-WebRequest -UseBasicParsing ('{0}/chart' -f $base) -TimeoutSec 20
    Check ($chart.StatusCode -eq 200) ('/chart returns 200 (got {0})' -f $chart.StatusCode)
    Check ($chart.Content -match 'PAPER') '/chart displays the PAPER badge'
    Check ($chart.Content -notmatch '(?i)live trading enabled|REAL MONEY') '/chart makes no live-money claim'

    $js = Invoke-WebRequest -UseBasicParsing ('{0}/static/chart/chart.js' -f $base) -TimeoutSec 20
    Check ($js.StatusCode -eq 200) 'chart.js is served from the installed package'
    Check ($js.Content -match 'markerPoint') 'the served chart.js is the real renderer'
    $css = Invoke-WebRequest -UseBasicParsing ('{0}/static/chart/chart.css' -f $base) -TimeoutSec 20
    Check ($css.StatusCode -eq 200) 'chart.css is served from the installed package'

    $anon = -1
    try {
        $null = Invoke-WebRequest -UseBasicParsing ('{0}/chart/data?symbol=DEMO&timeframe=1D' -f $base) -TimeoutSec 10
        $anon = 200
    } catch {
        if ($_.Exception.Response) { $anon = [int]$_.Exception.Response.StatusCode }
    }
    Check ($anon -eq 401) ('a chart data route still refuses to answer without a token (got {0})' -f $anon)
    $anonTrading = -1
    try {
        $null = Invoke-WebRequest -UseBasicParsing ('{0}/api/trading/status' -f $base) -TimeoutSec 10
        $anonTrading = 200
    } catch {
        if ($_.Exception.Response) { $anonTrading = [int]$_.Exception.Response.StatusCode }
    }
    Check ($anonTrading -eq 401) ('Vantage status refuses anonymous access (got {0})' -f $anonTrading)
    $anonMarketData = -1
    try {
        $null = Invoke-WebRequest -UseBasicParsing ('{0}/api/market-data/status' -f $base) -TimeoutSec 10
        $anonMarketData = 200
    } catch {
        if ($_.Exception.Response) { $anonMarketData = [int]$_.Exception.Response.StatusCode }
    }
    Check ($anonMarketData -eq 401) ('independent market-data status refuses anonymous access (got {0})' -f $anonMarketData)
    $anonPerformance = -1
    try {
        $null = Invoke-WebRequest -UseBasicParsing ('{0}/api/trading/performance' -f $base) -TimeoutSec 10
        $anonPerformance = 200
    } catch {
        if ($_.Exception.Response) { $anonPerformance = [int]$_.Exception.Response.StatusCode }
    }
    Check ($anonPerformance -eq 401) ('performance diagnostics refuse anonymous access (got {0})' -f $anonPerformance)
    $anonListed = -1
    try {
        $null = Invoke-WebRequest -UseBasicParsing ('{0}/api/listed-assets/status' -f $base) -TimeoutSec 10
        $anonListed = 200
    } catch {
        if ($_.Exception.Response) { $anonListed = [int]$_.Exception.Response.StatusCode }
    }
    Check ($anonListed -eq 401) ('listed-asset status refuses anonymous access (got {0})' -f $anonListed)

    $privateConfig = Join-Path $dataDir 'axiom.env'
    Check (Test-Path $privateConfig) 'the launcher created the private per-user configuration'
    $configText = Get-Content $privateConfig -Raw
    $token = (($configText -split "`n" | Where-Object { $_ -match '^API_TOKEN=' } | Select-Object -First 1) -replace '^API_TOKEN=', '').Trim()
    Check ([bool]$token) 'a local API token was generated (value not logged)'
    $headers = @{ Authorization = "Bearer $token" }
    $trading = Invoke-RestMethod -Headers $headers ('{0}/api/trading/status' -f $base) -TimeoutSec 15
    Check ($trading.mode -eq 'AXIOM_PAPER' -and -not $trading.armed -and -not $trading.active) 'installed app starts in PAPER and disarmed'
    $performance = Invoke-RestMethod -Headers $headers ('{0}/api/trading/performance' -f $base) -TimeoutSec 15
    Check ($performance.sample_capacity_per_metric -eq 512 -and $performance.note -match 'zero-latency') 'installed diagnostics are bounded and disclaim zero latency'
    $listed = Invoke-RestMethod -Headers $headers ('{0}/api/listed-assets/status' -f $base) -TimeoutSec 15
    Check (-not $listed.execution_supported -and -not $listed.mt5_routing_allowed -and -not $listed.cfd_substitution_allowed) 'installed listed execution and MT5/CFD substitution are disabled'
    $capabilities = Invoke-RestMethod -Headers $headers ('{0}/api/trading/connection' -f $base) -TimeoutSec 15
    Check ($capabilities.secret_store -match 'Windows DPAPI') 'installed app can use Windows DPAPI'
    Check ($capabilities.mt5_package_available -eq $true) 'official MT5 module is present in installed app'
    $marketData = Invoke-RestMethod -Headers $headers ('{0}/api/market-data/status' -f $base) -TimeoutSec 15
    Check ($marketData.execution_mode -eq 'AXIOM_PAPER' -and $marketData.read_only -eq $true -and -not $marketData.connected -and -not $marketData.orders_modified -and -not $marketData.live_trading) 'installed independent MT5 feed starts disconnected, read-only and paper-only'
    $marketConnect = -1
    try {
        $null = Invoke-WebRequest -UseBasicParsing -Method Post -Headers $headers ('{0}/api/market-data/connect' -f $base) -TimeoutSec 10
        $marketConnect = 200
    } catch {
        if ($_.Exception.Response) { $marketConnect = [int]$_.Exception.Response.StatusCode }
    }
    Check ($marketConnect -eq 409) ('installed market-data connect without DEMO credentials fails closed (got {0})' -f $marketConnect)
    $afterMarketRefusal = Invoke-RestMethod -Headers $headers ('{0}/api/trading/status' -f $base) -TimeoutSec 15
    Check ($afterMarketRefusal.mode -eq 'AXIOM_PAPER' -and -not $afterMarketRefusal.active) 'installed feed refusal preserves LOCAL PAPER and never activates'
    $demo = Invoke-RestMethod -Method Post -Headers $headers -ContentType 'application/json' -Body (@{ mode = 'VANTAGE_DEMO' } | ConvertTo-Json) ('{0}/api/trading/session' -f $base) -TimeoutSec 15
    Check ($demo.mode -eq 'VANTAGE_DEMO' -and -not $demo.connected -and -not $demo.armed -and -not $demo.active) 'DEMO selection does not connect, Arm or Activate'
    $disconnected = Invoke-RestMethod -Headers $headers ('{0}/api/trading/workspace' -f $base) -TimeoutSec 15
    Check ($disconnected.broker_state_available -eq $false -and $null -eq $disconnected.reconciled_at) 'disconnected workspace is not presented as a flat account'
    $null = Invoke-RestMethod -Method Post -Headers $headers -ContentType 'application/json' -Body (@{ mode = 'AXIOM_PAPER' } | ConvertTo-Json) ('{0}/api/trading/session' -f $base) -TimeoutSec 15
    $launcherText = @((Get-Content $outLog -Raw -ErrorAction SilentlyContinue), (Get-Content $errLog -Raw -ErrorAction SilentlyContinue)) -join "`n"
    Check (-not ($token -and $launcherText.Contains($token))) 'installed launcher does not print the local API token'

    $envFile = Join-Path $dataDir 'config.json'
    if (Test-Path $envFile) {
        $cfgText = Get-Content -Path $envFile -Raw
        Check ($cfgText -notmatch '(?i)"(live_trading)"\s*:\s*true') 'the runtime config does not enable live trading'
        Check ($cfgText -notmatch '(?i)(api_secret|access_token|secret_key)') 'the runtime config holds no broker credential'
    } else {
        Say 'no config.json was written; nothing to inspect for live credentials'
    }

    # ------------------------------------------------------------- phase 5b
    Step 'upgrade' 'reinstalling over the installed copy and verifying user-data preservation'
    $stoppedForUpgrade = Stop-App
    Check $stoppedForUpgrade 'application stopped before upgrade'
    $sentinel = Join-Path $dataDir 'upgrade-preservation-sentinel.txt'
    Set-Content -Path $sentinel -Value 'keep this benign operator-data sentinel' -Encoding utf8
    $configBeforeUpgrade = Get-Content $privateConfig -Raw
    $databasePath = Join-Path $dataDir 'axiom-paper.db'
    Check (Test-Path $databasePath) 'operator SQLite database exists before upgrade'
    $upgradeProc = Start-Process -FilePath $setup -ArgumentList $setupArgs -PassThru -Wait
    Check ($upgradeProc.ExitCode -eq 0) ('silent upgrade installer reported success (exit {0})' -f $upgradeProc.ExitCode)
    Check (Test-Path $installedExe) 'upgraded executable is present'
    Check ((Get-Content $privateConfig -Raw) -eq $configBeforeUpgrade) 'upgrade preserved the complete private configuration and local token'
    Check ((Get-Content $sentinel -Raw) -match 'keep this benign') 'upgrade preserved the operator-data sentinel'
    Check (Test-Path $databasePath) 'upgrade preserved the operator SQLite database'
    Remove-Item -Path $outLog, $errLog -ErrorAction SilentlyContinue
    $app = Start-Process -FilePath $installedExe -ArgumentList '--no-browser' -PassThru -RedirectStandardOutput $outLog -RedirectStandardError $errLog
    $upgradeHealthy = $false
    for ($i = 0; $i -lt 240; $i++) {
        Start-Sleep -Milliseconds 500
        if ($app.HasExited) { break }
        try {
            $null = Invoke-WebRequest -UseBasicParsing ('{0}/health' -f $base) -TimeoutSec 2
            $upgradeHealthy = $true
            break
        } catch { }
    }
    Check $upgradeHealthy 'upgraded installed copy launches and answers /health'
    if (-not $upgradeHealthy) { throw 'the upgraded installed copy did not launch' }
    $upgradedTerminal = Invoke-WebRequest -UseBasicParsing ('{0}/terminal' -f $base) -TimeoutSec 20
    Check ($upgradedTerminal.StatusCode -eq 200) 'upgraded installed copy serves the operator console'

    # ---------------------------------------------------------------- phase 6
    Step 'uninstall' 'stopping the application and removing it'
    Say ('AXIOM-Chart processes before shutdown: {0}' -f (@(Get-AppProcesses).Count))
    $stopped = Stop-App
    Check $stopped 'every AXIOM-Chart process stopped before uninstalling'
    Start-Sleep -Seconds 3
    if (-not (Test-Path $uninstaller)) { throw 'the uninstaller disappeared before it could be run' }
    $uninstallLog = Join-Path $env:TEMP 'axiom-uninstall.log'
    $unArgs = @(
        '/VERYSILENT'
        '/SUPPRESSMSGBOXES'
        '/NORESTART'
        ('/LOG="{0}"' -f $uninstallLog)
    )
    $un = Start-Process -FilePath $uninstaller -ArgumentList $unArgs -PassThru -Wait
    Say ('uninstaller exit code: {0}' -f $un.ExitCode)
    Check ($un.ExitCode -eq 0) 'the uninstaller reported success'
    for ($i = 0; $i -lt 120 -and (Test-Path $installedExe); $i++) { Start-Sleep -Milliseconds 500 }
    if (Test-Path $installedExe) {
        Get-ChildItem -Path $installDir | ForEach-Object { Say ('  left| {0}' -f $_.Name) }
        Say ('AXIOM-Chart processes after uninstall: {0}' -f (@(Get-AppProcesses).Count))
        if (Test-Path $uninstallLog) {
            Say '--- uninstaller log (tail)'
            Get-Content -Path $uninstallLog -Tail 80 | ForEach-Object { Say ('  unins| {0}' -f $_) }
        } else {
            Say 'the uninstaller produced no log file'
        }
    }
    Check (-not (Test-Path $installedExe)) 'the uninstaller removed AXIOM-Chart.exe'
    Check (Test-Path $dataDir) ('the uninstaller kept the operator data folder {0}' -f $dataDir)
    Check (Test-Path $privateConfig) 'silent uninstall preserved the private configuration'
    Check (Test-Path $databasePath) 'silent uninstall preserved the operator SQLite database'
    Check (Test-Path $sentinel) 'silent uninstall preserved the upgrade sentinel'

    # ------------------------------------------------------------------ result
    Say ''
    if ($script:Failures.Count -gt 0) {
        throw ('{0} check(s) failed: {1}' -f $script:Failures.Count, ($script:Failures -join '; '))
    }
    Note 'PASS - install, operator/chart launch, token gates, safe Vantage default, upgrade preservation and uninstall verified on this machine'
    exit 0
}
catch {
    $message = ($_ | Out-String).Trim()
    $stack = $_.ScriptStackTrace
    Say ''
    Say ('FAILED in phase [{0}]' -f $script:Phase)
    Say $message
    if ($stack) { Say ('stack: {0}' -f $stack) }
    if ($app -and -not $app.HasExited) { $app.Kill() }
    if (Test-Path $script:LogPath) {
        Copy-Item -Path $script:LogPath -Destination (Join-Path $DistDir 'installer-test-log.txt') -Force -ErrorAction SilentlyContinue
    }
    Set-Content -Path $script:ErrorPath -Encoding utf8 -Value (@(
        ('phase: {0}' -f $script:Phase)
        $message
        ('stack: {0}' -f $stack)
    ) -join "`n")
    # Annotations are capped and single-line, so send the phase and the first
    # line of the message; the full text is in dist\INSTALL_TEST_ERROR.txt.
    $firstLine = ($_.Exception.Message -split "`n" | Where-Object { $_.Trim() } | Select-Object -First 1)
    if (-not $firstLine) { $firstLine = ($message -split "`n" | Where-Object { $_.Trim() } | Select-Object -First 1) }
    Write-Host ('::error title=install-test failed in phase {0}::{1}' -f $script:Phase, $firstLine)
    if ($stack) {
        $stackLine = ($stack -split "`n" | Where-Object { $_.Trim() } | Select-Object -First 1)
        Write-Host ('::error title=install-test at::{0}' -f $stackLine)
    }
    exit 1
}
