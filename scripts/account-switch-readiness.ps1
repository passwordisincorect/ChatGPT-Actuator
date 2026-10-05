$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$Workspace = Split-Path -Parent $Root
$Policy = Join-Path $Workspace "routing-policy.json"
$Deployment = Join-Path $PSScriptRoot "deployment-status.ps1"
$OrchestratorManager = Join-Path $PSScriptRoot "orchestrator-manager.ps1"

$actuatorText = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Deployment 2>&1 | Out-String
$orchestratorJson = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $OrchestratorManager -Action Status -Json 2>$null
$orchestrator = $null
try { $orchestrator = $orchestratorJson | ConvertFrom-Json } catch {}

$result = [ordered]@{
    routing_policy_present = (Test-Path $Policy)
    routing_policy_path = $Policy
    actuator_local_ready = ($actuatorText -match "Tunnel health:\s*True" -and $actuatorText -match "Admin health:\s*True")
    orchestrator_local_ready = [bool]($orchestrator -and $orchestrator.running -and $orchestrator.health_ok)
    account_action_required = $true
    account_action = "On the other ChatGPT account/workspace, enable/connect ChatGPT-Actuator and ChatGPT-Orchestrator and refresh/scan tools. Local routing policy remains on this PC."
}

$result | ConvertTo-Json -Depth 4
