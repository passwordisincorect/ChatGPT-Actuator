param(
    [string]$RuntimeApiKey = ""
)

$ErrorActionPreference = "Stop"

$TunnelClient = Join-Path $env:LOCALAPPDATA "OpenAI\TunnelClient\tunnel-client.exe"

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

& $TunnelClient doctor --profile "chatgpt-actuator" --explain
