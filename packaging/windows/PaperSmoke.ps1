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

# Shared frozen-runtime import gate.
# A packaged build that lost a C extension (OpenSSL, SQLite, zlib, ...) used to
# fail deep inside a request or an installer phase with a bare ImportError. This
# runs the launcher's own diagnostic, which reads no configuration and touches
# no user data, so the failure names the missing module at build/install time.
function Test-AxiomRuntimeImports {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Executable)

    $output = @(& $Executable --runtime-check 2>&1)
    $exitCode = $LASTEXITCODE
    $json = $output | Where-Object { "$_" -match '^\{' } | Select-Object -Last 1
    $result = $null
    try { $result = $json | ConvertFrom-Json -ErrorAction Stop } catch { }
    if ($null -eq $result) {
        Write-Host 'frozen runtime import diagnostic returned no valid JSON result'
        return $false
    }
    $failures = @($result.failures)
    $valid = ($exitCode -eq 0 -and $result.status -eq 'PASS' -and $failures.Count -eq 0)
    if (-not $valid) {
        Write-Host ("frozen runtime import diagnostic failed: exit={0}; status={1}; failures={2}" -f
            $exitCode, $result.status, ($failures -join ' | '))
    }
    return [bool]$valid
}

# Shared protected-credential gate (synthetic values, isolated directory).
# The executable performs the whole DPAPI save/read/delete cycle inside a fresh
# temporary folder with a generated synthetic secret; it never opens the
# operator's real credential store and never prints the secret.
function Test-AxiomDpapiStorage {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Executable)

    $output = @(& $Executable --dpapi-selftest 2>&1)
    $exitCode = $LASTEXITCODE
    $json = $output | Where-Object { "$_" -match '^\{' } | Select-Object -Last 1
    $result = $null
    try { $result = $json | ConvertFrom-Json -ErrorAction Stop } catch { }
    if ($null -eq $result) {
        Write-Host 'DPAPI self-test returned no valid JSON result'
        return $false
    }
    $failures = @($result.failures)
    $valid = (
        $exitCode -eq 0 -and $result.status -eq 'PASS' -and
        $result.isolated_directory -eq $true -and
        $result.roundtrip_ok -eq $true -and
        $result.encrypted_at_rest -eq $true -and
        $result.delete_ok -eq $true -and
        $result.missing_key_fail_closed -eq $true -and
        $result.redaction_ok -eq $true -and
        $result.secret_echoed -eq $false -and
        $failures.Count -eq 0
    )
    if (-not $valid) {
        Write-Host ("DPAPI self-test failed: exit={0}; status={1}; failures={2}" -f
            $exitCode, $result.status, ($failures -join ' | '))
    }
    return [bool]$valid
}
