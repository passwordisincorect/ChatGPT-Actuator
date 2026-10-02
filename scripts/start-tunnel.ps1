param(
    [string]$RuntimeApiKey = ""
)

$ErrorActionPreference = "Stop"

$TunnelClient = Join-Path $env:LOCALAPPDATA "OpenAI\TunnelClient\tunnel-client.exe"
$ProfileName = "chatgpt-actuator"

if (-not (Test-Path $TunnelClient)) {
    $Found = Get-Command tunnel-client.exe -ErrorAction SilentlyContinue
    if (-not $Found) {
        throw "tunnel-client.exe was not found."
    }
    $TunnelClient = $Found.Source
}

if ($RuntimeApiKey) {
    $env:CONTROL_PLANE_API_KEY = $RuntimeApiKey
}

Write-Host "Starting Secure MCP Tunnel for ChatGPT-Actuator..."
Write-Host "Profile: $ProfileName"
Write-Host "Admin UI: http://127.0.0.1:8765/"
Write-Host "Open Admin: .\scripts\open-admin.ps1"
Write-Host ""

& $TunnelClient run --profile $ProfileName
