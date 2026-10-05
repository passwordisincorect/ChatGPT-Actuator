$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$HealthUrl = "http://127.0.0.1:8764/healthz"
$LogDir = Join-Path $ProjectRoot "logs"
$StdoutLog = Join-Path $LogDir "control-center.stdout.log"
$StderrLog = Join-Path $LogDir "control-center.stderr.log"
$KeyFile = Join-Path $ProjectRoot "deployment\control-plane-key.dpapi"

if (-not (Test-Path $Python)) {
    throw "Virtual environment not found. Run .\scripts\install.ps1 first."
}

try {
    $Response = Invoke-WebRequest -UseBasicParsing -Uri $HealthUrl -TimeoutSec 1 -ErrorAction Stop
    if ($Response.StatusCode -eq 200) {
        Write-Host "Control Center is already running at http://127.0.0.1:8764/"
        exit 0
    }
} catch {}

if (Test-Path $KeyFile) {
    $Protected = [System.IO.File]::ReadAllText($KeyFile).Trim()
    if ($Protected) {
        $Secure = ConvertTo-SecureString $Protected
        $Bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secure)
        try { $env:CONTROL_PLANE_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($Bstr) }
        finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($Bstr) }
    }
}

New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
$Proc = Start-Process -FilePath $Python `
    -ArgumentList @("-m", "chatgpt_actuator.control_center") `
    -WorkingDirectory $ProjectRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput $StdoutLog `
    -RedirectStandardError $StderrLog `
    -PassThru

$Deadline = (Get-Date).AddSeconds(8)
do {
    Start-Sleep -Milliseconds 250
    if ($Proc.HasExited) {
        $Tail = if (Test-Path $StderrLog) { (Get-Content $StderrLog -Tail 30 -ErrorAction SilentlyContinue) -join [Environment]::NewLine } else { "" }
        throw "Control Center exited early with code $($Proc.ExitCode). $Tail"
    }
    try {
        $Response = Invoke-WebRequest -UseBasicParsing -Uri $HealthUrl -TimeoutSec 1 -ErrorAction Stop
        if ($Response.StatusCode -eq 200) {
            Write-Host "Control Center started. PID $($Proc.Id)"
            Write-Host "URL: http://127.0.0.1:8764/"
            exit 0
        }
    } catch {}
} while ((Get-Date) -lt $Deadline)

throw "Control Center process started but health check did not become ready."
