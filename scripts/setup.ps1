param(
    [switch]$EnableAutostart,
    [switch]$StartTunnel,
    [switch]$RecreateVenv
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$InstallScript = Join-Path $PSScriptRoot "install.ps1"
$AutostartScript = Join-Path $PSScriptRoot "install-autostart.ps1"
$Manager = Join-Path $PSScriptRoot "tunnel-manager.ps1"
$ProfilePath = Join-Path $env:APPDATA "tunnel-client\chatgpt-actuator.yaml"
$TunnelClient = Join-Path $env:LOCALAPPDATA "OpenAI\TunnelClient\tunnel-client.exe"
$PyprojectPath = Join-Path $ProjectRoot "pyproject.toml"
$ConfigPath = Join-Path $ProjectRoot "config\config.json"

Set-Location $ProjectRoot

$Pyproject = Get-Content $PyprojectPath -Raw
$VersionMatch = [regex]::Match($Pyproject, '(?ms)^\[project\].*?^version\s*=\s*"([^"]+)"')
if (-not $VersionMatch.Success) { throw "Could not determine project version from pyproject.toml." }
$ExpectedVersion = $VersionMatch.Groups[1].Value

if (Test-Path $ConfigPath) {
    $ConfigRaw = Get-Content $ConfigPath -Raw | ConvertFrom-Json
    $Changed = $false
    if (@($ConfigRaw.filesystem.allowed_roots) -contains "__CHATGPT_ACTUATOR_ROOT__") {
        $ConfigRaw.filesystem.allowed_roots = @($ProjectRoot)
        $Changed = $true
    }
    if (@($ConfigRaw.powershell.allowed_working_roots) -contains "__CHATGPT_ACTUATOR_ROOT__") {
        $ConfigRaw.powershell.allowed_working_roots = @($ProjectRoot)
        $Changed = $true
    }
    if ($ConfigRaw.powershell.default_cwd -eq "__CHATGPT_ACTUATOR_ROOT__") {
        $ConfigRaw.powershell.default_cwd = $ProjectRoot
        $Changed = $true
    }
    if ($Changed) {
        $ConfigRaw | ConvertTo-Json -Depth 20 | Set-Content -Path $ConfigPath -Encoding UTF8
        Write-Host "Initialized release config paths for this installation."
    }
}

if ($RecreateVenv -or -not (Test-Path $VenvPython)) {
    Write-Host "Installing/recreating Python environment..."
    if ($RecreateVenv) { & $InstallScript -RecreateVenv } else { & $InstallScript }
    if ($LASTEXITCODE -ne 0) { throw "install.ps1 failed." }
}
else {
    Write-Host "Refreshing editable ChatGPT-Actuator installation..."
    & $VenvPython -m pip install -e .
    if ($LASTEXITCODE -ne 0) { throw "pip install -e . failed." }
}

$Version = (& $VenvPython -c "import importlib.metadata; print(importlib.metadata.version('chatgpt-actuator'))" | Select-Object -Last 1).Trim()
if ($Version -ne $ExpectedVersion) {
    throw "Expected ChatGPT-Actuator $ExpectedVersion, got $Version."
}

if (-not (Test-Path $TunnelClient)) {
    $Found = Get-Command tunnel-client.exe -ErrorAction SilentlyContinue
    if ($Found) { $TunnelClient = $Found.Source }
}

$TunnelClientPresent = Test-Path $TunnelClient
$ProfilePresent = Test-Path $ProfilePath

if (($EnableAutostart -or $StartTunnel) -and -not $TunnelClientPresent) {
    throw "tunnel-client.exe is required for autostart/start-tunnel but was not found."
}
if (($EnableAutostart -or $StartTunnel) -and -not $ProfilePresent) {
    throw "Tunnel profile chatgpt-actuator is not configured. Run .\scripts\configure-tunnel.ps1 first."
}

if ($EnableAutostart) { & $AutostartScript }
if ($StartTunnel) { & $Manager -Action Start }

$StateDir = Join-Path $ProjectRoot "deployment"
New-Item -ItemType Directory -Path $StateDir -Force | Out-Null
$State = [ordered]@{
    version = $Version
    project_root = $ProjectRoot
    python = $VenvPython
    tunnel_client = if ($TunnelClientPresent) { $TunnelClient } else { $null }
    tunnel_profile_present = $ProfilePresent
    autostart_requested = [bool]$EnableAutostart
    installed_at = (Get-Date).ToUniversalTime().ToString("o")
}
$State | ConvertTo-Json -Depth 4 | Set-Content -Path (Join-Path $StateDir "install-state.json") -Encoding UTF8

Write-Host ""
Write-Host "ChatGPT-Actuator $Version setup complete."
Write-Host "Project:    $ProjectRoot"
Write-Host "Python:     $VenvPython"
Write-Host "Tunnel:     $TunnelClientPresent"
Write-Host "Profile:    $ProfilePresent"
Write-Host "Admin UI:   http://127.0.0.1:8765/"
Write-Host "Status:     .\scripts\deployment-status.ps1"
Write-Host "Autostart:  .\scripts\autostart-status.ps1"
