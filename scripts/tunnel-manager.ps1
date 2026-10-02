param(
    [ValidateSet("Status","Start","Stop","Restart")]
    [string]$Action = "Status",
    [string]$ProfileName = "chatgpt-actuator",
    [string]$HealthUrl = "http://127.0.0.1:8081/healthz",
    [string]$AdminHealthUrl = "http://127.0.0.1:8765/healthz",
    [int]$WaitSeconds = 15,
    [switch]$Json
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $ProjectRoot "logs"
$StdoutLog = Join-Path $LogDir "tunnel.stdout.log"
$StderrLog = Join-Path $LogDir "tunnel.stderr.log"
$TunnelClientDefault = Join-Path $env:LOCALAPPDATA "OpenAI\TunnelClient\tunnel-client.exe"

function Resolve-TunnelClient {
    if (Test-Path $TunnelClientDefault) { return $TunnelClientDefault }
    $Found = Get-Command tunnel-client.exe -ErrorAction SilentlyContinue
    if ($Found) { return $Found.Source }
    throw "tunnel-client.exe was not found."
}

function Get-TunnelProcesses {
    $EscapedProfile = [regex]::Escape($ProfileName)
    Get-CimInstance Win32_Process -Filter "Name='tunnel-client.exe'" -ErrorAction SilentlyContinue | Where-Object {
        if (-not $_.CommandLine) { return $false }
        $Cmd = $_.CommandLine
        $HasRun = $Cmd -match "(?i)\brun\b"
        $HasProfileFlag = $Cmd -match "(?i)--profile"
        $HasProfileName = $Cmd -match $EscapedProfile
        return ($HasRun -and $HasProfileFlag -and $HasProfileName)
    }
}

function Test-Http {
    param([string]$Url)
    try {
        $Response = Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 2 -ErrorAction Stop
        return ($Response.StatusCode -ge 200 -and $Response.StatusCode -lt 300)
    }
    catch { return $false }
}

function Get-StatusObject {
    $Processes = @(Get-TunnelProcesses)
    [pscustomobject]@{
        profile = $ProfileName
        running = ($Processes.Count -gt 0)
        pids = @($Processes | ForEach-Object { [int]$_.ProcessId })
        health_ok = (Test-Http $HealthUrl)
        admin_ok = (Test-Http $AdminHealthUrl)
        health_url = $HealthUrl
        admin_health_url = $AdminHealthUrl
        stdout_log = $StdoutLog
        stderr_log = $StderrLog
    }
}

function Write-Status {
    $Status = Get-StatusObject
    if ($Json) { $Status | ConvertTo-Json -Depth 4 -Compress; return }
    Write-Host "Profile:       $($Status.profile)"
    Write-Host "Running:       $($Status.running)"
    Write-Host "PIDs:          $($Status.pids -join ', ')"
    Write-Host "Tunnel health: $($Status.health_ok)"
    Write-Host "Admin health:  $($Status.admin_ok)"
    Write-Host "Health URL:    $($Status.health_url)"
    Write-Host "Admin URL:     $($Status.admin_health_url)"
    Write-Host "Stdout log:    $($Status.stdout_log)"
    Write-Host "Stderr log:    $($Status.stderr_log)"
}

function Start-Tunnel {
    $Existing = @(Get-TunnelProcesses)
    if ($Existing.Count -gt 0) {
        Write-Host "ChatGPT-Actuator tunnel is already running. PID(s): $($Existing.ProcessId -join ', ')"
        return
    }

    $Client = Resolve-TunnelClient
    $ProfilePath = Join-Path $env:APPDATA "tunnel-client\$ProfileName.yaml"
    if (-not (Test-Path $ProfilePath)) { throw "Tunnel profile was not found: $ProfilePath" }

    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
    $StartArgs = @{
        FilePath = $Client
        ArgumentList = @("run", "--profile", $ProfileName)
        WindowStyle = "Hidden"
        RedirectStandardOutput = $StdoutLog
        RedirectStandardError = $StderrLog
        PassThru = $true
    }
    $Proc = Start-Process @StartArgs
    Write-Host "Started tunnel-client PID $($Proc.Id). Waiting for health..."

    $Deadline = (Get-Date).AddSeconds([Math]::Max(1, $WaitSeconds))
    do {
        Start-Sleep -Milliseconds 500
        if ($Proc.HasExited) {
            $Tail = ""
            if (Test-Path $StderrLog) { $Tail = (Get-Content $StderrLog -Tail 20 -ErrorAction SilentlyContinue) -join [Environment]::NewLine }
            throw "tunnel-client exited early with code $($Proc.ExitCode). $Tail"
        }
        if (Test-Http $HealthUrl) {
            Write-Host "Tunnel health is ready."
            if (Test-Http $AdminHealthUrl) { Write-Host "ChatGPT-Actuator Admin health is ready." }
            return
        }
    } while ((Get-Date) -lt $Deadline)

    throw "Tunnel process started, but health did not become ready within $WaitSeconds seconds. Check $StderrLog"
}

function Stop-Tunnel {
    $Processes = @(Get-TunnelProcesses)
    if ($Processes.Count -eq 0) { Write-Host "ChatGPT-Actuator tunnel is not running."; return }

    foreach ($Proc in $Processes) {
        $PidValue = [int]$Proc.ProcessId
        Write-Host "Stopping tunnel-client PID $PidValue..."
        Stop-Process -Id $PidValue -ErrorAction SilentlyContinue
    }

    $Deadline = (Get-Date).AddSeconds(5)
    do {
        Start-Sleep -Milliseconds 250
        if (@(Get-TunnelProcesses).Count -eq 0) { Write-Host "Tunnel stopped."; return }
    } while ((Get-Date) -lt $Deadline)

    foreach ($Proc in @(Get-TunnelProcesses)) { Stop-Process -Id ([int]$Proc.ProcessId) -Force -ErrorAction SilentlyContinue }
    Write-Host "Tunnel stop was forced after timeout."
}

switch ($Action) {
    "Status"  { Write-Status }
    "Start"   { Start-Tunnel; Write-Status }
    "Stop"    { Stop-Tunnel; Write-Status }
    "Restart" { Stop-Tunnel; Start-Tunnel; Write-Status }
}
