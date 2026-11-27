# Builds the distributable Windows package.
#
#   pwsh -File build.ps1              # folder build + zip  (RECOMMENDED, always works)
#   pwsh -File build.ps1 -OneFile     # single .exe convenience build
#   pwsh -File build.ps1 -OneFile -Both   # both
#
# Why a folder build is the default:
#   A PyInstaller --onefile executable unpacks itself into %TEMP%\_MEIxxxxxx on
#   every start. On locked-down Windows profiles (restricted TEMP ACLs, or
#   360 / 火绒 / Defender interfering with the unpack) the bootloader aborts with
#   "Could not create temporary directory!" and the program never starts.
#   A folder build has nothing to unpack, so it starts on any machine.
#
# Requires: python -m pip install -r requirements.txt pyinstaller

param(
    [switch]$OneFile,
    [switch]$Both,
    [switch]$SkipClean
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$python = if (Test-Path ".venv\Scripts\python.exe") { ".venv\Scripts\python.exe" } else { "python" }
$appName = "IndexTranslate-desktop"

# 版本号只维护 app/core/config.py 一处，打包产物自动跟随
$versionMatch = Select-String -Path "app\core\config.py" -Pattern '^VERSION\s*=\s*"([^"]+)"' | Select-Object -First 1
$version = if ($versionMatch) { $versionMatch.Matches[0].Groups[1].Value } else { "0.0.0" }
Write-Host "version: $version" -ForegroundColor DarkGray

Write-Host "[1/4] icon" -ForegroundColor Cyan
if (-not (Test-Path "assets\icon.ico")) {
    & $python tools\make_icon.py
}

$commonArgs = @(
    "--noconfirm",
    "--clean",
    "--windowed",
    "--name", $appName,
    "--icon", "assets\icon.ico",
    "--version-file", "version_info.txt",
    "--add-data", "assets;assets",
    "--collect-all", "qfluentwidgets",
    "--collect-all", "qframelesswindow",
    "--exclude-module", "PyQt5",
    "--exclude-module", "tkinter",
    "--exclude-module", "matplotlib",
    "--exclude-module", "numpy",
    "run.py"
)

function Invoke-PyInstaller {
    param([string[]]$Extra)
    Write-Host "[2/4] pyinstaller $($Extra -join ' ')" -ForegroundColor Cyan
    & $python -m PyInstaller @Extra @commonArgs
    if ($LASTEXITCODE -ne 0) { throw "pyinstaller failed with exit code $LASTEXITCODE" }
}

function New-FolderZip {
    param([string]$Version)
    Write-Host "[3/4] zip folder build" -ForegroundColor Cyan
    $folder = Join-Path "dist" $appName
    if (-not (Test-Path $folder)) { return }
    $zip = Join-Path "dist" "$appName-$Version-win64.zip"
    if (Test-Path $zip) { Remove-Item $zip -Force }
    Compress-Archive -Path $folder -DestinationPath $zip -Force
    Write-Host ("OK  {0}  ({1} MB)" -f $zip, [math]::Round((Get-Item $zip).Length / 1MB, 1)) -ForegroundColor Green
}

function New-OneFileAlias {
    param([string]$Version)
    $exe = Join-Path "dist" "$appName.exe"
    if (-not (Test-Path $exe)) { return }
    $alias = Join-Path "dist" "$appName-$Version-win64-onefile.exe"
    Copy-Item $exe $alias -Force
    Write-Host ("OK  {0}  ({1} MB)" -f $alias, [math]::Round((Get-Item $alias).Length / 1MB, 1)) -ForegroundColor Green
}

$buildFolder = -not $OneFile -or $Both
$buildFile = $OneFile -or $Both

if ($buildFolder) {
    Invoke-PyInstaller @()
    $exe = "dist\$appName\$appName.exe"
    if (-not (Test-Path $exe)) { throw "expected $exe was not produced" }
    Write-Host ("OK  {0}  ({1} MB)" -f $exe, [math]::Round((Get-Item $exe).Length / 1MB, 1)) -ForegroundColor Green
    New-FolderZip -Version $version
}

if ($buildFile) {
    # A relative --runtime-tmpdir keeps the unpack out of %TEMP%: the bootloader
    # creates the folder next to the executable instead of in the user profile.
    Invoke-PyInstaller @("--onefile", "--runtime-tmpdir", ".IndexTranslateRuntime")
    $exe = "dist\$appName.exe"
    if (-not (Test-Path $exe)) { throw "expected $exe was not produced" }
    Write-Host ("OK  {0}  ({1} MB)" -f $exe, [math]::Round((Get-Item $exe).Length / 1MB, 1)) -ForegroundColor Green
    New-OneFileAlias -Version $version
}

Write-Host "[4/4] done" -ForegroundColor Cyan
Write-Host ""
Write-Host "Artifacts:"
Get-ChildItem dist -File -ErrorAction SilentlyContinue | ForEach-Object { "  $($_.Name)  ($([math]::Round($_.Length/1MB,1)) MB)" }
if (Test-Path "dist\$appName") { "  $appName\  (folder build - unpack this and run $appName.exe)" }
