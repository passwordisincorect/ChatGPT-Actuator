param([switch]$Json)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$TunnelManager = Join-Path $PSScriptRoot "tunnel-manager.ps1"
$AutostartStatus = Join-Path $PSScriptRoot "autostart-status.ps1"
$ProfilePath = Join-Path $env:APPDATA "tunnel-client\chatgpt-actuator.yaml"

$Version = $null
if (Test-Path $Python) {
    try { $Version = (& $Python -c "import importlib.metadata; print(importlib.metadata.version('chatgpt-actuator'))" | Select-Object -Last 1).Trim() } catch {}
}

$TunnelRaw = & $TunnelManager -Action Status -Json
$AutoRaw = & $AutostartStatus -Json
$Tunnel = $TunnelRaw | ConvertFrom-Json
$Autostart = $AutoRaw | ConvertFrom-Json

$Result = [pscustomobject]@{
    version = $Version
    project_root = $ProjectRoot
    python_ready = (Test-Path $Python)
    profile_present = (Test-Path $ProfilePath)
    tunnel = $Tunnel
    autostart = $Autostart
    admin_url = "http://127.0.0.1:8765/"
}

if ($Json) { $Result | ConvertTo-Json -Depth 6 -Compress } else {
    Write-Host "ChatGPT-Actuator deployment status"
    Write-Host "Version:       $($Result.version)"
    Write-Host "Project:       $($Result.project_root)"
    Write-Host "Python ready:  $($Result.python_ready)"
    Write-Host "Profile ready: $($Result.profile_present)"
    Write-Host "Tunnel running:$($Result.tunnel.running)"
    Write-Host "Tunnel health: $($Result.tunnel.health_ok)"
    Write-Host "Admin health:  $($Result.tunnel.admin_ok)"
    Write-Host "Autostart:     $($Result.autostart.installed)"
    Write-Host "Admin UI:      $($Result.admin_url)"
}
