$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$KeyFile = Join-Path $ProjectRoot "deployment\control-plane-key.dpapi"
$Manager = Join-Path $PSScriptRoot "tunnel-manager.ps1"
$ControlCenter = Join-Path $PSScriptRoot "start-control-center.ps1"

if (Test-Path $KeyFile) {
    $Protected = [System.IO.File]::ReadAllText($KeyFile).Trim()
    if ($Protected) {
        $Secure = ConvertTo-SecureString $Protected
        $Bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secure)
        try {
            $env:CONTROL_PLANE_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($Bstr)
        } finally {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($Bstr)
        }
    }
}

& $ControlCenter
& $Manager -Action Start
