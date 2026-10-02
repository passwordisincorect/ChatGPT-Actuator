param(
    [switch]$SkipReproBuild
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$Pyproject = Join-Path $ProjectRoot "pyproject.toml"
$Builder = Join-Path $PSScriptRoot "build_release.py"
$RuntimeVerifier = Join-Path $PSScriptRoot "verify_runtime.py"
$Dist = Join-Path $ProjectRoot "dist"

if (-not (Test-Path $Python)) {
    throw "Virtual environment missing. Run .\scripts\setup.ps1 first."
}

$PyprojectText = Get-Content $Pyproject -Raw
$VersionMatch = [regex]::Match($PyprojectText, '(?ms)^\[project\].*?^version\s*=\s*"([^"]+)"')
if (-not $VersionMatch.Success) {
    throw "Could not determine version from pyproject.toml."
}
$ExpectedVersion = $VersionMatch.Groups[1].Value
$InstalledVersion = (& $Python -c "import importlib.metadata; print(importlib.metadata.version('chatgpt-actuator'))" | Select-Object -Last 1).Trim()
if ($InstalledVersion -ne $ExpectedVersion) {
    throw "Installed package version $InstalledVersion does not match project version $ExpectedVersion. Run .\scripts\setup.ps1."
}

Write-Host "=== ChatGPT-Actuator release verification ==="
Write-Host "Version: $ExpectedVersion"
Write-Host ""

Write-Host "[1/7] PowerShell syntax..."
$SyntaxErrors = @()
Get-ChildItem $PSScriptRoot -Filter "*.ps1" -File | ForEach-Object {
    $Tokens = $null
    $Errors = $null
    [void][System.Management.Automation.Language.Parser]::ParseFile($_.FullName, [ref]$Tokens, [ref]$Errors)
    foreach ($ErrorItem in @($Errors)) {
        $SyntaxErrors += "$($_.Name): $($ErrorItem.Message)"
    }
}
if ($SyntaxErrors.Count -gt 0) {
    throw ("PowerShell syntax errors: " + ($SyntaxErrors -join "; "))
}
Write-Host "PASS"

Write-Host "[2/7] Python compile..."
& $Python -m compileall -q (Join-Path $ProjectRoot "src") (Join-Path $ProjectRoot "tests") $Builder $RuntimeVerifier
if ($LASTEXITCODE -ne 0) { throw "Python compileall failed." }
Write-Host "PASS"

Write-Host "[3/7] pip dependency check..."
& $Python -m pip check
if ($LASTEXITCODE -ne 0) { throw "pip check failed." }
Write-Host "PASS"

Write-Host "[4/7] Unit/regression tests with ResourceWarning as error..."
& $Python -W "error::ResourceWarning" -m unittest discover -s (Join-Path $ProjectRoot "tests") -v
if ($LASTEXITCODE -ne 0) { throw "Unit/regression tests failed." }
Write-Host "PASS"

Write-Host "[5/7] Isolated MCP/Admin runtime..."
$RuntimeJson = & $Python $RuntimeVerifier --json
if ($LASTEXITCODE -ne 0) { throw "Isolated runtime verification failed." }
$RuntimeResult = $RuntimeJson | ConvertFrom-Json
if (-not $RuntimeResult.ok) { throw "Isolated runtime verification returned ok=false." }
if ($RuntimeResult.server_version -ne $ExpectedVersion) { throw "Isolated runtime version mismatch." }
if ($RuntimeResult.tool_count -ne 45) { throw "Expected 45 MCP tools, found $($RuntimeResult.tool_count)." }
Write-Host "PASS"

Write-Host "[6/7] Reproducible artifact build..."
if (-not $SkipReproBuild) {
    $TempBase = Join-Path $env:TEMP ("chatgpt-actuator-repro-" + [guid]::NewGuid().ToString("N"))
    $A = Join-Path $TempBase "a"
    $B = Join-Path $TempBase "b"
    New-Item -ItemType Directory -Path $A,$B -Force | Out-Null
    try {
        & $Python $Builder --output-dir $A | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Reproducibility build A failed." }
        & $Python $Builder --output-dir $B | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Reproducibility build B failed." }

        $ManifestA = Get-Content (Join-Path $A "release-manifest.json") -Raw | ConvertFrom-Json
        $ManifestB = Get-Content (Join-Path $B "release-manifest.json") -Raw | ConvertFrom-Json

        foreach ($Kind in @("bootstrap_zip","wheel")) {
            $ArtifactA = $ManifestA.artifacts | Where-Object { $_.kind -eq $Kind } | Select-Object -First 1
            $ArtifactB = $ManifestB.artifacts | Where-Object { $_.kind -eq $Kind } | Select-Object -First 1
            if (-not $ArtifactA -or -not $ArtifactB) { throw "Missing $Kind artifact during reproducibility test." }
            if ($ArtifactA.sha256 -ne $ArtifactB.sha256) {
                throw "$Kind is not reproducible: $($ArtifactA.sha256) != $($ArtifactB.sha256)"
            }
        }
    }
    finally {
        Remove-Item $TempBase -Recurse -Force -ErrorAction SilentlyContinue
    }
    Write-Host "PASS"
}
else {
    Write-Host "SKIPPED"
}

Write-Host "[7/7] Final release artifact..."
& $Python $Builder --output-dir $Dist | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Final release build failed." }
$ManifestPath = Join-Path $Dist "release-manifest.json"
$Manifest = Get-Content $ManifestPath -Raw | ConvertFrom-Json
if ($Manifest.version -ne $ExpectedVersion) { throw "Final artifact version mismatch." }

$Zip = $Manifest.artifacts | Where-Object { $_.kind -eq "bootstrap_zip" } | Select-Object -First 1
$Wheel = $Manifest.artifacts | Where-Object { $_.kind -eq "wheel" } | Select-Object -First 1

Write-Host "PASS"
Write-Host ""
Write-Host "Release verification PASSED."
Write-Host "Version:       $ExpectedVersion"
Write-Host "MCP tools:     $($RuntimeResult.tool_count)"
Write-Host "ZIP:           $($Zip.path)"
Write-Host "ZIP SHA-256:   $($Zip.sha256)"
Write-Host "Wheel:         $($Wheel.path)"
Write-Host "Wheel SHA-256: $($Wheel.sha256)"
Write-Host "Manifest:      $ManifestPath"
