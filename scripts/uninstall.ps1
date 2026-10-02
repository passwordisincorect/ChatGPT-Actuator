param(
    [switch]$StopTunnel,
    [switch]$RemoveAutostart = $true
)

$ErrorActionPreference = "Stop"
$Manager = Join-Path $PSScriptRoot "tunnel-manager.ps1"
$RemoveTask = Join-Path $PSScriptRoot "remove-autostart.ps1"

if ($StopTunnel) { & $Manager -Action Stop }
if ($RemoveAutostart) { & $RemoveTask }

Write-Host ""
Write-Host "ChatGPT-Actuator deployment integration removed."
Write-Host "Project files, config, logs, tunnel profile, and protected credentials were left untouched."
Write-Host "Use remove-runtime-key.ps1 separately if you also want to delete the DPAPI-protected runtime key."
