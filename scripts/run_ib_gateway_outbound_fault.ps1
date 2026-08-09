[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$ruleName = 'IBEMS-B2-IBGateway-Outbound-Block-20260810'
$gatewayPath = 'D:\tws\ibgateway\ibgateway.exe'
$blockSeconds = 45
$ruleCreated = $false

$gatewayProcesses = @(
    Get-Process -Name 'ibgateway' -ErrorAction Stop |
        Where-Object { $_.Path -eq $gatewayPath }
)
if ($gatewayProcesses.Count -ne 1) {
    throw "Expected exactly one IB Gateway process at $gatewayPath; found $($gatewayProcesses.Count)."
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
        GatewayPid = $gatewayProcesses[0].Id
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
