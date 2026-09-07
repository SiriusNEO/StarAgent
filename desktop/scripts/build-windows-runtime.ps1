param(
  [string]$TargetTriple = ""
)

$ErrorActionPreference = "Stop"
if (-not $TargetTriple) {
  $TargetTriple = (& rustc --print host-tuple).Trim()
}
if ($TargetTriple -notmatch "windows") {
  throw "The bundled Windows runtime must be built on Windows (target: $TargetTriple)."
}

python (Join-Path $PSScriptRoot "build_runtime.py") --target-triple $TargetTriple
if ($LASTEXITCODE -ne 0) {
  throw "Bundled runtime build failed with exit code $LASTEXITCODE."
}
