# Builds the distributable Windows package.
#
#   pwsh -File build.ps1                  # host, folder build + zip  (RECOMMENDED)
#   pwsh -File build.ps1 -OneFile         # host, single .exe convenience build
#   pwsh -File build.ps1 -OneFile -Both   # host, both artifacts
#   pwsh -File build.ps1 -Target extensions   # the three sidecar extension zips
#   pwsh -File build.ps1 -Target all          # host + extensions
#
# Why two targets:
#   The host must stay a light, pure-API client (no PyMuPDF / ONNX Runtime /
#   OCR / ASR inside it). Those live in extensions_src\ and are built separately
#   into dist\extensions\<id>-<version>.zip, then installed at runtime from the
#   extension page. Bundling them would push the installer past 1 GB, slow down
#   start-up and let a C++ inference crash take the tray and the proxy with it.
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
    [ValidateSet("host", "extensions", "all")]
    [string]$Target = "host",
    [switch]$OneFile,
    [switch]$Both,
    [switch]$SkipClean,
    # 扩展要打进 rapidocr / PyMuPDF / sherpa-onnx，用独立环境（.extvenv）构建，
    # 免得主程序的依赖被这些重型轮子污染。
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$python = if ($Python) { $Python } elseif (Test-Path ".extvenv\Scripts\python.exe") { ".extvenv\Scripts\python.exe" } elseif (Test-Path ".venv\Scripts\python.exe") { ".venv\Scripts\python.exe" } else { "python" }
$hostPython = if (Test-Path ".venv\Scripts\python.exe") { ".venv\Scripts\python.exe" } else { "python" }
$appName = "IndexTranslate-desktop"

# 版本号只维护 app/core/config.py 一处，打包产物自动跟随
$versionMatch = Select-String -Path "app\core\config.py" -Pattern '^VERSION\s*=\s*"([^"]+)"' | Select-Object -First 1
$version = if ($versionMatch) { $versionMatch.Matches[0].Groups[1].Value } else { "0.0.0" }
Write-Host "version: $version" -ForegroundColor DarkGray

Write-Host "[1/4] icon" -ForegroundColor Cyan
if (-not (Test-Path "assets\icon.ico")) {
    & $hostPython tools\make_icon.py
}

$commonArgs = @(
    "--noconfirm",
    "--clean",
    "--windowed",
    "--name", $appName,
    "--icon", "assets\icon.ico",
    "--version-file", "version_info.txt",
    "--add-data", "assets;assets",
    # 界面词表按 app/core/i18n.py 的相对位置解析，所以目录结构要一起打进去
    "--add-data", "app\locales;app\locales",
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
    & $hostPython -m PyInstaller @Extra @commonArgs
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
$buildHost = $Target -eq "host" -or $Target -eq "all"
$buildExtensions = $Target -eq "extensions" -or $Target -eq "all"

function Build-Extensions {
    param([string]$Version)

    Write-Host "[ext] building sidecar extensions" -ForegroundColor Cyan
    $sourceRoot = "extensions_src"
    if (-not (Test-Path $sourceRoot)) {
        Write-Host "no extensions_src, skipped" -ForegroundColor DarkGray
        return
    }
    $outDir = Join-Path "dist" "extensions"
    New-Item -ItemType Directory -Force -Path $outDir | Out-Null

    foreach ($dir in Get-ChildItem $sourceRoot -Directory) {
        $manifestPath = Join-Path $dir.FullName "manifest.json"
        if (-not (Test-Path $manifestPath)) { continue }
        $manifest = Get-Content $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $extId = $manifest.id
        $extVersion = $manifest.version
        $entry = Join-Path $dir.FullName "main.py"
        if (-not (Test-Path $entry)) { throw "$extId 缺少 main.py" }

        Write-Host ("  - {0} {1}" -f $extId, $extVersion) -ForegroundColor Cyan
        $workName = "$extId-main"

        # 各扩展的依赖里带「数据文件」（模型、config.yaml、原生库），PyInstaller 只靠
        # import 分析抓不到，必须 --collect-all，否则打出来的 exe 一跑就报缺少文件。
        $collectArgs = switch ($extId) {
            "ext-ocr-overlay" { @("--collect-all", "rapidocr_onnxruntime", "--collect-all", "onnxruntime") }
            "ext-pdf-bilingual" { @("--collect-all", "pymupdf") }
            "ext-live-asr" { @("--collect-all", "sherpa_onnx") }
            default { @() }
        }

        $pyExtArgs = @(
            "--noconfirm", "--clean", "--onefile", "--console",
            # 同宿主一样：onefile 解压目录不能落在 %TEMP%（部分机器 / 安全软件会拒绝创建）。
            # 相对路径会按子进程的工作目录解析，而宿主启动扩展时 cwd 就是扩展自己的目录。
            "--runtime-tmpdir", ".IndexTranslateRuntime",
            "--name", $workName,
            "--distpath", (Join-Path $outDir ".build"),
            "--workpath", (Join-Path $outDir ".work-$extId"),
            "--specpath", (Join-Path $outDir ".build")
        ) + $collectArgs + @($entry)
        & $python -m PyInstaller @pyExtArgs
        if ($LASTEXITCODE -ne 0) { throw "pyinstaller failed for $extId" }

        $stage = Join-Path $outDir (".stage-" + $extId)
        if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
        New-Item -ItemType Directory -Force -Path $stage | Out-Null
        Copy-Item (Join-Path $outDir ".build\$workName.exe") (Join-Path $stage "main.exe") -Force
        Copy-Item $manifestPath (Join-Path $stage "manifest.json") -Force
        Get-ChildItem $dir.FullName -File |
            Where-Object { $_.Extension -notin @(".py", ".pyc") } |
            ForEach-Object { Copy-Item $_.FullName $stage -Force }

        # 源码清单里 entry 写的是 main.py（开发时用 python 直接跑），打包后入口是 main.exe。
        # 必须在上面那次拷贝之后再改写，否则会被源目录的 manifest.json 覆盖掉。
        # 注意：必须用 .NET 的 UTF-8 读取，Get-Content 会按 CP936 解码，中文 name/description 直接坏掉。
        $stagedManifest = [System.IO.Path]::GetFullPath((Join-Path $stage "manifest.json"))
        $stagedRaw = [System.IO.File]::ReadAllText($stagedManifest, [System.Text.Encoding]::UTF8)
        $staged = $stagedRaw | ConvertFrom-Json
        if ($staged.entry -eq "main.py") {
            $staged.entry = "main.exe"
            $stagedJson = $staged | ConvertTo-Json -Depth 6
            [System.IO.File]::WriteAllText($stagedManifest, $stagedJson, (New-Object System.Text.UTF8Encoding($false)))
        }

        $zip = Join-Path $outDir "$extId-$extVersion.zip"
        if (Test-Path $zip) { Remove-Item $zip -Force }
        Compress-Archive -Path (Join-Path $stage "*") -DestinationPath $zip -Force
        $hash = (Get-FileHash -Path $zip -Algorithm SHA256).Hash.ToLowerInvariant()

        # 校验信息放在 zip 外面：zip 的哈希不可能写进 zip 自己内部。
        # 必须写无 BOM 的 UTF-8：带 BOM 的 JSON 会让 python json.loads / 第三方工具直接报错。
        $catalogPath = Join-Path $outDir "$extId-$extVersion.json"
        $manifest | Add-Member -NotePropertyName sha256 -NotePropertyValue $hash -Force
        $manifest | Add-Member -NotePropertyName host_version -NotePropertyValue $Version -Force
        $json = $manifest | ConvertTo-Json -Depth 6
        $fullPath = [System.IO.Path]::GetFullPath((Join-Path (Get-Location) $catalogPath))
        [System.IO.File]::WriteAllText($fullPath, $json, (New-Object System.Text.UTF8Encoding($false)))

        Remove-Item $stage -Recurse -Force
        Write-Host ("    OK {0} ({1} KB)  sha256={2}" -f $zip, [math]::Round((Get-Item $zip).Length / 1KB, 0), $hash.Substring(0, 12)) -ForegroundColor Green
    }
}

if ($buildHost -and $buildFolder) {
    Invoke-PyInstaller @()
    $exe = "dist\$appName\$appName.exe"
    if (-not (Test-Path $exe)) { throw "expected $exe was not produced" }
    Write-Host ("OK  {0}  ({1} MB)" -f $exe, [math]::Round((Get-Item $exe).Length / 1MB, 1)) -ForegroundColor Green
    New-FolderZip -Version $version
}

if ($buildHost -and $buildFile) {
    # A relative --runtime-tmpdir keeps the unpack out of %TEMP%: the bootloader
    # creates the folder next to the executable instead of in the user profile.
    Invoke-PyInstaller @("--onefile", "--runtime-tmpdir", ".IndexTranslateRuntime")
    $exe = "dist\$appName.exe"
    if (-not (Test-Path $exe)) { throw "expected $exe was not produced" }
    Write-Host ("OK  {0}  ({1} MB)" -f $exe, [math]::Round((Get-Item $exe).Length / 1MB, 1)) -ForegroundColor Green
    New-OneFileAlias -Version $version
}

if ($buildExtensions) {
    Build-Extensions -Version $version
}

Write-Host "[4/4] done" -ForegroundColor Cyan
Write-Host ""
Write-Host "Artifacts:"
Get-ChildItem dist -File -ErrorAction SilentlyContinue | ForEach-Object { "  $($_.Name)  ($([math]::Round($_.Length/1MB,1)) MB)" }
if (Test-Path "dist\$appName") { "  $appName\  (folder build - unpack this and run $appName.exe)" }
