# 安卓 App ↔ 电脑端同步契约（v1）

> 本文是「自研安卓 App」与电脑端服务的唯一接口约定。电脑端实现在 `app/mobile.py`（路由前缀 `/api/app`），
> App 实现在 `android/`。两端字段名、语义必须与本文一致；有分歧以本文为准，改契约需同时改两端 + 本文。

## 0. 总原则

- **电脑端是唯一权威数据源**（SQLite `tasks` 表）。App 是「本地缓存 + 操作队列」的离线客户端。
- 同步 = **拉增量（rev 游标）+ 推操作（action_id 幂等）**。
- 时间格式：**无时区的本地时间**，`yyyy-MM-ddTHH:mm:ss`（Asia/Shanghai）。App 端用 `LocalDateTime` 解析/生成，
  **禁止**带 `Z`、禁止带偏移量。`due_at`、`snooze_until`、`completed_at`、`server_time` 均为此格式。
- 鉴权：请求头 `X-Auth-Token: <APP_TOKEN>`（与网页同一个 token）。401 = token 错误，App 必须停下并提示重新配对。
- 传输：局域网为 `http://`（已允许明文）；远程为 ngrok 的 `https://` 固定域名。证书为公网可信证书，无需 pin。
- 压缩：服务端对 JSON 支持 gzip/br，客户端 `Accept-Encoding: gzip` 即可（可选）。

## 1. 端点一览

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/app/ping` | 心跳 / 版本 |
| GET | `/api/app/pairing` | 配对信息（地址 + token + 配对码） |
| GET | `/api/app/sync?cursor=<int>` | 拉任务增量（含删除墓碑） |
| GET | `/api/app/tasks/{id}` | 任务详情（含微信原文来源、所属事项） |
| POST | `/api/app/actions` | 批量提交操作（幂等） |

除 pairing 外全部需要 token。pairing 也需要 token（网页设置页在已登录状态调用）。

## 2. GET /api/app/ping

```json
{"ok": true, "app": "wechat-task-hub", "version": "0.1.0", "server_time": "2026-10-26T19:20:00", "tz": "Asia/Shanghai"}
```

App 用它测连通性（超时 5 秒），也可在设置页「测试连接」用。

## 3. GET /api/app/pairing

```json
{
  "v": 1,
  "name": "DESKTOP-ABC 微信待办",
  "token": "wt-7f3k9m2x",
  "endpoints": [
    {"kind": "lan", "url": "http://192.168.1.20:8765", "priority": 0},
    {"kind": "lan", "url": "http://10.141.57.7:8765", "priority": 1},
    {"kind": "remote", "url": "https://xxxx.ngrok-free.app", "priority": 0}
  ],
  "code": "eyJ2IjoxLCJuYW1lIjoiLi4uIn0"
}
```

- `code` = `base64url(UTF-8 JSON, 无 padding)`，JSON 内容：

```json
{"v":1,"name":"DESKTOP-ABC 微信待办","token":"wt-...","endpoints":[{"kind":"lan","url":"http://192.168.1.20:8765"},{"kind":"remote","url":"https://xxxx.ngrok-free.app"}],"ts":"2026-10-26T19:20:00"}
```

- App 支持两种配对方式：
  1. 粘贴 `code`（推荐，一次搞定地址 + token）；
  2. 手动填「服务器地址 + token」。
- App 收到 endpoints 后按 `lan` 优先、`remote` 兜底排序；同一优先级内保持服务端顺序。
- App 需做「地址自动切换」：当前地址连续失败（连接超时 / 5xx）→ 试下一个；成功后记住该地址（可持久化 active url）。
- 网页设置页「手机 App」卡片展示同一个 `code`（带复制按钮）与每个地址，供用户复制到手机。

## 4. GET /api/app/sync?cursor=<int>

`cursor` 为上次响应的 `cursor`（整数 rev）。首次同步传 0 或省略。

```json
{
  "server_time": "2026-10-26T19:20:00",
  "cursor": 128,
  "full": false,
  "has_more": false,
  "tasks": [TaskDTO, ...],
  "deleted": ["taskid-a", "taskid-b"],
  "counts": {"today": 2, "week": 3, "later": 1, "done": 5, "pending": 8}
}
```

语义：

- `full=true`：`tasks` 是**全量快照**（App 应清空本地任务表后整体落库），`deleted` 为空。
  触发条件：cursor 缺失/为 0/非法/大于服务端当前 rev/落后超过墓碑保留期。
- `has_more=true`：还有增量没取完，App 立即用新 cursor 再拉一次直到 false（单页上限 500）。
- `deleted`：服务端已物理删除的任务 id（墓碑）。App 直接删本地行，并清理对应的待办提醒。
- 增量按 rev 升序，App 落库用「服务端行覆盖本地行（同 id）」；本地「未同步操作」相关的行由 outbox 保证不丢（见 §6）。
- `counts` 仅用于角标/导航，App 也可自己按本地数据算。

### TaskDTO（字段固定，全部出现；可空字段用 null）

```json
{
  "id": "8f3a1c9d2e4b",
  "title": "周五前交实验报告",
  "notes": "群公告里提的，附件在群文件",
  "due_at": "2026-10-30T18:00:00",
  "priority": "high",
  "status": "pending",
  "group_name": "项目群",
  "event_id": "ab12cd34ef56",
  "source_count": 3,
  "created_at": "2026-10-26T10:00:00",
  "updated_at": "2026-10-26T10:05:00",
  "completed_at": null,
  "snooze_until": null,
  "rev": 127
}
```

- `status` ∈ `pending | done | ignored | snoozed`。
- `priority` ∈ `high | normal | low`。
- `updated_at` 用于冲突检测（见 §5「stale」）。
- `source_count` 是来源消息条数（详情里才给全文）。

### GET /api/app/tasks/{task_id}（详情）

```json
{
  "task": TaskDTO,
  "event": {"id": "ab12...", "title": "实验报告相关", "summary": "群里通知周五前交", "status": "open", "group_name": "项目群"},
  "sources": [
    {"rawid": "msg-001", "group_name": "项目群", "sender": "李老师", "content": "周五前把实验报告交上来", "ts": "2026-10-26T10:00:00"}
  ]
}
```

- `event` 可能为 `null`（手动添加的任务没有事项）。
- `sources` 按消息时间升序；`ts` 是**本地 ISO 字符串**（不是 epoch）；`rawid` 是消息唯一 id。
- 任务不存在 → 404 `{"detail": "task not found"}`。
- 列表页点击任务卡片时按需调用（不要全量拉详情）。

## 5. POST /api/app/actions

请求（最多 50 条，按数组顺序依次执行；同一批里后面的操作能看到前面的结果）：

```json
{
  "actions": [
    {"action_id": "c0ffee01-...", "type": "complete", "task_id": "8f3a1c9d2e4b", "client_rev": 127},
    {"action_id": "c0ffee02-...", "type": "update", "task_id": "8f3a1c9d2e4b", "client_rev": 127,
     "fields": {"title": "周五前交实验报告（定稿）", "due_at": "2026-10-30T20:00:00", "priority": "normal", "notes": ""}},
    {"action_id": "c0ffee03-...", "type": "snooze", "task_id": "8f3a1c9d2e4b", "snooze_until": "2026-10-27T09:00:00"},
    {"action_id": "c0ffee04-...", "type": "create", "local_id": "local-5b2f...",
     "fields": {"title": "买实验耗材", "due_at": null, "priority": "normal", "notes": ""}}
  ]
}
```

> `client_rev` 是 App 本地保存的该任务 `rev`（TaskDTO 字段），用于冲突检测；**推荐必带**（秒级 `updated_at` 会撞车）。
> 兼容旧方式 `client_updated_at`，二者都有时以 `client_rev` 为准。

响应：

```json
{
  "server_time": "2026-10-26T19:20:01",
  "cursor": 131,
  "results": [
    {"action_id": "c0ffee01-...", "ok": true, "deduped": false, "stale": false, "task": TaskDTO},
    {"action_id": "c0ffee02-...", "ok": true, "deduped": false, "stale": true,  "task": TaskDTO},
    {"action_id": "c0ffee03-...", "ok": false, "error": "not_found", "task": null},
    {"action_id": "c0ffee04-...", "ok": true, "deduped": false, "stale": false, "task": TaskDTO}
  ]
}
```

规则：

- **幂等**：`action_id` 是客户端生成的 UUID。服务端把它和结果存表；重复提交同一个 `action_id` 直接返回首次结果，`deduped=true`。App 只有在收到该 action 的结果后才从 outbox 删除。
- `type`：
  - `complete`：status→done，completed_at=now，snooze_until=null；
  - `uncomplete`：status→pending，completed_at=null；
  - `ignore`：status→ignored；
  - `restore`：status→pending（把 ignored/done 恢复成待办）；
  - `snooze`：status→snoozed，snooze_until=`snooze_until`（缺省 now+1h）；
  - `update`：只改 `fields` 里出现的键（title/notes/due_at/priority）；`due_at:null` 表示清空；
  - `create`：新建任务（group_name 固定「手机添加」，status=pending），返回新 TaskDTO；
  - `delete`：物理删除 + 写墓碑；返回 `task: null`。
- **冲突（stale）**：请求带 `client_rev`（或旧的 `client_updated_at`）且与**该任务在本批的基准值**不一致时：
  - `update`：**不覆盖**服务端，返回 `stale=true` + 服务端最新 TaskDTO（App 用最新值刷新本地并提示「电脑端已修改」）；
  - `complete` / `uncomplete` / `ignore` / `restore` / `snooze` / `delete`：照常执行，同样返回 `stale=true` + 结果 TaskDTO（状态类操作是明确意图，直接生效）。
- **批内基准 rev（重要）**：同一批里同一任务可能有多条操作（离线积压时很常见，例如 `complete` → `restore` → `update`），
  它们带的是 App 本地**同一个**旧 `client_rev`。服务端把该任务在**本批首次出现时**的服务端 `rev`/`updated_at`
  记为「批内基准」，本批后续动作一律与基准比较，而不是与本客户端前面动作抬高后的当前 `rev` 比较；
  否则同一端的连续操作会被误判 stale，`update` 不生效并返回服务端旧值，App 覆盖本地后用户离线期间的编辑就丢了
  （2026-10-27 事故）。即：批内首次动作的 `client_rev` 与基准一致时，整批对该任务的操作都算「同一端连续操作」，全部生效；
  真实电脑端冲突（批内首次动作就已过期）仍按上面的规则处理（`update` 不覆盖，状态类照常执行但标 stale）。
- **跨批必须重基 `client_rev`**：「批内基准」语义只在**同一批**内成立。App 收到某任务的新 TaskDTO（包括自己操作返回的 `task`）后，
  必须用返回的 `rev` 重写该任务 outbox 里剩余操作的 `client_rev`（安卓端已按此实现）；否则下一批仍带旧 `rev`，
  会被服务端正确判为 stale，同样会造成编辑丢失。
- **同批 create 的 `local_id` 解析**：同一批里 `create` 之后引用其 `local_id` 的动作（例如离线「新建后立刻编辑」时
  outbox 里的 `[create(local_id=local-x), update(task_id=local-x)]`），服务端会自动把 `local_id` 解析成本批新建任务的
  真实 `task_id` 再执行（整批重发/去重命中同样适用）；这类任务由本客户端在本批刚创建、批内不可能有电脑端并发修改，
  所以**不会**被判定 `stale`，`update` 必定生效并返回更新后的 TaskDTO。
- 单条失败不影响其它条；`ok=false` 时给 `error`：`not_found | invalid_fields | unsupported_type | bad_request`。
- App 收到 `ok=true` 的 TaskDTO 后**用服务端值覆盖本地行**（并更新本地 `rev`）。
- **响应里的 `cursor` 只是信息字段，App 不得用它推进 `/sync` 游标**：服务端执行完会更新全局 rev，该
  `cursor` 会覆盖「离线期间电脑端产生、手机尚未拉取」的 rev 区间（新增任务与删除墓碑都会被跳过），
  拿它去拉增量会永久漏掉这批变更（2026-10-28 事故：手机待办比电脑少一条，根因即此）。App 必须继续用
  **上一次 `/sync` 响应保存的 cursor** 拉增量（见 §6 第 3 步），actions 的 `cursor` 仅可用于日志/调试展示。

## 6. App 侧同步算法（必须按此实现，否则会丢操作）

本地表：`tasks`（服务端镜像 + 本地新增）、`outbox`（未确认操作）、`sync_state`（cursor / active_url / last_sync_at）。

1. **落库**：`/sync` 返回的 TaskDTO 覆盖本地同 id 行。**例外**：若该行有 pending 的 outbox 操作（如刚点了完成还没推送），
   保留本地 `status/字段`，只更新其它列；等 outbox 清空后自然被服务端值覆盖。
2. **推送 outbox**（按 created 升序，最多 50 条/批）：
   - 失败（网络/5xx）：停止推送，本次同步结束（保留 outbox，下次重试）；
   - 401：停止并标记「token 失效，需重新配对」；
   - `deduped` / `ok`：从 outbox 删除该条，用返回 TaskDTO 覆盖本地；
   - `ok=false`：`not_found` → 删除本地行；其余 → 从 outbox 删除并记录日志（避免死循环）。
   - `create` 成功：把该 action 的 `local_id` 映射到服务端 `task.id`：删本地 local 行，插入服务端行，
     并把 outbox 中其它引用该 `local_id` 的操作的 `task_id` 改写成真实 id（并确保它们使用最新 `client_updated_at`）。
3. **拉增量**：`/sync?cursor=` → 落库 → 处理 deleted → 保存新 cursor → `has_more` 则继续。
   这里的 `cursor` 必须是**上一次 `/sync` 响应保存的 cursor**；**禁止**用 `/api/app/actions` 响应的
   `cursor` 推进（见 §5 末条：会跳过离线期间电脑端新增任务/删除墓碑的 rev 区间）。
4. **首次配对**：cursor=0 触发 full，清空本地任务表重灌。
5. **触发时机**：App 启动/回到前台、下拉刷新、手动「立即同步」、网络从断到连、WorkManager 周期任务（15 分钟）、
   以及每条本地操作后立即尝试后台推送（失败就留给下次）。

**通知与提醒**

- 新待办通知：同步后若出现「新出现的 pending 任务」（非首次同步），发一条汇总通知「新增 N 条待办」，
  正文列前 3 条标题；点击打开 App。用户可在设置里关闭。
- 到点提醒：对每个 `status=pending` 且 `due_at` 在未来的任务，用 `AlarmManager` 预约本地闹钟（`setExactAndAllowWhileIdle`；
  无权限时退化为 `setAndAllowWhileIdle`）。到点发通知「到点：<title>」，点击进任务的详情。
  每次同步、开机（`BOOT_COMPLETED`）、改时区后重排；已完成/忽略/稍后/删除的任务取消闹钟。
- 权限：`POST_NOTIFICATIONS`（Android 13+ 运行时申请）、`SCHEDULE_EXACT_ALARM` / `USE_EXACT_ALARM`、
  `RECEIVE_BOOT_COMPLETED`、`WAKE_LOCK`、`INTERNET`、`ACCESS_NETWORK_STATE`。
  HyperOS（小米）额外引导：设置页给「自启动」「省电策略→无限制」「精确闹钟」三个入口按钮（Intent 跳系统页，跳不过去就显示文字指引）。

## 7. App 功能范围（v1）

页面（2 个主页面 + 2 个弹层，黑白极简，跟随系统深色）：

1. **任务列表**（主页）
   - 顶部：标题「微信待办」+ 同步状态（上次同步时间/同步中/离线）+ 手动同步 + 设置入口；
   - 分段筛选：`今天 / 本周 / 以后 / 已完成`（按 `due_at` 分组：逾期和今天 → 今天；7 天内 → 本周；更远或无 → 以后；
     `status=done` → 已完成；`ignored/snoozed` 收进「已完成」页的二级筛选或单独入口，不能丢）；
   - 列表：圆形勾选圈、标题（最多 2 行）、到期标签（逾期红色）、群名、来源数；点击进详情；
   - 空态、离线提示、加载/错误态、下拉刷新；
   - FAB「+」新建任务。
2. **任务详情**（全屏或 bottom sheet，因内容多建议全屏）
   - 可编辑：标题、备注、截止时间（DatePicker + TimePicker，可清除）、优先级（高/普通/低）；
   - 操作按钮：完成/取消完成、稍后 1 小时、忽略、恢复待办、删除（二次确认）；
   - 只读：所属群、创建/更新时间、所属事项标题与摘要、微信原文来源列表（发送人 + 时间 + 内容，可复制）；
   - 保存修改走 `update` action；所有操作本地立即生效（乐观更新）再后台推送。
3. **设置**
   - 配对：粘贴配对码 / 手动填地址+token / 显示当前地址列表并「测试连接」；
   - 同步：立即同步、上次同步时间、cursor 调试信息（可折叠）；
   - 通知：新待办通知开关、到点提醒开关、通知权限状态；
   - 系统权限引导（小米）：自启动、省电无限制、精确闹钟；
   - 关于：版本号、电脑端版本、服务器地址。

其他要求：

- 不支持横屏特殊布局也行，但 390dp 宽必须无横向滚动、点击区域 ≥44dp；
- 深色模式：背景 #0E0E0E，卡片 #1A1A1A，文字 #EDEDED；浅色：白底黑字（和网页版一致）；
- 列表 100 条内不卡顿；冷启动 <2 秒出缓存；
- 所有网络请求超时：连接 5s、读取 15s；批量接口 30s。

## 8. 工程与构建

- 工程目录：`D:\微信通知总结\wechat-task-hub\android\`（独立 Gradle 工程；**不要改 `android/` 以外任何文件**）。
- 脚手架直接复用本机已验证可构建的工程 `D:\ds\LingSuNote`（AGP 8.7.3 / Kotlin 2.0.21 / Gradle 8.10.2 / Compose BOM 2024.12.01 /
  compileSdk 35 / minSdk 26 / JDK 17）。拷贝：`gradlew`、`gradlew.bat`、`gradle/`、`build.gradle.kts`、`settings.gradle.kts`、
  `gradle.properties`、`local.properties`（`sdk.dir=D:/Android/LocalAndroid/Sdk`）、`app/proguard-rules.pro`、
  `app/src/main/res/{values,values-night,mipmap-anydpi-v26,drawable}` 按需改，再重写 `app/build.gradle.kts` 与源码。
- 包名：`com.wxtask.hub`；应用名：`微信待办`；`versionCode 2`，`versionName "1.0.1"`（1.0.1 起 Room 为 v2，含 v1→v2 游标自愈迁移）。
- 依赖：Compose（BOM 2024.12.01）、Room 2.6.1（KSP）、WorkManager `androidx.work:work-runtime-ktx:2.10.0`、
  OkHttp 4.12.0 + kotlinx-serialization-json 1.7.3、DataStore Preferences 1.1.1。
- 构建：`set JAVA_HOME=D:\Android\jdk17\jdk-17.0.20+8` 后 `.\gradlew.bat assembleDebug`（网络直连 Maven，无需代理）。
- 真机：`C:\platform-tools\adb.exe -s 8b7b2e6a install -r app\build\outputs\apk\debug\app-debug.apk`，
  启动 `adb shell am start -n com.wxtask.hub/.MainActivity`，截图 `adb exec-out screencap -p > shot.png`。
- 调试用配对注入（方便 adb 测试，也用于以后扫码）：MainActivity 处理
  `wxtask://pair?code=<配对码>`：
  ```
  adb shell am start -a android.intent.action.VIEW -d "wxtask://pair?code=<code>" com.wxtask.hub
  ```
  （Manifest 配 intent-filter，scheme=wxtask，host=pair；App 解析 code 后自动保存并同步。）
- APK 交付：`app-debug.apk` 用于真机调试；`assembleRelease`（沿用 debug 签名）产出 `app-release.apk` 作为长期安装包，
  两者都放在 `android/dist/` 备份。

## 9. 服务端实现位置（供对照，App 不需要关心）

- `app/mobile.py`：路由与业务逻辑；
- `app/db.py`：`tasks.rev` 自增、`task_tombstones`、`app_actions` 幂等表、`revisions` 计数器；
- `web/settings.html/js`：设置页「手机 App（直连同步）」卡片（显示配对码、地址、复制、同步状态）。
- 旧 CalDAV（`/dav/`、Tasks.org）已在当前版本删除，App 是唯一的手机同步方式。
