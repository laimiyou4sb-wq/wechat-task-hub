# 注册开机自启（当前用户，无需管理员），并尽力放行防火墙 8765 端口
# 用法：右键“使用 PowerShell 运行”
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot   # scripts\ 的上一级 = 项目根
$pythonw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $pythonw) { $pythonw = (Get-Command python.exe).Source }

$action = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$root\run.py`"" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn
Register-ScheduledTask -TaskName "WeChatTaskHub" -Action $action -Trigger $trigger `
    -Description "微信待办后台服务：监听微信消息并生成待办" -Force | Out-Null
Write-Host "[OK] 已注册计划任务 WeChatTaskHub（登录后自动运行）" -ForegroundColor Green
Write-Host "     取消自启：Unregister-ScheduledTask -TaskName WeChatTaskHub -Confirm:`$false"

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if ($isAdmin) {
    try {
        if (-not (Get-NetFirewallRule -DisplayName "WeChatTaskHub 8765" -ErrorAction SilentlyContinue)) {
            New-NetFirewallRule -DisplayName "WeChatTaskHub 8765" -Direction Inbound -Protocol TCP `
                -LocalPort 8765 -Action Allow -Profile Private | Out-Null
        }
        Write-Host "[OK] 已放行防火墙 8765（仅专用网络）" -ForegroundColor Green
    } catch { Write-Host "[WARN] 防火墙规则添加失败：$_" -ForegroundColor Yellow }
} else {
    Write-Host "[提示] 非管理员运行，未改防火墙；手机访问不了时请手动放行 TCP 8765 或改用 ngrok 隧道。" -ForegroundColor Yellow
}

# 显示手机访问地址
$ip = (Get-NetIPAddress -AddressFamily IPv4 |
    Where-Object { $_.IPAddress -notlike "127.*" -and $_.PrefixOrigin -ne "WellKnown" } |
    Select-Object -First 1).IPAddress
Write-Host "手机访问： http://${ip}:8765/?token=<你的 APP_TOKEN>" -ForegroundColor Cyan
