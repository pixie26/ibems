[CmdletBinding()]
param(
    [string]$PythonExe = '',
    [int]$ApiClientId = 962
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$ruleName = 'IBEMS-B2-IBGateway-Outbound-Block-20260810'
$gatewayPath = 'D:\tws\ibgateway\ibgateway.exe'
$blockSeconds = 45
$ruleCreated = $false

if (-not $PythonExe) {
    $repoPythonCandidates = @(
        (Join-Path $PSScriptRoot '..\.venv312\python.exe'),
        (Join-Path $PSScriptRoot '..\.venv312\Scripts\python.exe')
    )
    $repoPython = $repoPythonCandidates |
        Where-Object { Test-Path -LiteralPath $_ } |
        Select-Object -First 1
    if ($repoPython) {
        $PythonExe = (Resolve-Path -LiteralPath $repoPython).Path
    }
    else {
        $PythonExe = (Get-Command python -ErrorAction Stop).Source
    }
}

$detector = Join-Path $PSScriptRoot 'detect_ib_gateway.py'
$detectionJson = @(
    & $PythonExe $detector `
        --expected-path $gatewayPath `
        --port 4002 `
        --api-client-id $ApiClientId
)
$detectorExit = $LASTEXITCODE
if ($detectionJson.Count -eq 0) {
    throw "Gateway detector produced no result (exit $detectorExit)."
}
$detection = ($detectionJson -join [Environment]::NewLine) | ConvertFrom-Json
if (-not $detection.is_running) {
    throw "Gateway state is $($detection.state), not confirmed running: $($detection.diagnostics -join '; ')"
}
if ($detection.path_status -ne 'MATCH') {
    throw (
        "Gateway is running ($($detection.state)), but exact executable path is " +
        "$($detection.path_status). Refusing a program-scoped firewall rule."
    )
}
if ($detection.process_pids.Count -ne 1) {
    throw "Expected exactly one confirmed IB Gateway PID; found $($detection.process_pids.Count)."
}
if (Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue) {
    throw "Firewall rule already exists; refusing to overwrite: $ruleName"
}

try {
    $createdRule = New-NetFirewallRule `
        -DisplayName $ruleName `
        -Description 'Temporary IBEMS Gate B2 fault injection; exact Gateway executable only.' `
        -Direction Outbound `
        -Program $gatewayPath `
        -Action Block `
        -Profile Any `
        -Enabled True
    $ruleCreated = $true
    [pscustomobject]@{
        Event = 'FIREWALL_RULE_APPLIED'
        Utc = [DateTime]::UtcNow.ToString('o')
        DisplayName = $createdRule.DisplayName
        Program = $gatewayPath
        GatewayPid = $detection.process_pids[0]
        DetectionState = $detection.state
        ApiStatus = $detection.api_status
        BlockSeconds = $blockSeconds
    } | Format-List
    Start-Sleep -Seconds $blockSeconds
}
finally {
    if ($ruleCreated -or (Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue)) {
        Remove-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
    }
    $stillPresent = [bool](
        Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
    )
    [pscustomobject]@{
        Event = 'FIREWALL_RULE_CLEANUP'
        Utc = [DateTime]::UtcNow.ToString('o')
        DisplayName = $ruleName
        RuleStillPresent = $stillPresent
    } | Format-List
    if ($stillPresent) {
        throw "Firewall cleanup failed; remove manually: $ruleName"
    }
}
