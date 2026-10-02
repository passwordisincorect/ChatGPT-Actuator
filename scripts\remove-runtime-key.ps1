$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$KeyFile = Join-Path $ProjectRoot "deployment\control-plane-key.dpapi"
if (Test-Path $KeyFile) {
    Remove-Item $KeyFile -Force
    Write-Host "Removed protected runtime API key file."
} else {
    Write-Host "Protected runtime API key file is not present."
}
