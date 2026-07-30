param(
    [string]$Output = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    $Python = "python"
}

$Arguments = @((Join-Path $PSScriptRoot "build_release.py"))
if ($Output) {
    $Arguments += @("--output", $Output)
}
& $Python @Arguments
exit $LASTEXITCODE
