param(
    [ValidateSet("Status","Start","Stop","Restart")]
    [string]$Action = "Status",
    [int]$WaitSeconds = 15,
    [switch]$Json
)

$ErrorActionPreference = "Stop"
$ActuatorRoot = Split-Path -Parent $PSScriptRoot
$WorkspaceRoot = Split-Path -Parent $ActuatorRoot
$OrchestratorRoot = if ($env:CHATGPT_ORCHESTRATOR_ROOT) { $env:CHATGPT_ORCHESTRATOR_ROOT } else { Join-Path $WorkspaceRoot "ChatGPT-Orchestrator" }
$ProfileName = if ($env:CHATGPT_ORCHESTRATOR_TUNNEL_PROFILE) { $env:CHATGPT_ORCHESTRATOR_TUNNEL_PROFILE } else { "chatgpt-orchestrator" }
$HealthUrl = if ($env:CHATGPT_ORCHESTRATOR_HEALTH) { $env:CHATGPT_ORCHESTRATOR_HEALTH } else { "http://127.0.0.1:8082/healthz" }
$KeyFile = Join-Path $OrchestratorRoot "deployment\control-plane-key.dpapi"
$LogDir = Join-Path $OrchestratorRoot "logs"
$StdoutLog = Join-Path $LogDir "tunnel.stdout.log"
$StderrLog = Join-Path $LogDir "tunnel.stderr.log"
$TunnelClientDefault = Join-Path $env:LOCALAPPDATA "OpenAI\TunnelClient\tunnel-client.exe"

function Resolve-TunnelClient {
    if (Test-Path $TunnelClientDefault) { return $TunnelClientDefault }
    $Found = Get-Command tunnel-client.exe -ErrorAction SilentlyContinue
    if ($Found) { return $Found.Source }
    throw "tunnel-client.exe was not found."
}

function Import-ControlPlaneKey {
    if (-not (Test-Path $KeyFile)) { return }
    $Protected = [System.IO.File]::ReadAllText($KeyFile).Trim()
    if (-not $Protected) { return }
    $Secure = ConvertTo-SecureString $Protected
    $Bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secure)
    try {
        $env:CONTROL_PLANE_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($Bstr)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($Bstr)
    }
}

function Get-TunnelProcesses {
    $EscapedProfile = [regex]::Escape($ProfileName)
    Get-CimInstance Win32_Process -Filter "Name='tunnel-client.exe'" -ErrorAction SilentlyContinue | Where-Object {
        $_.CommandLine -and
        $_.CommandLine -match "(?i)\brun\b" -and
        $_.CommandLine -match "(?i)--profile" -and
        $_.CommandLine -match $EscapedProfile
    }
}

function Test-Http {
    try {
        $Response = Invoke-WebRequest -UseBasicParsing -Uri $HealthUrl -TimeoutSec 2 -ErrorAction Stop
        return ($Response.StatusCode -ge 200 -and $Response.StatusCode -lt 300)
    } catch {
        return $false
    }
}

function Convert-CreationDate {
    param($Value)
    if (-not $Value) { return $null }
    if ($Value -is [datetime]) { return $Value.ToUniversalTime() }
    try {
        return ([Management.ManagementDateTimeConverter]::ToDateTime([string]$Value)).ToUniversalTime()
    } catch {
        try { return ([datetime]::Parse([string]$Value)).ToUniversalTime() } catch { return $null }
    }
}

function Get-StatusObject {
    $Processes = @(Get-TunnelProcesses)
    $StartedAt = $null
    $UptimeSeconds = 0
    if ($Processes.Count -gt 0) {
        $Oldest = $Processes | Sort-Object CreationDate | Select-Object -First 1
        $StartedAt = Convert-CreationDate $Oldest.CreationDate
        if ($StartedAt) {
            $UptimeSeconds = [Math]::Max(0, [int]((Get-Date).ToUniversalTime() - $StartedAt).TotalSeconds)
        }
    }
    [pscustomobject]@{
        profile = $ProfileName
        running = ($Processes.Count -gt 0)
        pids = @($Processes | ForEach-Object { [int]$_.ProcessId })
        health_ok = (Test-Http)
        health_url = $HealthUrl
        started_at = if ($StartedAt) { $StartedAt.ToString("o") } else { $null }
        uptime_seconds = $UptimeSeconds
        stdout_log = $StdoutLog
        stderr_log = $StderrLog
        root = $OrchestratorRoot
    }
}

function Write-Status {
    $Status = Get-StatusObject
    if ($Json) {
        $Status | ConvertTo-Json -Depth 4 -Compress
    } else {
        $Status | Format-List
    }
}

function Start-Tunnel {
    $Existing = @(Get-TunnelProcesses)
    if ($Existing.Count -gt 0) { return }

    $Client = Resolve-TunnelClient
    $ProfilePath = Join-Path $env:APPDATA "tunnel-client\$ProfileName.yaml"
    if (-not (Test-Path $ProfilePath)) { throw "Tunnel profile was not found: $ProfilePath" }

    Import-ControlPlaneKey
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
    $Proc = Start-Process -FilePath $Client -ArgumentList @("run","--profile",$ProfileName) -WindowStyle Hidden -RedirectStandardOutput $StdoutLog -RedirectStandardError $StderrLog -PassThru

    $Deadline = (Get-Date).AddSeconds([Math]::Max(1, $WaitSeconds))
    do {
        Start-Sleep -Milliseconds 500
        if ($Proc.HasExited) {
            $Tail = ""
            if (Test-Path $StderrLog) { $Tail = (Get-Content $StderrLog -Tail 20 -ErrorAction SilentlyContinue) -join [Environment]::NewLine }
            throw "Orchestrator tunnel exited early with code $($Proc.ExitCode). $Tail"
        }
        if (Test-Http) { return }
    } while ((Get-Date) -lt $Deadline)

    throw "Orchestrator tunnel started, but health did not become ready within $WaitSeconds seconds."
}

function Stop-Tunnel {
    $Processes = @(Get-TunnelProcesses)
    foreach ($Proc in $Processes) {
        & taskkill.exe /PID ([int]$Proc.ProcessId) /T /F 2>$null | Out-Null
    }
    $Deadline = (Get-Date).AddSeconds(5)
    do {
        if (@(Get-TunnelProcesses).Count -eq 0) { return }
        Start-Sleep -Milliseconds 250
    } while ((Get-Date) -lt $Deadline)
    foreach ($Proc in @(Get-TunnelProcesses)) {
        & taskkill.exe /PID ([int]$Proc.ProcessId) /T /F 2>$null | Out-Null
    }
}

switch ($Action) {
    "Status"  { Write-Status }
    "Start"   { Start-Tunnel; Write-Status }
    "Stop"    { Stop-Tunnel; Write-Status }
    "Restart" { Stop-Tunnel; Start-Tunnel; Write-Status }
}
