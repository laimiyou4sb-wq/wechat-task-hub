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
  "source": {"type": "wcda", "configured": true, "connected": true,
             "last_event_at": "2026-10-31T01:03:12", "last_error": null,
             "last_collect_at": "2026-10-31T01:05:00"},
  "wcda": {"configured": true, "base": "http://127.0.0.1:10392",
           "connected": true, "last_error": null},
  "llm": {"configured": true},
  "push": {"bark": true, "ntfy": false},
  "filters": {"group_whitelist": [], "include_private": false},
  "whitelist": {"mode": "pinned", "custom_count": 0, "pinned_count": 13, "pinned_ready": true},
  "events": {"open": 5, "pending_sessions": 2, "pending_messages": 3,
             "failed_sessions": 0, "failed_messages": 0},
  "identity": {"account": "wxid_xxx", "account_source": "auto", "filter_enabled": true,
               "fuzzy_enabled": true, "groups_cached": 13,
               "judgments": {"kept_me": 3, "kept_all": 1, "kept_none": 12,
                             "dropped_other": 5, "since": "2026-10-26T08:30:00"}},
  "collection": {"enabled": true, "mode": "watermark", "seconds": 120,
                 "lookback_minutes": 30, "backfill_minutes": 0, "max_hours": 168,
                 "page_size": 500, "round_budget": 2000, "empty_history_hours": 24,
                 "overlap_seconds": 300, "deep_scan_minutes": 60,
                 "deep_scan_lookback_minutes": 1440, "max_attempts": 3,
                 "watermarks": 14, "cursor_sessions": 14,
                 "pending_messages": 3, "pending_sessions": 2,
                 "failed_messages": 0, "failed_sessions": 0,
                 "degraded": false, "degraded_reasons": [], "partial": false,
                 "llm_batches": 3, "llm_fallbacks": 0, "llm_fallback_total": 0,
                 "last_llm_error": null, "llm_fallback_streak": 0,
                 "extraction_cap_hits": 0, "messages_without_task": 2,
                 "read_rows": 120, "duplicate_rows": 0, "read_ratio": 1.0,
                 "llm": {"enabled": true, "configured": true, "batch_messages": 40},
                 "llm_batch_messages": 40,
                 "deep_scan": {"minutes": 60, "lookback_minutes": 1440, "disabled": false,
                               "due": false, "last_at": "2026-10-31T00:50:00",
                               "last": {"at": "2026-10-31T00:50:00", "inserted": 1,
                                        "processed": 1, "tasks_created": 0,
                                        "scanned": 20, "lookback_minutes": 1440}},
                 "last_ok_at": "2026-10-31T01:05:00",
                 "last": {"ok": true, "mode": "watermark", "minutes": 10080, "sessions": 14,
                          "scanned": 120, "inserted": 12, "processed": 10, "pages": 2,
                          "tasks_created": 3, "tasks_updated": 1, "dropped_other": 1,
                          "failed": 0, "dead_lettered": 0, "failed_queue": 0,
                          "budget_exhausted": false, "skipped": [], "pending": 3,
                          "partial": false, "degraded": false, "degraded_reasons": [],
                          "llm_batches": 3, "llm_fallbacks": 0, "last_llm_error": null,
                          "extraction_cap_hits": 0, "messages_without_task": 2,
                          "read_rows": 120, "duplicate_rows": 0, "read_ratio": 1.0,
                          "sessions_failed": [], "sessions_failed_count": 0,
                          "duration_ms": 3210, "at": "2026-10-31T01:05:00"}},
  "tasks": {"pending": 5},
  "version": "0.3.0"
}
```
`source` 表示当前启用的数据源；`wcda` 是其明细（兼容字段）；`filters` 是当前监控范围快照。

- `source.connected`：`true` 已连接 / 数据源可达（个别会话读取或处理失败时仍为 `true`，具体看 `collection.degraded` / `collection.partial`）；`false` 数据源不可达、本轮整体失败或 sessions 接口失败；`null` 首轮采集尚未完成（前端显示「等待首轮采集」）。
- `source.last_collect_at`：上次采集轮时间（成功或失败，ISO，可为 `null`；与 `collection.last.at` 一致）；`source.last_event_at`：最近一次采集到新消息的时间；`source.last_error`：最近一次数据源 / 处理错误（无错误为 `null`；非空且 `connected=true` 时表示部分会话失败）；**LLM 回退不会写进 `source.last_error`，而是写在 `collection.last_llm_error`**。
- `collection`（`catchup` 是同一 payload 的兼容别名，**deprecated**；前端优先读 `collection`，缺省回退 `catchup`）：`enabled`（是否启用周期采集轮）、`mode`（`watermark` = 按会话 localId 游标 / `off` = 关闭周期轮，此时启动后仍会采一轮）、`seconds`（轮间隔）、`lookback_minutes`（每轮最小回看）、`backfill_minutes`（启动额外回补）、`max_hours`（单会话单轮最长回看）、`page_size`（分页大小）、`round_budget`（每轮处理预算）、`empty_history_hours`（无游标且启动前无历史时的回看窗口）、`overlap_seconds`（退回时间窗时的重叠回看）、`deep_scan_minutes` / `deep_scan_lookback_minutes`（晚到消息深扫间隔 / 回看窗口，`0` = 关闭深扫）、`max_attempts`（单条消息最大重试次数）、`watermarks`（会话游标数）、`cursor_sessions`（已建立 localId 游标的会话数）、`pending_messages` / `pending_sessions`（已入库未处理消息数 / 会话数）、`failed_messages` / `failed_sessions`（失败队列消息数 / 会话数）、`last_ok_at`（上次完全成功时间）、`last`（上次采集结果：`ok`、`mode`、`minutes`、`sessions`、`scanned`、`inserted`、`processed`、`pages`、`tasks_created`、`tasks_updated`、`dropped_other`、`failed`、`dead_lettered`、`failed_queue`、`budget_exhausted`、`skipped`、`pending`、`partial`、`degraded`、`degraded_reasons`、`llm_batches`、`llm_fallbacks`、`last_llm_error`、`extraction_cap_hits`、`messages_without_task`、`read_rows`、`duplicate_rows`、`sessions_failed`、`sessions_failed_count`、`read_ratio`、`at`；失败轮 `ok=false` 且带 `reason`；深扫轮带 `deep_scan=true`）。
- **`ok` / `partial` / `degraded` 语义（P1）**：`last.ok=false` 表示「本轮有会话读取 / 处理失败」（含**部分失败**、整体性失败、sessions 接口失败）；`last.partial=true` 表示本轮仍有消息被处理（即部分成功，失败只影响部分会话）；`sessions_failed` 是失败会话 id 列表（最多 20 个）、`sessions_failed_count` 是失败会话数；`degraded=true` 表示本轮出现了降级原因（部分失败 / sessions 接口失败 / LLM 回退 / 抽取上限 / 采集轮异常）。**仅 LLM 调用失败回退规则提取时 `ok=true` 但 `degraded=true` 且 `last_llm_error` 非空**。`degraded_reasons` 取值码：`llm_fallback`（LLM 回退规则模式）、`extraction_cap`（抽取命中「8 事项 / 12 任务」上限，可能漏建）、`partial_failure`（部分会话失败）、`sustained_failure`（连续多轮整体性失败）、`sessions_unavailable`（会话列表接口失败）、`collect_error`（采集轮未捕获异常，`reason` 为异常信息）。
- `collection.llm_batches` / `llm_fallbacks`：上一轮 LLM 分片数 / 回退规则模式次数；`llm_fallback_total`（进程累计）、`llm_fallback_streak`（连续回退轮数）、`last_llm_error`（最近一次 LLM 错误，无则 `null`）；`collection.llm` 给出 `enabled`（是否启用）、`configured`（是否已配置可用 Key）、`batch_messages`（`LLM_BATCH_MESSAGES`，默认 40 条/批），`collection.llm_batch_messages` 是同一值的便捷别名；顶层 `llm.configured` 是旧字段（是否启用 LLM）。
- `collection.extraction_cap_hits` / `messages_without_task`（上一轮）：抽取命中「8 事项 / 12 任务」上限的次数、已处理但未生成待办的消息条数——只表示待办可能漏建，不是数据错误。
- `collection.read_rows` / `duplicate_rows` / `read_ratio`（上一轮）：分页读取到的行数 / 其中与已入库重复的行数 / 读取放大倍数；分页读取放大只是观测值（按 createTime 翻页所致），不是数据错误。
- `collection.deep_scan`：`minutes` / `lookback_minutes`（深扫间隔 / 回看窗口）、`disabled`（是否关闭）、`due`（本轮是否到期待扫）、`last_at`（上次深扫时间）、`last`（上次深扫结果：`at`、`lookback_minutes`、`scanned`、`inserted`、`processed`、`tasks_created`；未执行过为 `null`；**深扫失败时 `last` 带 `error`、没有 `last_at`**）。
- `events.pending_messages` / `events.pending_sessions`：已入库未处理的消息数 / 会话数；`events.failed_messages` / `events.failed_sessions`：失败队列（重试超限）的消息数 / 会话数。

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
 "pinned_count": 13, "pinned_ready": true, "data_source": "wcda"}
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

### 5.5 采集 / 置顶同步（按会话 localId 游标采集、置顶变化时用，需要鉴权）

`POST /api/pinned/refresh`
```json
{"ok": true, "added": ["49675822102@chatroom"], "pinned_count": 14,
 "pinned": [{"session_id": "...", "group_name": "项目群"}]}
```
立即同步微信里的置顶变化（不用等 `PINNED_REFRESH_SECONDS` 定时刷新）。后台默认每 60 秒同步一次；
`pinned` / `pinned_custom` 模式下，新置顶的聊天会自动纳入监控，并按 `WCDA_NEW_PIN_BACKFILL_MINUTES`（默认 120 分钟）自动回补一次，避免置顶前那几分钟的消息漏掉。

采集是 **wcda 的唯一采集方法、全自动**，不提供手动触发接口：

- 服务启动 8 秒后先按会话 `localId` 游标采集一次，之后每 `WCDA_CATCHUP_SECONDS`（120 秒）一轮；
- **增量游标**：每会话从 `catchup_state.last_local_id` 继续，只收 `localId > 游标` 的消息。`localId` 是微信 WCDB 的写入行序（与 `createTime` 解耦），因此「写入晚、业务时间旧」的晚到消息也能覆盖；WCDA 的 `/api/chat/messages` **没有 localId 过滤参数、分页按 createTime 排序**，客户端逐页过滤，某一页已没有游标之后的行即停止分页；
- **首次建立游标 / 游标失效**（localId 重装 / 换库 / 回绕）：回退时间窗重采，并把游标重设为本批最大 localId——无游标时用「启动前已入库」的最新消息时间续采；启动前也没有该会话历史时回看 `WCDA_CATCHUP_EMPTY_HISTORY_HOURS`（默认 24 小时）；单会话单轮最长回看 `WCDA_CATCHUP_MAX_HOURS`（默认 168 小时），按 `WCDA_CATCHUP_PAGE_SIZE`（默认 500 条/页）分页；
- **晚到消息深扫**：因分页按 createTime 排序，晚到消息可能落在游标扫描深度之外，故每 `WCDA_DEEP_SCAN_MINUTES`（默认 60 分钟，`0` = 关闭）按 `WCDA_DEEP_SCAN_LOOKBACK_MINUTES`（默认 1440 分钟）时间窗重扫一次；深扫**不推进 localId 游标**，靠 `rawid` 去重，不会重复建待办；深扫轮的 `last.deep_scan=true`。**晚到消息的兜底上限就是这个深扫回看窗口（默认 24 小时），不是 `WCDA_CATCHUP_MAX_HOURS`（168 小时）**；断网 / 休眠更久后补同步的晚到消息需调大 `WCDA_DEEP_SCAN_LOOKBACK_MINUTES`，代价是每次深扫多翻页；
- 每轮最多处理约 `WCDA_CATCHUP_ROUND_BUDGET`（默认 2000 条），没处理完的消息保留 `processed=0`，下一轮从 DB 自动兜底继续；
- 每轮拿到消息后**在同一轮内按 `LLM_BATCH_MESSAGES`（默认 40 条/批）分片多次归并**（跨分片共用事项账本，不再按会话一次处理、也不是每个分片单独推送）；**同轮新建的待办在轮末合并成一条推送**（标题「微信待办 +N」，最多 6 条明细），采集轮之外的即时推送行为不变；
- `window` 模式（新置顶回补 / 启动额外回补 / 晚到消息深扫）**不推进游标**，不会影响增量采集；
- 已采集消息入库时 `processed=0`；处理成功后置 `processed=1`；`existing_message_ids` 只跳过 `processed=1`（已处理）与 `processed=2`（失败队列），`processed=0` 的消息每轮自动兜底重试；失败队列的详细语义见 5.6；
- **失败重试与失败队列**：小批次（≤30 条）整批失败后逐条隔离重试，能处理的立即处理；**大积压（>30 条）先隔离探测前 30 条**——探测样本**全部**失败视为 LLM / 网络等整体性故障，本轮只记错误、**不增加 attempts**，下一轮自动重试（不会把积压灌进失败队列）；样本里有成功则按毒消息累计 attempts，多轮逐批排空。连续 3 轮仍整体性失败会推送「采集持续失败」high 告警，并把本轮结果标记为 **`last.ok=false` + `degraded=true`**（`partial=true` 表示本轮仍有消息被处理；`degraded_reasons` 给出 `partial_failure` / `sustained_failure` 等原因码，`source.last_error` 同步记录），**不再出现「`ok` 仍为 true 但 `last_error` 非空」的静默态**；设置页「上次采集」与主页横幅都会显示降级 / 失败原因。单条消息累计失败 `MESSAGE_MAX_ATTEMPTS`（默认 3）次后置 `processed=2` 进失败队列，并推送 high 优先级告警（不受免打扰限制），不再阻塞同一会话后续消息；
- 幂等：任务按 `dedupe_key` 去重；读取失败 / 积压超过 `WCDA_CATCHUP_MAX_MESSAGES` 的会话保留原游标，下一轮自动重试。

`GET /api/collect/state`（主接口，需要鉴权；`GET /api/catchup/state` 为兼容别名，**deprecated**，返回同一 payload）
```json
{"mode": "watermark", "seconds": 120, "lookback_minutes": 30, "backfill_minutes": 0,
 "max_hours": 168, "page_size": 500, "round_budget": 2000, "empty_history_hours": 24,
 "overlap_seconds": 300, "deep_scan_minutes": 60, "deep_scan_lookback_minutes": 1440,
 "max_attempts": 3, "watermark_sessions": 14, "cursor_sessions": 14,
 "pending_messages": 3, "pending_sessions": 2,
 "failed_messages": 0, "failed_sessions": 0, "last_ok_at": "2026-10-31T01:05:00",
 "states": [{"session_id": "...", "last_ts": 1793100000, "last_local_id": 88231,
             "updated_at": "..."}],
 "last": {"ok": true, "mode": "watermark", "sessions": 14, "scanned": 120,
          "inserted": 12, "processed": 10, "pending": 3, "failed": 0,
          "dead_lettered": 0, "failed_queue": 0, "...": "..."}}
```
排查「采到哪里了」用：`states` 是每会话游标（`last_ts` 时间水位、`last_local_id` localId 游标；unix 秒 / 行号），
最多返回最近 200 个会话；`mode` 在周期采集关闭/非 wcda 时为 `off`（关闭周期轮时，服务启动后仍会按游标采一轮）。

采集参数（`.env`，均有默认值，不配也能用）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `WCDA_CATCHUP_SECONDS` | `120` | 采集轮间隔（秒；0=关闭周期轮，仅启动时采一轮） |
| `WCDA_CATCHUP_LOOKBACK_MINUTES` | `30` | 每轮最小回看（分钟；游标为空/失效时的时间兜底） |
| `WCDA_BACKFILL_MINUTES` | `0` | 启动时额外按固定窗口回补（分钟，不推进游标） |
| `WCDA_CATCHUP_MAX_HOURS` | `168` | 单会话单轮最长回看（小时） |
| `WCDA_CATCHUP_EMPTY_HISTORY_HOURS` | `24` | 无游标且启动前无历史时，自动回看窗口（小时） |
| `WCDA_CATCHUP_PAGE_SIZE` | `500` | 分页每页条数（WCDA offset/order 分页，范围 50~1000） |
| `WCDA_CATCHUP_MAX_MESSAGES` | `50000` | 单会话单轮最多拉取条数（防接口异常失控） |
| `WCDA_CATCHUP_ROUND_BUDGET` | `2000` | 每轮最多处理条数（0=不限制），未处理消息保留 `processed=0` 下一轮继续 |
| `WCDA_CATCHUP_OVERLAP_SECONDS` | `300` | 时间水位重叠回看（秒）；只在无 localId 游标、退回时间窗时生效 |
| `WCDA_NEW_PIN_BACKFILL_MINUTES` | `120` | 新置顶聊天自动回补最近 N 分钟（0=不回补） |
| `WCDA_DEEP_SCAN_MINUTES` | `60` | 晚到消息深扫间隔（分钟；0=关闭）：按时间窗重扫、不推进游标 |
| `WCDA_DEEP_SCAN_LOOKBACK_MINUTES` | `1440` | 深扫回看窗口（分钟，默认 24 小时）；晚到消息兜底上限就是它（不是 `WCDA_CATCHUP_MAX_HOURS`），断网更久需调大（代价：每次深扫多翻页） |
| `MESSAGE_MAX_ATTEMPTS` | `3` | 单条消息处理失败最大重试次数，超限进失败队列并告警 |
| `LLM_BATCH_MESSAGES` | `40` | 同一轮内 LLM 归并分片大小（条/批；0=不分批，旧行为）。分片避免单次 prompt 过大及「8 事项 / 12 任务」上限在洪峰下静默饱和，命中上限会计入 `extraction_cap_hits` |

后台默认每 `WCDA_CATCHUP_SECONDS`（120 秒）按 localId 游标自动采集一次；服务启动 8 秒后会先按游标采一次
（开机/断网恢复后自动追赶）；`WCDA_BACKFILL_MINUTES>0` 时再额外按「固定窗口」补一次（不推进游标）。
`GET /api/health` 的 `collection` 字段（`catchup` 为兼容别名，deprecated）会显示是否启用、参数、游标会话数、待处理消息/会话数、失败队列与最近一次结果，
以及降级 / 回退 / 抽取上限 / 读取放大等可观测字段（`degraded`、`degraded_reasons`、`partial`、`llm_batches`、`llm_fallbacks`、`llm_fallback_total`、`llm_fallback_streak`、`last_llm_error`、`extraction_cap_hits`、`messages_without_task`、`read_rows`、`duplicate_rows`、`read_ratio`、`llm`、`llm_batch_messages`、`deep_scan`，见 5 节字段说明）。
`GET /api/collect/state` 的 `last` 与 `collection.last` 同结构，同样含 `partial` / `degraded` / `degraded_reasons` / `llm_batches` / `llm_fallbacks` / `last_llm_error` / `extraction_cap_hits` / `messages_without_task` / `read_rows` / `duplicate_rows` / `read_ratio` / `sessions_failed` / `sessions_failed_count`。

### 5.6 失败队列（重试超限的消息，需要鉴权）

消息处理状态 `messages.processed`：`0` = 已入库待处理、`1` = 已处理（过滤/归并决策完成）、`2` = 失败队列（重试超限，不再自动重试，需人工重试回到待处理）。

> 整体性故障（大积压整批失败且前 30 条隔离探测样本全部失败，如 LLM / 网络中断）不消耗 attempts、不会进入失败队列，下一轮采集自动重试；同时本轮采集会标记 `last.ok=false` + `degraded=true`（`partial=true` 表示仍有进展；`degraded_reasons` 给出原因码），并在 `last_llm_error` / `source.last_error` 记录原因。

`GET /api/collect/failed?limit=100&offset=0`

- `limit`：1~1000，默认 100；`offset`：默认 0；按 `failed_at` 倒序返回。
- 返回失败队列快照：
```json
{"failed": [{"rawid": "...", "session_id": "123@chatroom", "group_name": "项目群",
             "sender": "张三", "content": "@我 明早交材料", "ts": 1793099000,
             "attempts": 3, "last_error": "ValueError: ...",
             "failed_at": "2026-10-31T01:04:00"}],
 "count": 1,
 "sessions": [{"session_id": "123@chatroom", "group_name": "项目群", "failed": 1}],
 "max_attempts": 3}
```
- `failed`：失败消息明细（`attempts` 已尝试次数、`last_error` 最近一次错误、`failed_at` 进入失败队列时间）；`count`：失败消息总数；`sessions`：涉及会话及各自失败条数；`max_attempts`：当前 `MESSAGE_MAX_ATTEMPTS`。

`POST /api/collect/failed/retry`

- body 可省略；可选字段 `rawids`（只重试这些消息）与 `limit`（不传 `rawids` 时按失败时间倒序最多放回多少条，默认 500）。
```json
{"rawids": ["..."]}
```
```json
{"ok": true, "requeued": 1, "remaining": 0}
```
- 放回的消息置为 `processed=0` 并清空 `attempts/last_error/failed_at`，下一轮采集会自动重新处理；`requeued` 为本次放回条数、`remaining` 为剩余失败队列条数。
- 设置页「运行状态 → 采集 → 重试失败队列」调用该接口；失败超限时也会推送 high 优先级告警（不受免打扰限制）。

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
写入几条虚构微信消息并立即走一次「过滤 → 提取 → 入库 → 推送」，用于没有 WeChatDataAnalysis 时体验。

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
  - 「05 运行状态」读 `GET /api/health`：数据源连接三态（已连接 / 未连接 / 等待首轮采集）、上次采集时间、最近消息时间与错误；采集开关/间隔/回看上限/水位会话数/localId 游标数/待处理消息与会话/失败队列/上次采集结果；置顶模式/数量/就绪；事项账本开放数与待处理数；LLM/待办数/版本。采集为全自动，不提供手动触发；页面提供「同步置顶」（`POST /api/pinned/refresh`）、「刷新状态」与「重试失败队列」（`POST /api/collect/failed/retry`）三个操作。
  - 「06 手机 App」含配对码 + 地址列表 + 最近同步，接口 `/api/app/pairing`，失效时降级提示。
- 通过 `?token=` 读取并保存到 `localStorage`，之后所有请求带 `X-Auth-Token`；401 时显示设置 token 的界面。
- 移动优先，底部/顶部分段：今天 / 本周 / 以后 / 已完成；卡片展示标题、截止时间、群名、优先级；点击弹出详情（原文、完成、稍后、忽略）。
- 任务详情（`GET /api/tasks/{id}`）：显示 `sources` 全部来源原文与条数、`match_reason` 判定依据、`event` 所属事项（标题/摘要/群名/状态：归纳中或已关闭）；手动任务无 `event` 时不显示该区块。
- 列表卡片：`source_count > 1` 时提示「来源 N 条」。
- 数据源异常提示：`/api/health` 的 `source.configured=false` 或 `source.connected=false` 且非 demo 模式时，主页面显示可重试的提示横幅，并附 `source.last_error`；恢复后自动消失。
- 下拉或按钮刷新；60 秒自动刷新；空状态、加载态、错误提示。
- `manifest.webmanifest` + `sw.js`（静态资源缓存，API 不缓存），可添加到主屏幕。
