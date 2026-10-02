param(
    [string]$TaskName = "ChatGPT-Actuator Tunnel",
    [switch]$StartNow,
    [switch]$DryRun,
    [switch]$AllowMissingCredential
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Manager = Join-Path $PSScriptRoot "tunnel-manager.ps1"
$Entry = Join-Path $PSScriptRoot "autostart-entry.ps1"
$KeyFile = Join-Path $ProjectRoot "deployment\control-plane-key.dpapi"

if (-not (Test-Path $Manager)) { throw "Missing tunnel manager: $Manager" }
if (-not (Test-Path $Entry)) { throw "Missing autostart entry: $Entry" }

$UserKey = [Environment]::GetEnvironmentVariable("CONTROL_PLANE_API_KEY", "User")
$HasProtectedKey = Test-Path $KeyFile
if (-not $UserKey -and -not $HasProtectedKey -and -not $AllowMissingCredential) {
    throw "Autostart credential is not persistent. Run .\scripts\save-runtime-key.ps1 first, or set CONTROL_PLANE_API_KEY at User scope. Use -AllowMissingCredential only if your tunnel profile authenticates independently."
}

$UserId = "$env:USERDOMAIN\$env:USERNAME"
$PowerShellExe = (Get-Command powershell.exe -ErrorAction Stop).Source
$Arguments = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Entry`""

$Plan = [pscustomobject]@{
    task_name = $TaskName
    user = $UserId
    executable = $PowerShellExe
    arguments = $Arguments
    credential_source = if ($HasProtectedKey) { "dpapi_file" } elseif ($UserKey) { "user_environment" } else { "profile_or_missing" }
    trigger = "At logon"
    run_level = "Limited"
    project_root = $ProjectRoot
}

if ($DryRun) {
    $Plan | Format-List
    Write-Host "Dry run only. No scheduled task was created."
    exit 0
}

$Action = New-ScheduledTaskAction -Execute $PowerShellExe -Argument $Arguments
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $UserId
$Principal = New-ScheduledTaskPrincipal -UserId $UserId -LogonType Interactive -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Principal $Principal -Settings $Settings -Description "Start ChatGPT-Actuator Secure MCP Tunnel when the current user signs in." -Force | Out-Null

Write-Host "Autostart task installed successfully."
Write-Host "Task: $TaskName"
Write-Host "User: $UserId"
Write-Host "Manager: $Manager"

if ($HasProtectedKey) {
    Write-Host "Credential source: DPAPI-protected current-user file."
} elseif ($UserKey) {
    Write-Host "Credential source: User-scope environment variable."
} else {
    Write-Warning "No persistent runtime API key was detected. Autostart may fail unless the tunnel profile authenticates independently."
}

if ($StartNow) {
    Write-Host ""
    Write-Host "Starting scheduled task now..."
    Start-ScheduledTask -TaskName $TaskName
}
