<#
.SYNOPSIS
  微信待办 · ngrok 固定域名配置向导（一次性，可重复运行）

.DESCRIPTION
  把「手机在外网访问电脑上的微信待办」配好，步骤：
    1. 确保 tools\ngrok\ngrok.exe 存在（没有就自动下载官方 Windows 版）
    2. 引导输入 ngrok authtoken（免费账号即可；也可用 -Authtoken 或 .env 里的 NGROK_AUTHTOKEN）
    3. 写 %LOCALAPPDATA%\ngrok\ngrok.yml（version: 3 + authtoken + tunnels.wxtask → 127.0.0.1:8765 + 可选固定域名）
    4. 把 TUNNEL_ENABLED=1 / NGROK_DOMAIN 写进项目 .env（用 -NoEnv 可跳过）
    5. 可选：真实公网连通性验证；可选：注册开机自启计划任务（用 -NoTask 跳过）

  之后双击桌面「微信待办」时，launcher.pyw 会自动把隧道一起拉起来。
  本脚本幂等：可以反复运行来换 token / 换域名。

.PARAMETER Authtoken
  ngrok authtoken；不填则按 环境变量 → .env → 交互输入 的顺序取。

.PARAMETER Domain
  固定 dev domain（形如 xxx.ngrok-free.dev 或 xxx.ngrok-free.app）；不填则按 环境变量 → .env 取，
  都没有就留空 = 自动用 ngrok 账号分配的那个免费 dev domain（也是固定的，推荐不填）。
  注意：非交互环境（脚本调用/无 TTY）不会弹提示，直接走「留空自动分配」，避免卡住。

.PARAMETER NoTask
  不提示注册开机自启计划任务。

.PARAMETER NoEnv
  不修改项目 .env。

.PARAMETER SkipVerify
  跳过「真实公网连通性验证」。

.PARAMETER Verify
  不询问，直接做「真实公网连通性验证」（适合脚本 / 非交互环境）。

.PARAMETER UninstallTask
  只卸载计划任务 WeChatTaskHubTunnel，然后退出。

.PARAMETER ConfigPath
  可选：写入哪个 ngrok.yml（默认 %LOCALAPPDATA%\ngrok\ngrok.yml）。
  主要用于测试/多实例；正常使用不要改。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\tools\ngrok_setup.ps1

.EXAMPLE
  # 非交互（脚本/远程执行）：-Authtoken + -NoTask 即可，不会卡在任何提问
  powershell -ExecutionPolicy Bypass -File .\tools\ngrok_setup.ps1 -Authtoken <你的token> -NoTask

.EXAMPLE
  # 非交互 + 自动做一次真实公网验证；域名留空 = 用账号自动分配的 dev domain
  powershell -ExecutionPolicy Bypass -File .\tools\ngrok_setup.ps1 -Authtoken <你的token> -NoTask -Verify
#>
[CmdletBinding()]
param(
    [string]$Authtoken = "",
    [string]$Domain = "",
    [switch]$NoTask,
    [switch]$NoEnv,
    [switch]$SkipVerify,
    [switch]$Verify,
    [switch]$UninstallTask,
    [string]$ConfigPath = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$ToolDir = Join-Path $Root "tools"
$NgrokDir = Join-Path $ToolDir "ngrok"
$NgrokExe = Join-Path $NgrokDir "ngrok.exe"
$NgrokZipUrl = "https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-windows-amd64.zip"
$NgrokZipFallback = "https://github.com/ngrok/ngrok/releases/latest/download/ngrok-v3-stable-windows-amd64.zip"
$NgrokConfig = Join-Path $env:LOCALAPPDATA "ngrok\ngrok.yml"
if ($ConfigPath) {
    $resolvedConfig = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($ConfigPath)
    $NgrokConfig = $resolvedConfig
}
$EnvFile = Join-Path $Root ".env"
$TaskName = "WeChatTaskHubTunnel"
$DashboardDomains = "https://dashboard.ngrok.com/domains"
$DashboardToken = "https://dashboard.ngrok.com/get-started/your-authtoken"

function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host "== $Message ==" -ForegroundColor Cyan
}
function Write-Ok([string]$Message) {
    Write-Host "[OK] $Message" -ForegroundColor Green
}
function Write-Note([string]$Message) {
    Write-Host "[提示] $Message" -ForegroundColor Yellow
}
function Fail([string]$Message) {
    Write-Host ""
    Write-Host "[ERROR] $Message" -ForegroundColor Red
    Write-Host "如果问题持续：见 docs\手机同步.md 的常见问题，或把上面的输出发给维护者。" -ForegroundColor Red
    exit 1
}
function Read-Answer([string]$Prompt) {
    # 交互输入的兜底：
    #  - 有 TTY：正常返回用户输入；
    #  - 非交互（-NonInteractive / 无控制台）：Read-Host 可能抛异常，也可能返回 $null（EOF）。
    #    两种情况都统一返回空字符串，绝不把 $null 传给调用方（否则 .Trim() 会报 null 方法错误）。
    $answer = ""
    try {
        $answer = Read-Host $Prompt
    } catch {
        Write-Note "非交互环境，跳过询问：$Prompt"
    }
    if ($null -eq $answer) {
        if (-not $script:NonInteractiveNoted) {
            Write-Note "当前是非交互环境（读不到键盘输入），需要人工确认的步骤会按「默认/跳过」处理"
            $script:NonInteractiveNoted = $true
        }
        return ""
    }
    return [string]$answer
}
function Get-EnvValue([string]$Path, [string]$Key) {
    if (-not (Test-Path $Path)) { return "" }
    $pattern = "^\s*" + [regex]::Escape($Key) + "\s*=\s*(.*)$"
    foreach ($line in (Get-Content -Path $Path -Encoding UTF8)) {
        if ($line -match $pattern) {
            return ($matches[1].Trim().Trim('"').Trim("'"))
        }
    }
    return ""
}
function Set-EnvValue([string]$Path, [string]$Key, [string]$Value) {
    $lines = @()
    if (Test-Path $Path) { $lines = @(Get-Content -Path $Path -Encoding UTF8) }
    $output = New-Object System.Collections.Generic.List[string]
    $found = $false
    $pattern = "^\s*" + [regex]::Escape($Key) + "\s*="
    foreach ($line in $lines) {
        if (-not $found -and $line -match $pattern) {
            $output.Add("$Key=$Value")
            $found = $true
        } else {
            $output.Add($line)
        }
    }
    if (-not $found) {
        if ($output.Count -gt 0 -and $output[$output.Count - 1].Trim() -ne "") { $output.Add("") }
        $output.Add("$Key=$Value")
    }
    [System.IO.File]::WriteAllLines($Path, $output, (New-Object System.Text.UTF8Encoding($false)))
}
function Test-LocalHealth {
    try {
        $response = Invoke-RestMethod -Uri "http://127.0.0.1:8765/api/health" -TimeoutSec 4
        return [bool]$response.ok
    } catch {
        return $false
    }
}
function Get-NgrokApiTunnels {
    try {
        return Invoke-RestMethod -Uri "http://127.0.0.1:4040/api/tunnels" -TimeoutSec 2
    } catch {
        return $null
    }
}

Write-Host ""
Write-Host "微信待办 · ngrok 固定域名配置向导" -ForegroundColor White
Write-Host "项目目录：$Root"

# ---------------------------------------------------------------------------
# 0. 只卸载计划任务
# ---------------------------------------------------------------------------
if ($UninstallTask) {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($task) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Ok "已删除计划任务 $TaskName（ngrok.yml 和 .env 保留，不影响手动启动）"
    } else {
        Write-Host "没有找到计划任务 $TaskName，无需卸载。"
    }
    exit 0
}

# ---------------------------------------------------------------------------
# 1. ngrok.exe 就位
# ---------------------------------------------------------------------------
Write-Step "1/5 检查 ngrok.exe"
if (-not (Test-Path $NgrokExe)) {
    New-Item -ItemType Directory -Force -Path $NgrokDir | Out-Null
    $zipPath = Join-Path $env:TEMP "ngrok-v3-stable-windows-amd64.zip"
    Write-Host "未找到 $NgrokExe，开始下载官方 Windows 版（约 12MB）…"
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $downloaded = $false
    try {
        Invoke-WebRequest -Uri $NgrokZipUrl -OutFile $zipPath -UseBasicParsing
        $downloaded = $true
    } catch {
        Write-Note "官方 CDN 下载失败，尝试 GitHub releases 备用源…"
    }
    if (-not $downloaded) {
        try {
            Invoke-WebRequest -Uri $NgrokZipFallback -OutFile $zipPath -UseBasicParsing
            $downloaded = $true
        } catch {
            Fail "下载 ngrok 失败：$($_.Exception.Message)`n请手动下载后解压：$NgrokZipUrl`n目标路径：$NgrokExe"
        }
    }
    try {
        Expand-Archive -Path $zipPath -DestinationPath $NgrokDir -Force
    } catch {
        Fail "解压 ngrok 失败：$($_.Exception.Message)`n请手动把 ngrok.exe 放到 $NgrokDir"
    }
    Remove-Item $zipPath -ErrorAction SilentlyContinue
}
try {
    $ngrokVersion = (& $NgrokExe version 2>&1 | Out-String).Trim()
} catch {
    Fail "ngrok.exe 无法运行：$($_.Exception.Message)`n文件：$NgrokExe"
}
if ($LASTEXITCODE -ne 0 -or -not $ngrokVersion) {
    Fail "ngrok.exe 无法运行（exit=$LASTEXITCODE）。输出：$ngrokVersion"
}
Write-Ok "ngrok 就绪：$ngrokVersion"

# ---------------------------------------------------------------------------
# 2. authtoken
# ---------------------------------------------------------------------------
Write-Step "2/5 ngrok authtoken（免费账号）"
$token = $Authtoken
if (-not $token) { $token = $env:NGROK_AUTHTOKEN }
if (-not $token) { $token = Get-EnvValue $EnvFile "NGROK_AUTHTOKEN" }
if (-not $token) {
    Write-Host "还没有 authtoken，先花 1 分钟注册（免费，不用信用卡）："
    Write-Host "  1) 打开 $DashboardToken"
    Write-Host "  2) 注册 / 登录（支持 GitHub / Google / 邮箱）"
    Write-Host "  3) 复制页面上的 Your Authtoken"
    $token = Read-Answer "把 authtoken 粘贴到这里（输入内容会显示）"
}
$token = ([string]$token) -replace "\s", ""
$token = $token.Trim()
if ([string]::IsNullOrWhiteSpace($token)) {
    Fail "authtoken 不能为空。非交互模式请用 -Authtoken 参数（或设置环境变量/.env 的 NGROK_AUTHTOKEN）；交互模式到 $DashboardToken 复制后重新运行。"
}
if ($token.Length -lt 30) {
    Write-Note "authtoken 长度只有 $($token.Length) 个字符，正常应该更长；如果验证失败请重新复制。"
}
Write-Ok "已拿到 authtoken（长度 $($token.Length)，不打印明文）"

# ---------------------------------------------------------------------------
# 3. 固定域名（免费 dev domain）
#
# 非交互安全：这里**不弹提示**。域名来源只有「-Domain 参数 → 环境变量 → .env」，
# 都没有就留空，交给 ngrok 自动使用账号分配的那个固定 dev domain（效果一样，也固定）。
# ---------------------------------------------------------------------------
Write-Step "3/5 固定域名（免费 dev domain）"
$domainValue = [string]$Domain
if (-not $domainValue) { $domainValue = [string]$env:NGROK_DOMAIN }
if (-not $domainValue) { $domainValue = [string](Get-EnvValue $EnvFile "NGROK_DOMAIN") }
$domainValue = $domainValue.Trim()
$domainValue = $domainValue -replace "^https?://", ""
$domainValue = $domainValue.Trim("/").ToLower()
if ($domainValue -and ($domainValue -notmatch "^[a-z0-9][a-z0-9.-]*$")) {
    Fail "域名格式不对：'$domainValue'。应该只填主机名，例如 my-pc.ngrok-free.dev（或 my-pc.ngrok-free.app），不要带 https:// 或路径"
}
if ($domainValue) {
    Write-Ok "固定域名：$domainValue"
} else {
    Write-Ok "固定域名：未指定 → 自动使用 ngrok 账号分配的那个 dev domain（固定不变）"
    Write-Host "      查看/改名：$DashboardDomains"
    Write-Host "      想显式指定时加参数：-Domain your-name.ngrok-free.dev（一般不需要）"
}

# ---------------------------------------------------------------------------
# 4. 写 ngrok.yml
# ---------------------------------------------------------------------------
Write-Step "4/5 写入 ngrok 配置"
$configDir = Split-Path -Parent $NgrokConfig
New-Item -ItemType Directory -Force -Path $configDir | Out-Null
if (Test-Path $NgrokConfig) {
    $backup = "$NgrokConfig.bak-" + (Get-Date -Format "yyyyMMddHHmmss")
    Copy-Item -Path $NgrokConfig -Destination $backup -Force
    Write-Note "已有 ngrok 配置，已备份到：$backup（如果里面还有别的隧道，需要手动合并回去）"
}
$yamlLines = New-Object System.Collections.Generic.List[string]
$yamlLines.Add("version: 3")
$yamlLines.Add("agent:")
$yamlLines.Add("  authtoken: `"$token`"")
$yamlLines.Add("tunnels:")
$yamlLines.Add("  wxtask:")
$yamlLines.Add("    proto: http")
$yamlLines.Add("    addr: 8765")
if ($domainValue) {
    $yamlLines.Add("    domain: $domainValue")
}
$yamlText = ($yamlLines -join "`r`n") + "`r`n"
[System.IO.File]::WriteAllText($NgrokConfig, $yamlText, (New-Object System.Text.UTF8Encoding($false)))
Write-Host "已写入：$NgrokConfig"

$checkOutput = (& $NgrokExe config check --config $NgrokConfig 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0) {
    Fail "ngrok 配置校验失败：`n$checkOutput"
}
Write-Ok "ngrok config check 通过"
Write-Host "      （authtoken 保存在这个 yml 里，属于本机个人配置，不要发给别人/截图外传）"

$envUpdated = $false
if (-not $NoEnv) {
    if (-not (Test-Path $EnvFile)) {
        $example = Join-Path $Root ".env.example"
        if (Test-Path $example) {
            Copy-Item -Path $example -Destination $EnvFile
            Write-Note "没有 .env，已从 .env.example 复制一份（其中的 APP_TOKEN 记得改成自己的随机串）"
        } else {
            New-Item -ItemType File -Path $EnvFile | Out-Null
        }
    }
    Set-EnvValue $EnvFile "TUNNEL_ENABLED" "1"
    if ($domainValue) {
        Set-EnvValue $EnvFile "NGROK_DOMAIN" $domainValue
    }
    $oldBaseUrl = Get-EnvValue $EnvFile "APP_BASE_URL"
    if ($oldBaseUrl -and ($oldBaseUrl -match "\.ts\.net")) {
        Set-EnvValue $EnvFile "APP_BASE_URL" ""
        Write-Note "已清空 .env 里的 APP_BASE_URL（原来指向旧的 Tailscale 地址；远程地址改由 ngrok 提供）"
    }
    $envUpdated = $true
    Write-Ok ".env 已更新：TUNNEL_ENABLED=1"
    if ($domainValue) { Write-Host "      NGROK_DOMAIN=$domainValue" }
    Write-Note ".env 改动需要重启服务才生效：桌面「停止微信待办」→ 再双击「微信待办」"
} else {
    Write-Note "按 -NoEnv 要求，未修改 .env。请手动确认 .env 里有 TUNNEL_ENABLED=1"
}

# ---------------------------------------------------------------------------
# 5. 可选：真实公网连通性验证
# ---------------------------------------------------------------------------
$existingTunnel = Get-NgrokApiTunnels
if (-not $SkipVerify -and $existingTunnel) {
    Write-Step "5/5 跳过在线验证（已有 ngrok 隧道在运行）"
    Write-Note "请用：python tools\tunnel.py --check 查看现有隧道状态"
} elseif (-not $SkipVerify) {
    $doVerify = [bool]$Verify
    if (-not $doVerify) {
        Write-Host ""
        $verifyAnswer = Read-Answer "现在做一次真实公网验证吗？（自动启停一次 ngrok，约 15 秒）[y/N]"
        if ($verifyAnswer -match "^(y|yes|是)$") { $doVerify = $true }
    }
    if ($doVerify) {
        Write-Step "5/5 真实公网连通性验证"
        $serviceUp = Test-LocalHealth
        if (-not $serviceUp) {
            Write-Note "本机 8765 服务没在运行，跳过在线验证。先双击桌面「微信待办」启动服务，再运行 python tools\tunnel.py --check"
        } else {
            $verifyLog = Join-Path $env:TEMP "ngrok-verify.log"
            Remove-Item $verifyLog -ErrorAction SilentlyContinue
            $ngrokArgs = @("http", "8765", "--log", "stdout", "--log-format", "logfmt", "--config", $NgrokConfig)
            if ($domainValue) { $ngrokArgs += @("--url", "https://$domainValue") }
            $ngrokProcess = Start-Process -FilePath $NgrokExe -ArgumentList $ngrokArgs `
                -RedirectStandardOutput $verifyLog -RedirectStandardError "$verifyLog.err" `
                -PassThru -WindowStyle Hidden
            try {
                $publicUrl = ""
                for ($i = 0; $i -lt 30; $i++) {
                    Start-Sleep -Seconds 1
                    if ($ngrokProcess.HasExited) { break }
                    $tunnels = Get-NgrokApiTunnels
                    if ($tunnels -and $tunnels.tunnels) {
                        foreach ($tunnel in $tunnels.tunnels) {
                            if ($tunnel.public_url -like "https://*") { $publicUrl = $tunnel.public_url; break }
                        }
                    }
                    if ($publicUrl) { break }
                }
                if (-not $publicUrl) {
                    Write-Host ""
                    Write-Host "验证失败：没有拿到公网地址。" -ForegroundColor Red
                    if (Test-Path $verifyLog) { Write-Host "ngrok 日志尾部：" ; Get-Content $verifyLog -Tail 15 }
                    if (Test-Path "$verifyLog.err") { Get-Content "$verifyLog.err" -Tail 15 }
                    Fail "ngrok 没能建立隧道（常见原因：authtoken 无效 / 域名不属于该账号 / 网络被拦截）"
                }
                Write-Host "公网地址：$publicUrl"
                [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
                $healthUrl = "$publicUrl/api/health"
                $ok = $false
                for ($i = 0; $i -lt 6; $i++) {
                    try {
                        $health = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 15 -Headers @{ "ngrok-skip-browser-warning" = "1" }
                        if ($health.ok) { $ok = $true; break }
                    } catch {
                        Start-Sleep -Seconds 2
                    }
                }
                if ($ok) {
                    Write-Ok "公网链路验证通过：$healthUrl 返回 ok=true"
                    Write-Host "      手机上用这个地址（带 token）即可在外网打开：$publicUrl/?token=<APP_TOKEN>"
                } else {
                    Write-Host ""
                    Write-Host "公网地址拿到了（$publicUrl），但 /api/health 没有返回 ok=true。" -ForegroundColor Red
                    Fail "若本机服务正常，可能是 ngrok 账号/域名配置问题；详见 docs\手机同步.md 常见问题"
                }
            } finally {
                if ($ngrokProcess -and -not $ngrokProcess.HasExited) {
                    Stop-Process -Id $ngrokProcess.Id -Force -ErrorAction SilentlyContinue
                }
                Start-Sleep -Milliseconds 500
                Remove-Item $verifyLog -ErrorAction SilentlyContinue
                Remove-Item "$verifyLog.err" -ErrorAction SilentlyContinue
            }
        }
    } else {
        Write-Host "跳过在线验证。之后随时可以用：python tools\tunnel.py --check"
        Write-Note "非交互环境可直接加 -Verify 参数自动验证；或先双击「微信待办」再运行上面的检查命令"
    }
} else {
    Write-Step "5/5 已按 -SkipVerify 跳过在线验证"
}

# ---------------------------------------------------------------------------
# 可选：开机自启计划任务
# ---------------------------------------------------------------------------
if (-not $NoTask) {
    Write-Host ""
    $taskAnswer = Read-Answer "注册开机自启计划任务（登录后自动启动隧道）？一般不需要，桌面快捷方式会拉起隧道 [y/N]"
    if ($taskAnswer -match "^(y|yes|是)$") {
        try {
            $pythonw = $null
            $pythonExe = (Get-Command python -ErrorAction SilentlyContinue).Source
            if ($pythonExe) {
                $candidate = Join-Path (Split-Path $pythonExe -Parent) "pythonw.exe"
                if (Test-Path $candidate) { $pythonw = $candidate }
            }
            if (-not $pythonw) {
                $command = Get-Command pythonw.exe -ErrorAction SilentlyContinue
                if ($command) { $pythonw = $command.Source }
            }
            if (-not $pythonw) { Fail "找不到 pythonw.exe，无法注册计划任务" }
            $action = New-ScheduledTaskAction -Execute $pythonw `
                -Argument "`"$Root\tools\tunnel.py`"" -WorkingDirectory $Root
            $trigger = New-ScheduledTaskTrigger -AtLogOn
            Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
                -Description "微信待办后台隧道（ngrok）：把本机 8765 暴露到固定公网地址" -Force | Out-Null
            Write-Ok "已注册计划任务 $TaskName（登录后自动运行 tools\tunnel.py）"
            Write-Host "      卸载：powershell -ExecutionPolicy Bypass -File .\tools\ngrok_setup.ps1 -UninstallTask"
        } catch {
            Write-Note "注册计划任务失败：$($_.Exception.Message)（不影响手动启动，可忽略）"
        }
    } else {
        Write-Host "未注册计划任务：双击桌面「微信待办」时会自动拉起隧道。"
    }
}

# ---------------------------------------------------------------------------
# 总结
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "================ 配置完成 ================" -ForegroundColor Green
Write-Host "ngrok 可执行文件：$NgrokExe"
Write-Host "ngrok 配置     ：$NgrokConfig"
if ($domainValue) { Write-Host "固定域名       ：https://$domainValue" }
Write-Host ""
Write-Host "接下来："
Write-Host "  1. 双击桌面「微信待办」→ 服务 + 隧道一起启动，浏览器打开任务表"
Write-Host "  2. 等 10~20 秒后检查隧道：python tools\tunnel.py --check"
Write-Host "  3. 打开网页右上角 ⚙ →「手机 App」卡片 → 复制配对码，粘贴到手机 App"
Write-Host "  4. 手机在外网时 App 会自动使用 https://<你的域名>（局域网时优先直连）"
Write-Host ""
Write-Host "详细教程：docs\手机同步.md"
Write-Host "查看/停止隧道：python tools\tunnel.py --check / --stop"
Write-Host "免费额度提醒：1GB 流量/月、20000 请求/月；不够可升级或改用局域网直连。"
Write-Host ""
