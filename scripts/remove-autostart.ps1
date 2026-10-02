param(
    [string]$TaskName = "ChatGPT-Actuator Tunnel",
    [switch]$StopTunnel
)

$ErrorActionPreference = "Stop"
$Manager = Join-Path $PSScriptRoot "tunnel-manager.ps1"

$Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($Task) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed autostart task: $TaskName"
}
else {
    Write-Host "Autostart task is not installed: $TaskName"
}

if ($StopTunnel) {
    & $Manager -Action Stop
}
