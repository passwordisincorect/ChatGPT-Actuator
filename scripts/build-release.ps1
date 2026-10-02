param(
    [string]$OutputDir = "",
    [switch]$SkipWheel
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$Builder = Join-Path $PSScriptRoot "build_release.py"

if (-not (Test-Path $Python)) {
    throw "Virtual environment missing. Run .\scripts\setup.ps1 first."
}

$Args = @($Builder)
if ($OutputDir) {
    $Args += @("--output-dir", $OutputDir)
}
if ($SkipWheel) {
    $Args += "--skip-wheel"
}

& $Python @Args
if ($LASTEXITCODE -ne 0) {
    throw "Release package build failed."
}
