<#
Build a portable M110 ZIP for isolated feature testing.

    pip install -e ".[build]"
    pip install "uranometria @ git+https://github.com/devonjones/uranometria"
    powershell -ExecutionPolicy Bypass -File packaging\windows\build_portable.ps1

Produces dist\M110-portable-<git-sha>.zip. The included run-dev.cmd keeps
settings and data under D:\m110-dev by default, separate from an installed M110.

Use -PackageOnly with an existing PyInstaller onedir output when testing or when
the application folder has already been built.
#>
#Requires -Version 5
[CmdletBinding()]
param(
    [switch]$PackageOnly,
    [string]$AppDirectory,
    [string]$OutputDirectory,
    [string]$ArtifactSuffix
)

$ErrorActionPreference = "Stop"

$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = (Resolve-Path (Join-Path $Here "..\..")).Path
if (-not $AppDirectory) { $AppDirectory = Join-Path $Root "dist\M110" }
if (-not $OutputDirectory) { $OutputDirectory = Join-Path $Root "dist" }

if (-not $PackageOnly) {
    if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
        throw "python not found. Activate the build environment first."
    }
    python -c "import PyInstaller" 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller not found. Run: pip install -e `".[build]`""
    }
    python -c "import uranometria" 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "uranometria not found. Run: pip install `"uranometria @ git+https://github.com/devonjones/uranometria`""
    }

    Write-Host "==> generating M110.ico"
    python (Join-Path $Here "make_ico.py")

    Write-Host "==> PyInstaller onedir build"
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue `
        (Join-Path $Root "build\M110"), (Join-Path $Root "dist\M110")
    python -m PyInstaller (Join-Path $Here "M110.spec") --noconfirm --clean
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }
}

$AppDirectory = [IO.Path]::GetFullPath($AppDirectory)
$OutputDirectory = [IO.Path]::GetFullPath($OutputDirectory)
$Executable = Join-Path $AppDirectory "M110.exe"
if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) {
    throw "Portable app executable not found: $Executable"
}

if (-not $ArtifactSuffix) {
    if (Get-Command git -ErrorAction SilentlyContinue) {
        $ArtifactSuffix = (& git -C $Root rev-parse --short=7 HEAD 2>$null).Trim()
    }
    if (-not $ArtifactSuffix) { $ArtifactSuffix = "local" }
}
$ArtifactSuffix = $ArtifactSuffix -replace '[^A-Za-z0-9._-]', '-'
if (-not $ArtifactSuffix) { throw "ArtifactSuffix must contain a filename-safe character." }

New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
$Stage = Join-Path $OutputDirectory (".portable-stage-" + [guid]::NewGuid().ToString("N"))
$Archive = Join-Path $OutputDirectory "M110-portable-$ArtifactSuffix.zip"

try {
    Write-Host "==> staging portable app"
    New-Item -ItemType Directory -Path $Stage | Out-Null
    Copy-Item -LiteralPath $AppDirectory -Destination (Join-Path $Stage "M110") -Recurse

    $Launcher = @(
        '@echo off',
        'rem Runs this portable M110 build isolated from any installed M110.',
        'rem Override the location with:  set M110_DEV_ROOT=E:\somewhere',
        'if "%M110_DEV_ROOT%"=="" set "M110_DEV_ROOT=D:\m110-dev"',
        'set "USERPROFILE=%M110_DEV_ROOT%\home"',
        'set "M110_DATA_ROOT=%M110_DEV_ROOT%\data"',
        'if not exist "%USERPROFILE%" mkdir "%USERPROFILE%"',
        'if not exist "%M110_DATA_ROOT%" mkdir "%M110_DATA_ROOT%"',
        'start "" "%~dp0M110\M110.exe"'
    )
    Set-Content -LiteralPath (Join-Path $Stage "run-dev.cmd") -Value $Launcher -Encoding ascii

    if (Test-Path -LiteralPath $Archive) { Remove-Item -LiteralPath $Archive -Force }
    Write-Host "==> creating portable ZIP"
    Compress-Archive -Path (Join-Path $Stage "*") -DestinationPath $Archive
}
finally {
    if (Test-Path -LiteralPath $Stage) { Remove-Item -LiteralPath $Stage -Recurse -Force }
}

Write-Host "==> built: $Archive"
Write-Host "    Extract the ZIP and run run-dev.cmd (isolated under D:\m110-dev by default)."

