<#
.SYNOPSIS
  Build TimelinerX for Windows (dist\TimelinerX.exe).

.DESCRIPTION
  Creates an isolated virtual environment, installs the pinned dependencies,
  runs the test suite (unless -SkipTests), and builds with PyInstaller.
  End users of the resulting .exe do not need Python.

.PARAMETER Python       Python to use, e.g. "py -3.12" or "C:\Python312\python.exe".
                        Default: auto-detect Python 3.11, 3.12 or 3.13 (py launcher first, then python on PATH).
.PARAMETER SkipTests    Skip pytest.
.PARAMETER OneDir       Build a folder instead of one file (recommended for strict LGPL re-linking of Qt).
.PARAMETER FFmpegDir    Folder containing ffmpeg.exe and ffprobe.exe to bundle. Read THIRD_PARTY_NOTICES.md first:
                        GPL builds of FFmpeg impose GPL source-offer obligations on redistribution.
.PARAMETER SignCert     Path to a .pfx code-signing certificate (optional).
.PARAMETER SignPassword Password for -SignCert.
.PARAMETER TimestampUrl RFC 3161 timestamp server (default: http://timestamp.digicert.com).
.PARAMETER Clean        Remove build/, dist/ and .venv-build/ before building.
.PARAMETER CartoKey     CARTO basemap API key to build into the executables (default: the first line of
                        carto_key.txt next to this script, if that file exists). Anyone who has the .exe can
                        extract a built-in key and use your CARTO quota; users can always enter their own
                        key in Settings, which takes precedence.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File build_windows.ps1
  powershell -ExecutionPolicy Bypass -File build_windows.ps1 -FFmpegDir C:\ffmpeg\bin -SignCert cert.pfx -SignPassword ***
#>
param(
  [string]$Python = "",
  [switch]$SkipTests,
  [switch]$OneDir,
  [string]$FFmpegDir = "",
  [string]$SignCert = "",
  [string]$SignPassword = "",
  [string]$TimestampUrl = "http://timestamp.digicert.com",
  [switch]$Clean,
  [string]$CartoKey = ""
)
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }

if ($Clean) {
  Step "Cleaning previous build output"
  foreach ($d in @("build", "dist", ".venv-build")) { if (Test-Path $d) { Remove-Item -Recurse -Force $d } }
}

# numpy 2.4 needs Python >= 3.11; PySide6 6.11 wheels exist up to 3.13.
$Supported = @("3.11", "3.12", "3.13")

function Get-PyVersion([string[]]$cmd) {
  try {
    $exe = $cmd[0]; $rest = @(); if ($cmd.Count -gt 1) { $rest = $cmd[1..($cmd.Count - 1)] }
    $v = & $exe @rest -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
    if ($LASTEXITCODE -eq 0 -and $v) { return ($v | Select-Object -Last 1).Trim() }
  } catch { }
  return $null
}

function Find-Python {
  if ($Python) {
    $cmd = @($Python -split '\s+' | Where-Object { $_ })
    $v = Get-PyVersion $cmd
    if (-not $v) { throw "-Python '$Python' could not be started." }
    if ($Supported -notcontains $v) { throw "-Python '$Python' is Python $v; TimelinerX needs $($Supported -join ', ')." }
    return ,$cmd
  }
  $candidates = @()
  if (Get-Command py -ErrorAction SilentlyContinue) {
    foreach ($ver in $Supported) { $candidates += ,@("py", "-$ver") }
  }
  foreach ($name in @("python", "python3")) {
    $c = Get-Command $name -ErrorAction SilentlyContinue
    # skip the Microsoft Store alias stub in WindowsApps, which only opens the Store
    if ($c -and $c.Source -notlike "*\WindowsApps\*") { $candidates += ,@($c.Source) }
  }
  foreach ($cmd in $candidates) {
    $v = Get-PyVersion $cmd
    if ($v -and ($Supported -contains $v)) { return ,$cmd }
  }
  $found = @()
  foreach ($cmd in $candidates) { $v = Get-PyVersion $cmd; if ($v) { $found += "$($cmd -join ' ') -> $v" } }
  $msg = "No supported Python found (need $($Supported -join ', '))."
  if ($found) { $msg += " Found: $($found -join '; ')." }
  $msg += " Install one (e.g. 'winget install Python.Python.3.12') or pass -Python <path-to-python.exe>."
  throw $msg
}

$venvPy = Join-Path $PSScriptRoot ".venv-build\Scripts\python.exe"
if ((Test-Path ".venv-build") -and -not (Test-Path $venvPy)) {
  Step "Removing incomplete .venv-build from an earlier failed run"
  Remove-Item -Recurse -Force ".venv-build"
}
if (-not (Test-Path $venvPy)) {
  $base = Find-Python
  $baseExe = $base[0]; $baseArgs = @(); if ($base.Count -gt 1) { $baseArgs = $base[1..($base.Count - 1)] }
  Step "Creating virtual environment (.venv-build) with $($base -join ' ') (Python $(Get-PyVersion $base))"
  & $baseExe @baseArgs -m venv .venv-build
  if ($LASTEXITCODE -ne 0 -or -not (Test-Path $venvPy)) { throw "could not create the virtual environment .venv-build" }
} else {
  $v = Get-PyVersion @($venvPy)
  if ($Supported -notcontains $v) { throw ".venv-build uses Python $v (unsupported). Re-run with -Clean." }
  Step "Reusing virtual environment (.venv-build, Python $v)"
}
$py = $venvPy
& $py -m pip install --upgrade pip | Out-Null
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed" }
& $py -m pip install -r requirements-dev.txt
if ($LASTEXITCODE -ne 0) { throw "dependency installation failed" }

if (-not $SkipTests) {
  Step "Running tests"
  $env:QT_QPA_PLATFORM = "offscreen"
  & $py -m pytest -q
  if ($LASTEXITCODE -ne 0) { throw "tests failed (use -SkipTests to build anyway)" }
  Remove-Item Env:\QT_QPA_PLATFORM
}

if (-not $CartoKey -and (Test-Path "carto_key.txt")) {
  $CartoKey = (Get-Content "carto_key.txt" -TotalCount 1).Trim()
}
$buildInfo = Join-Path $PSScriptRoot "src\timelinerx\_buildinfo.py"
if ($CartoKey) {
  if ($CartoKey -notmatch '^[A-Za-z0-9_\-]+$') { throw "the CARTO key contains unexpected characters" }
  Step "Embedding a CARTO API key (ending ...$($CartoKey.Substring([Math]::Max(0, $CartoKey.Length - 4))))"
  "# generated by build_windows.ps1 - do not commit`nCARTO_KEY = `"$CartoKey`"`n" | Out-File -Encoding utf8 $buildInfo
} else {
  Write-Host "No CARTO key embedded: users must enter their own key in Settings to use CARTO maps." -ForegroundColor Yellow
}

Step "Building with PyInstaller"
if ($OneDir) { $env:TLX_ONEDIR = "1" } else { Remove-Item Env:\TLX_ONEDIR -ErrorAction SilentlyContinue }
if ($FFmpegDir) { $env:TLX_FFMPEG_DIR = $FFmpegDir } else { Remove-Item Env:\TLX_FFMPEG_DIR -ErrorAction SilentlyContinue }
try {
  & $py -m PyInstaller packaging\TimelinerX.spec --noconfirm --clean
  if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
} finally {
  if (Test-Path $buildInfo) { Remove-Item -Force $buildInfo }   # the key never stays in the source tree
}

$exe = if ($OneDir) { "dist\TimelinerX\TimelinerX.exe" } else { "dist\TimelinerX.exe" }
$cli = if ($OneDir) { "dist\TimelinerX\timelinerx-cli.exe" } else { "dist\timelinerx-cli.exe" }
foreach ($f in @($exe, $cli)) { if (-not (Test-Path $f)) { throw "expected output $f not found" } }

Step "Smoke test: $cli --version / import"
& $cli --version
if ($LASTEXITCODE -ne 0) { throw "the CLI executable did not start" }
& $cli import fixtures\small.json | Out-Null
if ($LASTEXITCODE -ne 0) { throw "the CLI executable could not import a fixture" }

if ($SignCert) {
  Step "Code signing"
  $signtool = Get-Command signtool.exe -ErrorAction SilentlyContinue
  if (-not $signtool) { throw "signtool.exe not found (install the Windows SDK)" }
  & $signtool.Source sign /f $SignCert /p $SignPassword /fd SHA256 /tr $TimestampUrl /td SHA256 $exe $cli
  if ($LASTEXITCODE -ne 0) { throw "signing failed" }
  & $signtool.Source verify /pa $exe $cli
} else {
  Write-Host "Not signed (no -SignCert). Windows SmartScreen may warn on first launch." -ForegroundColor Yellow
}

foreach ($f in @($exe, $cli)) {
  $hash = (Get-FileHash $f -Algorithm SHA256).Hash
  "$hash  $(Split-Path $f -Leaf)" | Out-File -Encoding ascii "$f.sha256"
  Step "Done: $f (SHA-256 $hash)"
}
