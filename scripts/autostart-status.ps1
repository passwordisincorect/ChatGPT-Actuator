param(
    [string]$TaskName = "ChatGPT-Actuator Tunnel",
    [switch]$Json
)

$ErrorActionPreference = "Stop"
$Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $Task) {
    $Result = [pscustomobject]@{ installed = $false; task_name = $TaskName; state = "NotInstalled"; last_run_time = $null; last_task_result = $null }
}
else {
    $Info = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction SilentlyContinue
    $Result = [pscustomobject]@{
        installed = $true
        task_name = $TaskName
        state = [string]$Task.State
        last_run_time = if ($Info) { $Info.LastRunTime } else { $null }
        last_task_result = if ($Info) { $Info.LastTaskResult } else { $null }
    }
}
if ($Json) { $Result | ConvertTo-Json -Compress } else { $Result | Format-List }
