# 演示：向正在运行的服务写入虚构消息并生成待办
# 用法：先 python run.py --demo，再运行本脚本
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot   # scripts\ 的上一级 = 项目根
$envFile = Join-Path $root ".env"
$token = "wechat-tasks"
$port = "8765"
if (Test-Path $envFile) {
    foreach ($line in Get-Content $envFile) {
        if ($line -match "^\s*APP_TOKEN\s*=\s*(.+)$") { $token = $matches[1].Trim().Trim('"').Trim("'") }
        if ($line -match "^\s*PORT\s*=\s*(\d+)") { $port = $matches[1] }
    }
}
$url = "http://127.0.0.1:$port/api/demo/seed"
$result = Invoke-RestMethod -Method Post -Uri $url -Headers @{ "X-Auth-Token" = $token }
$result | ConvertTo-Json -Depth 5
Write-Host "打开 http://127.0.0.1:$port/?token=$token 查看任务表" -ForegroundColor Cyan
