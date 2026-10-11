param(
    [string]$Profile = "tailscale",
    [string]$ProfilesFile = ""
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
if (!$ProfilesFile) { $ProfilesFile = Join-Path $Root 'config\connection-profiles.local.json' }
$ProfilesFile = (Resolve-Path $ProfilesFile -ErrorAction Stop).Path
$Python = Join-Path $Root '.venv\Scripts\python.exe'
if (!(Test-Path $Python)) { throw "Install the project with -Tailscale first" }
Push-Location $Root
try {
    & $Python (Join-Path $Root 'integrations\tailscale_gateway\run_actuator_http.py') --profiles $ProfilesFile --profile $Profile
    if ($LASTEXITCODE -ne 0) { throw "Actuator HTTP failed" }
} finally { Pop-Location }
