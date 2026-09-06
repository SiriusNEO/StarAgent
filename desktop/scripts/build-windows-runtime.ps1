param(
  [string]$TargetTriple = ""
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path
if (-not $TargetTriple) {
  $TargetTriple = (& rustc --print host-tuple).Trim()
}
if ($TargetTriple -notmatch "windows") {
  throw "The bundled Windows runtime must be built on Windows (target: $TargetTriple)."
}

$BuildRoot = Join-Path ([System.IO.Path]::GetTempPath()) "staragent-pyinstaller"
$DistRoot = Join-Path $BuildRoot "dist"
$WorkRoot = Join-Path $BuildRoot "work"
$SpecRoot = Join-Path $BuildRoot "spec"
$Destination = Join-Path $RepoRoot "desktop/src-tauri/binaries/staragent-runtime-$TargetTriple.exe"

Remove-Item $BuildRoot -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $DistRoot, $WorkRoot, $SpecRoot | Out-Null

Push-Location $RepoRoot
try {
  python -m PyInstaller `
    --clean `
    --noconfirm `
    --onefile `
    --noconsole `
    --name staragent-runtime `
    --collect-all staragent `
    --collect-all winpty `
    --collect-submodules uvicorn `
    --distpath $DistRoot `
    --workpath $WorkRoot `
    --specpath $SpecRoot `
    staragent/desktop_runtime.py
  if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE."
  }
  New-Item -ItemType Directory -Force (Split-Path $Destination) | Out-Null
  Copy-Item (Join-Path $DistRoot "staragent-runtime.exe") $Destination -Force
  Write-Host "Bundled runtime: $Destination"
} finally {
  Pop-Location
  Remove-Item $BuildRoot -Recurse -Force -ErrorAction SilentlyContinue
}
