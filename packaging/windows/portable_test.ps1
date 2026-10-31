<#
.SYNOPSIS
    Extract and smoke-test the onedir portable ZIP on Windows.

.DESCRIPTION
    Verifies the complete package, safe LOCAL PAPER startup, operator assets,
    authentication and the structural no-_MEI-TEMP-extraction workaround.
    No broker account, credential, DEMO login or LIVE action is used.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$PackagePath,
    [Parameter(Mandatory = $true)][string]$ExpectedCommit,
    [int]$Port = 0
)

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
. (Join-Path $PSScriptRoot 'PaperSmoke.ps1')
$Package = (Resolve-Path $PackagePath).Path
$DistDir = Split-Path $Package -Parent
$LogPath = Join-Path $DistDir 'portable-test-log.txt'
$ErrorPath = Join-Path $DistDir 'PORTABLE_TEST_ERROR.txt'
$script:Failures = New-Object System.Collections.Generic.List[string]
$script:ExitCode = 0
$script:App = $null
$script:Exe = $null

function Say([string]$Message) {
    Write-Host $Message
    Add-Content -Path $LogPath -Value $Message -Encoding utf8
}
function Check([bool]$Condition, [string]$Description) {
    if ($Condition) { Say "PASS  $Description" }
    else { Say "FAIL  $Description"; $script:Failures.Add($Description) }
}
function Stop-Portable {
    if (-not $script:Exe) { return }
    Get-Process -Name 'AXIOM-Chart' -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -eq $script:Exe } |
        ForEach-Object {
            try { $_.Kill(); $_.WaitForExit(10000) | Out-Null } catch { }
        }
}
function Get-FreePort {
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
    $listener.Start()
    $port = ([System.Net.IPEndPoint]$listener.LocalEndpoint).Port
    $listener.Stop()
    return $port
}

Set-Content -Path $LogPath -Value ('AXIOM onedir portable test  ' + (Get-Date -Format o)) -Encoding utf8
Remove-Item $ErrorPath -ErrorAction SilentlyContinue
$Root = Join-Path $env:TEMP ('axiom-portable-' + [guid]::NewGuid().ToString('N'))
$DataDir = Join-Path $env:TEMP ('axiom-portable-data-' + [guid]::NewGuid().ToString('N'))

try {
    New-Item -ItemType Directory -Force -Path $Root, $DataDir | Out-Null
    Expand-Archive -Path $Package -DestinationPath $Root -Force
    $AppRoot = Join-Path $Root 'AXIOM-Operator'
    $script:Exe = Join-Path $AppRoot 'AXIOM-Chart.exe'
    Check (Test-Path $script:Exe) 'portable ZIP contains AXIOM-Chart.exe in its application folder'
    Check (Test-Path (Join-Path $AppRoot '_internal')) 'portable ZIP contains the onedir _internal dependencies'
    Check (Test-Path (Join-Path $AppRoot 'README-FIRST.txt')) 'portable ZIP contains extraction and policy instructions'
    Check (Test-Path (Join-Path $AppRoot 'docs\WINDOWS_APP_CONTROL.md')) 'portable ZIP contains the App Control guide'
    $CommitFile = Join-Path $AppRoot 'SOURCE_COMMIT.txt'
    Check (Test-Path $CommitFile) 'portable ZIP contains its exact source record'
    if (Test-Path $CommitFile) {
        Check ((Get-Content $CommitFile -Raw) -match [regex]::Escape($ExpectedCommit)) 'portable source record matches the requested commit'
    }
    $PortableBlockers = Test-AxiomPaperBlockerSmoke $script:Exe
    Check $PortableBlockers 'portable frozen PAPER blocker fixes: candles, null event, persistence and market close'
    if (-not $PortableBlockers) { throw 'portable frozen PAPER blocker diagnostics failed' }

    $TempBefore = @(Get-ChildItem $env:TEMP -Directory -Filter '_MEI*' -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName })
    if ($Port -le 0) { $Port = Get-FreePort }
    $Base = "http://127.0.0.1:$Port"
    $env:AXIOM_DATA_DIR = $DataDir
    $env:AXIOM_PORT = "$Port"
    $env:MODE = 'PAPER'
    $env:LIVE_TRADING = 'false'
    $env:BROKER = 'LOCAL_PAPER'
    $OutLog = Join-Path $Root 'portable-stdout.log'
    $ErrLog = Join-Path $Root 'portable-stderr.log'
    $script:App = Start-Process -FilePath $script:Exe -ArgumentList '--no-browser' -PassThru -WindowStyle Hidden -RedirectStandardOutput $OutLog -RedirectStandardError $ErrLog
    $Healthy = $false
    for ($i = 0; $i -lt 240; $i++) {
        Start-Sleep -Milliseconds 500
        if ($script:App.HasExited) { break }
        try {
            $null = Invoke-WebRequest -UseBasicParsing "$Base/health" -TimeoutSec 2
            $Healthy = $true
            break
        } catch { }
    }
    if (-not $Healthy) {
        foreach ($file in @($OutLog, $ErrLog)) {
            if (Test-Path $file) { Get-Content $file -Tail 60 | ForEach-Object { Say "app| $_" } }
        }
        throw "onedir portable application never answered $Base/health"
    }
    Check $Healthy 'extracted onedir portable answered /health on loopback'
    $TempAfter = @(Get-ChildItem $env:TEMP -Directory -Filter '_MEI*' -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName })
    $NewTemp = @($TempAfter | Where-Object { $TempBefore -notcontains $_ })
    Check ($NewTemp.Count -eq 0) 'onedir runtime created no new PyInstaller _MEI TEMP extraction directory'
    if ($NewTemp.Count) { Say ('unexpected TEMP directories: ' + ($NewTemp -join ', ')) }

    $Terminal = Invoke-WebRequest -UseBasicParsing "$Base/terminal" -TimeoutSec 20
    Check ($Terminal.StatusCode -eq 200 -and $Terminal.Content -match 'LOCAL PAPER' -and $Terminal.Content -match 'Upstox' -and $Terminal.Content -match 'Alpaca' -and $Terminal.Content -notmatch 'VANTAGE LIVE') 'portable serves LOCAL PAPER, Upstox (NSE), and Alpaca (NASDAQ) without Vantage UI'
    Check ($Terminal.Content -match 'execution-boundary' -and $Terminal.Content -match 'drawing-tool' -and $Terminal.Content -match 'performance-summary') 'portable serves the redesigned terminal controls'
    $Manual = Invoke-WebRequest -UseBasicParsing "$Base/manual" -TimeoutSec 20
    Check ($Manual.StatusCode -eq 200 -and $Manual.Content -match 'Upstox' -and $Manual.Content -match 'Alpaca') 'portable serves the bundled operator manual'
    $Autonomy = Invoke-WebRequest -UseBasicParsing "$Base/autonomy" -TimeoutSec 20
    Check ($Autonomy.StatusCode -eq 200 -and $Autonomy.Content -match 'UPSTOX_LIVE') 'portable serves the daily autonomy boundary'
    $AutonomyManual = Invoke-WebRequest -UseBasicParsing "$Base/autonomy/manual" -TimeoutSec 20
    Check ($AutonomyManual.StatusCode -eq 200 -and $AutonomyManual.Content -match 'not a live-readiness certificate') 'portable serves autonomy limitations'
    $Anonymous = -1
    try {
        $null = Invoke-WebRequest -UseBasicParsing "$Base/api/trading/status" -TimeoutSec 10
        $Anonymous = 200
    } catch {
        if ($_.Exception.Response) { $Anonymous = [int]$_.Exception.Response.StatusCode }
    }
    Check ($Anonymous -eq 401) "portable trading API refuses anonymous access ($Anonymous)"

    $PrivateConfig = Join-Path $DataDir 'axiom.env'
    Check (Test-Path $PrivateConfig) 'portable created its private per-user configuration'
    $Token = ''
    if (Test-Path $PrivateConfig) {
        $Token = (((Get-Content $PrivateConfig | Select-String '^API_TOKEN=').Line) -replace '^API_TOKEN=', '').Trim()
    }
    Check ([bool]$Token) 'portable generated a local API token (value not logged)'
    if ($Token) {
        $Headers = @{ Authorization = "Bearer $Token" }
        $Status = Invoke-RestMethod -Headers $Headers "$Base/api/trading/status" -TimeoutSec 15
        Check ($Status.mode -eq 'AXIOM_PAPER' -and -not $Status.connected -and -not $Status.armed -and -not $Status.active) 'portable starts LOCAL PAPER, disconnected and disarmed'
        $Capabilities = Invoke-RestMethod -Headers $Headers "$Base/api/trading/connection" -TimeoutSec 15
        Check ($Capabilities.secret_store -match 'Windows DPAPI' -and $Capabilities.mt5_package_available) 'portable includes Windows DPAPI and the official MT5 module'
        $AutonomyStatus = Invoke-RestMethod -Headers $Headers "$Base/api/autonomy/status" -TimeoutSec 15
        Check (-not $AutonomyStatus.automatic_arm_or_activate -and $AutonomyStatus.sessions.Count -eq 0) 'portable autonomy starts without sessions or automatic approval'
    }
    Stop-Portable
    Start-Sleep -Milliseconds 500
    $Remaining = @(Get-Process -Name 'AXIOM-Chart' -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $script:Exe })
    Check ($Remaining.Count -eq 0) 'portable process stopped cleanly after the test'
    $LauncherText = @((Get-Content $OutLog -Raw -ErrorAction SilentlyContinue), (Get-Content $ErrLog -Raw -ErrorAction SilentlyContinue)) -join "`n"
    Check (-not ($Token -and $LauncherText.Contains($Token))) 'portable launcher output does not expose the local API token'
    if ($script:Failures.Count) { throw "portable acceptance failed: $($script:Failures.Count) check(s)" }
    Say 'PASS - extracted onedir portable package completed all acceptance checks without broker access'
}
catch {
    $message = $_.Exception.Message
    Set-Content -Path $ErrorPath -Value @($message, $_.InvocationInfo.PositionMessage) -Encoding utf8
    Write-Host "::error title=AXIOM portable gate::$message"
    $script:ExitCode = 1
}
finally {
    Stop-Portable
}

# Do not infer success from the last cleanup cmdlet: Get-Process returns a
# non-terminating no-match status after the portable app has already stopped.
# Explicit status makes the GitHub hard gate match the logged acceptance result.
if ($script:ExitCode -ne 0) { exit 1 }
exit 0
