# 在桌面创建「微信待办」启动/停止快捷方式（不打包成应用）
# 用法：右键 →「使用 PowerShell 运行」，或在项目目录执行 .\scripts\install_shortcuts.ps1

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot   # scripts\ 的上一级 = 项目根
$launcher = Join-Path $root "launcher.pyw"
$icon = Join-Path $root "assets\wechat-task.ico"

if (-not (Test-Path $launcher)) { throw "找不到 launcher.pyw" }

# 找到 pythonw.exe（没有控制台窗口）
$pythonw = $null
try {
    $pythonExe = (python -c "import sys; print(sys.executable)").Trim()
    $candidate = Join-Path (Split-Path $pythonExe -Parent) "pythonw.exe"
    if (Test-Path $candidate) { $pythonw = $candidate }
} catch {}
if (-not $pythonw) {
    $cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if ($cmd) { $pythonw = $cmd.Source }
}
if (-not $pythonw) { throw "找不到 pythonw.exe，请确认 Python 已安装并加入 PATH" }

$desktop = [Environment]::GetFolderPath("Desktop")
$shell = New-Object -ComObject WScript.Shell

function New-Shortcut($path, $arguments, $description) {
    $shortcut = $shell.CreateShortcut($path)
    $shortcut.TargetPath = $pythonw
    $shortcut.Arguments = "`"$launcher`" $arguments".Trim()
    $shortcut.WorkingDirectory = $root
    $shortcut.IconLocation = "$icon,0"
    $shortcut.Description = $description
    $shortcut.Save()
    Write-Host "[OK] $path" -ForegroundColor Green
}

New-Shortcut (Join-Path $desktop "微信待办.lnk") "" "启动微信待办并打开任务表"
New-Shortcut (Join-Path $desktop "停止微信待办.lnk") "--stop" "停止微信待办后台服务"

# 开始菜单也放一份（可选）
$startMenu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\微信待办"
New-Item -ItemType Directory -Force -Path $startMenu | Out-Null
New-Shortcut (Join-Path $startMenu "微信待办.lnk") "" "启动微信待办并打开任务表"
New-Shortcut (Join-Path $startMenu "停止微信待办.lnk") "--stop" "停止微信待办后台服务"

Write-Host ""
Write-Host "完成。桌面和开始菜单都已有快捷方式。" -ForegroundColor Cyan
Write-Host "首次双击「微信待办」会自动启动服务并打开浏览器；如果 WeChatDataAnalysis 没开，会自动尝试启动它。"
