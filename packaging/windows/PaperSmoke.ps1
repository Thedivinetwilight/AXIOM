# Shared frozen-package PAPER diagnostic gate.
# The executable creates isolated temporary state and emits one sanitized JSON
# result.  This wrapper never reads owner configuration, credentials or data.
function Test-AxiomPaperBlockerSmoke {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Executable)

    $output = @(& $Executable --autonomy-blocker-smoke 2>&1)
    $exitCode = $LASTEXITCODE
    $json = $output | Where-Object { "$_" -match '^\{' } | Select-Object -Last 1
    $result = $null
    try { $result = $json | ConvertFrom-Json -ErrorAction Stop } catch { }
    if ($null -eq $result) {
        Write-Host 'frozen PAPER blocker diagnostic returned no valid JSON result'
        return $false
    }
    $required = @(
        'candle_ordering', 'null_event_status',
        'database_failure_fail_closed', 'market_close_cleanup'
    )
    $checks = @($result.checks)
    $missing = @($required | Where-Object { $checks -notcontains $_ })
    $valid = (
        $exitCode -eq 0 -and $result.status -eq 'PASS' -and
        $result.mode -eq 'PAPER' -and $result.live_enabled -eq $false -and
        $result.broker_order_endpoint_calls -eq 0 -and $missing.Count -eq 0
    )
    if (-not $valid) {
        Write-Host ("frozen PAPER blocker diagnostic failed: exit={0}; status={1}; error={2}; missing={3}" -f
            $exitCode, $result.status, $result.error_type, ($missing -join ','))
    }
    return [bool]$valid
}
