param()

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$DeploymentDir = Join-Path $ProjectRoot "deployment"
$KeyFile = Join-Path $DeploymentDir "control-plane-key.dpapi"

$Value = $env:CONTROL_PLANE_API_KEY
if ([string]::IsNullOrWhiteSpace($Value)) {
    throw "CONTROL_PLANE_API_KEY is not set in the current process. This script never asks you to paste the key into the command line."
}

New-Item -ItemType Directory -Path $DeploymentDir -Force | Out-Null
$Secure = ConvertTo-SecureString $Value -AsPlainText -Force
$Protected = ConvertFrom-SecureString $Secure
[System.IO.File]::WriteAllText($KeyFile, $Protected, (New-Object System.Text.UTF8Encoding($false)))

Write-Host "Runtime API key was protected with Windows DPAPI for the current user."
Write-Host "Stored encrypted file: $KeyFile"
Write-Host "The plaintext key was not written to disk or printed."
