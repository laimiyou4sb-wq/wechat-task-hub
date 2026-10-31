# 一键部署 · 交给 AI Agent 的提示词（含人工辅助清单）

> **适用版本**：仓库 `main`（v0.2.0，对应提交 `8539b0c`，2026-10）。
> **适用平台**：Windows 10/11 x64（本项目只支持 Windows；Python 3.10+，已在 3.13 验证）。
> **思路**：把「环境检查 → 克隆代码 → 装依赖 → 生成配置 → 启动服务 → 验收 → 可选 ngrok 远程」交给一个**能执行 PowerShell 的 AI Agent**；
> 需要你本人动手的注册、扫码登录、装软件、手机操作、UAC 提权，全部单列在 [§3](#3-需要人工辅助的步骤单列)，Agent 会在需要时停下来找你要。

---

## 0. 30 秒上手

1. 准备一台 Windows 10/11 电脑，能上网。没装 Python/Git 也没关系：Agent 第 P0 步会检测并让你装（命令见 **M1**）。
2. 打开一个**能执行命令**的 AI Agent（Claude Code、Codex、DeepSeek Harness、Cursor、Cline 等），把 [§1 的整段提示词](#1-提示词复制下面整段给-agent) 粘进去。
3. 回答它的 5 个配置问题：数据源用 `demo` 还是 `wcda`、有没有 LLM Key、要不要外网（ngrok）、要不要推送、要不要桌面快捷方式。**不确定就说「用默认」**。
4. 它说「【需要你操作】」时，对照 [§3](#3-需要人工辅助的步骤单列) 完成对应项，再回复它。
5. 它给出最终报告，且浏览器打开 `http://127.0.0.1:8765/?token=<APP_TOKEN>` 能看到任务表 → 部署完成。

> ⚠️ **安全底线**
> - Agent 会拿到你本机的命令权限，并接触 `APP_TOKEN`、LLM Key、ngrok authtoken，**只在你自己的机器上跑**。
> - 不要把 `.env`、token 截图、或「公网地址 + token」的组合发到公开群/仓库/Issue。
> - 用 ngrok 开外网时，8765 端口的唯一门禁就是 `APP_TOKEN`，务必用随机串（Agent 会生成），不要用默认值。
> - 公网暴露的是**本机聊天摘要服务**，请确认你接受这个风险再选「外网访问=是」。

---

## 1. 提示词（复制下面整段给 Agent）

````text
你是 Windows 部署工程师。任务：在当前 Windows 10/11 x64 机器上，从 GitHub 源码把「微信待办 / wechat-task-hub」部署到可用状态，并逐阶段验收。
你只有 PowerShell（以及 python/git/curl）可用，不要用浏览器自动化去替用户注册账号。

## 部署参数（用户已确认；留空按默认）
- 仓库：https://github.com/laimiyou4sb-wq/wechat-task-hub.git
- 安装目录：%USERPROFILE%\wechat-task-hub
- 数据源：wcda（真实，默认，需要 WeChatDataAnalysis + 本机微信）| demo（演示，不连微信）
- LLM Key：<空 = 规则模式>
- 推送：<Bark URL / ntfy 主题 / 空 = 不推送>
- 外网访问：否（默认）| 是（需要用户提供 ngrok authtoken）
- 桌面快捷方式 / 开机自启：是（默认）
- 项目：main 分支 v0.2.0，仅支持 Windows

## 铁律
1. 需要人工的步骤（账号注册、扫码登录、手机安装、UAC 提权）必须停下，用固定格式请求：
   【需要你操作】做什么 | 打开哪个链接/界面 | 你要拿到什么 | 完成后回复什么
2. 机密（APP_TOKEN、LLM Key、ngrok authtoken）只写进 .env 或 %LOCALAPPDATA%\ngrok\ngrok.yml；报告里只说「已生成/已写入」，不打印明文。
3. 不改业务代码；不删除/重置 data/；不执行 git reset / checkout / clean；不 commit / 不 add 任何文件。
4. 每条命令都检查退出码和输出，不要假设成功；同一问题修复 2 次仍失败就停下，附证据（命令、退出码、日志尾部 30 行）。
5. 只用项目 .venv 里的 Python 跑本项目；不升级全局 Python；不装 requirements.txt 之外的包（Playwright 可选，先问用户）。
6. 端口默认 8765。被占用时先判断是不是已部署的本项目（GET /api/health 的 version 字段）→ 是则复用；不是则停下，让用户在「换端口（.env 的 PORT）| 关掉占用进程」里选。
7. 需要管理员权限时不要自行提权：把命令和步骤交给用户，让用户用「以管理员身份运行 PowerShell」执行。
8. 不把服务暴露到公网，除非用户明确选了「外网访问=是」且提供了 authtoken。

## P0 环境探测
执行并记录：
  $PSVersionTable.PSVersion
  python --version
  py -0p
  git --version
  Get-Command pythonw -ErrorAction SilentlyContinue | Select-Object Source
  Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
  （wcda 模式）Invoke-RestMethod http://127.0.0.1:10392/docs -TimeoutSec 3
判定：Windows 10/11 x64、PowerShell 5.1+、Python 3.10+、git 可用。
缺 Python/Git → 停，让用户执行（装完回复「已安装」后重跑本阶段）：
  winget install -e --id Python.Python.3.12    安装时勾选 Add python.exe to PATH
  winget install --id Git.Git
如果 python 是微软商店占位符（提示 “Python was not found”），改用 py -3，或让用户安装正式版。
8765 被占用：curl.exe -s http://127.0.0.1:8765/api/health；是本项目就复用，否则停下问用户。

## P1 获取代码
if (Test-Path <安装目录>\.git) { git -C <安装目录> pull --ff-only; git -C <安装目录> status --porcelain }
else { git clone https://github.com/laimiyou4sb-wq/wechat-task-hub.git <安装目录> }
验收：git -C <安装目录> log --oneline -1；git status 必须干净（有本地改动 → 报告并停，不要 reset）。

## P2 安装依赖
cd <安装目录>
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -U pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
网络失败时重试一次加 -i https://pypi.tuna.tsinghua.edu.cn/simple；仍失败 → 停并贴错误。
验收：.\.venv\Scripts\python.exe -c "import fastapi,uvicorn,httpx,openai,pydantic;print('deps ok')"

## P3 生成 .env（必须做）
1) if (-not (Test-Path .env)) { Copy-Item .env.example .env }
   # 注意：python run.py 不会自动创建 .env；缺失时会用弱默认 token「wechat-tasks」，必须避免
2) $token = .\.venv\Scripts\python.exe -c "import secrets;print('wt-'+secrets.token_hex(8))"
   把值写进 .env 的 APP_TOKEN= 行，屏幕上只说「已生成随机 token」。
3) 按参数覆盖（保持其余行与注释不动）：
   - demo：DATA_SOURCE=demo、DEMO_MODE=1
   - wcda：DATA_SOURCE=wcda、DEMO_MODE=0、WCDA_BASE=http://127.0.0.1:10392
   - LLM：LLM_API_KEY=<key>、LLM_BASE_URL=https://api.deepseek.com/v1、LLM_MODEL=deepseek-chat
   - 推送：PUSH_BARK_URL=<url> 或 NTFY_TOPIC=<topic>
   - 外网：TUNNEL_ENABLED=1（否则 0）
   # LLM_ENABLED 以 Key 非空为准；不填 Key 自动规则模式，不要删变量
4) 自检（只输出布尔/长度，不输出密钥）：
   .\.venv\Scripts\python.exe -c "from app.config import Settings;s=Settings.load();print('PORT',s.port,'SRC',s.data_source,'TOKEN_LEN',len(s.app_token),'LLM',bool(s.llm_api_key),'DEMO',s.demo_mode)"
   判据：PORT=8765、TOKEN_LEN>=16、数据源与选择一致。

## P4 数据源（仅 wcda 模式）
能否访问 http://127.0.0.1:10392/docs 和 http://127.0.0.1:10392/api/health。
不能 → 停：
  【需要你操作】安装并启动 WeChatDataAnalysis（https://github.com/LifeArchiveProject/WeChatDataAnalysis/releases/latest 的 Setup.exe）| 用电脑版微信 4.x 登录并保持在线 | 在 WCDA 界面选择账号并完成数据库解密 | 确认 http://127.0.0.1:10392/docs 能打开后回复「WCDA 就绪」
demo 模式跳过本阶段。

## P5 启动服务
cd <安装目录>; New-Item -ItemType Directory -Force data | Out-Null
$out = Join-Path <安装目录> 'data\deploy.out.log'
$err = Join-Path <安装目录> 'data\deploy.err.log'
$p = Start-Process -FilePath (Join-Path <安装目录> '.venv\Scripts\python.exe') -ArgumentList 'run.py' `
     -WorkingDirectory <安装目录> -WindowStyle Hidden -PassThru `
     -RedirectStandardOutput $out -RedirectStandardError $err
Write-Output "PID=$($p.Id)"
轮询（最长 90 秒）：
  $deadline=(Get-Date).AddSeconds(90); $h=$null
  do { Start-Sleep 2; try { $h=Invoke-RestMethod http://127.0.0.1:8765/api/health -TimeoutSec 5 } catch {} } while (-not $h -and (Get-Date) -lt $deadline)
判据：$h.ok -eq $true。
再等 10 秒，用 Get-Process -Id $p.Id 确认进程仍存活；若启动进程被当前 shell 回收（进程消失），改用桌面快捷方式 / 计划任务方式启动，或明确告诉用户双击「微信待办」。
失败 → 看 data\deploy.err.log 尾部；进程立即退出时先查端口与依赖；仍失败停下报告。
wcda 模式：source.connected 首轮为 null 是正常的（启动 8 秒后的首轮采集结束才变 true）；60 秒仍 false 则报告 source.last_error。

## P6 功能验收（必须真实请求，不能看日志猜）
$t = 从 .env 读到的 APP_TOKEN（不打印明文）
- 健康：Invoke-RestMethod http://127.0.0.1:8765/api/health → ok=true，version 与 app/version.py 一致（当前 0.2.0）
- 鉴权：Invoke-RestMethod http://127.0.0.1:8765/api/tasks -Headers @{ "X-Auth-Token" = $t } → 200（不是 401）
- demo：Invoke-RestMethod -Method Post http://127.0.0.1:8765/api/demo/seed -Headers @{ "X-Auth-Token"=$t } → tasks_created>0；再查 /api/tasks 应有 3~4 条
- wcda：等 source.connected=true；GET /api/collect/state 的 pending_messages 应趋于稳定、failed_messages=0（有失败可 POST /api/collect/failed/retry）
- 推送（若配了）：Invoke-RestMethod -Method Post http://127.0.0.1:8765/api/push/test -Headers @{ "X-Auth-Token"=$t }，让用户确认手机收到「微信待办 · 测试」
- 配对（App 场景）：Invoke-RestMethod http://127.0.0.1:8765/api/app/pairing -Headers @{ "X-Auth-Token"=$t } → 有 code 与 lan_urls
- 页面：Invoke-WebRequest "http://127.0.0.1:8765/?token=$t" → 200 且返回 HTML

## P7 外网访问（仅「外网访问=是」）
需要用户提供 ngrok authtoken；没有就停：
  【需要你操作】注册 https://dashboard.ngrok.com/signup（免费、免信用卡）| 复制 https://dashboard.ngrok.com/get-started/your-authtoken 的 authtoken | 私信发给 Agent（不要发公开群、不要截图外传）| 回复「token 已发」
拿到 token 后：
  powershell -ExecutionPolicy Bypass -File .\tools\ngrok_setup.ps1 -Authtoken <token> -NoTask -Verify
  （脚本会下载 ngrok.exe、写 %LOCALAPPDATA%\ngrok\ngrok.yml、把 .env 的 TUNNEL_ENABLED 改成 1；-NoTask 不注册计划任务）
启动隧道守护：
  Start-Process -FilePath .\.venv\Scripts\python.exe -ArgumentList 'tools\tunnel.py' -WorkingDirectory <安装目录> -WindowStyle Hidden
等待 20~60 秒后：
  .\.venv\Scripts\python.exe tools\tunnel.py --check     → 末行应为「结论：健康」（退出码 0）
读 data\tunnel.json 里的 url（形如 https://xxx.ngrok-free.dev；旧账号为 .ngrok-free.app）。
公网复核：curl.exe -s -H "ngrok-skip-browser-warning: 1" https://<域名>/api/health → ok=true
注意：浏览器直接打开公网地址会先显示 ngrok 警告页（ERR_NGROK_6024），点 Visit 即可，属正常；免费额度 1GB/月、20000 请求/月。

## P8 收尾
1) 桌面快捷方式（不需要管理员）：powershell -ExecutionPolicy Bypass -File .\scripts\install_shortcuts.ps1
2) 开机自启 + 防火墙：先非管理员运行 .\scripts\install_task.ps1（自启会成功，防火墙会因缺管理员提示跳过）；如果需要手机同 Wi-Fi 访问，请用户用管理员 PowerShell 再跑一次。
3) 告诉用户日常启动方式：双击桌面「微信待办」；服务已健康时它只打开网页，不会重复启动。
4) 最终报告（固定格式，不打印明文机密）：
   - 结论：成功 / 部分成功（缺什么）/ 失败（卡在哪、错误证据）
   - 环境：Windows 版本、Python 版本、项目 commit（git rev-parse --short HEAD）
   - 地址：本机 http://127.0.0.1:8765/?token=<掩码>；局域网 http://<IP>:8765/?token=<掩码>；远程 https://<域名>/（如有）
   - 数据源：demo / wcda + connected；LLM：已配置 / 规则模式；推送：Bark / ntfy / 未配置
   - 已改动文件：通常只有 .env（以及 ngrok 写出的 %LOCALAPPDATA%\ngrok\ngrok.yml）
   - 仍需人工的事项：列出还等用户处理的项（注册账号 / 扫码登录 / 安装微信与 WeChatDataAnalysis / UAC 提权）
   - 日志：data\deploy.out.log、data\deploy.err.log、data\launcher.log、data\tunnel.log
   - 停止/卸载：python tools\tunnel.py --stop 停止隧道；结束 .venv\Scripts\python.exe 的 run.py 进程；删除安装目录即可（data\ 是本地数据，删前问用户是否备份）

## 常见故障速查（先自查再求助）
- 页面 401 / token 不对：确认 .env 存在且 APP_TOKEN 不是默认值 wechat-tasks。
- source.connected=false：WCDA 没运行 / 端口不对 / 微信没登录或没解密；source.last_error 含 No chat accounts found → 在 WCDA 界面选账号并解密一次。
- 手机同 Wi-Fi 打不开：管理员放行防火墙 TCP 8765；公司/校园网 AP 隔离 → 只能走 ngrok。
- data\tunnel.log 有 ERR_NGROK_105：authtoken 无效，重跑 ngrok_setup.ps1。
- 端口 4040 被占：本机已有别的 ngrok 在跑，先关掉再启动隧道。
- 页面像旧版 / 没有 ⚙：Ctrl+Shift+R 强刷；仍不行 DevTools → Application → Service Workers → Unregister。
- 启动脚本报 'nul' 不是内部或外部命令：start.bat 编码问题，改用 python run.py。
````

---

## 2. Agent 会自动完成的事（不用你动手）

| 阶段 | 动作 | 通过标准 |
|---|---|---|
| P0 | 检测 Windows / PowerShell / Python / Git / 端口 8765（wcda 模式还查 10392） | 缺什么就明确告诉你装什么 |
| P1 | `git clone`（或 `pull --ff-only`）到安装目录 | `git status` 干净 |
| P2 | 建 `.venv`、装 `requirements.txt`（fastapi/uvicorn/httpx/openai/pydantic） | 5 个包都能 import |
| P3 | 从 `.env.example` 生成 `.env`、生成随机 `APP_TOKEN`、按你的选择写入数据源/LLM/推送/隧道开关 | 自检输出 PORT=8765、TOKEN_LEN≥16 |
| P4 | 检查 WeChatDataAnalysis 的 10392 端口（wcda 模式） | 打不开就停下来找你要 |
| P5 | 后台启动 `python run.py`，轮询 `/api/health` 最长 90 秒 | `ok=true` |
| P6 | 真实请求 health / tasks / demo seed / pairing / 推送测试 | 全部通过 |
| P7 | （可选）`ngrok_setup.ps1 -Authtoken … -NoTask -Verify` + 隧道守护 | `tunnel.py --check` 结论健康 |
| P8 | 桌面快捷方式、开机自启（防火墙需你提权）、输出部署报告 | 报告齐全 |

> 说明：Agent **不会**替你安装微信、WeChatDataAnalysis、注册 ngrok/DeepSeek、装手机 App —— 见下一节。

---

## 3. 需要人工辅助的步骤（单列）

> 这些步骤需要**账号注册/实名、扫码登录、手机或 GUI 操作、UAC 提权**，无头 Agent 做不了。它会在对应阶段停下来，把需求贴给你。

### 3.1 总览：你实际需要做哪几项

| 你的目标 | 必须做的人工项 |
|---|---|
| 只在本机跑 demo（不读微信） | **M1**（仅当缺 Python/Git） |
| 真实读取微信消息 | **M1** + **M2** + **M3** + **M4**（LLM 可选但强烈建议） |
| 手机同 Wi-Fi 访问网页 | 加 **M6** 管理员放行 8765（或用 ngrok 绕开） |
| 手机在外网访问 | 加 **M5** ngrok 注册 + authtoken |
| 用自研安卓 App | 加 **M7** 装 APK + 粘贴配对码 |
| 发布/自动更新（可选） | 加 **M8** |

### M1 本机环境：Python / Git（只需一次）

- **为什么必须人工**：安装包的 UAC 授权、PATH 勾选无法代做（Agent 可以帮你执行 winget，但授权弹窗要你点）。
- **入口**：`winget install -e --id Python.Python.3.12`（安装向导里勾选 **Add python.exe to PATH**）；`winget install --id Git.Git`。或官网 <https://www.python.org/downloads/windows/>、<https://git-scm.com/download/win>。
- **要拿到**：`python --version` ≥ 3.10、`git --version` 正常。
- **交给 Agent**：回复「已安装」，它会重新检测。
- **注意**：不要用微软商店的「python」占位符；装完新开一个 PowerShell 窗口再验证。

### M2 电脑版微信 4.x

- **为什么必须人工**：需要手机扫码登录，账号身份无法代替。
- **入口**：<https://weixin.qq.com/> 下载电脑版微信 4.x，手机微信扫码登录。
- **要拿到**：微信保持登录并**一直运行**（本项目只读它的本地数据，不操作微信）。
- **注意**：只支持微信 4.x；退出微信后采集会失败（`source.connected=false`）。

### M3 WeChatDataAnalysis（WCDA）

- **为什么必须人工**：桌面端安装 + 首次必须在它自己的界面里选账号、完成数据库解密，没有命令行替代。
- **入口**：<https://github.com/LifeArchiveProject/WeChatDataAnalysis/releases/latest> → 下载 Windows 的 `Setup.exe` 安装并打开。
- **要拿到**：① 在 WCDA 界面选择微信账号并完成解密；② 浏览器能打开 <http://127.0.0.1:10392/docs>（本地只读 API，无 Token，只监听本机）。
- **交给 Agent**：确认 10392 可访问后回复「WCDA 就绪」。
- **注意**：WCDA 必须**保持运行**（托盘/后台即可）；它负责解密与提供 API，本项目不直接碰微信数据库。没配置账号时项目会报 `No chat accounts found`。

### M4 LLM Key（DeepSeek，强烈建议）

- **为什么必须人工**：平台注册、实名/充值、创建 Key 需要账号身份。
- **入口**：<https://platform.deepseek.com> → 注册/登录 → **API keys** → 创建。
- **要拿到**：`sk-...` 形式的 Key（默认端点 `https://api.deepseek.com/v1`、模型 `deepseek-chat`）。
- **怎么给**：推荐直接发给 Agent 让它写 `.env`（`LLM_API_KEY`）；也可以启动后自己在网页右上角 **⚙ → API Key** 里填（保存即生效，无需重启）。
- **注意**：**不填 Key 也能用**，会退化为本地规则提取，只识别含行动词/时间的消息，准确率明显下降。任何 OpenAI 兼容端点都行（OpenAI、硅基流动、本地 Ollama 等），价格/免费额度以各平台为准。

### M5 ngrok（注册 + authtoken + 免费固定域名，远程访问）

- **为什么必须人工**：邮箱/GitHub/Google 注册、验证码、复制 token 都是账号操作。
- **入口**：
  1. 注册：<https://dashboard.ngrok.com/signup>（免费，**不用信用卡**）；
  2. 复制 authtoken：<https://dashboard.ngrok.com/get-started/your-authtoken>（形如 `2abc...XYZ` 的长串）；
  3. 固定域名：<https://dashboard.ngrok.com/domains> —— 免费账号自动分配 1 个固定 dev domain（形如 `xxx-yyy.ngrok-free.dev`，旧账号 `.ngrok-free.app`），固定不变、可改名一次。**一般不用填 `NGROK_DOMAIN`**，留空即用账号分配的域名。
- **怎么给 Agent**：把 authtoken **私信**发给它（或自己跑向导：`powershell -ExecutionPolicy Bypass -File .\tools\ngrok_setup.ps1`，按提示粘贴）。非交互写法：`ngrok_setup.ps1 -Authtoken <token> -NoTask`。token 最终保存在 `%LOCALAPPDATA%\ngrok\ngrok.yml`，属于本机机密。
- **免费额度（写进官方文档的）**：**1 GB 流量/月、20,000 请求/月**，同一账号同时最多 3 个在线端点；超了 ngrok 会直接拒绝请求，下月自动恢复。用量：<https://dashboard.ngrok.com/usage>。
- **注意**：
  - `data\tunnel.log` 出现 `ERR_NGROK_105` = authtoken 无效/没配，重跑向导；
  - 浏览器直接打开公网地址会先出现 ngrok 警告页（`ERR_NGROK_6024` 的 Visit 按钮），免费版正常现象，点一下即可，App/API 不受影响；
  - 本机若已有别的 ngrok 占着 4040 端口，先关掉（隧道健康检查读 4040）；
  - 电脑休眠/断网 = 隧道断，恢复后会重连；
  - 不想用外网就保持 `TUNNEL_ENABLED=0`，局域网直连不受影响。

### M6 管理员放行防火墙 8765 / 开机自启（可选）

- **为什么必须人工**：UAC 提权无法由无头 Agent 完成。
- **怎么做**：右键「以管理员身份运行」PowerShell，执行：
  ```powershell
  powershell -ExecutionPolicy Bypass -File .\scripts\install_task.ps1
  ```
- **它会做**：注册「登录后自动启动」计划任务 `WeChatTaskHub`（不需要管理员）；以管理员运行时额外放行**入站 TCP 8765（仅专用网络）**。
- **注意**：不做这一步，手机在同 Wi-Fi 下通常打不开 `http://<电脑IP>:8765`；公司/学校网络可能禁止设备互访（AP 隔离），这种情况只能用 ngrok。

### M7 安卓 App（安装 + 配对，可选）

- **为什么必须人工**：手机侧允许安装、粘贴配对码、系统权限都需要手点。
- **APK 从哪来**：仓库里 `android/dist/` 是构建产物，**不随 git 分发**。两种方式：
  - 直接用维护者给的可信 APK（找发你项目的人要，或从 Release 下载，如有）；
  - 自己构建：装 JDK 17 + Android SDK（platform 35 + build-tools），在 `android/` 下执行 `.\gradlew.bat assembleRelease`，产物在 `android/dist/app-release.apk`。
- **安装**：把 APK 传到手机（数据线/adb），允许「未知来源安装」。报 `INSTALL_FAILED_USER_RESTRICTED` → 开发者选项里打开「USB 安装」，亮屏解锁后重试。
- **配对**：电脑网页右上角 **⚙ →「手机 App（直连同步）」卡片 → 复制配对码**；打开 App → **设置 → 配对 → 粘贴配对码 → 保存 → 测试连接**。
- **注意**：
  - 配对码里含局域网 + ngrok 地址，**换网络/换 token 后要回设置页重新复制**；
  - 手机里填 `127.0.0.1` 会指向手机自己，必须用电脑局域网 IP；
  - 小米/HyperOS 等：设置里给 App 开「自启动」、省电策略选「无限制」、允许通知与精确闹钟，否则到点提醒会被杀。

### M8 发布与自动更新（可选，仅发布者）

- **为什么必须人工**：建仓库、确认 Release、发布动作都需要 GitHub 账号。
- **入口**：流程与产物格式见 [开源发布与自动更新方案](开源发布与自动更新方案.md)：打 tag → GitHub Actions 构建 → Release 上传 4 个资产（`WeChatTaskHub-Setup.exe`、`portable.zip`、`latest.json`、`sha256sums.txt`）。
- **注意**：工作流用内置 `GITHUB_TOKEN`（`permissions: contents: write`），文档里**不需要**你额外创建 PAT 或 Secrets；客户端拉公开 `latest.json` 也不需要鉴权。纯自用不必发布。

### 3.2 其他需要人确认的小项

- **PWA 添加到主屏幕**：手机浏览器打开局域网地址后「添加到主屏幕」，像 App 一样用（缓存问题用 `Ctrl+Shift+R` / 重装图标解决）。
- **电源不休眠**：要长期外网访问时，把 Windows 电源计划改成「从不睡眠」。
- **Cloudflare 快速隧道兜底**（免注册）：下载 `cloudflared-windows-amd64.exe` 存为 `tools\cloudflared.exe`，在 `.env` 取消 `TUNNEL_PROVIDER=cloudflared` 注释。**地址每次启动都变**，仅临时救急，正式还是用 ngrok。
- **数据目录位置**：`data\` 不要放在 OneDrive / 网络盘上（SQLite WAL 容易损坏）。
- **杀软误报**：未签名的 `WeChatTaskHub.exe` 可能被拦，加信任即可（源码部署时用的是 Python，不受影响）。

---

## 4. 验收清单

### 4.1 最小可用（demo，不读微信）

```powershell
# 健康检查（无需 token）
Invoke-RestMethod http://127.0.0.1:8765/api/health | ConvertTo-Json -Depth 3
# 生成演示数据（需 APP_TOKEN）
Invoke-RestMethod -Method Post http://127.0.0.1:8765/api/demo/seed -Headers @{ "X-Auth-Token" = "<APP_TOKEN>" }
# 任务列表
Invoke-RestMethod http://127.0.0.1:8765/api/tasks -Headers @{ "X-Auth-Token" = "<APP_TOKEN>" }
```

- [ ] `/api/health` 返回 `ok: true`，`version` 与 `app/version.py` 一致（当前 0.2.0）
- [ ] `/api/tasks` 不是 401，demo seed 后能看到 3~4 条任务
- [ ] 浏览器打开 `http://127.0.0.1:8765/?token=<APP_TOKEN>` 显示任务表

### 4.2 真实采集（wcda）

- [ ] `/api/health` 的 `source.connected = true`（首轮采集前为 `null` 属正常，8 秒后变 true）
- [ ] `collection.pending_messages` 不再持续增长、`failed_messages = 0`
- [ ] 实际发一条群消息，1~2 分钟内出现在任务表（或「已忽略/无待办」）

### 4.3 手机 / 外网（按需）

- [ ] 同一 Wi-Fi：手机打开 `http://<电脑IP>:8765/?token=<APP_TOKEN>`（打不开先查防火墙 8765 / AP 隔离）
- [ ] 外网：`python tools\tunnel.py --check` 末行「结论：健康」，`data\tunnel.json` 里的 URL 能访问
- [ ] 推送：`POST /api/push/test` 后手机收到「微信待办 · 测试」
- [ ] App：设置页复制配对码 → App 配对成功 → 在家走局域网、出门自动走 ngrok

---

## 5. 部署期常见问题

| 现象 | 原因 / 修复 |
|---|---|
| 页面 401 `unauthorized` | 没 `.env` 或 `APP_TOKEN` 不匹配；确认 `.env` 存在且不是默认 `wechat-tasks` |
| `source.connected=false` | WCDA 没运行 / 10392 端口不对 / 微信没登录或没解密；看 `source.last_error` |
| `last_error` 含 `No chat accounts found` | WCDA 还没完成账号配置：打开其界面选账号、解密一次 |
| 手机同 Wi-Fi 打不开 | 管理员放行 TCP 8765；或公司/校园网 AP 隔离 → 改用 ngrok |
| `data\tunnel.log` 有 `ERR_NGROK_105` | authtoken 无效/没配，重跑 `tools\ngrok_setup.ps1` |
| 公网地址先出警告页 / `ERR_NGROK_6024` | ngrok 免费版正常现象，点 Visit；App/API 不受影响（可加 `ngrok-skip-browser-warning` 头） |
| 8765 端口占用 | 改 `.env` 的 `PORT` 或关掉旧 `python.exe` |
| ngrok 本地 4040 被占 | 本机已有别的 ngrok 在跑，先关掉 |
| 页面像旧版 / 看不到 ⚙ | Service Worker 缓存：`Ctrl+Shift+R`；仍不行 DevTools → Application → Service Worker → Unregister |
| 启动脚本报 `'nul' 不是内部或外部命令` | `start.bat` 编码问题（需 ASCII+CRLF）；直接用 `python run.py` |
| 推送收不到 | Bark URL 必须是完整 `https://api.day.app/KEY`；ntfy 主题两边一致；免打扰时段（默认 23:00–07:30）静默 |
| 微信升级后读不到数据 | 升级 WeChatDataAnalysis 到最新版（本项目不直接读微信库） |

---

## 6. 停止 / 卸载

```powershell
# 停隧道（若开了外网）
.\.venv\Scripts\python.exe tools\tunnel.py --stop
# 停服务：结束 run.py 对应的 python 进程，或双击桌面「停止微信待办」
# 移除开机自启（如注册过）
Unregister-ScheduledTask -TaskName WeChatTaskHub -Confirm:$false
Unregister-ScheduledTask -TaskName WeChatTaskHubTunnel -Confirm:$false
# 删快捷方式：桌面/开始菜单里的「微信待办」「停止微信待办」
```

- 删除安装目录即可彻底卸载；`data\` 里是本地数据库（任务与原文），删前确认是否需要备份。
- 若外网暴露过，建议在 ngrok/DeepSeek 后台吊销旧 token 并重新生成 `APP_TOKEN`。

---

## 7. 相关文档

- 从零图文配置（WCDA / DeepSeek / Bark / ntfy / 手机访问）：[配置教程.md](配置教程.md)
- ngrok 注册、免费额度、配对、排查： [手机同步.md](手机同步.md)
- API 契约： [API.md](API.md)
- 目录分层与路径规则： [目录分层.md](目录分层.md)
- 打包成桌面 exe： [packaging/README.md](../packaging/README.md)
- 开源发布与自动更新： [开源发布与自动更新方案.md](开源发布与自动更新方案.md)
- 安卓 App 构建： [android/README.md](../android/README.md)

---

> **维护提示**：本提示词与代码同步，依据 `.env.example`、`run.py` / `app/cli.py`、`launcher.pyw`、`tools/ngrok_setup.ps1`、`tools/tunnel.py` 的实际行为编写。
> 若上述文件或端口/参数发生变化（例如默认端口、启动参数、ngrok 向导参数），请同步更新本文件。
