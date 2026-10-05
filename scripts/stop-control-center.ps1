$ErrorActionPreference = "Stop"
$Port = 8764
$Connections = @(Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
if ($Connections.Count -eq 0) {
    Write-Host "Control Center is not running."
    exit 0
}
$Pids = @($Connections | Select-Object -ExpandProperty OwningProcess -Unique)
foreach ($PidValue in $Pids) {
    $Proc = Get-CimInstance Win32_Process -Filter "ProcessId=$PidValue" -ErrorAction SilentlyContinue
    if (-not $Proc -or $Proc.CommandLine -notmatch "chatgpt_actuator\.control_center") {
        throw "Port $Port is owned by an unexpected process (PID $PidValue). Refusing to stop it."
    }
    Stop-Process -Id $PidValue -Force
    Write-Host "Stopped Control Center PID $PidValue."
}
