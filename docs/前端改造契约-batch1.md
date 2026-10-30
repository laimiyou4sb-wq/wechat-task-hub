# 前端改造契约 · 批次 1（桌面三栏 + 黑白极简 + 编辑/找回/撤销）

> 状态：**执行中**。本文件是 `index.html`（已改好）、`app.js`、`styles.css` 三方之间的唯一契约。
> 目标设备：**电脑端 ≥1024px 为全新三栏布局；<1024px 保持现有布局结构，仅把配色换成黑白灰**（不加手机端新功能）。

---

## 0. 文件所有权（写操作范围，避免互相覆盖）

| 角色 | 写入范围 |
| --- | --- |
| Lead | `web/index.html`、`web/sw.js`、`docs/`、验证脚本与截图 |
| theme | `web/styles.css`、`web/settings.css` |
| app | `web/app.js` |
| backend | `app/main.py`、`app/api.py`、`tests/test_api.py` |

**禁止跨范围写文件。** 需要对方改类名/结构时，先在共享任务里留言，由 Lead 裁决。

---

## 1. 视觉基线（已确认）

- 白底（`#fff`）+ 黑（`#111`）为主，**全站零彩色**（无绿、无红、无蓝）。逾期/优先级只能用**字重、左侧竖线、黑点标签、等宽小字**表达。
- 桌面：顶栏 56px（品牌 / 搜索 / 同步状态 / 刷新 / 设置）+ 三栏 `196px | 自适应 | 388px`，栏间 1px 发丝线（`rgba(0,0,0,.075)`），全屏铺满、无外边距、无卡片阴影堆叠。
- 详情栏 sci-fi 元素（纯 CSS，固定写在 `index.html`，不要用 JS 生成）：
  - 极淡点阵网格（`.detail::before`，16px 网格、不透明度 ≈0.10、顶部渐隐）
  - 四角 HUD 括线（`.hud.hud-tl/tr/bl/br`，11px、1px 描边、`rgba(0,0,0,.32)`）
  - 底部星轨表盘（`.orbit`，168px：外圈刻度环 `.orbit__dial`、三条轨道 `.orbit__ring`、两颗公转卫星 `.orbit__sat`、中心核 `.orbit__core`），`prefers-reduced-motion` 时停止动画
  - 等宽铭牌小字（`.code`、`.detail__art p`）
- 参考文件：`output/frontend-preview-v2/desktop-v3.html`（可直接搬运其中桌面端样式，再适配真实类名）。
- 窄屏：保留今天的移动端布局（顶栏 + 4 个 tab + 列表 + 底部抽屉 + FAB），**只换色**；`--text-3` 等次要文字需 ≥4.5:1 对比度（白底建议 `#6b6b73`）。

---

## 2. `index.html` 已提供的 DOM 契约（不要改结构，只可读取）

### 2.1 顶栏
| 元素 | id / 类 | 说明 |
| --- | --- | --- |
| 顶栏 | `#topbar.topbar` | 桌面与移动共用 |
| 品牌 | `.brand` `.orbit-mark` `.brand-title` `.brand-tag` | |
| 搜索 | `#searchBox.search` → `#searchInput` + `.search-kbd` | 桌面显示；窄屏可隐藏 |
| 同步状态 | `#syncStatus` `#syncDot` `#syncText` | 保持原 id |
| 刷新 / 设置 | `#refreshBtn` `#settingsLink` | 保持原 id |

### 2.2 导航
| 元素 | id | `data-tab` |
| --- | --- | --- |
| 移动 tabs（4 个） | `#tabs` → `#tab-today/week/later/done` | today/week/later/done |
| 桌面左导航（6 个） | `#rail` → `#nav-today/week/later/done/ignored/snoozed` | 同上 + ignored/snoozed |
| 计数 | `[data-count="today"]` 等 | JS 更新文本 |
| 本周进度 | `#weekProgressText` `#weekProgressBar` | 文本 `完成 / 总数`，bar 用 `style.width` |

### 2.3 列表
| 元素 | id / 类 | 说明 |
| --- | --- | --- |
| 列表头 | `#listHead` → `#listTitle` `#listSub` `#newTaskBtn` | 标题/计数/新建按钮 |
| 列表 | `#taskList`（`ul`） | JS 渲染 |
| 骨架屏 | `#skeleton` | 首屏 |
| 状态视图 | `#stateView` | 空态/错误态 |
| 横幅 | `#bannerSlot` | 错误横幅 |
| FAB | `#addBtn` | 窄屏新建 |

### 2.4 桌面详情面板
| 元素 | id / 类 | 说明 |
| --- | --- | --- |
| 面板 | `#detailPanel.detail` | 窄屏 CSS 隐藏 |
| 头部 | `.detail__head` → `#detailCode` `#detailState` | `TASK · 0000`、`只读`/`编辑中` |
| 内容 | `#detailBody.detail__content` | **JS 唯一可替换区域** |
| 装饰 | `.detail__art` → `#detailOrbitCode` | 静态，不要删 |
| 底部操作 | `#detailFoot.detail__foot` | JS 渲染按钮 |

### 2.5 移动端（保持原状）
`#sheetOverlay` / `#detailSheet` / `#detailClose` / `#detailScroll` / `#detailFooter` / `#newSheet` / `#newClose` / `#newTitleInput` / `#newDueInput` / `#newNotesInput` / `#newError` / `#newSubmit`
新增（窄屏也显示）：`#newPrioritySeg`（`.seg-btn[data-priority]`）、`#newDueChips`（`.chip[data-due]`）。

---

## 3. app.js 行为契约

### 3.1 视图
- `state.tab ∈ {today, week, later, done, ignored, snoozed}`。
- 桌面 `#rail` 与移动 `#tabs` 双向同步（同一 `state.tab`）；移动端 `ignored/snoozed` 暂不暴露（桌面才可进入）。
- `#listTitle` / `#listSub` 随视图更新：今天 → `今天` + `N 项 · M 逾期`；已完成 → `已完成`；已忽略 → `已忽略`；稍后 → `稍后`。
- 计数由 `state.tasks` 计算写入所有 `[data-count]`；「已完成」徽标只显示 done 数。

### 3.2 桌面判定
- 用 `const desktop = matchMedia('(min-width: 1024px)')`，监听 `change` 重渲染。
- `desktop.matches` 为真：详情渲染进 `#detailBody` / `#detailFoot`；为假：沿用底部抽屉。

### 3.3 详情面板（桌面）
- 未选中：`#detailBody` 显示 `.detail-empty` 提示；`#detailFoot` 为空；`#detailState` = `只读`。
- 选中：`#detailState` = `只读`，`#detailCode` = `TASK · <id 前 4 位大写>`，`#detailOrbitCode` 同步。
- 读态 DOM：
```html
<div class="detail-read">
  <h3 class="detail-title">标题</h3>
  <div class="detail-tags"><span class="tag">逾期 2 天</span>…</div>
  <dl class="detail-meta">
    <div><dt>截止时间</dt><dd>今天 18:00</dd></div>
    <div><dt>优先级</dt><dd>高</dd></div>
    <div><dt>来源</dt><dd>项目群</dd></div>
    <div><dt>状态</dt><dd>待办</dd></div>
  </dl>
  <div class="detail-notes"><b>备注</b><p>…</p></div>
  <div class="source"><b>来源原文 · 项目群</b>@所有人 …</div>
</div>
```
- 底部按钮（读态）：`完成`（primary）/ `稍后` / `忽略` / `编辑`，类 `.btn`、`.btn-primary`、`.btn-ghost`。

### 3.4 编辑（桌面，批次 1 必做）
- 点「编辑」→ `#detailState` = `编辑中`，`#detailBody` 换成 `.edit-form`：
  - 标题 `input.input[name=title]`（maxlength 120）
  - 优先级 `.seg` 三个 `.seg-btn[data-priority]`
  - 截止时间 `.chips` 四个 `.chip[data-due]`（`today-18` / `tomorrow-09` / `friday-18` / `clear`）+ `input.input[type=datetime-local][name=due_at]`，两者双向同步
  - 备注 `textarea.input[name=notes]`（maxlength 2000）
  - `.form-error[hidden]` 错误位
- 底部按钮：`保存修改`（primary）/ `取消`。
- 保存：只 `PATCH` 变更字段；`due_at` 清空发 `""`；`notes` 可传空串；成功后用返回的 `task` 合并进 `state.tasks`，重渲染列表与详情，toast「已保存」，**不再额外 `loadTasks()`**。
- `Esc` 取消编辑；编辑中 60s 轮询不得覆盖表单（可跳过重渲染，只更新其他任务）。

### 3.5 找回已忽略 / 稍后
- 视图 `ignored` / `snoozed` 的任务卡片右侧提供 `恢复` 按钮（`.task-restore`）。
- 恢复 = `PATCH {status:'pending'}`；成功后 toast「已恢复」并刷新。
- 卡片右上角 `pri` 与 `task-restore` 同时存在时，恢复按钮优先显示。

### 3.6 撤销（批次 1 必做）
- `showToast(message, type, options)` 扩展：`{ actionLabel, onAction, duration=5000 }`。
- 完成 / 忽略 / 稍后 成功后都给「撤销」：完成→`status:pending`；忽略→`status:pending`；稍后→`status:pending`（`snooze_until` 由后端忽略）。
- toast DOM（CSS 需要）：
```html
<div class="toast" role="status"><span class="toast-text">…</span><button class="toast-action" type="button">撤销</button><span class="toast-timer"></span></div>
```
- 同时最多 2 条，5s 自动消失，`pointer-events:auto`；撤销后焦点回到原任务卡片。

### 3.7 增量渲染与焦点
- 列表按 `data-id` 复用节点：新增插入、删除移除、状态变化只更新对应卡片（可用 `Map`）。
- 渲染后若 `document.activeElement` 在列表内，按 `data-id` 恢复；找不到则聚焦同位置上一项，否则 `#listTitle`。
- 60s 轮询、`visibilitychange`、`online` 刷新同样走增量渲染。

### 3.8 搜索（基础版，批次 2 再增强）
- `#searchInput` 输入 200ms 防抖，按 `title / notes / group_name` 本地过滤（跨视图，显示 `状态` 标签）。
- `/` 聚焦搜索；`Esc` 清空并还原视图；空结果显示 `.empty-result` 文案。
- 搜索中 `#listTitle` = `搜索结果`，`#listSub` = `N 项`。

### 3.9 新建
- `#newTaskBtn`（桌面）与 `#addBtn`（窄屏）都打开 `#newSheet`；桌面下 sheet 居中显示。
- 新建提交带 `priority`（取 `#newPrioritySeg` 选中值）；截止时间 chips 与 `datetime-local` 同步。

### 3.10 其它
- 保持现有令牌鉴权、401 流程、SW 注册、`aria-live`、焦点陷阱。
- 保留 `loadTasks({silent, notify})` 接口，去掉写操作后的全量 GET。
- 「已完成/已忽略/稍后」的卡片仍可点击打开详情。

---

## 4. styles.css 契约（theme）

### 4.1 必须支持的类名
移动端既有类名全部保留（`.app-top` `.header` `.tabs` `.tab` `.badge` `.task-list` `.task-card` `.skeleton*` `.state-view` `.banner*` `.fab` `.sheet*` `.toast*` `.auth-*` `.btn` …），只改颜色。

桌面新增/替换类名：
```
.shell  .rail  .rail__label  .rail__sep  .rail__foot  .rail__bar  .nav-item  .nav-item.is-active  .nav-item b
.topbar  .brand  .orbit-mark  .brand-title  .brand-tag  .search  .search-kbd  .top-right  .icon-btn  .status  .sync
.list-head  .list-title  .list-sub  .list-spacer  .chip-btn  .task  .check  .task-main  .task-title  .task-meta  .tag
.pri  .pri-high  .group  .group h3  .group em  .rule  .task-restore
.detail  .detail__head  .detail__scroll  .detail__content  .detail__foot  .detail__art  .detail-empty
.hud  .hud-tl  .hud-tr  .hud-bl  .hud-br  .orbit  .orbit__dial  .orbit__ring  .orbit__sat  .orbit__core  .code
.detail-read  .detail-title  .detail-tags  .detail-meta  .detail-notes  .source
.edit-form  .field  .input  .seg  .seg-btn  .chips  .chip  .form-error  .btn-ghost
.toast  .toast-text  .toast-action  .toast-timer
.empty-result
```

### 4.2 响应式要求
- `@media (min-width: 1024px)`：启用三栏；隐藏 `#tabs`、`.tabs-wrap`、`#addBtn`、`.sheet-overlay`；`#searchBox` 显示；`.main` 去掉 FAB 预留底部内边距。
- `<1024px`：保持现有布局；新增的 `.rail`、`#detailPanel`、`.list-head` 中桌面专用部分隐藏（`#newTaskBtn` 可保留在窄屏列表头也可隐藏，二选一但要能在窄屏新建）。
- 详情面板 `#detailBody` 为空（未选中）时不塌陷；`.detail__art` 在内容不足时贴底（`margin-top:auto`）。
- 320 / 390 / 768 / 1024 / 1440 / 1920 无横向滚动。
- 全站禁止绿色/红色/蓝色状态色；聚焦环统一 2px 黑色描边（深色块上用白色描边）。

---

## 5. backend 契约（batch 1）

1. `app/main.py`：`GZipMiddleware(minimum_size=500)`，验证 HTML/CSS/JS/JSON 带 `content-encoding: gzip`。
2. `app/api.py` `TaskPatch` 增加 `notes: str | None = None`；`update_task` 处理：`payload.notes is not None` → `fields["notes"] = payload.notes.strip()[:2000]`。
3. 确认 `db.update_task` 支持 `notes`，并检查是否触发 CalDAV 同步（与现有 title/due 修改一致）。
4. 测试：新增 PATCH notes 用例；不改旧用例语义。`python -m pytest -q` 全绿。
5. 改完重启 `python run.py`（PID 见现场），`curl -I -H "Accept-Encoding: gzip"` 验证。

---

## 6. 批次 1 验收清单

- [ ] 1440×900 / 1920×1080 三栏布局，无横向滚动，无卡片阴影堆叠
- [ ] 390×844 仍是原移动布局（仅换色），底部抽屉可开
- [ ] 点任务 → 右侧详情；点「编辑」→ 改标题/优先级/截止/备注 → 保存后列表与详情同步
- [ ] 左导航「已忽略 / 稍后」可进入并把任务恢复为待办
- [ ] 完成/忽略/稍后 → toast 有「撤销」，5 秒内可回退
- [ ] 键盘 Tab 到卡片复选框 → 空格完成 → 焦点仍在列表内
- [ ] 搜索框输入即过滤，`Esc` 清空
- [ ] 首页响应 `content-encoding: gzip`，传输 ≤22 KB
- [ ] `python -m pytest -q` 全绿
- [ ] 零控制台报错；黑白灰、无彩色
