param(
    [ValidateSet("Validate","Run","Status","EnableFunnel","OpenOwner")]
    [string]$Action = "Validate",
    [string]$Profile = "tailscale",
    [string]$ProfilesFile = ""
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
if (!$ProfilesFile) { $ProfilesFile = Join-Path $Root 'config\connection-profiles.local.json' }
$ProfilesFile = (Resolve-Path $ProfilesFile -ErrorAction Stop).Path
$All = Get-Content -Raw -Encoding UTF8 $ProfilesFile | ConvertFrom-Json
$Config = $All.profiles.PSObject.Properties[$Profile].Value
if (!$Config) { throw "Unknown profile: $Profile" }
if ($Config.transport -eq 'openai_tunnel') {
    if ($Action -eq 'EnableFunnel' -or $Action -eq 'OpenOwner') { throw "Action applies to Tailscale profiles only" }
    if ($Action -eq 'Validate') { Write-Output "OpenAI tunnel profile: $($Config.tunnel_profile)"; exit 0 }
    $TunnelAction = if ($Action -eq 'Run') { 'Start' } else { 'Status' }
    & (Join-Path $PSScriptRoot 'tunnel-manager.ps1') -Action $TunnelAction -ProfileName $Config.tunnel_profile
    exit $LASTEXITCODE
}
if ($Config.transport -ne 'tailscale_funnel') { throw "Unsupported transport" }
$Python = Join-Path $Root '.venv\Scripts\python.exe'
if (!(Test-Path $Python)) { throw "Install the project and optional tailscale dependencies first" }
$Gateway = Join-Path $Root 'integrations\tailscale_gateway\run_gateway.py'
$env:CHATGPT_ACTUATOR_PROFILE_FILE = $ProfilesFile
$env:CHATGPT_ACTUATOR_PROFILE = $Profile
# Import from the optional directory; validation occurs before any network action.
$env:PYTHONPATH = Join-Path $Root 'integrations\tailscale_gateway'
& $Python -c 'from gateway_config import load_profile; load_profile()'
if ($LASTEXITCODE -ne 0) { throw "Invalid gateway profile" }
$Port = [int]$Config.public_port
$Owner = [int]$Config.owner_port
if ($Action -eq 'Validate') { Write-Output "Validated $Profile ($($Config.issuer))"; exit 0 }
if ($Action -eq 'Status') {
    try { (Invoke-WebRequest "http://127.0.0.1:$Port/healthz" -UseBasicParsing -TimeoutSec 2).StatusCode }
    catch { Write-Output "Gateway not running on port $Port" }
    exit 0
}
if ($Action -eq 'OpenOwner') { Start-Process "http://127.0.0.1:$Owner/"; exit 0 }
if ($Action -eq 'Run') {
    & $Python $Gateway --profiles $ProfilesFile --profile $Profile
    exit $LASTEXITCODE
}
if ($Action -eq 'EnableFunnel') {
    $Health = Invoke-WebRequest "http://127.0.0.1:$Port/healthz" -UseBasicParsing -TimeoutSec 3
    if ($Health.StatusCode -ne 200) { throw "Gateway is not healthy" }
    $Tailscale = (Get-Command tailscale.exe -ErrorAction Stop).Source
    try {
        Invoke-WebRequest "http://127.0.0.1:$Port/full/mcp" -Method Post -ContentType 'application/json' -Body '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' -UseBasicParsing -TimeoutSec 3 | Out-Null
        throw "Anonymous MCP was accepted"
    } catch {
        if (!$_.Exception.Response -or [int]$_.Exception.Response.StatusCode -ne 401) { throw }
    }
    & $Tailscale funnel --bg --https=443 $Port
    if ($LASTEXITCODE -ne 0) { throw "Tailscale Funnel failed" }
    & $Tailscale funnel status
}
