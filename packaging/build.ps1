<#
.SYNOPSIS
    微信待办 WeChatTaskHub 打包脚本（PyInstaller onedir / windowed）。

.DESCRIPTION
    产物目录（exe 同级）：
        WeChatTaskHub.exe
        _internal\          PyInstaller 运行时（随包，升级时整体替换）
        web\  assets\       外置静态资源（可热更新；包内另有一份兜底）
        tools\              ngrok.exe / cloudflared.exe / tunnel.py 等（只外置）
        data\ .env          首次运行生成，升级时保留
        启动说明.txt

.PARAMETER Python
        用于构建的 Python 解释器，默认 "python"。可传绝对路径或 venv 里的 python.exe。

.PARAMETER SkipInstall
        跳过 PyInstaller 安装检查；缺失时直接报错（CI 已预装依赖时用）。

.PARAMETER NoZip
        只构建 dist\WeChatTaskHub\，不打 releases\WeChatTaskHub-<版本>-win64.zip。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File packaging\build.ps1
    powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -Python .venv\Scripts\python.exe -NoZip
#>
[CmdletBinding()]
param(
    [string]$Python = "python",
    [switch]$SkipInstall,
    [switch]$NoZip
)

$ErrorActionPreference = 'Stop'

# 控制台/日志统一 UTF-8，避免中文路径与中文提示在日志里乱码。
try {
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
    $OutputEncoding = [System.Text.UTF8Encoding]::new()
} catch { }

# ---------------------------------------------------------------------------
# 原生程序调用包装
#
# 坑：PowerShell 在 $ErrorActionPreference='Stop' 时，会把被重定向的原生程序
# stderr 当成 terminating error（NativeCommandError）。pip 的
# "WARNING: The script xxx.exe is installed in ... which is not on PATH" 这类
# 纯警告会直接打断构建。这里在调用原生程序期间临时切回 Continue，把
# stdout/stderr 统一经 Write-Host 输出（Start-Transcript 会落盘），
# 再用 $LASTEXITCODE 判断真实成败。
# ---------------------------------------------------------------------------
function Invoke-Native {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $FilePath @Arguments 2>&1 | ForEach-Object { Write-Host $_ }
        return $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
}

$Root = Split-Path -Parent $PSScriptRoot          # packaging/ 的上一级 = 项目根
$SpecPath = Join-Path $PSScriptRoot 'WeChatTaskHub.spec'
$EntryPath = Join-Path $Root 'launcher.pyw'
$IconPath = Join-Path $Root 'assets\wechat-task.ico'
$BuildRequirements = Join-Path $Root 'requirements-build.txt'
$DistPath = Join-Path $Root 'dist'
$AppName = 'WeChatTaskHub'
$AppDist = Join-Path $DistPath $AppName
$WorkPath = Join-Path $Root 'build\pyinstaller'
$VersionInfoPath = Join-Path $PSScriptRoot 'build\version_info.txt'
$LogDir = Join-Path $Root 'output\packaging'

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$TranscriptStarted = $false
try {
    try {
        Start-Transcript -Path (Join-Path $LogDir 'build.log') -Force | Out-Null
        $TranscriptStarted = $true
    } catch {
        Write-Warning "无法启动构建日志转录（继续构建）：$($_.Exception.Message)"
    }

    Set-Location -LiteralPath $Root
    Write-Host "=============================================================="
    Write-Host " 微信待办 WeChatTaskHub · PyInstaller onedir 构建"
    Write-Host "   项目根 : $Root"
    Write-Host "   Python : $Python"
    Write-Host "=============================================================="

    # ------------------------------------------------------------------
    # 0. 前置检查
    # ------------------------------------------------------------------
    foreach ($required in @($SpecPath, $EntryPath, $IconPath)) {
        if (-not (Test-Path -LiteralPath $required)) {
            throw "缺少构建必需文件：$required"
        }
    }

    # ------------------------------------------------------------------
    # 1. 版本号：唯一事实源 app/version.py
    # ------------------------------------------------------------------
    $VersionSource = Get-Content -LiteralPath (Join-Path $Root 'app\version.py') -Raw -Encoding UTF8
    if ($VersionSource -notmatch '__version__\s*=\s*["'']([^"'']+)["'']') {
        throw "无法从 app/version.py 解析 __version__"
    }
    $Version = $Matches[1].Trim()
    # Windows 版本资源要求 4 段数字，例如 0.1.0 -> (0, 1, 0, 0)
    $VersionNumbers = @(
        [regex]::Matches($Version, '\d+') | ForEach-Object { [int]$_.Value } | Select-Object -First 4
    )
    if ($VersionNumbers.Count -eq 0) {
        throw "版本号无法转换为数字：$Version"
    }
    while ($VersionNumbers.Count -lt 4) { $VersionNumbers += 0 }
    $VersionTuple = ($VersionNumbers -join ', ')
    $BuildTime = (Get-Date).ToString('yyyy-MM-ddTHH:mm:sszzz')
    Write-Host "[1/6] 版本：$Version  filevers=($VersionTuple)  构建时间=$BuildTime"

    # ------------------------------------------------------------------
    # 2. 生成 Windows 版本资源 packaging/build/version_info.txt
    # ------------------------------------------------------------------
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $VersionInfoPath) | Out-Null
    $VersionInfoText = @"
# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=($VersionTuple),
    prodvers=($VersionTuple),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '080404b0',
        [StringStruct('CompanyName', 'WeChatTaskHub'),
         StringStruct('FileDescription', '微信待办 WeChat Task Hub'),
         StringStruct('FileVersion', '$Version'),
         StringStruct('InternalName', 'WeChatTaskHub'),
         StringStruct('LegalCopyright', 'Copyright (c) 2026 WeChatTaskHub'),
         StringStruct('OriginalFilename', 'WeChatTaskHub.exe'),
         StringStruct('ProductName', '微信待办'),
         StringStruct('ProductVersion', '$Version'),
         StringStruct('Comments', 'PyInstaller onedir build at $BuildTime')])
    ]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
"@
    # version_info.txt 必须是无 BOM 的 UTF-8：PyInstaller 按 UTF-8 解析，BOM 会被当成源码字符。
    [System.IO.File]::WriteAllText($VersionInfoPath, $VersionInfoText, (New-Object System.Text.UTF8Encoding($false)))
    Write-Host "[2/6] 版本资源：$VersionInfoPath"

    # ------------------------------------------------------------------
    # 3. 检查 / 安装 PyInstaller
    # ------------------------------------------------------------------
    $PyInstallerVersion = $null
    try {
        $probe = & $Python -c "import PyInstaller; print(PyInstaller.__version__)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $probe) {
            $PyInstallerVersion = (@($probe) | Select-Object -Last 1).ToString().Trim()
        }
    } catch {
        $PyInstallerVersion = $null
    }

    if (-not $PyInstallerVersion) {
        if ($SkipInstall) {
            throw "未检测到 PyInstaller（-SkipInstall 已指定）。请先执行：`n  $Python -m pip install -r $BuildRequirements"
        }
        Write-Host "[3/6] 未检测到 PyInstaller，安装构建依赖：$BuildRequirements"
        $pipArgs = @(
            '-m', 'pip',
            '--disable-pip-version-check',
            'install',
            '--no-warn-script-location',
            '-r', $BuildRequirements
        )
        $pipExit = Invoke-Native -FilePath $Python -Arguments $pipArgs
        if ($pipExit -ne 0) {
            throw "pip install -r requirements-build.txt 失败（exit=$pipExit）。若本机 Python 是 Windows Store 版，改用 python.org 安装包 + venv 后重试。"
        }
        $probe = & $Python -c "import PyInstaller; print(PyInstaller.__version__)" 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $probe) {
            throw "PyInstaller 安装后仍无法导入，请检查 $Python 环境。"
        }
        $PyInstallerVersion = (@($probe) | Select-Object -Last 1).ToString().Trim()
    }
    Write-Host "[3/6] PyInstaller：$PyInstallerVersion"

    # ------------------------------------------------------------------
    # 4. PyInstaller 构建（onedir）
    # ------------------------------------------------------------------
    if (Test-Path -LiteralPath $AppDist) {
        try {
            Remove-Item -LiteralPath $AppDist -Recurse -Force
        } catch {
            throw "无法清理旧产物 $AppDist（可能 WeChatTaskHub.exe 仍在运行，请先关闭）：$($_.Exception.Message)"
        }
    }
    New-Item -ItemType Directory -Force -Path $DistPath | Out-Null

    # 版本资源路径交给 spec 读取（spec 内读取 WECHAT_TASK_VERSION_FILE）。
    $env:WECHAT_TASK_VERSION_FILE = $VersionInfoPath
    # 供 app/version.py 在打包时注入构建信息（版本唯一事实源仍是 app/version.py）。
    $env:WECHAT_TASK_VERSION = $Version
    $env:WECHAT_TASK_BUILD_TIME = $BuildTime

    Write-Host "[4/6] 运行 PyInstaller（onedir，约 1-3 分钟）..."
    $PyInstallerArgs = @(
        '-m', 'PyInstaller',
        '--clean', '--noconfirm',
        '--distpath', $DistPath,
        '--workpath', $WorkPath,
        $SpecPath
    )
    $pyiExit = Invoke-Native -FilePath $Python -Arguments $PyInstallerArgs
    if ($pyiExit -ne 0) {
        throw "PyInstaller 构建失败（exit=$pyiExit），完整日志见 $LogDir\build.log"
    }
    $ExePath = Join-Path $AppDist "$AppName.exe"
    if (-not (Test-Path -LiteralPath $ExePath)) {
        throw "PyInstaller 未生成 $ExePath，请检查上面的日志。"
    }
    Write-Host "[4/6] 已生成：$ExePath"

    # ------------------------------------------------------------------
    # 5. 外置资源：web\ assets\ tools\ + .env.example + README.md + 启动说明.txt
    #    PyInstaller 已把 web/assets 打进 _internal 作兜底，这里再放一份到
    #    exe 同级，前端可热更新；tools 只外置（二进制约 88MB，不进包）。
    # ------------------------------------------------------------------
    Copy-Item -LiteralPath (Join-Path $Root 'web') -Destination (Join-Path $AppDist 'web') -Recurse -Force
    Copy-Item -LiteralPath (Join-Path $Root 'assets') -Destination (Join-Path $AppDist 'assets') -Recurse -Force
    Copy-Item -LiteralPath (Join-Path $Root 'tools') -Destination (Join-Path $AppDist 'tools') -Recurse -Force
    Copy-Item -LiteralPath (Join-Path $Root '.env.example') -Destination (Join-Path $AppDist '.env.example') -Force
    Copy-Item -LiteralPath (Join-Path $Root 'README.md') -Destination (Join-Path $AppDist 'README.md') -Force
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot '启动说明.txt') -Destination (Join-Path $AppDist '启动说明.txt') -Force

    # 清理复制过来的 Python 缓存，保持分发目录干净。
    Get-ChildItem -LiteralPath $AppDist -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

    foreach ($tool in @('tools\ngrok\ngrok.exe', 'tools\cloudflared.exe', 'tools\tunnel.py')) {
        if (-not (Test-Path -LiteralPath (Join-Path $AppDist $tool))) {
            Write-Warning "外置工具缺失：$tool（隧道功能可能不可用）"
        }
    }
    Write-Host "[5/6] 已外置 web\、assets\、tools\、.env.example、README.md、启动说明.txt"

    # ------------------------------------------------------------------
    # 6. 打发布压缩包 releases\WeChatTaskHub-<版本>-win64.zip
    #    排除 data\、.env、__pycache__、*.log（运行数据不能带进发布包）。
    # ------------------------------------------------------------------
    $ZipPath = $null
    if (-not $NoZip) {
        $ReleasesDir = Join-Path $Root 'releases'
        New-Item -ItemType Directory -Force -Path $ReleasesDir | Out-Null
        $ZipPath = Join-Path $ReleasesDir "$AppName-$Version-win64.zip"
        if (Test-Path -LiteralPath $ZipPath) { Remove-Item -LiteralPath $ZipPath -Force }

        # .NET 的 ZipFile / ZipArchiveMode / CompressionLevel 分属不同程序集：
        # pwsh 7 需要 System.IO.Compression，Windows PowerShell 5.1 还需要
        # System.IO.Compression.FileSystem，两个都尝试加载（缺一个也不致命）。
        foreach ($assembly in @('System.IO.Compression', 'System.IO.Compression.FileSystem')) {
            try { Add-Type -AssemblyName $assembly -ErrorAction Stop } catch { }
        }
        $archive = [System.IO.Compression.ZipFile]::Open($ZipPath, [System.IO.Compression.ZipArchiveMode]::Create)
        try {
            $baseLength = $AppDist.Length + 1
            foreach ($file in Get-ChildItem -LiteralPath $AppDist -Recurse -File -Force) {
                $relative = $file.FullName.Substring($baseLength)
                if ($relative -match '(^|\\)__pycache__(\\|$)') { continue }
                if ($relative -eq '.env') { continue }
                if ($relative -match '^data(\\|$)') { continue }
                if ($relative -match '\.log$') { continue }
                $entryName = $relative -replace '\\', '/'
                [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
                    $archive, $file.FullName, $entryName,
                    [System.IO.Compression.CompressionLevel]::Optimal) | Out-Null
            }
        } finally {
            $archive.Dispose()
        }
        $zipSize = [math]::Round((Get-Item -LiteralPath $ZipPath).Length / 1MB, 1)
        Write-Host "[6/6] 发布包：$ZipPath（$zipSize MB）"
    } else {
        Write-Host "[6/6] 已指定 -NoZip，跳过压缩包"
    }

    # ------------------------------------------------------------------
    # 收尾：最终目录树 + 下一步提示
    # ------------------------------------------------------------------
    Write-Host ""
    Write-Host "=============================================================="
    Write-Host " 构建完成：$Version"
    Write-Host "=============================================================="
    Write-Host "dist\$AppName\："

    function Show-Tree {
        param([string]$Path, [int]$Depth, [string]$Prefix)
        $items = Get-ChildItem -LiteralPath $Path -Force |
            Sort-Object @{ Expression = { $_.PSIsContainer }; Descending = $true }, Name
        foreach ($item in $items) {
            if ($item.PSIsContainer) {
                if ($item.Name -eq '_internal') {
                    $count = @(Get-ChildItem -LiteralPath $item.FullName -Recurse -File -Force -ErrorAction SilentlyContinue).Count
                    Write-Host ("{0}{1}\  ({2} files)" -f $Prefix, $item.Name, $count)
                    continue
                }
                $count = @(Get-ChildItem -LiteralPath $item.FullName -Recurse -File -Force -ErrorAction SilentlyContinue).Count
                Write-Host ("{0}{1}\  ({2} files)" -f $Prefix, $item.Name, $count)
                if ($Depth -gt 1) {
                    Show-Tree -Path $item.FullName -Depth ($Depth - 1) -Prefix ($Prefix + '  ')
                }
            } else {
                $sizeText = '{0:N1} KB' -f ($item.Length / 1KB)
                if ($item.Length -ge 1MB) { $sizeText = '{0:N1} MB' -f ($item.Length / 1MB) }
                Write-Host ("{0}{1}  ({2})" -f $Prefix, $item.Name, $sizeText)
            }
        }
    }
    Show-Tree -Path $AppDist -Depth 2 -Prefix '  '

    Write-Host ""
    Write-Host "下一步："
    Write-Host "  1) 本地试运行：  & '.\dist\$AppName\$AppName.exe'"
    Write-Host "                 浏览器打开 http://127.0.0.1:8765/ ，日志在 dist\$AppName\data\"
    Write-Host "  2) 停止服务：    & '.\dist\$AppName\$AppName.exe' --stop"
    Write-Host "  3) 冒烟检查：    & '.\dist\$AppName\$AppName.exe' --serve  (前台阻塞，Ctrl+C 退出)"
    if ($ZipPath) { Write-Host "  4) 分发：把 $ZipPath 发给用户解压运行；首次运行自动生成 data\ 与 .env。" }
    Write-Host "  5) 升级：只替换 WeChatTaskHub.exe、_internal\、web\、assets\、tools\，保留 data\ 与 .env。"
    Write-Host "  6) 前端热更新：只替换 dist\$AppName\web\ 即可，无需重新打包。"
    Write-Host "=============================================================="
} finally {
    if ($TranscriptStarted) {
        try { Stop-Transcript | Out-Null } catch { }
    }
}
