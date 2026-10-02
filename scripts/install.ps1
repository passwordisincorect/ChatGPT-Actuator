param(
    [switch]$RecreateVenv
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

$Python312 = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"
if (-not (Test-Path $Python312)) {
    $Launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if (-not $Launcher) { throw "Python 3.12 was not found. Install Python 3.12 (64-bit) first." }
    $Resolved = & $Launcher.Source -3.12 -c "import sys; print(sys.executable)"
    if ($LASTEXITCODE -ne 0 -or -not $Resolved) { throw "Python 3.12 is installed but could not be started." }
    $Python312 = ($Resolved | Select-Object -Last 1).Trim()
}

Write-Host "Using Python: $Python312"
& $Python312 --version
if ($LASTEXITCODE -ne 0) { throw "Python 3.12 failed to start." }

$Venv = Join-Path $ProjectRoot ".venv"
if ($RecreateVenv -and (Test-Path $Venv)) {
    Write-Host "Removing existing virtual environment..."
    Remove-Item $Venv -Recurse -Force
}

if (-not (Test-Path $Venv)) {
    Write-Host "Creating virtual environment with Python 3.12..."
    & $Python312 -m venv $Venv
    if ($LASTEXITCODE -ne 0) { throw "Failed to create virtual environment." }
}
else {
    Write-Host "Reusing existing virtual environment."
}

$VenvPython = Join-Path $Venv "Scripts\python.exe"
Write-Host "Ensuring build tooling..."
& $VenvPython -m pip install "setuptools>=77" wheel
if ($LASTEXITCODE -ne 0) { throw "Failed to install build tooling." }

Write-Host "Installing/updating ChatGPT-Actuator dependencies..."
& $VenvPython -m pip install -e .
if ($LASTEXITCODE -ne 0) { throw "Failed to install project dependencies." }

$Version = (& $VenvPython -c "import importlib.metadata; print(importlib.metadata.version('chatgpt-actuator'))" | Select-Object -Last 1).Trim()
Write-Host ""
Write-Host "ChatGPT-Actuator $Version installed successfully."
Write-Host "Python:      $VenvPython"
Write-Host "Run tests:   .\scripts\test.ps1"
Write-Host "Tunnel:      .\scripts\start-tunnel.ps1"
Write-Host "Deployment:  .\scripts\deployment-status.ps1"
