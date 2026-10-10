param(
    [string]$Profile = "tailscale",
    [string]$ProfilesFile = ""
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
if (!$ProfilesFile) { $ProfilesFile = Join-Path $Root 'config\connection-profiles.local.json' }
$ProfilesFile = (Resolve-Path $ProfilesFile -ErrorAction Stop).Path
$All = Get-Content -Raw -Encoding UTF8 $ProfilesFile | ConvertFrom-Json
$Config = $All.profiles.PSObject.Properties[$Profile].Value
if (!$Config -or $Config.transport -ne 'tailscale_funnel') { throw "Choose a Tailscale profile" }
$Issuer = [uri]$Config.issuer
if ($Issuer.Scheme -ne 'https' -or !$Issuer.Host -or $Issuer.Host -eq 'YOUR-TAILNET-NAME.ts.net') { throw "Configure issuer first" }
$env:ACTUATOR_MCP_TRANSPORT = 'http'
$env:ACTUATOR_MCP_HOSTNAME = $Issuer.Host
$Python = Join-Path $Root '.venv\Scripts\python.exe'
Set-Location $Root
& $Python -m chatgpt_actuator.server
