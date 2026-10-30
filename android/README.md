# 微信待办 · 安卓 App（v1）

自研安卓客户端，不依赖第三方待办 App 或代理服务，直接与电脑端服务的 `/api/app/*` 接口同步微信待办（旧 CalDAV/Tasks.org/Tailscale 链路已从项目移除）。

- 包名：`com.wxtask.hub`　应用名：**微信待办**
- 版本：`1.0.1`（versionCode 2；1.0.1 修复同步游标 bug 并加入 Room v1→v2 自愈迁移）
- 技术栈：Kotlin 2.0.21 / Jetpack Compose（Material 3）/ Room 2.6.1 / DataStore / OkHttp 4.12 / kotlinx-serialization / WorkManager 2.10.0 / AlarmManager
- 构建基线：AGP 8.7.3、Gradle 8.10.2、compileSdk 35、minSdk 26、JDK 17
- 接口契约：[`docs/APP-SYNC-CONTRACT.md`](../docs/APP-SYNC-CONTRACT.md)（唯一权威）

---

## 1. 构建

前置：JDK 17、Android SDK（`platforms/android-35`、`build-tools`）。

```bat
set JAVA_HOME=D:\Android\jdk17\jdk-17.0.20+8
cd /d D:\微信通知总结\wechat-task-hub\android

:: 调试包
.\gradlew.bat assembleDebug
:: 长期安装包（沿用 debug 签名，可直接覆盖安装）
.\gradlew.bat assembleRelease
```

产物：

| 文件 | 说明 |
|---|---|
| `app/build/outputs/apk/debug/app-debug.apk` | 调试包 |
| `app/build/outputs/apk/release/app-release.apk` | Release 包（debug 签名） |
| `dist/app-debug.apk`、`dist/app-release.apk` | 备份副本 |
| `dist/shot-*.png` | 真机截图 |

> 工程路径含中文，已在 `gradle.properties` 里加 `android.overridePathCheck=true`（AGP 默认会拒绝非 ASCII 路径）。

## 2. 安装（真机）

```bat
C:\platform-tools\adb.exe -s <序列号> install -r app\build\outputs\apk\debug\app-debug.apk
C:\platform-tools\adb.exe -s <序列号> shell am start -n com.wxtask.hub/.MainActivity
```

> 小米 / HyperOS 若报 `INSTALL_FAILED_USER_RESTRICTED`：解锁手机保持亮屏，并在开发者选项里打开
> 「USB 安装」（部分机型还要「USB 调试（安全设置）」），安装时在手机上点「继续安装」。

## 3. 配对

电脑端服务启动后，在电脑网页 **设置 → 手机 App** 卡片里复制配对码（`base64url(JSON)`，含地址列表 + token）。

**方式 A：粘贴配对码（推荐）**
App → 设置 → 配对 → 粘贴配对码 → 「配对并同步」。

**方式 B：手动填地址 + token**
设置 → 「手动填地址 + token」→ 填 `http://<电脑局域网IP>:8765` 和 `APP_TOKEN`。

**方式 C：adb 深链注入（调试用）**

```bat
adb shell am start -a android.intent.action.VIEW -d "wxtask://pair?code=<配对码>" com.wxtask.hub
```

配对成功后 App 会自动全量同步（清空本地缓存重灌，契约 §4 full 快照），并记住成功地址
（`sync_state.active_url`）。地址切换顺序：**active_url → lan → remote**；连接失败 / 5xx / 429 自动试下一个，
401 立即停止并提示重新配对。

> ⚠️ 手机访问电脑必须用电脑的**局域网 IP**（如 `http://192.168.1.20:8765`）；
> 在手机上填 `127.0.0.1` 会指向手机自己。
>
> **多地址切换**：配对码里的地址会按 `lan` 优先、`remote` 兜底排序。手机与电脑在同一网段
> （同一 Wi-Fi / 电脑开热点 / 手机 USB 共享网络）时优先局域网直连；不在同一网络（外网）时
> 自动走远程 ngrok 地址。当前地址连接失败（超时 / 连接被拒 / 5xx）会自动试下一个，成功后记住可用地址。
> 远程访问统一用 ngrok，本项目不再依赖 Tailscale 等第三方代理服务。

## 4. 功能

- **任务列表**：今天 / 本周 / 以后 / 已完成 分段；`已完成` 页有「已完成 / 已忽略 / 稍后」二级筛选；
  逾期标红、勾选圈一键完成、下拉刷新、同步状态条、空态 / 错误态、FAB 新建；
- **任务详情**：编辑标题、备注、截止时间（DatePicker + TimePicker，可清除）、优先级；
  完成 / 取消完成 / 稍后 1 小时 / 忽略 / 恢复 / 删除（二次确认）；
  只读展示所属群、创建/更新时间、所属事项标题摘要、微信原文来源（发送人 + 时间 + 内容，点按复制）；
- **设置**：配对码 / 手动地址 + token、地址列表逐个测试连接、立即同步、cursor 调试信息、
  通知开关、HyperOS 自启动 / 省电无限制 / 精确闹钟引导、关于；
- **同步引擎**（契约 §6）：先推 outbox（`action_id` 幂等、create 后 local_id → 真实 id 映射并改写后续操作），
  再按 `rev` cursor 拉增量；`full=true` 清表重灌，`deleted` 删本地行，`has_more` 续拉；
  本地操作乐观更新（点完成立即生效），同任务的 update 合并为一条避免 `client_rev` 冲突丢失改动；
- **后台**：WorkManager 15 分钟周期同步（联网约束）+ 启动 / 回前台 / 每次本地操作 / 网络恢复时立即同步；
- **通知与提醒**：新待办汇总通知（正文列前 3 条）、到点提醒（AlarmManager：有精确闹钟权限时用
  `setAlarmClock`，不受 Doze 的 allow-while-idle 限流，国产 ROM 更可靠；无权限退化 `setAndAllowWhileIdle`）；
  同步 / 开机 / 改时区后重排，完成 / 忽略 / 删除 / 稍后取消；
- **深色模式**：跟随系统；浅色白底黑字，深色 `#0E0E0E` 背景 + `#1A1A1A` 卡片 + `#EDEDED` 文字。

## 5. 权限

| 权限 | 用途 |
|---|---|
| `INTERNET` / `ACCESS_NETWORK_STATE` | 同步电脑端接口 |
| `POST_NOTIFICATIONS` | 新待办汇总、到点提醒（Android 13+ 运行时申请） |
| `RECEIVE_BOOT_COMPLETED` | 开机重排提醒 + 恢复同步 |
| `WAKE_LOCK` | 提醒唤醒 |
| `SCHEDULE_EXACT_ALARM` / `USE_EXACT_ALARM` | 精确到点提醒（无权限时自动退化） |
| `REQUEST_IGNORE_BATTERY_OPTIMIZATIONS` | 「省电策略 → 无限制」入口 |

`application` 已设 `android:usesCleartextTraffic="true"`：局域网 `http://` 需要。

**小米 / HyperOS 后台保活建议**（设置页有直达按钮，跳不过去会显示手动路径）：
1. 自启动：设置 → 应用设置 → 应用管理 → 微信待办 → 自启动，允许；
2. 省电策略：同一路径 → 省电策略 → 无限制；
3. 精确闹钟：同一路径 → 其他权限 → 闹钟和提醒，允许；
4. 最近任务列表里把「微信待办」锁定，避免被一键清理。

## 6. 常见问题

| 现象 | 排查 |
|---|---|
| 配对后列表为空 / 同步失败 | 手机与电脑需在同一局域网；手机浏览器打开 `http://<电脑IP>:8765/api/health` 是否可达；确认没用 `127.0.0.1` |
| 一直提示「令牌失效」 | 电脑端 `APP_TOKEN` 变了，重新复制配对码配对即可（会清空本地缓存并重新全量同步） |
| 同步状态显示「电脑端返回 HTTP 500」 | 电脑端服务异常，重启服务后 App 下拉刷新即可；App 会保留 outbox 不丢操作 |
| 操作后电脑端没变化 | 设置里看「待上传操作」是否 >0：断网时会排队，恢复网络 / 打开 App 后自动补推 |
| 到点不提醒 | 检查通知权限、精确闹钟权限、HyperOS 省电策略；设置页有状态显示 |
| 后台不再同步 | HyperOS 会杀后台，按第 5 节开启自启动 + 无限制省电；周期任务被系统限制时打开 App 即会补同步 |
| `INSTALL_FAILED_USER_RESTRICTED` | 见第 2 节：解锁 + 开发者选项「USB 安装」+ 手机确认弹窗 |

## 7. 调试

```bat
:: 崩溃日志
adb logcat -d -s AndroidRuntime:E
:: API 客户端失败日志（TaskApi）
adb logcat -d -s TaskApi:W
```

本机没有 `sqlite3` 时，用 `exec-out` 把数据库（含 WAL）拉到电脑再查：

```powershell
$adb = "C:\platform-tools\adb.exe"
cmd /c "$adb -s <序列号> exec-out run-as com.wxtask.hub cat /data/data/com.wxtask.hub/databases/wxtask.db > $env:TEMP\wxtask.db"
cmd /c "$adb -s <序列号> exec-out run-as com.wxtask.hub cat /data/data/com.wxtask.hub/databases/wxtask.db-wal > $env:TEMP\wxtask.db-wal"
python -c "import sqlite3,os;c=sqlite3.connect(os.path.join(os.environ['TEMP'],'wxtask.db'));print(c.execute('select * from sync_state').fetchall());print(c.execute('select type,task_id,client_rev from outbox').fetchall())"
```

`sync_state` 里可看 `cursor / active_url / last_sync_at / last_error`；`outbox` 是未确认操作队列。

服务端接口自检：

```powershell
$h=@{'X-Auth-Token'='<APP_TOKEN>'}
Invoke-RestMethod -Uri http://127.0.0.1:8765/api/app/ping -Headers $h
Invoke-RestMethod -Uri 'http://127.0.0.1:8765/api/app/sync?cursor=0' -Headers $h
```

## 8. 目录结构（关键文件）

```
android/
├─ app/src/main/java/com/wxtask/hub/
│  ├─ AppContainer.kt            # 手工依赖容器 + 配对逻辑（base64url 解码）
│  ├─ WxTaskApp.kt               # Application：通知渠道 / 周期同步 / 网络回调
│  ├─ MainActivity.kt            # 单 Activity + Compose + 深链（wxtask://pair）
│  ├─ data/
│  │  ├─ local/                  # Room：tasks / outbox / sync_state
│  │  ├─ prefs/SettingsStore.kt  # DataStore：token / endpoints / 通知开关
│  │  ├─ remote/                 # DTO + OkHttp 客户端（多地址切换 / 401）
│  │  ├─ TaskRepository.kt       # 本地乐观更新 + outbox 入队
│  │  └─ TaskStore.kt            # 服务端数据落库规则（§6）
│  ├─ sync/                      # 同步引擎 / Worker / 通知 / AlarmManager
│  ├─ ui/                        # 列表 / 详情 / 新建 / 设置 / 主题
│  └─ util/                      # 时间处理、HyperOS Intent 引导
├─ app/src/main/res/             # 图标（黑底白勾自适应图标）、主题、通知小图标
└─ dist/                         # APK 与真机截图
```

## 9. 真机验证记录（小米 2206123SC / Android 15 / HyperOS 3.0）

测试环境：电脑端 `/api/app/*`（v0.1.0，token 已配对），手机通过 USB 共享网络访问
`http://192.168.182.1:8765`；`adb -s 8b7b2e6a`。

| 项目 | 结果 |
|---|---|
| 配对（`wxtask://pair?code=` 深链） | ✅ 自动保存 + 全量同步，列表出现 16 条待办 |
| 列表/详情/设置 UI（浅色 + 深色） | ✅ 截图见 `dist/shot-43/44/45`（浅色）、`dist/shot-40/41/42`（深色） |
| 勾选完成 / 取消完成 / 忽略 / 恢复 / 稍后 | ✅ 电脑端状态、`completed_at`、`snooze_until`、`rev` 均正确变化 |
| 编辑标题/优先级/截止 | ✅ 电脑端 `title/priority/due_at` 更新（`client_rev` 冲突检测生效） |
| 新建任务（App 内） | ✅ 电脑端出现，`group_name=手机添加`，本地 local_id 正确映射为服务端 id |
| 删除任务（二次确认） | ✅ 电脑端物理删除同时写墓碑，App 列表移除 |
| 新待办汇总通知 | ✅ 电脑端新增任务 → App 同步后通知「新增 2 条待办」，正文列前 3 条标题 |
| 到点提醒（熄屏 2 分钟） | ✅ 19:48 到点任务，屏幕处于 Dozing 时仍唤醒并发通知「到点：alarm-test-1948」 |
| 通知点击 | ✅ 点击到点通知直接打开对应任务详情 |
| 断网 → 本地操作 → 恢复 | ✅ 断开数据链路后勾选完成：outbox 保留 1 条 complete、UI 乐观更新；恢复后自动补推，电脑端变 done，outbox 清空 |
| 幂等（`action_id`） | ✅ 同一 action 重复提交返回 `deduped=true`，cursor/rev 不再变化 |
| 多地址切换 | ✅ active_url 指向不可用地址时自动回落到可用地址并记住 |
| 远程 ngrok 链路 | ✅ 仅配远端地址时真机同步经 ngrok 转发（服务端 sync 来源为手机移动网络 IPv6）；LAN + remote 双链路 40 秒内切换正常 |
| 深色模式 | ✅ 背景 `#0E0E0E`、卡片 `#1A1A1A`、文字 `#EDEDED` |

截图文件（`dist/`）：

- `01-list-light.png` / `02-list-dark.png`：任务列表（浅色 / 深色）
- `03-detail-light.png` / `04-detail-dark.png`：任务详情（浅色 / 深色）
- `05-settings-light.png` / `06-settings-dark.png`：设置（浅色 / 深色）
- `07-new-task-notification.png`：新待办汇总通知
- `08-due-reminder-notification.png`：到点提醒通知（熄屏 Dozing 状态收到）
- `09-notification-open-detail.png`：点击到点通知直接进入任务详情
- `10-offline-optimistic.png`：断网时本地乐观完成 + 同步失败提示
- `11-detail-sources.png`：详情里的微信原文来源
- `12-done-tab.png`：已完成页与「已完成/已忽略/稍后」二级筛选
- `13-release-home.png`：安装 release 包后的主界面
- `15-settings-endpoints.png`：地址列表 LAN + ngrok 两条「测试连接」均成功

## 10. 已知限制（v1）

- 无搜索、无批量选择、无手势快捷操作（契约范围外）；
- 横屏不专门适配（竖屏 390dp 无横向滚动，点击区域 ≥44dp）；
- 新待办通知只在同步后触发，不做系统推送（电脑端断网时 App 收不到提醒）；
- 到点提醒在到期时刻发一次，不做重复提醒 / 贪睡按钮（稍后 1 小时需要进详情操作）；
- 手动配对只保存一个地址；需要多地址（局域网 + 远程）请用配对码；
- ngrok 远程地址的 App 端切换已在逻辑上验证（失败自动回落），但尚未接入真实 ngrok 域名做真机复测。

