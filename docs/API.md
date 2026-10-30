# API 契约（前后端约定）

服务地址：`http://<电脑IP>:8765`。除 `/`、`/static/*`、`/api/health` 外，所有 `/api/*` 都要求鉴权：

- 请求头：`X-Auth-Token: <APP_TOKEN>`
- 或查询参数：`?token=<APP_TOKEN>`

鉴权失败返回 `401 {"detail":"unauthorized"}`。

## 数据模型

```json
Task {
  "id": "8f3c...",                       // string
  "title": "周五前提交报名表",
  "notes": "来源原文或补充说明",
  "due_at": "2026-10-25T18:00:00",       // ISO 本地时间，可为 null
  "priority": "normal",                   // high | normal | low
  "status": "pending",                    // pending | done | ignored | snoozed
  "group_name": "项目群",                  // 可为空字符串
  "event_id": "b581...",                  // 所属事项（事件账本）
  "created_at": "2026-10-23T21:00:00",
  "updated_at": "2026-10-23T21:00:00",
  "snooze_until": null,
  "source_count": 2
}
```

## 接口

### 1. 任务列表
`GET /api/tasks?status=pending|done|all&limit=200`

返回：
```json
{"tasks": [Task], "counts": {"today": 3, "week": 5, "later": 2, "done": 7}}
```
`status` 默认 `pending`。按 `due_at` 升序（无 due 的排最后），再按创建时间倒序。

### 2. 任务详情
`GET /api/tasks/{id}`

返回：
```json
{
  "task": Task,
  "sources": [{"content": "...", "group_name": "...", "sender": "...", "ts": 1760000000}],
  "event": {
    "id": "b581...",
    "session_id": "123@chatroom",
    "group_name": "项目群",
    "title": "周五前交报名表",
    "summary": "老师在群里通知周五前交报名表。",
    "status": "open",
    "created_at": "2026-10-23T21:00:00",
    "updated_at": "2026-10-26T09:30:00",
    "last_msg_ts": 1760000000,
    "closed_at": null
  }
}
```
- `event` 是任务所属的「事项」（事项账本），`status` 为 `open`（仍在归并新消息）/ `closed`（已关闭）；手动添加的任务为 `null`。
- `sources` 是该任务的全部来源消息（原始微信消息，`ts` 为 Unix 秒）。
- `Task.match_reason` 是「判定依据」（如 `@我（001大王）`、`直呼我（大王）`）；为空表示没有 @/点名信息（手动任务、普通群公告等）。
- `Task.source_count` 是来源消息条数（列表接口同样返回），前端可用于提示「由 N 条消息归纳」。

### 3. 修改任务
`PATCH /api/tasks/{id}`

Body（字段都可选）：
```json
{"status": "done|pending|ignored|snoozed", "snooze_until": "2026-10-24T08:00:00", "title": "可选", "due_at": "可选", "priority": "可选"}
```
返回 `{"task": Task}`。`status=done` 时服务端填 `completed_at`；恢复 `pending` 时清空。

### 3.1 删除任务（批次 3）
`DELETE /api/tasks/{id}`

物理删除，不可恢复。返回 `{"ok": true, "id": "..."}`；不存在返回 `404 {"detail":"task not found"}`。
删除会写墓碑，手机 App 下次增量同步时从本地移除该任务。

### 4. 手动新建任务
`POST /api/tasks`
```json
{"title": "买牛奶", "due_at": "2026-10-24T20:00:00", "notes": ""}
```
返回 `{"task": Task}`（201）。

### 5. 健康状态
`GET /api/health`（无需鉴权）
```json
{
  "ok": true,
  "data_source": "wcda",
  "source": {"type": "wcda", "configured": true, "connected": true, "last_event_at": "2026-10-23T23:09:41",
             "stream_mode": "per-session", "streams": 14},
  "wcda": {"configured": true, "base": "http://127.0.0.1:10392", "connected": true},
  "weflow": {"configured": false, "connected": false, "last_event_at": null},
  "llm": {"configured": true},
  "push": {"bark": true, "ntfy": false},
  "whitelist": {"mode": "pinned", "custom_count": 0, "pinned_count": 13, "pinned_ready": true},
  "identity": {"account": "wxid_xxx", "account_source": "auto", "filter_enabled": true,
               "fuzzy_enabled": true, "groups_cached": 13,
               "judgments": {"kept_me": 3, "kept_all": 1, "kept_none": 12,
                             "dropped_other": 5, "since": "2026-10-26T08:30:00"}},
  "only_new_messages": true,
  "tasks": {"pending": 5},
  "version": "0.1.0"
}
```
`source` 表示当前启用的数据源；`weflow` / `wcda` 分别是两个数据源的明细（兼容旧字段）。

### 5.1 LLM 设置（网页设置页使用，需要鉴权）

`GET /api/settings/llm`
```json
{"configured": true, "enabled": true, "base_url": "https://api.deepseek.com/v1",
 "model": "deepseek-chat", "timeout": 60, "key_saved": true, "key_hint": "sk-…abcd"}
```

`PUT /api/settings/llm`
```json
{"api_key": "sk-...", "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat", "enabled": true}
```
- `api_key` 省略 = 不修改；传 `""` = 清除；
- 保存后立即生效，并写回项目根目录 `.env`；返回同上结构 + `persisted`。

`POST /api/settings/llm/test`
```json
{"ok": true, "model": "deepseek-chat", "reply": "OK"}
```
失败时 `{"ok": false, "error": "..."}`。

### 5.2 推送设置（网页设置页使用，需要鉴权）

`GET /api/settings/push`
```json
{"configured": true, "bark": true, "bark_saved": true, "bark_hint": "api.day.app/…1234",
 "ntfy": false, "ntfy_server": "https://ntfy.sh", "ntfy_topic": "", "ntfy_token_saved": false}
```

`PUT /api/settings/push`
```json
{"bark_url": "https://api.day.app/xxx", "ntfy_server": "https://ntfy.sh",
 "ntfy_topic": "wx-tasks-xxx", "ntfy_token": ""}
```
字段省略 = 不修改；`""` = 清除。保存后立即重建推送客户端并写回 `.env`。

### 5.3 消息范围 / 白名单（网页设置页使用，需要鉴权）

`GET /api/settings/filters`
```json
{"include_private": true, "whitelist_mode": "pinned", "group_whitelist": [],
 "pinned_count": 13, "pinned_ready": true, "only_new_messages": true}
```

`PUT /api/settings/filters`
```json
{"include_private": true, "whitelist_mode": "pinned_custom",
 "group_whitelist": ["项目群", "wxid_xxxx"]}
```
`whitelist_mode`：`all` / `pinned` / `custom` / `pinned_custom`。保存后立即生效并写回 `.env`。

### 5.4 身份与判定（@ / 点名；网页设置页使用，需要鉴权）

判定发生在消息进入 LLM/规则提取之前：只有**明确指向某个具体人**的消息才参与判定——
微信原始 `atUsernames`（wxid 级，`notify@all` = @所有人）优先，其次是正文 `@昵称`、
开头直呼（`大王，…`）、指派句式（`让/请/叫…某人`）、回复我。指向别人 → 丢弃；指向我 /
@所有人 → 照常生成待办；没有指定对象（群公告等）→ **完全保持原逻辑**。

`GET /api/settings/identity`
```json
{"account": "wxid_xxx", "account_source": "auto", "data_source": "wcda",
 "filter_enabled": true, "fuzzy_enabled": true, "ttl_seconds": 600,
 "manual_names": ["大王"], "auto_names": ["001大王", "laimiyou001"],
 "aliases": ["大王", "001大王", "laimiyou001"],
 "groups": [{"session_id": "123@chatroom", "group_name": "项目群", "my_nickname": "001大王",
             "members": 14, "fetched_at": "2026-10-26T09:20:00", "stale": false, "error": ""}],
 "judgments": {"kept_me": 3, "kept_all": 1, "kept_none": 12, "dropped_other": 5,
               "since": "2026-10-26T08:30:00"},
 "last_drops": [{"at": "2026-10-26T09:21:03", "group": "项目群", "sender": "张三",
                 "targets": ["某某"], "reason": "@他人（某某）",
                 "content": "@某某 你去交表"}]}
```
- `account_source`：`auto`（自动识别）/ `manual`（`ME_WXID`）/ `none`；
- `groups` 只含已拉取过群成员的群；打开设置页不会触发拉取，避免变慢；
- `judgments` 为本次服务启动以来的计数，`last_drops` 最多 20 条。

`PUT /api/settings/identity`（字段都可选，省略 = 不修改；立即生效并写回 `.env`）
```json
{"me_names": ["大王", "老王"], "me_wxid": "", "identity_filter": true, "identity_fuzzy": true}
```
返回与 GET 相同结构，另加 `"persisted": true`。

`POST /api/identity/refresh`（body 可省略）
```json
{"session_ids": ["123@chatroom"]}
```
不传 `session_ids` 时：重拉当前监控的群（已缓存群 + 置顶群，最多 20 个）；返回
`{"ok": true, "refreshed": 2, ...与 GET 相同}`。

`POST /api/identity/check`（判定测试；带 `session_id` 时首次会拉一次群成员，之后走缓存）
```json
{"text": "@某某 你去交表", "session_id": "123@chatroom"}
```
```json
{"ok": true, "text": "...",
 "verdict": {"kind": "other", "source": "at", "reason": "@他人（某某）",
             "targets": ["某某"], "matched": ""},
 "identity": {"session_id": "123@chatroom", "group_name": "项目群", "group_nickname": "001大王",
              "account": "wxid_xxx", "aliases": ["..."], "members": 14, "auto": true}}
```
`kind`：`me`（需要我参与）/ `all`（@所有人）/ `other`（指向他人，丢弃）/ `none`（未指定，照旧）。
`source`：`at` / `text_at` / `vocative` / `assign` / `quote` / `none`。

`GET /api/identity/judgments`
```json
{"judgments": {"...": 0}, "last_drops": ["...同上..."]}
```

### 5.5 补采 / 置顶同步（实时流漏消息、置顶变化时用，需要鉴权）

`POST /api/pinned/refresh`
```json
{"ok": true, "added": ["49675822102@chatroom"], "pinned_count": 14,
 "pinned": [{"session_id": "...", "group_name": "项目群"}]}
```
立即同步微信里的置顶变化（不用等 `PINNED_REFRESH_SECONDS` 定时刷新）。后台默认每 60 秒同步一次；
`pinned` / `pinned_custom` 模式下，新置顶的聊天会自动纳入监控，并按 `WCDA_NEW_PIN_BACKFILL_MINUTES`（默认 120 分钟）自动回补一次，避免置顶前那几分钟的消息漏掉。

补采为**全自动**，不提供手动触发接口：

- 服务启动 8 秒后先按会话水位补一次，之后每 `WCDA_CATCHUP_SECONDS`（120 秒）一轮；
- 每会话从 `catchup_state.last_ts` 继续（水位模式），单轮最长回看 `WCDA_CATCHUP_MAX_HOURS`
  （默认 168 小时），每轮最多处理约 `WCDA_CATCHUP_ROUND_BUDGET`（默认 2000 条，按批边界最多
  多出 `BATCH_MESSAGES-1` 条），没处理完的由水位记录、下一轮自动继续；
- 水位缺失时用「启动前已入库」的最新消息时间续补；启动前也没有该会话历史时，自动回看
  `WCDA_CATCHUP_EMPTY_HISTORY_HOURS`（默认 24 小时），不盲目拉 7 天；
- 新置顶群仍自动回补最近 `WCDA_NEW_PIN_BACKFILL_MINUTES`（默认 120 分钟）；
- 内部 `window` 模式（新置顶/启动额外回补）**不推进水位**，不会影响水位补全；
- 幂等：已入库的 `rawid` 不会重复处理；任务按 `dedupe_key` 去重；读取失败/积压超过
  `WCDA_CATCHUP_MAX_MESSAGES` 的会话保留原水位，下一轮自动重试。

`GET /api/catchup/state`（需要鉴权）
```json
{"mode": "watermark", "max_hours": 168, "page_size": 500, "round_budget": 2000,
 "empty_history_hours": 24,
 "watermark_sessions": 14, "states": [{"session_id": "...", "last_ts": 1793100000, "updated_at": "..."}],
 "last": {"ok": true, "mode": "watermark", "inserted": 12, "...": "..."}}
```
排查「补到哪里了」用：`states` 是每会话上次完整扫到的时间（unix 秒），最多返回最近 200 个会话；
`mode` 在补采关闭/非 wcda 时为 `off`。

补采增强参数（`.env`，均有默认值，不配也能用）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `WCDA_CATCHUP_MAX_HOURS` | `168` | 水位模式单会话单轮最长回看（小时） |
| `WCDA_CATCHUP_EMPTY_HISTORY_HOURS` | `24` | 无水位且启动前无历史时，自动回看窗口（小时） |
| `WCDA_CATCHUP_PAGE_SIZE` | `500` | 分页每页条数（WCDA offset/order 分页，范围 50~1000） |
| `WCDA_CATCHUP_MAX_MESSAGES` | `50000` | 单会话单轮最多拉取条数（防接口异常失控） |
| `WCDA_CATCHUP_ROUND_BUDGET` | `2000` | 每轮最多处理条数（0=不限制），按批边界略有浮动 |
| `WCDA_CATCHUP_OVERLAP_SECONDS` | `300` | 水位重叠回看；稳态下被 lookback 覆盖，主要在积压续补时生效 |
| `WCDA_CATCHUP_BATCH_MESSAGES` | `80` | 每批归纳条数（另受 `EVENT_MAX_CHARS` 字符数约束） |

后台默认每 `WCDA_CATCHUP_SECONDS`（120 秒）**按水位**自动补采一次；服务启动 8 秒后会先按水位补一次
（开机/断网恢复后自动追赶）；`WCDA_BACKFILL_MINUTES>0` 时再额外按「固定窗口」补一次，保持旧配置语义。
`GET /api/health` 的 `catchup` 字段会显示是否启用、参数、水位会话数与最近一次结果。

### 6. 推送自检
`POST /api/push/test`（需要鉴权）

```json
{"ok": true, "results": [{"channel": "bark", "ok": true, "detail": "..."}]}
```
未配置推送通道时返回 `{"ok": false, "results": [], "reason": "未配置 Bark / ntfy"}`。

### 7. 演示数据（仅 DEMO_MODE=1）
`POST /api/demo/seed`（需要鉴权）
```json
{"ok": true, "messages": 6, "tasks_created": 3}
```
写入几条虚构微信消息并立即走一次「过滤 → 提取 → 入库 → 推送」，用于没有 WeFlow 时体验。

> 分桶口径：`due_at` 为空的任务计入 `counts.later`（与 App / 契约 §7 一致）；`counts.done` 为历史完成总数。

### 8. 安卓 App 直连同步（`/api/app/*`，需要鉴权）

自研安卓 App 专用接口（替代已移除的 CalDAV/Tasks.org 方案）。**完整字段与语义见 [APP-SYNC-CONTRACT.md](APP-SYNC-CONTRACT.md)**，这里只列摘要。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/app/ping` | 心跳，返回 `{ok, app, version, api_version, server_time, tz}` |
| GET | `/api/app/pairing` | 配对信息：`{v, name, token, endpoints:[{kind,url,priority}], code, lan_urls, remote_urls, app_last_sync, app_last_sync_client, task_count}`；`code` 是 base64url(JSON、无 padding) 的配对码，App 粘贴即完成地址+token 配置 |
| GET | `/api/app/sync?cursor=<int>` | 增量拉取：`{server_time, cursor, full, has_more, tasks:[TaskDTO], deleted:[id], counts}`；`cursor=0/非法` 返回 `full=true` 全量快照，`deleted` 为删除墓碑 |
| GET | `/api/app/tasks/{id}` | 详情：`{task: TaskDTO, event: {...}|null, sources:[{rawid, group_name, sender, content, ts}]}`，`ts` 为本地 ISO 字符串 |
| POST | `/api/app/actions` | 批量操作（≤50 条，按序执行）：`{actions:[{action_id, type, task_id?, client_rev?, fields?}]}`，返回每条结果 `{action_id, ok, deduped, stale, task}` |

- `TaskDTO` 比网页 `Task` 多一个 `rev`（整数，单调递增），App 用它做增量游标与冲突检测；
- `action_id` 幂等：同 id 重复提交返回首次结果（`deduped=true`）；
- `type`：`complete / uncomplete / ignore / restore / snooze / update / create / delete`；`client_rev` 与服务端不一致时 `update` 不覆盖（返回 `stale=true` + 最新值），状态类操作照常生效；
- 删除任务会写墓碑（`task_tombstones`），App 增量同步时从本地移除；全量快照不变；
- 配对码里包含 `APP_TOKEN`，等同待办读写权限，只复制给自己手机；换 token 后需重新配对。

### 9. 隧道配套（非 HTTP 接口）

`tools/tunnel.py` 维护 ngrok 隧道，公网 `/api/health` 验证通过后把地址写入 `data/tunnel.json`（`{"url","kind","updated_at"}`），`/api/app/pairing` 会读取它生成远程地址；隧道停止时删除该文件，配对只剩局域网地址。

## 前端要求摘要

- 纯静态：`web/index.html` + `web/app.js` + `web/styles.css`，无构建步骤、无 CDN。
- 设置页 `web/settings.html` 分区顺序：**01 大模型 / 02 手机推送 / 03 消息范围与白名单 / 04 身份与判定 / 05 运行状态 / 06 手机 App（直连同步）/ 07 其余配置**。
  - 「05 运行状态」读 `GET /api/health`：数据源连接、实时流数、最近消息时间与错误；补采开关/间隔/回看上限/水位会话数/上次结果；置顶模式/数量/就绪；事项账本开放数与缓冲；LLM/推送/待办数/版本。补采为全自动，不提供手动触发；页面提供「同步置顶」（`POST /api/pinned/refresh`）、「刷新状态」两个操作。
  - 「06 手机 App」含配对码 + 地址列表 + 最近同步，接口 `/api/app/pairing`，失效时降级提示。
- 通过 `?token=` 读取并保存到 `localStorage`，之后所有请求带 `X-Auth-Token`；401 时显示设置 token 的界面。
- 移动优先，底部/顶部分段：今天 / 本周 / 以后 / 已完成；卡片展示标题、截止时间、群名、优先级；点击弹出详情（原文、完成、稍后、忽略）。
- 任务详情（`GET /api/tasks/{id}`）：显示 `sources` 全部来源原文与条数、`match_reason` 判定依据、`event` 所属事项（标题/摘要/群名/状态：归纳中或已关闭）；手动任务无 `event` 时不显示该区块。
- 列表卡片：`source_count > 1` 时提示「来源 N 条」。
- 数据源异常提示：`/api/health` 的 `source.configured=false` 或 `source.connected=false` 且非 demo 模式时，主页面显示可重试的提示横幅，并附 `source.last_error`；恢复后自动消失。
- 下拉或按钮刷新；60 秒自动刷新；空状态、加载态、错误提示。
- `manifest.webmanifest` + `sw.js`（静态资源缓存，API 不缓存），可添加到主屏幕。
