param(
    [Parameter(Mandatory=$true)]
    [string]$TunnelId,

    [string]$RuntimeApiKey = ""
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$TunnelClient = Join-Path $env:LOCALAPPDATA "OpenAI\TunnelClient\tunnel-client.exe"
$ProfileName = "chatgpt-actuator"
$HealthListenAddr = "127.0.0.1:8081"

if (-not (Test-Path $Python)) {
    throw "ChatGPT-Actuator virtual environment was not found. Run .\scripts\install.ps1 first."
}

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

$PythonForTunnel = $Python -replace '\\', '/'
$McpCommand = ('"{0}" -m chatgpt_actuator.server' -f $PythonForTunnel)

Write-Host "Tunnel client: $TunnelClient"
Write-Host "Profile:       $ProfileName"
Write-Host "Tunnel ID:     $TunnelId"
Write-Host "Health:        $HealthListenAddr"
Write-Host "MCP command:   $McpCommand"
Write-Host ""

$InitArgs = @(
    "init",
    "--sample", "sample_mcp_stdio_local",
    "--profile", $ProfileName,
    "--tunnel-id", $TunnelId,
    "--mcp-command", $McpCommand,
    "--health-listen-addr", $HealthListenAddr,
    "--force"
)

& $TunnelClient @InitArgs
if ($LASTEXITCODE -ne 0) {
    throw "tunnel-client init failed."
}

Write-Host ""
Write-Host "Running tunnel doctor..."
& $TunnelClient doctor --profile $ProfileName --explain

if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "Profile was updated, but doctor reported a problem."
    exit $LASTEXITCODE
}

Write-Host ""
Write-Host "Tunnel profile configured successfully."
Write-Host "Start it with: .\scripts\start-tunnel.ps1"
