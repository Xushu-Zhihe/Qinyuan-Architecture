param(
    [string]$Output = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
if (-not $Output) {
    $Output = Join-Path $Root "QinyuanEnvironmentChecker.exe"
} elseif (-not [System.IO.Path]::IsPathRooted($Output)) {
    $Output = Join-Path (Get-Location) $Output
}
$Output = [System.IO.Path]::GetFullPath($Output)
$Source = Join-Path $PSScriptRoot "environment-checker\Program.cs"
$CompilerCandidates = @(
    (Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319\csc.exe"),
    (Join-Path $env:WINDIR "Microsoft.NET\Framework\v4.0.30319\csc.exe")
)
$Compiler = $CompilerCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $Compiler) {
    throw "Cannot find the .NET Framework C# compiler (csc.exe)."
}

$OutputDirectory = Split-Path -Parent $Output
New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
$Temporary = Join-Path $env:TEMP ("QinyuanEnvironmentChecker-{0}.exe" -f [Guid]::NewGuid().ToString("N"))
try {
    & $Compiler /nologo /target:winexe /optimize+ /platform:anycpu /utf8output "/out:$Temporary" /reference:System.dll /reference:System.Core.dll /reference:System.Drawing.dll /reference:System.Windows.Forms.dll $Source
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $Temporary)) {
        throw "Environment checker compilation failed with exit code $LASTEXITCODE."
    }
    Copy-Item -LiteralPath $Temporary -Destination $Output -Force
} finally {
    Remove-Item -LiteralPath $Temporary -Force -ErrorAction SilentlyContinue
}

$Version = (Get-Item -LiteralPath $Output).VersionInfo
Write-Host ("Built {0} (file version {1})" -f $Output, $Version.FileVersion)
