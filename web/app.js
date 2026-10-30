/* 微信待办 · 任务表（原生 JS，无构建、无依赖） */
(() => {
  'use strict';

  const TOKEN_KEY = 'wechat-task-hub.token';
  const REFRESH_INTERVAL = 60 * 1000;
  /* 批次 1 视图：桌面 6 个（含已忽略/稍后），移动端仍只暴露前 4 个 */
  const VIEWS = ['today', 'week', 'later', 'done', 'ignored', 'snoozed'];
  const MOBILE_VIEWS = ['today', 'week', 'later', 'done'];
  /* 批次 4（需求 D）：手机端「已完成」tab 下用分段切换访问的归档视图 */
  const ARCHIVE_VIEWS = ['done', 'ignored', 'snoozed'];
  const VIEW_TITLES = {
    today: '今天',
    week: '本周',
    later: '以后',
    done: '已完成',
    ignored: '已忽略',
    snoozed: '稍后',
  };
  const STATUS_LABELS = {
    pending: '待办',
    done: '已完成',
    snoozed: '已稍后',
    ignored: '已忽略',
  };
  /* 批次 2：搜索结果里的状态标签文案（稍后不带「已」，更紧凑） */
  const SEARCH_STATUS_LABELS = {
    pending: '待办',
    done: '已完成',
    snoozed: '稍后',
    ignored: '已忽略',
  };
  const PRIORITY_LABELS = {
    high: '高优先级',
    normal: '普通',
    low: '低优先级',
  };
  const PRIORITY_SHORT = {
    high: '高',
    normal: '普通',
    low: '低',
  };
  const PRIORITY_OPTIONS = [
    ['high', '高'],
    ['normal', '普通'],
    ['low', '低'],
  ];
  const DUE_CHOICES = [
    ['today-18', '今天 18:00'],
    ['tomorrow-09', '明天 09:00'],
    ['friday-18', '本周五 18:00'],
    ['clear', '清除'],
  ];
  const EMPTY_COPY = {
    today: { title: '今天没有待办', text: '微信里来了新任务，会自动出现在这里。' },
    week: { title: '本周没有待办', text: '接下来几天暂时轻松，先歇一会儿。' },
    later: { title: '以后没有待办', text: '更远期的任务会收在这里。' },
    done: { title: '还没有已完成的任务', text: '勾选完成后，任务会归档到这里。' },
    ignored: { title: '没有已忽略的任务', text: '忽略掉的任务会收在这里，随时可以恢复。' },
    snoozed: { title: '没有稍后的任务', text: '设为稍后的任务会暂时收在这里。' },
  };
  const API = {
    tasks: '/api/tasks',
    task: (id) => `/api/tasks/${encodeURIComponent(id)}`,
    health: '/api/health',
  };
  const SVG_NS = 'http://www.w3.org/2000/svg';
  const ICONS = {
    check: ['M20 6 9 17l-5-5'],
    plus: ['M12 5v14', 'M5 12h14'],
    refresh: ['M20.5 12a8.5 8.5 0 1 1-2.49-6.01', 'M20.5 4v5.5H15'],
    close: ['M18 6 6 18', 'M6 6l12 12'],
    clock: ['M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18Z', 'M12 7.5V12l3 2'],
    trash: [
      'M4 7h16',
      'M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2',
      'M6.5 7l.9 12.1A1 1 0 0 0 8.4 20h7.2a1 1 0 0 0 1-1l.9-12.1',
      'M10 11v5',
      'M14 11v5',
    ],
    inbox: [
      'M4.5 5.5h15L21 13.5v4a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 17.5v-4l1.5-8Z',
      'M3 13.5h4.5l1.5 2.5h6l1.5-2.5H21',
    ],
    checkCircle: ['M21 12a9 9 0 1 1-2.64-6.36', 'M8.5 12.5l2.5 2.5 4.5-5'],
    alert: [
      'M12 9v4',
      'M12 17h.01',
      'M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0Z',
    ],
  };

  const desktopQuery = window.matchMedia('(min-width: 1024px)');

  const state = {
    tasks: [],
    counts: { today: 0, week: 0, later: 0, done: 0, ignored: 0, snoozed: 0 },
    tab: 'today',
    search: '',
    searchRaw: '',
    loaded: false,
    loading: false,
    loadPromise: null,
    error: null,
    connection: 'unknown',
    health: null,
    authOpen: false,
    sheet: null,
    detail: null,
    /* 批次 2：键盘选中态（j/k 移动，与详情分离，Enter 才打开详情） */
    selectedId: null,
    /* 批次 3：批量选择（仅桌面 ≥1024px），selectedIds 存字符串 id */
    selectMode: false,
    selectedIds: new Set(),
    pickAnchorId: null,
    bulkBusy: false,
    deletingId: null,
    editing: null,
    submitting: false,
    saving: false,
  };

  const dom = {};
  const cardMap = new Map();
  /* 批次 2：时间分组表头节点（.group），按「视图|分组键」复用 */
  const groupMap = new Map();
  let iconEditTitleFrame = 0;
  let authWaitPromise = null;
  let authWaitResolve = null;
  let lastFocused = null;
  /* 批次 4（需求 D）：观察 sticky 头部高度，给手机归档分段算吸顶偏移 */
  let stickyObserver = null;
  let searchTimer = 0;

  class ApiError extends Error {
    constructor(status, detail) {
      super(detail || `请求失败（${status}）`);
      this.name = 'ApiError';
      this.status = status;
      this.detail = detail || '';
    }
  }

  /* ---------------- 日期与文案 ---------------- */

  const timeFmt = new Intl.DateTimeFormat('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
  });
  const monthDayFmt = new Intl.DateTimeFormat('zh-CN', { month: 'long', day: 'numeric' });
  const weekdayFmt = new Intl.DateTimeFormat('zh-CN', { weekday: 'short' });

  const pad2 = (value) => String(value).padStart(2, '0');

  function parseDate(value) {
    if (!value) return null;
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? null : date;
  }

  function toLocalIso(date) {
    return [
      `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}`,
      `${pad2(date.getHours())}:${pad2(date.getMinutes())}:${pad2(date.getSeconds())}`,
    ].join('T');
  }

  /* datetime-local 输入框的格式（精确到分钟） */
  function toDateTimeLocal(date) {
    return [
      `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}`,
      `${pad2(date.getHours())}:${pad2(date.getMinutes())}`,
    ].join('T');
  }

  /* 把任意 ISO / datetime-local 值归一成「分钟键」，用于比较与 chips 高亮 */
  function dueKey(value) {
    const date = parseDate(value);
    if (!date) return '';
    return toDateTimeLocal(date);
  }

  function startOfDay(date) {
    return new Date(date.getFullYear(), date.getMonth(), date.getDate());
  }

  function startOfWeek(date) {
    const day = (date.getDay() + 6) % 7; // 周一为一周开始
    return new Date(date.getFullYear(), date.getMonth(), date.getDate() - day);
  }

  function dayDiff(date, base = new Date()) {
    return Math.round((startOfDay(date) - startOfDay(base)) / 86400000);
  }

  function formatDate(date, withWeekday = true) {
    if (!date) return '';
    const sameYear = date.getFullYear() === new Date().getFullYear();
    const year = sameYear ? '' : `${date.getFullYear()}年`;
    const weekday = withWeekday ? ` ${weekdayFmt.format(date)}` : '';
    return `${year}${monthDayFmt.format(date)}${weekday}`;
  }

  function formatDateTime(date) {
    return `${formatDate(date)} ${timeFmt.format(date)}`;
  }

  function dueInfo(task) {
    const due = parseDate(task.due_at);
    if (!due) return null;

    const time = timeFmt.format(due);
    const isDone = task.status === 'done';
    if (isDone) {
      return { text: `${formatDate(due)} ${time}`, tone: 'normal' };
    }

    const now = new Date();
    const diff = dayDiff(due, now);
    if (due.getTime() < now.getTime()) {
      const day = diff === 0 ? '今天' : formatDate(due);
      return { text: `已逾期 · ${day} ${time}`, tone: 'overdue' };
    }
    if (diff === 0) return { text: `今天 ${time}`, tone: 'today' };
    if (diff === 1) return { text: `明天 ${time}`, tone: 'tomorrow' };
    return { text: `${formatDate(due)} ${time}`, tone: 'normal' };
  }

  function formatSourceTime(timestamp) {
    const value = Number(timestamp);
    if (!Number.isFinite(value)) return '';
    const date = new Date(value > 1e12 ? value : value * 1000);
    if (Number.isNaN(date.getTime())) return '';
    return formatDateTime(date);
  }

  function bucketOf(task) {
    if (task.status !== 'pending') return null;
    const due = parseDate(task.due_at);
    // 无截止时间 → 「以后」，与 App（TimeUtil.bucketOf）及契约 §7 一致；服务端 counts 同口径。
    if (!due) return 'later';
    const diff = dayDiff(due);
    if (diff <= 0) return 'today';
    if (diff <= 7) return 'week';
    return 'later';
  }

  function computeCounts(tasks) {
    const counts = { today: 0, week: 0, later: 0, done: 0, ignored: 0, snoozed: 0 };
    for (const task of tasks) {
      if (task.status === 'done') {
        counts.done += 1;
        continue;
      }
      if (task.status === 'ignored') {
        counts.ignored += 1;
        continue;
      }
      if (task.status === 'snoozed') {
        counts.snoozed += 1;
        continue;
      }
      const bucket = bucketOf(task);
      if (bucket) counts[bucket] += 1;
    }
    return counts;
  }

  function weekProgress() {
    const now = new Date();
    const start = startOfWeek(now);
    const end = new Date(start.getTime() + 7 * 86400000);
    let done = 0;
    let pending = 0;
    for (const task of state.tasks) {
      if (task.status === 'done') {
        const at = parseDate(task.completed_at) || parseDate(task.updated_at);
        if (at && at >= start && at < end) done += 1;
      } else if (task.status === 'pending') {
        const bucket = bucketOf(task);
        if (bucket === 'today' || bucket === 'week') pending += 1;
      }
    }
    return { done, total: done + pending };
  }

  function matchesSearch(task) {
    if (!state.search) return true;
    const needle = state.search;
    return [task.title, task.notes, task.group_name].some(
      (value) => typeof value === 'string' && value.toLowerCase().includes(needle),
    );
  }

  function visibleTasks() {
    if (state.search) {
      /* 批次 2：标题命中优先展示，其余命中保持原顺序 */
      const needle = state.search;
      const rank = (task) => (
        typeof task.title === 'string' && task.title.toLowerCase().includes(needle) ? 1 : 0
      );
      return state.tasks
        .filter(matchesSearch)
        .slice()
        .sort((a, b) => rank(b) - rank(a));
    }
    if (state.tab === 'done') {
      return state.tasks
        .filter((task) => task.status === 'done')
        .slice()
        .sort((a, b) => timestamp(b.updated_at) - timestamp(a.updated_at));
    }
    if (state.tab === 'ignored') {
      return state.tasks
        .filter((task) => task.status === 'ignored')
        .slice()
        .sort((a, b) => timestamp(b.updated_at) - timestamp(a.updated_at));
    }
    if (state.tab === 'snoozed') {
      return state.tasks
        .filter((task) => task.status === 'snoozed')
        .slice()
        .sort((a, b) => timestamp(b.updated_at) - timestamp(a.updated_at));
    }
    return state.tasks.filter(
      (task) => task.status === 'pending' && bucketOf(task) === state.tab,
    );
  }

  function timestamp(value) {
    const date = parseDate(value);
    return date ? date.getTime() : 0;
  }

  function normalizePriority(value) {
    return Object.prototype.hasOwnProperty.call(PRIORITY_LABELS, value) ? value : 'normal';
  }

  function normalizeStatus(value) {
    return Object.prototype.hasOwnProperty.call(STATUS_LABELS, value) ? value : 'pending';
  }

  function findTask(id) {
    return state.tasks.find((task) => String(task.id) === String(id)) || null;
  }

  function isTask(value) {
    return Boolean(
      value
      && typeof value === 'object'
      && value.id != null
      && typeof value.title === 'string',
    );
  }

  function describeError(error, fallback = '操作失败，请重试。') {
    if (!error) return fallback;
    if (error instanceof ApiError) {
      if (error.status === 401) return '访问令牌无效或已过期，请重新输入。';
      if (error.status === 404) return '任务不存在或已被删除。';
      if (error.status >= 500) return '服务器暂时不可用，请稍后重试。';
      if (error.detail) return error.detail;
      return fallback;
    }
    if (isNetworkError(error)) return '网络连接失败，请检查网络后重试。';
    return error.message || fallback;
  }

  function isNetworkError(error) {
    return error instanceof TypeError || (error && error.name === 'TypeError');
  }

  function prefersReducedMotion() {
    return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  }

  function isDesktop() {
    return desktopQuery.matches;
  }

  /* 批次 4（需求 D）：归档视图 = 已完成 / 已忽略 / 稍后 */
  function isArchiveView() {
    return ARCHIVE_VIEWS.includes(state.tab);
  }

  /* 批次 4（需求 D）：手机归档分段吸顶偏移 = 顶部 sticky 头部的实际高度 */
  function updateStickyOffsets() {
    const top = dom.appTop;
    if (!top) return;
    const height = Math.round(top.getBoundingClientRect().height);
    if (height > 0) {
      document.documentElement.style.setProperty('--app-top-h', `${height}px`);
    }
  }

  /* ---------------- DOM 辅助 ---------------- */

  function el(tag, options = {}, children = []) {
    const node = document.createElement(tag);
    if (options.class) node.className = options.class;
    if (options.text != null) node.textContent = String(options.text);
    if (options.attrs) {
      for (const [name, value] of Object.entries(options.attrs)) {
        if (value == null || value === false) continue;
        node.setAttribute(name, String(value));
      }
    }
    if (options.on) {
      for (const [name, handler] of Object.entries(options.on)) {
        node.addEventListener(name, handler);
      }
    }
    const list = Array.isArray(children) ? children.flat(Infinity) : [children];
    for (const child of list) {
      if (child == null || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  function createIcon(name, size = 16, strokeWidth = 2) {
    const svg = document.createElementNS(SVG_NS, 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('width', String(size));
    svg.setAttribute('height', String(size));
    svg.setAttribute('fill', 'none');
    svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', String(strokeWidth));
    svg.setAttribute('stroke-linecap', 'round');
    svg.setAttribute('stroke-linejoin', 'round');
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('focusable', 'false');
    const paths = ICONS[name] || ICONS.alert;
    for (const d of paths) {
      const path = document.createElementNS(SVG_NS, 'path');
      path.setAttribute('d', d);
      svg.append(path);
    }
    return svg;
  }

  function buttonLabel(iconName, label) {
    return [
      createIcon(iconName, 16, 2.2),
      el('span', { text: label }),
    ];
  }

  /* toast：type 仅用于语义，样式契约只认 .toast；带 action 时默认 5s */
  function showToast(message, type = 'info', options = {}) {
    if (!message) return null;
    const {
      actionLabel = '',
      onAction = null,
      duration = actionLabel ? 5000 : type === 'error' ? 4200 : 2600,
    } = options || {};

    const toast = el('div', { class: 'toast', attrs: { role: 'status' } });
    toast.append(el('span', { class: 'toast-text', text: message }));

    let dismissed = false;
    const dismiss = () => {
      if (dismissed) return;
      dismissed = true;
      toast.classList.add('is-leaving');
      window.setTimeout(() => toast.remove(), 220);
    };

    if (actionLabel && typeof onAction === 'function') {
      const actionButton = el('button', {
        class: 'toast-action',
        attrs: { type: 'button' },
        text: actionLabel,
        on: {
          click: () => {
            if (actionButton.disabled) return;
            actionButton.disabled = true;
            Promise.resolve()
              .then(() => onAction())
              .catch(() => {})
              .finally(() => dismiss());
          },
        },
      });
      toast.append(actionButton);
    }

    const timer = el('span', { class: 'toast-timer', attrs: { 'aria-hidden': 'true' } });
    timer.style.animationDuration = `${duration}ms`;
    toast.append(timer);

    dom.toastRegion.append(toast);
    // 契约：同时最多 2 条
    while (dom.toastRegion.children.length > 2) {
      dom.toastRegion.firstElementChild.remove();
    }
    window.setTimeout(dismiss, duration);
    return toast;
  }

  function updateAuthHealth() {
    if (!dom.authHealthText) return;
    let text = '正在检查服务器连接…';
    let dotState = 'syncing';
    if (state.connection === 'ok') {
      text = '服务器已连接';
      dotState = 'ok';
    } else if (state.connection === 'fail') {
      text = '无法连接服务器，请检查网络或服务是否已启动';
      dotState = 'error';
    }
    dom.authHealthText.textContent = text;
    dom.authHealthDot.dataset.state = dotState;
  }

  /* ---------------- 令牌 ---------------- */

  function getToken() {
    try {
      return localStorage.getItem(TOKEN_KEY) || '';
    } catch (error) {
      return '';
    }
  }

  function setToken(token) {
    try {
      localStorage.setItem(TOKEN_KEY, token);
    } catch (error) {
      /* 隐私模式下可能不可用，保留当前会话 */
    }
  }

  function consumeUrlToken() {
    try {
      const url = new URL(window.location.href);
      if (!url.searchParams.has('token')) return;
      const token = (url.searchParams.get('token') || '').trim();
      if (token) setToken(token);
      url.searchParams.delete('token');
      const next = `${url.pathname}${url.search}${url.hash}`;
      window.history.replaceState(null, '', next);
    } catch (error) {
      /* 地址栏不可改写时忽略 */
    }
  }

  function syncBackgroundInert() {
    const modalOpen = state.authOpen || Boolean(state.sheet);
    if (dom.appShell) dom.appShell.inert = modalOpen;
    if (dom.addBtn) dom.addBtn.inert = modalOpen;
    dom.sheetOverlay.inert = state.authOpen || !state.sheet;
  }

  function openAuth(message = '') {
    if (!authWaitPromise) {
      authWaitPromise = new Promise((resolve) => {
        authWaitResolve = resolve;
      });
    }
    if (!state.authOpen) {
      state.authOpen = true;
      dom.authOverlay.hidden = false;
      document.body.classList.add('auth-open');
      syncBackgroundInert();
      dom.tokenInput.value = '';
      dom.tokenInput.type = 'password';
      dom.tokenToggle.textContent = '显示';
      dom.tokenToggle.setAttribute('aria-label', '显示令牌');
      dom.authError.hidden = true;
      dom.authError.textContent = '';
      window.setTimeout(() => {
        try {
          dom.tokenInput.focus({ preventScroll: true });
        } catch (error) {
          /* 忽略焦点失败 */
        }
      }, 60);
    }
    if (message) {
      dom.authError.textContent = message;
      dom.authError.hidden = false;
    }
    renderStatus();
    updateAuthHealth();
    return authWaitPromise;
  }

  function handleAuthSubmit(event) {
    event.preventDefault();
    const token = dom.tokenInput.value.trim();
    if (!token) {
      dom.authError.textContent = '请输入访问令牌。';
      dom.authError.hidden = false;
      dom.tokenInput.focus();
      return;
    }
    setToken(token);
    state.authOpen = false;
    dom.authOverlay.hidden = true;
    dom.authError.hidden = true;
    dom.authError.textContent = '';
    document.body.classList.remove('auth-open');
    syncBackgroundInert();

    const resolve = authWaitResolve;
    authWaitResolve = null;
    authWaitPromise = null;
    if (resolve) resolve();

    const navFocus = isDesktop() ? dom.railButtons[state.tab] : dom.tabButtons[state.tab];
    const focusTarget = state.sheet === 'detail'
      ? dom.detailClose
      : state.sheet === 'new'
        ? dom.newTitleInput
        : navFocus || dom.listTitle;
    window.setTimeout(() => {
      try {
        if (focusTarget) focusTarget.focus({ preventScroll: true });
      } catch (error) {
        /* 忽略焦点失败 */
      }
    }, 60);

    renderStatus();
    updateAuthHealth();
    void checkHealth();
    void loadTasks({ silent: state.loaded });
  }

  function toggleTokenVisibility() {
    const revealed = dom.tokenInput.type === 'text';
    dom.tokenInput.type = revealed ? 'password' : 'text';
    dom.tokenToggle.textContent = revealed ? '显示' : '隐藏';
    dom.tokenToggle.setAttribute('aria-label', revealed ? '显示令牌' : '隐藏令牌');
  }

  /* ---------------- API ---------------- */

  async function api(path, options = {}) {
    const method = options.method || 'GET';
    for (let attempt = 0; attempt < 6; attempt += 1) {
      const token = getToken();
      if (!token) {
        await openAuth();
        continue;
      }
      const headers = new Headers(options.headers || {});
      headers.set('X-Auth-Token', token);
      headers.set('Accept', 'application/json');
      if (options.body != null && !headers.has('Content-Type')) {
        headers.set('Content-Type', 'application/json');
      }

      let response;
      try {
        response = await fetch(path, {
          method,
          headers,
          body: options.body,
          cache: 'no-store',
        });
      } catch (error) {
        throw new TypeError('网络连接失败');
      }

      if (response.status === 401) {
        await openAuth('访问令牌无效或已过期，请重新输入。');
        continue;
      }

      if (!response.ok) {
        let detail = '';
        try {
          const payload = await response.json();
          if (payload && typeof payload.detail === 'string') detail = payload.detail;
        } catch (error) {
          /* 非 JSON 错误响应 */
        }
        throw new ApiError(response.status, detail);
      }

      if (response.status === 204) return null;
      const contentType = response.headers.get('content-type') || '';
      if (!contentType.includes('application/json')) return null;
      return response.json();
    }
    throw new ApiError(401, '认证失败，请重新输入访问令牌。');
  }

  function loadTasks({ silent = false, notify = false } = {}) {
    if (state.loadPromise) return state.loadPromise;

    state.loading = true;
    if (!silent) state.error = null;
    renderStatus();
    renderList();

    const request = (async () => {
      try {
        const data = await api(`${API.tasks}?status=all&limit=200`);
        const tasks = data && Array.isArray(data.tasks) ? data.tasks.filter(isTask) : [];
        state.tasks = tasks;
        /* 契约 3.1：计数统一由 state.tasks 计算（含 ignored/snoozed） */
        state.counts = computeCounts(tasks);
        state.loaded = true;
        state.error = null;
        state.connection = 'ok';
        if (state.detail) {
          const latest = findTask(state.detail.id);
          if (latest) state.detail.task = { ...state.detail.task, ...latest };
        }
      } catch (error) {
        state.error = error;
        if (isNetworkError(error)) state.connection = 'fail';
        if (!silent || notify) showToast(describeError(error, '加载失败，请重试。'), 'error');
        if (error instanceof ApiError && error.status === 401) {
          openAuth('访问令牌无效，请重新输入。');
        }
      } finally {
        state.loading = false;
        state.loadPromise = null;
        renderAll();
      }
    })();

    state.loadPromise = request;
    return request;
  }

  async function checkHealth() {
    try {
      if ('onLine' in navigator && !navigator.onLine) throw new TypeError('offline');
      const response = await fetch(API.health, {
        cache: 'no-store',
        headers: { Accept: 'application/json' },
      });
      if (!response.ok) throw new Error(`health ${response.status}`);
      const data = await response.json();
      state.health = data;
      state.connection = data && data.ok === false ? 'fail' : 'ok';
    } catch (error) {
      state.health = null;
      state.connection = 'fail';
    }
    updateAuthHealth();
    renderStatus();
    renderBanner();
    return state.connection;
  }

  function refreshAll() {
    return Promise.all([
      loadTasks({ silent: state.loaded }),
      checkHealth(),
    ]);
  }

  /* ---------------- 状态渲染 ---------------- */

  function renderAll() {
    renderTabs();
    renderMobileArchive();
    renderList();
    renderStatus();
    renderBanner();
    renderProgress();
    if (isDesktop()) renderDetail();
    updateAuthHealth();
  }

  function renderStatus() {
    let statusState = 'syncing';
    let label = '连接中';

    if (state.authOpen) {
      statusState = 'auth';
      label = '需要令牌';
    } else if (state.loading) {
      statusState = 'syncing';
      label = '同步中';
    } else if (state.error) {
      statusState = 'error';
      label = '同步失败';
    } else if (state.loaded) {
      statusState = 'ok';
      label = '已同步';
    } else if (state.connection === 'fail') {
      statusState = 'error';
      label = '连接失败';
    }

    dom.syncDot.dataset.state = statusState;
    dom.syncText.textContent = label;
    dom.syncStatus.title = `同步状态：${label}`;
    dom.syncStatus.setAttribute('aria-label', `同步状态：${label}`);
    dom.refreshBtn.classList.toggle('is-spinning', state.loading);
    dom.refreshBtn.setAttribute('aria-busy', state.loading ? 'true' : 'false');
  }

  function renderTabs() {
    for (const view of VIEWS) {
      const count = Math.max(0, Number(state.counts[view]) || 0);
      for (const node of dom.countNodes[view] || []) {
        node.textContent = String(count);
        node.dataset.zero = count === 0 ? 'true' : 'false';
      }
      // 桌面左导航是普通按钮导航（aria-current），移动端 tabs 是 tablist（aria-selected）
      const railButton = dom.railButtons[view];
      if (railButton) {
        const selected = state.tab === view;
        if (selected) railButton.setAttribute('aria-current', 'page');
        else railButton.removeAttribute('aria-current');
        railButton.classList.toggle('is-active', selected);
      }
      const tabButton = dom.tabButtons[view];
      if (tabButton) {
        /* 批次 4（需求 D）：手机端归档视图（已忽略/稍后）仍归到「已完成」tab */
        const selected = view === 'done' ? isArchiveView() : state.tab === view;
        tabButton.setAttribute('aria-selected', selected ? 'true' : 'false');
        tabButton.tabIndex = selected ? 0 : -1;
        tabButton.classList.toggle('is-active', selected);
      }
    }
  }

  /* 批次 4（需求 D）：手机端「已完成」下的归档分段切换 */
  function renderMobileArchive() {
    const bar = dom.mobileArchiveBar;
    if (!bar) return;
    const visible = Boolean(
      !isDesktop()
      && state.loaded
      && !state.search
      && isArchiveView()
    );
    bar.hidden = !visible;
    if (!visible) return;
    for (const view of ARCHIVE_VIEWS) {
      const active = state.tab === view;
      const button = dom.mobileArchiveButtons[view];
      if (button) {
        button.setAttribute('aria-pressed', active ? 'true' : 'false');
        button.classList.toggle('is-active', active);
      }
      const badge = dom.mobileArchiveCounts[view];
      if (badge) {
        const count = Math.max(0, Number(state.counts[view]) || 0);
        badge.textContent = String(count);
        badge.dataset.zero = count === 0 ? 'true' : 'false';
      }
    }
  }

  function switchArchiveTab(view) {
    if (isDesktop() || !ARCHIVE_VIEWS.includes(view)) return;
    if (state.tab === view) return;
    switchTab(view);
  }

  function handleArchiveKeydown(event) {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    if (isDesktop() || !isArchiveView()) return;
    event.preventDefault();
    const current = ARCHIVE_VIEWS.indexOf(state.tab);
    const base = current >= 0 ? current : 0;
    let next = base;
    if (event.key === 'ArrowLeft') next = (base + ARCHIVE_VIEWS.length - 1) % ARCHIVE_VIEWS.length;
    if (event.key === 'ArrowRight') next = (base + 1) % ARCHIVE_VIEWS.length;
    if (event.key === 'Home') next = 0;
    if (event.key === 'End') next = ARCHIVE_VIEWS.length - 1;
    switchArchiveTab(ARCHIVE_VIEWS[next]);
    const target = dom.mobileArchiveButtons[ARCHIVE_VIEWS[next]];
    if (target) target.focus();
  }

  function renderProgress() {
    if (!dom.weekProgressText || !dom.weekProgressBar) return;
    const { done, total } = weekProgress();
    dom.weekProgressText.textContent = `${done} / ${total}`;
    dom.weekProgressBar.style.width = total ? `${Math.round((done / total) * 100)}%` : '0%';
  }

  function renderHeader(tasks) {
    let title = VIEW_TITLES[state.tab] || '今天';
    let sub = `${tasks.length} 项`;
    if (state.search) {
      title = '搜索结果';
    } else if (state.tab === 'today') {
      const overdue = tasks.filter((task) => {
        const info = dueInfo(task);
        return Boolean(info && info.tone === 'overdue');
      }).length;
      sub = `${tasks.length} 项 · ${overdue} 逾期`;
    }
    dom.listTitle.textContent = title;
    dom.listSub.textContent = sub;
  }

  function renderList() {
    /* 批次 3：列表渲染前同步选择入口 / 批量条状态，并清掉已不在当前视图的选中项 */
    renderSelectControls();
    if (!state.loaded) {
      dom.taskList.hidden = true;
      dom.taskList.replaceChildren();
      clearTaskNodes();
      dom.skeleton.hidden = Boolean(state.error);
      if (state.error) {
        dom.stateView.hidden = false;
        dom.stateView.replaceChildren(buildErrorView(state.error));
      } else {
        dom.stateView.hidden = true;
        dom.stateView.replaceChildren();
      }
      renderHeader([]);
      return;
    }

    dom.skeleton.hidden = true;
    const tasks = visibleTasks();
    renderHeader(tasks);

    if (!tasks.length) {
      dom.taskList.hidden = true;
      clearTaskNodes();
      dom.stateView.hidden = false;
      dom.stateView.replaceChildren(state.search ? buildSearchEmptyView() : buildEmptyView());
      return;
    }

    dom.stateView.hidden = true;
    dom.stateView.replaceChildren();
    dom.taskList.hidden = false;
    syncTaskNodes(tasks, taskSections(tasks));
  }

  function clearTaskNodes() {
    for (const entry of cardMap.values()) entry.node.remove();
    cardMap.clear();
    for (const entry of groupMap.values()) entry.node.remove();
    groupMap.clear();
  }

  /* ---------------- 批次 2：时间分组 ---------------- */

  function taskSections(tasks) {
    if (state.search) return null; // 搜索结果不分组
    if (state.tab === 'today') return todaySections(tasks);
    if (state.tab === 'week') return weekSections(tasks);
    if (state.tab === 'done') return doneSections(tasks);
    return null;
  }

  function sortByDue(tasks) {
    return tasks.slice().sort((a, b) => {
      const left = parseDate(a.due_at);
      const right = parseDate(b.due_at);
      const leftAt = left ? left.getTime() : Number.MAX_SAFE_INTEGER;
      const rightAt = right ? right.getTime() : Number.MAX_SAFE_INTEGER;
      return leftAt - rightAt;
    });
  }

  function todaySections(tasks) {
    const now = Date.now();
    const overdue = [];
    const dueToday = [];
    /* 「今天」页入参已由 bucketOf 过滤（无 due_at 的任务归「以后」），这里防御性跳过无截止任务。 */
    for (const task of tasks) {
      const due = parseDate(task.due_at);
      if (!due) continue;
      if (due.getTime() < now) overdue.push(task);
      else dueToday.push(task);
    }
    const sections = [];
    if (overdue.length) sections.push({ key: 'overdue', title: '逾期', tasks: sortByDue(overdue) });
    if (dueToday.length) sections.push({ key: 'today', title: '今天', tasks: sortByDue(dueToday) });
    return sections;
  }

  function weekSectionTitle(due) {
    const diff = dayDiff(due);
    if (diff === 0) return '今天';
    if (diff === 1) return '明天';
    return `${weekdayFmt.format(due)} ${monthDayFmt.format(due)}`;
  }

  function weekSections(tasks) {
    const buckets = new Map();
    for (const task of tasks) {
      const due = parseDate(task.due_at);
      const key = due
        ? `${due.getFullYear()}-${pad2(due.getMonth() + 1)}-${pad2(due.getDate())}`
        : 'unscheduled';
      let bucket = buckets.get(key);
      if (!bucket) {
        bucket = { key, due, tasks: [] };
        buckets.set(key, bucket);
      }
      bucket.tasks.push(task);
    }
    return Array.from(buckets.values())
      .sort((a, b) => {
        const left = a.due ? a.due.getTime() : Number.MAX_SAFE_INTEGER;
        const right = b.due ? b.due.getTime() : Number.MAX_SAFE_INTEGER;
        return left - right;
      })
      .map((bucket) => ({
        key: bucket.key,
        title: bucket.due ? weekSectionTitle(bucket.due) : '待安排',
        tasks: sortByDue(bucket.tasks),
      }));
  }

  function completionDate(task) {
    return parseDate(task.completed_at) || parseDate(task.updated_at);
  }

  function doneSections(tasks) {
    const today = [];
    const yesterday = [];
    const earlier = [];
    for (const task of tasks) {
      const at = completionDate(task);
      const diff = at ? dayDiff(at) : null;
      if (diff === 0) today.push(task);
      else if (diff === -1) yesterday.push(task);
      else earlier.push(task);
    }
    const sections = [];
    if (today.length) sections.push({ key: 'today', title: '今天完成', tasks: today });
    if (yesterday.length) sections.push({ key: 'yesterday', title: '昨天', tasks: yesterday });
    if (earlier.length) sections.push({ key: 'earlier', title: '更早', tasks: earlier });
    return sections;
  }

  /* 分组表头：<li class="group"><h2>..</h2><em>n</em><span class="rule"></span></li>
     h2 与列表标题同级，移动端隐藏列表标题时也不会跳级（a11y heading-order）。 */
  function createGroupNode(title, count) {
    return el('li', { class: 'group' }, [
      el('h2', { text: title }),
      el('em', { text: String(count) }),
      el('span', { class: 'rule' }),
    ]);
  }

  function updateGroupNode(node, title, count) {
    const heading = node.querySelector('h2');
    const badge = node.querySelector('em');
    if (heading && heading.textContent !== title) heading.textContent = title;
    if (badge && badge.textContent !== String(count)) badge.textContent = String(count);
  }

  /* 增量渲染：按 data-id 复用卡片、按分组键复用 .group 表头，渲染后恢复列表内焦点 */
  function syncTaskNodes(tasks, sections) {
    const focusInfo = captureListFocus();
    const seen = new Set();
    const seenGroups = new Set();
    const nodes = [];

    const appendCard = (task) => {
      const id = String(task.id);
      seen.add(id);
      const signature = taskSignature(task);
      let entry = cardMap.get(id);
      if (!entry) {
        entry = { node: createTaskCard(task), sig: signature };
        cardMap.set(id, entry);
      } else if (entry.sig !== signature) {
        updateTaskCard(entry.node, task);
        entry.sig = signature;
      }
      nodes.push(entry.node);
    };

    if (sections) {
      for (const section of sections) {
        const key = `${state.tab}|${section.key}`;
        seenGroups.add(key);
        const signature = `${section.title}\u0000${section.tasks.length}`;
        let entry = groupMap.get(key);
        if (!entry) {
          entry = { node: createGroupNode(section.title, section.tasks.length), sig: signature };
          groupMap.set(key, entry);
        } else if (entry.sig !== signature) {
          updateGroupNode(entry.node, section.title, section.tasks.length);
          entry.sig = signature;
        }
        nodes.push(entry.node);
        for (const task of section.tasks) appendCard(task);
      }
    } else {
      for (const task of tasks) appendCard(task);
    }

    for (const [key, entry] of Array.from(groupMap.entries())) {
      if (seenGroups.has(key)) continue;
      entry.node.remove();
      groupMap.delete(key);
    }

    for (const [id, entry] of Array.from(cardMap.entries())) {
      if (seen.has(id)) continue;
      entry.node.remove();
      cardMap.delete(id);
    }

    let cursor = dom.taskList.firstElementChild;
    for (const node of nodes) {
      if (node === cursor) {
        cursor = cursor.nextElementSibling;
      } else {
        dom.taskList.insertBefore(node, cursor);
      }
    }

    restoreListFocus(focusInfo);
  }

  function cssEscape(value) {
    const text = String(value);
    if (window.CSS && typeof window.CSS.escape === 'function') return window.CSS.escape(text);
    return text.replace(/["\\]/g, '\\$&');
  }

  function findCardNode(id) {
    if (id == null) return null;
    return dom.taskList.querySelector(`[data-id="${cssEscape(id)}"]`);
  }

  function captureListFocus() {
    const active = document.activeElement;
    if (!active || !dom.taskList.contains(active)) return null;
    const card = active.closest('[data-id]');
    const cards = Array.from(dom.taskList.children).filter((node) => node.hasAttribute('data-id'));
    return {
      id: card ? card.dataset.id : null,
      role: active.classList.contains('task-check')
        ? 'check'
        : active.classList.contains('task-restore')
          ? 'restore'
          : active.classList.contains('task-pick')
            ? 'pick'
            : active === card
              ? 'card'
              : 'other',
      index: card ? cards.indexOf(card) : -1,
    };
  }

  function restoreListFocus(info) {
    if (!info) return;
    if (!dom.taskList.hidden && info.id) {
      const card = findCardNode(info.id);
      if (card) {
        const target = info.role === 'check'
          ? card.querySelector('.task-check')
          : info.role === 'restore'
            ? card.querySelector('.task-restore')
            : info.role === 'pick'
              ? card.querySelector('.task-pick')
              : card;
        if (target && typeof target.focus === 'function') {
          target.focus({ preventScroll: true });
          return;
        }
      }
    }
    if (!dom.taskList.hidden) {
      /* 分组表头也是 li，回退时只挑真正的任务卡片 */
      const cards = Array.from(dom.taskList.children).filter((node) => node.hasAttribute('data-id'));
      if (cards.length) {
        const fallbackIndex = info.index >= 0
          ? Math.min(info.index, cards.length - 1)
          : 0;
        const fallback = cards[fallbackIndex];
        if (fallback && typeof fallback.focus === 'function') {
          fallback.focus({ preventScroll: true });
          return;
        }
      }
    }
    if (info.index >= 0 || dom.taskList.hidden) focusListFallback();
  }

  function focusListFallback() {
    if (!dom.listTitle) return;
    try {
      dom.listTitle.focus({ preventScroll: true });
    } catch (error) {
      /* 忽略焦点失败 */
    }
  }

  function focusTaskCard(id, index = -1) {
    const card = findCardNode(id);
    if (card && !dom.taskList.hidden) {
      try {
        card.focus({ preventScroll: true });
        return true;
      } catch (error) {
        /* 忽略焦点失败 */
      }
    }
    if (!dom.taskList.hidden && dom.taskList.children.length) {
      const fallback = dom.taskList.children[Math.max(0, Math.min(index, dom.taskList.children.length - 1))];
      if (fallback && typeof fallback.focus === 'function') {
        fallback.focus({ preventScroll: true });
        return true;
      }
    }
    focusListFallback();
    return false;
  }

  function buildEmptyView() {
    const copy = EMPTY_COPY[state.tab] || EMPTY_COPY.today;
    const iconName = state.tab === 'done'
      ? 'checkCircle'
      : state.tab === 'ignored'
        ? 'close'
        : state.tab === 'snoozed'
          ? 'clock'
          : 'inbox';
    return el('div', { class: 'state-view-inner' }, [
      el('div', { class: 'state-icon' }, [createIcon(iconName, 27, 1.8)]),
      el('h2', { class: 'state-title', text: copy.title }),
      el('p', { class: 'state-text', text: copy.text }),
    ]);
  }

  function buildSearchEmptyView() {
    const raw = state.searchRaw.trim();
    return el('div', { class: 'empty-result' }, [
      el('b', { text: '没有匹配的任务' }),
      el('p', { text: raw ? `没有任何任务的标题、备注或群名包含「${raw}」。` : '还没有输入搜索关键词。' }),
      el('p', {
        class: 'list-sub',
        text: '按 Esc 也可以清空搜索',
      }),
      el('button', {
        class: 'btn btn-ghost',
        attrs: { type: 'button' },
        text: '清空搜索',
        on: { click: () => clearSearch({ focus: true }) },
      }),
    ]);
  }

  function buildErrorView(error) {
    return el('div', { class: 'state-view-inner' }, [
      el('div', { class: 'state-icon state-icon--error' }, [createIcon('alert', 27, 1.8)]),
      el('h2', { class: 'state-title', text: '加载失败' }),
      el('p', { class: 'state-text', text: describeError(error, '暂时无法加载任务，请稍后重试。') }),
      el('button', {
        class: 'btn btn-secondary state-retry',
        attrs: { type: 'button' },
        text: '重试',
        on: { click: () => { void refreshAll(); } },
      }),
    ]);
  }

  function truncateText(value, max = 80) {
    if (value == null) return '';
    const text = String(value).replace(/\s+/g, ' ').trim();
    if (!text) return '';
    return text.length > max ? `${text.slice(0, max).trimEnd()}…` : text;
  }

  function buildBannerLink() {
    const token = getToken();
    const href = token
      ? `./settings.html?token=${encodeURIComponent(token)}`
      : './settings.html';
    return el('a', {
      class: 'banner-link',
      attrs: { href },
      text: '去设置',
    });
  }

  function buildBanner(type, message, options = {}) {
    const { detail = '', showSettings = false } = options;
    return el('div', {
      class: `banner banner--${type}`,
      attrs: { role: type === 'error' ? 'alert' : 'status' },
    }, [
      el('span', { class: 'banner-icon' }, [createIcon('alert', 16, 2)]),
      el('div', { class: 'banner-body' }, [
        el('p', { class: 'banner-text', text: message }),
        detail ? el('p', { class: 'banner-detail', text: detail }) : null,
      ]),
      el('div', { class: 'banner-actions' }, [
        el('button', {
          class: 'banner-retry',
          attrs: { type: 'button' },
          text: '重试',
          on: { click: () => { void refreshAll(); } },
        }),
        showSettings ? buildBannerLink() : null,
      ]),
    ]);
  }

  /* 批次 4：数据源状态提示（非错误横幅，demo 实例不显示） */
  function buildDataSourceBanner() {
    if (!state.loaded || state.error || !state.health) return null;
    const health = state.health;
    if (health.data_source === 'demo') return null;
    const source = health.source;
    if (!source || typeof source !== 'object') return null;

    if (source.configured === false) {
      return buildBanner('warn', '未配置数据源：暂时不会自动生成待办，请检查设置', {
        showSettings: true,
      });
    }
    if (source.connected === false) {
      return buildBanner('warn', '数据源未连接：暂时不会自动生成待办，请确认微信/数据源已启动', {
        detail: truncateText(source.last_error, 80),
        showSettings: true,
      });
    }
    return null;
  }

  function renderBanner() {
    const nodes = [];
    if (state.loaded && state.error) {
      /* 错误横幅优先，二者不同时出现 */
      nodes.push(buildBanner('error', describeError(state.error, '刷新失败，请重试。')));
    } else {
      const sourceBanner = buildDataSourceBanner();
      if (sourceBanner) nodes.push(sourceBanner);
    }
    dom.bannerSlot.replaceChildren(...nodes);
  }

  /* ---------------- 任务卡片 ---------------- */

  function taskSignature(task) {
    const id = String(task.id);
    return JSON.stringify([
      id,
      task.status,
      task.title,
      task.due_at != null ? String(task.due_at) : '',
      normalizePriority(task.priority),
      typeof task.group_name === 'string' ? task.group_name : '',
      /* 批次 4：来源条数参与签名，刷新后卡片能及时更新 */
      Number(task.source_count) || 0,
      state.tab,
      state.search ? 's' : '',
      isDesktop() ? 'd' : 'm',
      selectedTaskId() || '',
      /* 批次 3：选择模式与选中态参与签名，切换时复用节点但重画卡片内容 */
      state.selectMode ? 'pick' : '',
      state.selectMode && state.selectedIds.has(id) ? 'picked' : '',
      new Date().toDateString(),
    ]);
  }

  function selectedTaskId() {
    if (state.selectedId != null) return String(state.selectedId);
    return state.detail ? String(state.detail.id) : null;
  }

  function selectedTask() {
    const id = selectedTaskId();
    return id ? findTask(id) : null;
  }

  function buildTaskCardParts(task) {
    const id = String(task.id);
    const desktop = isDesktop();
    const done = task.status === 'done';
    const info = dueInfo(task);
    const overdue = Boolean(info && info.tone === 'overdue' && !done);
    const priority = normalizePriority(task.priority);
    const status = normalizeStatus(task.status);
    const group = typeof task.group_name === 'string' ? task.group_name.trim() : '';
    /* 批次 4：来源条数（>1 才展示，避免单条噪音） */
    const sourceCount = Number(task.source_count) || 0;
    const selected = selectedTaskId() === id;
    /* 批次 3：选择模式只存在于桌面；此时行只在选中/未选中之间切换，不打开详情 */
    const selectMode = Boolean(state.selectMode && desktop);
    const picked = Boolean(selectMode && state.selectedIds.has(id));
    const restoreMode = !state.search && !selectMode
      && (state.tab === 'ignored' || state.tab === 'snoozed');
    const showStatus = state.search || restoreMode;

    const classes = [
      'task-card',
      desktop ? 'task' : '',
      done ? 'is-done' : '',
      overdue ? 'is-overdue' : '',
      task.status !== 'pending' ? `is-${task.status}` : '',
      selected && !selectMode ? 'is-selected' : '',
      selected && !selectMode ? 'is-active' : '',
      selectMode ? 'is-select-mode' : '',
      picked ? 'is-picked' : '',
    ].filter(Boolean).join(' ');

    const checkLabel = `${done ? '恢复为待办' : '标记为完成'}：${task.title}`;
    const checkButton = el('button', {
      class: desktop ? 'task-check check' : 'task-check',
      attrs: {
        type: 'button',
        role: 'checkbox',
        'aria-checked': done ? 'true' : 'false',
        'aria-label': checkLabel,
      },
      on: {
        click: (event) => {
          event.stopPropagation();
          toggleTaskById(id);
        },
      },
    }, [createIcon('check', 14, 3)]);

    /* 批次 3：选择模式下用方形 .task-pick 取代完成圆圈，避免误改状态 */
    const pickButton = el('button', {
      class: `task-pick${picked ? ' is-on' : ''}`,
      attrs: {
        type: 'button',
        role: 'checkbox',
        'aria-checked': picked ? 'true' : 'false',
        'aria-label': `${picked ? '取消选择' : '选择'}：${task.title}`,
        style: 'padding:0',
      },
      on: {
        click: (event) => {
          event.stopPropagation();
          handlePickClick(id, event);
        },
      },
    });

    const meta = [
      info
        ? el('span', { class: `due due--${info.tone}` }, [
          createIcon('clock', 12, 2.2),
          el('span', { text: info.text }),
        ])
        : el('span', { class: 'due due--none', text: '无截止时间' }),
      group ? el('span', { class: `pill tag`, attrs: { title: group }, text: group }) : null,
      sourceCount > 1
        ? el('span', {
          class: 'tag tag--source',
          attrs: { title: `由 ${sourceCount} 条消息归纳` },
          text: `来源 ${sourceCount} 条`,
        })
        : null,
      showStatus
        ? el('span', {
          class: 'tag tag--status',
          text: (state.search ? SEARCH_STATUS_LABELS : STATUS_LABELS)[status],
        })
        : null,
    ].filter(Boolean);

    const body = el('div', {
      class: desktop ? 'task-body task-main' : 'task-body',
      attrs: {
        role: 'button',
        tabindex: '0',
        'data-action': 'open',
        'aria-label': selectMode
          ? `${picked ? '取消选择' : '选择'}任务：${task.title}`
          : `打开任务：${task.title}，${info ? info.text : '无截止时间'}${group ? `，${group}` : ''}`,
      },
      on: {
        click: (event) => {
          event.stopPropagation();
          if (state.selectMode) {
            handlePickClick(id, event);
            return;
          }
          selectTask(id);
        },
        keydown: (event) => {
          if (event.key !== 'Enter' && event.key !== ' ') return;
          event.preventDefault();
          event.stopPropagation();
          if (event.repeat) return;
          if (state.selectMode) {
            /* 选择模式下：Enter 仍按批次 2 语义打开详情（并退出选择），空格切换选中 */
            if (event.key === 'Enter') selectTask(id);
            else handlePickClick(id, event);
            return;
          }
          selectTask(id);
        },
      },
    }, [
      el('p', { class: 'task-title', text: task.title }),
      el('div', { class: 'task-meta' }, meta),
    ]);

    let tail;
    if (restoreMode) {
      // 契约 3.5：恢复按钮优先于优先级标记
      tail = el('button', {
        class: 'task-restore',
        attrs: { type: 'button', 'data-action': 'restore', title: '恢复为待办' },
        text: '恢复',
        on: {
          click: (event) => {
            event.stopPropagation();
            restoreTaskById(id);
          },
        },
      });
    } else if (desktop) {
      // 契约 4.1：桌面用 .pri 文字胶囊；保留 .priority--* 供移动端主题画圆点
      tail = el('span', {
        class: `pri pri--${priority} priority priority--${priority}`,
        attrs: { title: PRIORITY_LABELS[priority] },
        text: PRIORITY_SHORT[priority],
      });
    } else {
      tail = el('span', {
        class: `priority priority--${priority}`,
        attrs: { title: PRIORITY_LABELS[priority], 'aria-hidden': 'true' },
      });
    }

    return {
      className: classes,
      children: [selectMode ? pickButton : checkButton, body, tail],
    };
  }

  function createTaskCard(task) {
    const id = String(task.id);
    // 卡片本身不再是交互元素：打开动作由内部 .task-body[role=button] 承担，
    // 复选框是独立控件，避免交互元素嵌套（a11y）。
    const card = el('li', {
      attrs: { 'data-id': id },
      on: {
        click: (event) => {
          if (state.selectMode) {
            if (event.target.closest('.task-pick')) return; // 复选框自己处理，避免重复切换
            handlePickClick(id, event);
            return;
          }
          if (event.target.closest('.task-check, .task-restore, [data-action="open"]')) return;
          selectTask(id);
        },
      },
    });
    updateTaskCard(card, task);
    return card;
  }

  function updateTaskCard(card, task) {
    const parts = buildTaskCardParts(task);
    card.className = parts.className;
    card.replaceChildren(...parts.children);
  }

  /* ---------------- 任务操作（乐观更新 + 撤销） ---------------- */

  function toggleTaskById(id) {
    const task = findTask(id);
    if (!task) return;
    const next = task.status === 'done' ? 'pending' : 'done';
    void applyTaskPatch(task, { status: next }, {
      message: next === 'done' ? '已完成' : '已恢复',
      undoable: next === 'done',
    });
  }

  function completeTask(task) {
    void applyTaskPatch(task, { status: 'done' }, { message: '已完成', undoable: true });
  }

  function snoozeTask(task) {
    const until = new Date(Date.now() + 60 * 60 * 1000);
    void applyTaskPatch(task, {
      status: 'snoozed',
      snooze_until: toLocalIso(until),
    }, { message: '已稍后 1 小时', undoable: true });
  }

  function ignoreTask(task) {
    void applyTaskPatch(task, { status: 'ignored' }, { message: '已忽略', undoable: true });
  }

  function restoreTask(task) {
    void applyTaskPatch(task, { status: 'pending' }, { message: '已恢复' });
  }

  function restoreTaskById(id) {
    const task = findTask(id);
    if (task) restoreTask(task);
  }

  /* ---------------- 批次 3：批量选择 + 批量/单条删除（桌面 ≥1024px） ---------------- */

  function visiblePickIds() {
    return visibleTasks().map((task) => String(task.id));
  }

  function clearSelectState() {
    state.selectMode = false;
    state.selectedIds = new Set();
    state.pickAnchorId = null;
  }

  function exitSelectMode({ focus = false } = {}) {
    const wasSelecting = state.selectMode;
    clearSelectState();
    if (!wasSelecting) return;
    renderAll();
    if (focus && dom.selectBtn && !dom.selectBtn.hidden) {
      try {
        dom.selectBtn.focus({ preventScroll: true });
      } catch (error) {
        /* 忽略焦点失败 */
      }
    }
  }

  function toggleSelectMode() {
    if (!isDesktop() || state.authOpen || state.bulkBusy || !state.loaded || state.search) return;
    if (state.selectMode) {
      exitSelectMode({ focus: true });
      return;
    }
    state.selectMode = true;
    state.selectedIds = new Set();
    state.pickAnchorId = null;
    renderAll();
    if (dom.selectBtn) {
      try {
        dom.selectBtn.focus({ preventScroll: true });
      } catch (error) {
        /* 忽略焦点失败 */
      }
    }
  }

  function handlePickClick(id, event) {
    if (!state.selectMode || state.bulkBusy) return;
    const key = String(id);
    const ids = visiblePickIds();
    const targetIndex = ids.indexOf(key);
    /* Shift + 点击：按当前可见顺序整段选中（锚点停留在上次普通点击的任务上） */
    if (event && event.shiftKey && state.pickAnchorId != null) {
      const anchorIndex = ids.indexOf(String(state.pickAnchorId));
      if (anchorIndex >= 0 && targetIndex >= 0) {
        const start = Math.min(anchorIndex, targetIndex);
        const end = Math.max(anchorIndex, targetIndex);
        for (let index = start; index <= end; index += 1) state.selectedIds.add(ids[index]);
        renderList();
        return;
      }
    }
    if (state.selectedIds.has(key)) state.selectedIds.delete(key);
    else state.selectedIds.add(key);
    state.pickAnchorId = key;
    renderList();
  }

  function selectAllVisible() {
    if (!state.selectMode) return;
    const ids = visiblePickIds();
    for (const id of ids) state.selectedIds.add(id);
    state.pickAnchorId = ids.length ? ids[ids.length - 1] : null;
    renderList();
  }

  function selectActions() {
    /* 批量条按当前视图给动作：待办类 → 完成/忽略；归档类 → 恢复/删除 */
    if (state.tab === 'done' || state.tab === 'ignored' || state.tab === 'snoozed') {
      return ['restore', 'delete'];
    }
    return ['done', 'ignore'];
  }

  function bulkTargetIds() {
    const selected = state.selectedIds;
    return visiblePickIds().filter((id) => selected.has(id));
  }

  function renderSelectControls() {
    const desktop = isDesktop();
    const canSelect = Boolean(desktop && state.loaded && !state.search && !state.authOpen);
    if (!canSelect && state.selectMode) clearSelectState();

    if (dom.selectBtn) {
      dom.selectBtn.hidden = !canSelect;
      const pressed = Boolean(canSelect && state.selectMode);
      dom.selectBtn.setAttribute('aria-pressed', pressed ? 'true' : 'false');
      dom.selectBtn.textContent = pressed ? '退出选择' : '选择';
    }

    if (!dom.bulkBar) return;

    /* 轮询 / 刷新后，把已经不在当前视图的 id 从选中集里剔除 */
    if (state.selectMode && state.loaded) {
      const valid = new Set(visiblePickIds());
      for (const id of Array.from(state.selectedIds)) {
        if (!valid.has(id)) state.selectedIds.delete(id);
      }
    }

    const active = Boolean(canSelect && state.selectMode && state.selectedIds.size > 0);
    dom.bulkBar.hidden = !active;
    if (!active) return;

    if (dom.bulkCount) dom.bulkCount.textContent = `已选 ${state.selectedIds.size} 项`;
    const actions = selectActions();
    const buttons = dom.bulkButtons || {};
    for (const [name, button] of Object.entries(buttons)) {
      if (name === 'all' || name === 'cancel') button.hidden = false;
      else button.hidden = !actions.includes(name);
      button.disabled = Boolean(state.bulkBusy);
    }
  }

  function removeTaskLocally(id) {
    const key = String(id);
    const nextTasks = state.tasks.filter((task) => String(task.id) !== key);
    if (nextTasks.length !== state.tasks.length) state.tasks = nextTasks;
    state.selectedIds.delete(key);
    if (state.selectedId != null && String(state.selectedId) === key) state.selectedId = null;
    if (state.detail && String(state.detail.id) === key) {
      state.detail = null;
      state.editing = null;
    }
    state.counts = computeCounts(state.tasks);
  }

  async function deleteTask(task) {
    if (!task || state.deletingId || state.bulkBusy) return;
    const latest = findTask(task.id) || task;
    const id = String(latest.id);
    const title = typeof latest.title === 'string' ? latest.title : '';
    if (!window.confirm(`确定删除任务「${title}」？删除后无法恢复。`)) return;

    const focusInDetail = Boolean(
      dom.detailPanel
      && document.activeElement instanceof Element
      && dom.detailPanel.contains(document.activeElement),
    );
    state.deletingId = id;
    try {
      await api(API.task(id), { method: 'DELETE' });
      removeTaskLocally(id);
      state.error = null;
      renderAll();
      /* 批次 4（需求 D）：手机抽屉里删除后关闭抽屉，回到归档列表 */
      if (!isDesktop() && state.sheet === 'detail') closeSheet({ restoreFocus: false });
      showToast('已删除', 'success');
      if (focusInDetail) focusListFallback();
    } catch (error) {
      showToast(describeError(error, '删除失败，请重试。'), 'error');
      if (error instanceof ApiError && error.status === 401) {
        openAuth('访问令牌无效，请重新输入。');
      }
    } finally {
      state.deletingId = null;
    }
  }

  async function runBulkAction(kind) {
    if (!state.selectMode || state.bulkBusy) return;
    const ids = bulkTargetIds();
    if (!ids.length) return;

    const isDelete = kind === 'delete';
    if (isDelete && !window.confirm(`确定删除选中的 ${ids.length} 项任务？删除后无法恢复。`)) return;
    const status = kind === 'done' ? 'done' : kind === 'restore' ? 'pending' : 'ignored';

    state.bulkBusy = true;
    renderSelectControls();

    const removed = [];
    const patched = [];
    let firstError = null;

    /* 串行请求：失败不中断，最后按成功条数报告 */
    for (const id of ids) {
      try {
        if (isDelete) {
          await api(API.task(id), { method: 'DELETE' });
          removed.push(id);
        } else {
          const data = await api(API.task(id), {
            method: 'PATCH',
            body: JSON.stringify({ status }),
          });
          const saved = data && isTask(data.task) ? data.task : null;
          if (saved) {
            mergeSavedTask(id, saved);
          } else {
            const index = state.tasks.findIndex((item) => String(item.id) === id);
            if (index >= 0) {
              const nextTasks = state.tasks.slice();
              nextTasks[index] = { ...nextTasks[index], status };
              state.tasks = nextTasks;
            }
          }
          patched.push(id);
        }
      } catch (error) {
        if (!firstError) firstError = error;
      }
    }

    state.bulkBusy = false;
    for (const id of removed) removeTaskLocally(id);
    state.counts = computeCounts(state.tasks);
    if (!firstError) state.error = null;
    clearSelectState();
    renderAll();

    const count = isDelete ? removed.length : patched.length;
    if (count) {
      const message = isDelete
        ? `已删除 ${count} 项`
        : kind === 'done'
          ? `已批量完成 ${count} 项`
          : kind === 'ignore'
            ? `已批量忽略 ${count} 项`
            : `已批量恢复 ${count} 项`;
      showToast(message, 'success');
    }
    if (firstError) showToast(describeError(firstError, '批量操作失败，请重试。'), 'error');
    if (dom.selectBtn && !dom.selectBtn.hidden) {
      try {
        dom.selectBtn.focus({ preventScroll: true });
      } catch (error) {
        /* 忽略焦点失败 */
      }
    }
  }

  /* ---------------- 批次 2：键盘快捷键（n / e / x / j / k / Enter / Esc） ---------------- */

  function isTypingTarget(target) {
    if (!(target instanceof Element)) return false;
    return Boolean(target.closest('input, textarea, select, [contenteditable="true"]'));
  }

  function moveSelection(delta) {
    const tasks = visibleTasks();
    if (!tasks.length) return;
    const currentId = selectedTaskId();
    const current = currentId
      ? tasks.findIndex((item) => String(item.id) === String(currentId))
      : -1;
    let next = current < 0
      ? (delta > 0 ? 0 : tasks.length - 1)
      : current + delta;
    next = Math.max(0, Math.min(tasks.length - 1, next));
    state.selectedId = String(tasks[next].id);
    renderList();
    scrollSelectedIntoView();
  }

  function scrollSelectedIntoView() {
    const id = selectedTaskId();
    const card = id ? findCardNode(id) : null;
    if (!card) return;
    try {
      card.scrollIntoView({
        block: 'nearest',
        behavior: prefersReducedMotion() ? 'auto' : 'smooth',
      });
    } catch (error) {
      try { card.scrollIntoView(); } catch (ignored) { /* 忽略滚动失败 */ }
    }
  }

  function toggleSelectedTask() {
    const task = selectedTask();
    if (!task) {
      showToast('先按 j / k 选中一条任务', 'info');
      return;
    }
    const id = String(task.id);
    const before = visibleTasks();
    const index = before.findIndex((item) => String(item.id) === id);
    const remaining = before.filter((item) => String(item.id) !== id);
    const next = remaining.length
      ? remaining[Math.min(Math.max(index, 0), remaining.length - 1)]
      : null;
    /* 先把选中态挪到下一条，optimistic 重渲染后就能继续按 x */
    state.selectedId = next ? String(next.id) : null;
    if (task.status === 'done') {
      void applyTaskPatch(task, { status: 'pending' }, { message: '已恢复' });
    } else {
      void applyTaskPatch(task, { status: 'done' }, { message: '已完成', undoable: true });
    }
  }

  function openSelectedTask() {
    const task = selectedTask();
    if (task) selectTask(task.id);
  }

  function editSelected() {
    const task = selectedTask();
    if (!task) {
      showToast('先按 j / k 选中一条任务', 'info');
      return;
    }
    if (!isDesktop()) {
      selectTask(task.id); // 窄屏没有内联编辑，直接打开详情抽屉
      return;
    }
    if (!state.detail || String(state.detail.id) !== String(task.id)) selectTask(task.id);
    startEdit(findTask(task.id) || task);
  }

  function clearSelection() {
    const hadFocusInDetail = Boolean(
      dom.detailPanel
      && document.activeElement instanceof Element
      && dom.detailPanel.contains(document.activeElement),
    );
    state.selectedId = null;
    state.detail = null;
    state.editing = null;
    if (isDesktop()) {
      renderDetail();
      renderList();
    } else {
      renderList();
    }
    if (hadFocusInDetail) focusListFallback();
  }

  function mergeSavedTask(id, saved) {
    const key = String(id);
    const index = state.tasks.findIndex((item) => String(item.id) === key);
    if (index >= 0) {
      const next = state.tasks.slice();
      next[index] = { ...next[index], ...saved };
      state.tasks = next;
    }
    if (state.detail && String(state.detail.id) === key) {
      state.detail.task = { ...state.detail.task, ...saved };
    }
  }

  async function applyTaskPatch(task, patch, { message = '', undoable = false } = {}) {
    const id = String(task.id);
    const index = state.tasks.findIndex((item) => String(item.id) === id);
    if (index < 0) {
      showToast('任务不存在或已被删除。', 'error');
      return;
    }
    const capturedIndex = visibleTasks().findIndex((item) => String(item.id) === id);

    const previous = state.tasks[index];
    const optimistic = { ...previous, ...patch };
    if (patch.status === 'pending') {
      optimistic.completed_at = null;
      optimistic.snooze_until = null;
    }
    if (patch.status === 'done' && !optimistic.completed_at) {
      optimistic.completed_at = toLocalIso(new Date());
    }

    const nextTasks = state.tasks.slice();
    nextTasks[index] = optimistic;
    state.tasks = nextTasks;
    state.counts = computeCounts(state.tasks);
    state.error = null;
    renderAll();
    if (!isDesktop() && state.sheet) closeSheet({ restoreFocus: false });

    try {
      const data = await api(API.task(id), {
        method: 'PATCH',
        body: JSON.stringify(patch),
      });
      const saved = data && isTask(data.task) ? data.task : null;
      if (saved) mergeSavedTask(id, saved);
      state.counts = computeCounts(state.tasks);
      state.error = null;
      renderAll();

      if (message) {
        const latest = findTask(id) || optimistic;
        if (undoable) {
          showToast(message, 'success', {
            actionLabel: '撤销',
            duration: 5000,
            onAction: async () => {
              state.selectedId = id;
              await applyTaskPatch(latest, { status: 'pending' }, {
                message: '已撤销',
                undoable: false,
              });
              focusTaskCard(id, capturedIndex);
            },
          });
        } else {
          showToast(message, 'success');
        }
      }
    } catch (error) {
      const currentIndex = state.tasks.findIndex((item) => String(item.id) === id);
      if (currentIndex >= 0) {
        const rolledBack = state.tasks.slice();
        rolledBack[currentIndex] = previous;
        state.tasks = rolledBack;
      }
      state.counts = computeCounts(state.tasks);
      renderAll();
      showToast(describeError(error, '操作失败，请重试。'), 'error');
      if (error instanceof ApiError && error.status === 401) {
        openAuth('访问令牌无效，请重新输入。');
      }
    }
  }

  /* ---------------- 抽屉（窄屏） ---------------- */

  function openSheet(kind) {
    if (state.authOpen || state.sheet === kind) return;
    lastFocused = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    state.sheet = kind;
    dom.detailSheet.hidden = kind !== 'detail';
    dom.newSheet.hidden = kind !== 'new';
    dom.sheetOverlay.classList.add('is-open');
    dom.sheetOverlay.setAttribute('aria-hidden', 'false');
    document.body.classList.add('sheet-open');
    syncBackgroundInert();
    window.requestAnimationFrame(() => {
      const target = kind === 'detail' ? dom.detailClose : dom.newTitleInput;
      try {
        target.focus({ preventScroll: true });
      } catch (error) {
        /* 忽略焦点失败 */
      }
    });
  }

  function closeSheet({ restoreFocus = true, keepDetail = false } = {}) {
    if (!state.sheet) return;
    const closing = state.sheet;
    state.sheet = null;
    dom.sheetOverlay.classList.remove('is-open');
    dom.sheetOverlay.setAttribute('aria-hidden', 'true');
    document.body.classList.remove('sheet-open');
    if (closing === 'detail' && !keepDetail) {
      state.detail = null;
      state.editing = null;
    }
    syncBackgroundInert();

    window.setTimeout(() => {
      if (state.sheet) return;
      dom.detailSheet.hidden = true;
      dom.newSheet.hidden = true;
    }, 320);

    if (restoreFocus) {
      const fallback = isDesktop()
        ? (dom.railButtons[state.tab] || dom.listTitle)
        : (dom.mobileArchiveButtons[state.tab] || dom.tabButtons[state.tab] || dom.listTitle);
      const target = lastFocused && document.contains(lastFocused) ? lastFocused : fallback;
      try {
        if (target) target.focus({ preventScroll: true });
      } catch (error) {
        /* 忽略焦点失败 */
      }
    }
    lastFocused = null;
  }

  function trapFocus(event) {
    if (!state.sheet || event.key !== 'Tab') return;
    const container = state.sheet === 'detail' ? dom.detailSheet : dom.newSheet;
    const focusable = Array.from(container.querySelectorAll(
      'button, input, textarea, select, a[href], [tabindex]:not([tabindex="-1"])',
    )).filter((node) => !node.disabled && node.offsetParent !== null);
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  function trapAuthFocus(event) {
    if (!state.authOpen || event.key !== 'Tab') return;
    const focusable = [dom.tokenInput, dom.tokenToggle, dom.authSubmit]
      .filter((node) => node && !node.disabled && node.offsetParent !== null);
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (!focusable.includes(document.activeElement)) {
      event.preventDefault();
      first.focus();
    } else if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  /* ---------------- 详情：桌面面板 / 窄屏抽屉 ---------------- */

  function getDetailTask() {
    if (!state.detail) return null;
    return findTask(state.detail.id) || state.detail.task || null;
  }

  function selectTask(taskId) {
    if (state.authOpen) return;
    const task = findTask(taskId);
    if (!task) return;
    /* 批次 3：打开详情自动退出批量选择 */
    if (state.selectMode) clearSelectState();
    const id = String(taskId);
    state.selectedId = id;
    state.editing = null;
    state.detail = {
      id,
      task,
      sources: null,
      /* 批次 4：事项账本里的所属事项（GET /api/tasks/{id} 的 event 段） */
      event: null,
      sourcesLoading: true,
      sourcesError: null,
    };
    if (isDesktop()) {
      renderAll();
    } else {
      openSheet('detail');
      renderSheetDetail();
    }
    void loadDetailSources(id);
  }

  async function loadDetailSources(id) {
    if (!state.detail || String(state.detail.id) !== String(id)) return;
    state.detail.sourcesLoading = true;
    state.detail.sourcesError = null;
    renderDetail();

    try {
      const data = await api(API.task(id));
      if (!state.detail || String(state.detail.id) !== String(id)) return;
      if (data && isTask(data.task)) {
        const index = state.tasks.findIndex((item) => String(item.id) === String(id));
        if (index >= 0) {
          const nextTasks = state.tasks.slice();
          nextTasks[index] = { ...state.tasks[index], ...data.task };
          state.tasks = nextTasks;
          state.counts = computeCounts(state.tasks);
          state.detail.task = { ...state.detail.task, ...data.task };
        }
      }
      state.detail.sources = data && Array.isArray(data.sources) ? data.sources : [];
      state.detail.event = data && data.event && typeof data.event === 'object' ? data.event : null;
      state.detail.sourcesLoading = false;
      state.detail.sourcesError = null;
      if (state.loaded) renderList();
      renderDetail();
    } catch (error) {
      if (!state.detail || String(state.detail.id) !== String(id)) return;
      state.detail.sourcesLoading = false;
      state.detail.sourcesError = error;
      renderDetail();
    }
  }

  function renderDetail() {
    if (!state.detail) {
      if (isDesktop()) renderDesktopDetail();
      return;
    }
    if (isDesktop()) {
      renderDesktopDetail();
    } else if (state.sheet === 'detail') {
      renderSheetDetail();
    }
  }

  function setDetailHead(task) {
    const code = task ? String(task.id).slice(0, 4).toUpperCase() : '0000';
    if (dom.detailCode) dom.detailCode.textContent = `TASK · ${code}`;
    if (dom.detailOrbitCode) dom.detailOrbitCode.textContent = code;
    if (dom.detailState) dom.detailState.textContent = state.editing ? '编辑中' : '只读';
  }

  function buildDetailEmpty() {
    return el('div', { class: 'detail-empty' }, [
      el('b', { text: '选择一条任务查看详情' }),
      el('p', { text: '点击左侧列表中的任务，这里会显示完整内容并支持编辑。' }),
      el('p', {
        class: 'list-sub',
        text: 'j / k 选择 · Enter 打开 · e 编辑 · x 完成 · n 新建',
      }),
    ]);
  }

  function detailMetaRow(label, value) {
    return el('div', {}, [
      el('dt', { text: label }),
      el('dd', { text: value }),
    ]);
  }

  function detailTags(task) {
    const tags = [];
    const info = dueInfo(task);
    const due = parseDate(task.due_at);
    if (info && info.tone === 'overdue' && task.status !== 'done') {
      const diff = due ? dayDiff(due) : 0;
      tags.push(el('span', { class: 'tag tag--overdue', text: diff === 0 ? '今天已逾期' : `逾期 ${Math.abs(diff)} 天` }));
    }
    const priority = normalizePriority(task.priority);
    if (priority !== 'normal') tags.push(el('span', { class: 'tag', text: PRIORITY_LABELS[priority] }));
    const status = normalizeStatus(task.status);
    if (status !== 'pending') tags.push(el('span', { class: `tag tag--${status}`, text: STATUS_LABELS[status] }));
    return el('div', { class: 'detail-tags' }, tags);
  }

  /* ---------------- 批次 4：所属事项（事项账本 event 段） ---------------- */

  const EVENT_STATUS_LABELS = { open: '归纳中', closed: '已关闭' };

  function normalizeComparable(value) {
    return String(value == null ? '' : value)
      .replace(/\s+/g, '')
      .replace(/[。．.！!？?，,；;：:]+$/u, '');
  }

  function eventView() {
    const detail = state.detail;
    if (!detail) return null;
    /* 加载中 / 失败时不渲染，避免带出上一条任务的旧数据 */
    if (detail.sourcesLoading || detail.sourcesError) return null;
    const event = detail.event;
    if (!event || typeof event !== 'object') return null;

    const rawTitle = typeof event.title === 'string' ? event.title.trim() : '';
    const rawSummary = typeof event.summary === 'string' ? event.summary.trim() : '';
    const fallbackTitle = rawSummary ? rawSummary.split(/\r?\n/)[0].trim() : '';
    const title = truncateText(rawTitle || fallbackTitle, 120) || '未命名事项';
    const summary = rawSummary && normalizeComparable(rawSummary) !== normalizeComparable(rawTitle || fallbackTitle)
      ? truncateText(rawSummary, 160)
      : '';
    const group = typeof event.group_name === 'string' ? event.group_name.trim() : '';
    const status = event.status === 'closed' ? 'closed' : 'open';
    return { title, summary, group, status, statusLabel: EVENT_STATUS_LABELS[status] };
  }

  /* 桌面右栏读态：备注与来源原文之间 */
  function buildDetailEvent() {
    const view = eventView();
    if (!view) return null;
    const children = [
      el('div', { class: 'detail-event-head' }, [
        el('b', { text: '所属事项' }),
        el('span', { class: `tag tag--event tag--event-${view.status}`, text: view.statusLabel }),
      ]),
      el('p', { class: 'detail-event-title', text: view.title }),
    ];
    if (view.summary) children.push(el('p', { class: 'detail-event-summary', text: view.summary }));
    if (view.group) children.push(el('p', { class: 'detail-event-meta', text: `群 · ${view.group}` }));
    return el('div', { class: 'detail-event' }, children);
  }

  /* 窄屏抽屉：备注之后、来源原文之前 */
  function buildSheetEvent() {
    const view = eventView();
    if (!view) return null;
    const children = [
      el('h3', { class: 'section-title' }, [
        el('span', { text: '所属事项' }),
        el('span', { class: `tag tag--event tag--event-${view.status}`, text: view.statusLabel }),
      ]),
      el('p', { class: 'notes event-title', text: view.title }),
    ];
    if (view.summary) children.push(el('p', { class: 'notes event-summary', text: view.summary }));
    if (view.group) children.push(el('p', { class: 'event-meta', text: `群 · ${view.group}` }));
    return el('section', { class: 'detail-section' }, children);
  }

  function buildDetailRead(task) {
    const info = dueInfo(task);
    const group = typeof task.group_name === 'string' ? task.group_name.trim() : '';
    const notes = typeof task.notes === 'string' ? task.notes : '';
    const status = normalizeStatus(task.status);
    const priority = normalizePriority(task.priority);
    const matchReason = typeof task.match_reason === 'string' ? task.match_reason.trim() : '';

    const metaRows = [
      detailMetaRow('截止时间', info ? info.text : '无截止时间'),
      detailMetaRow('优先级', PRIORITY_LABELS[priority]),
      detailMetaRow('来源', group || '手动添加'),
    ];
    if (matchReason) metaRows.push(detailMetaRow('判定依据', matchReason));
    metaRows.push(detailMetaRow('状态', STATUS_LABELS[status]));

    return el('div', { class: 'detail-read' }, [
      el('h3', { class: 'detail-title', text: task.title }),
      detailTags(task),
      el('dl', { class: 'detail-meta' }, metaRows),
      el('div', { class: 'detail-notes' }, [
        el('b', { text: '备注' }),
        el('p', { text: notes || '暂无备注' }),
      ]),
      buildDetailEvent(),
      ...buildDesktopSources(),
    ]);
  }

  function buildDesktopSources() {
    const detail = state.detail;
    if (!detail) return [];
    if (detail.sourcesLoading) {
      return [el('div', { class: 'source' }, [
        el('b', { text: '来源原文' }),
        el('p', { text: '加载中…' }),
      ])];
    }
    if (detail.sourcesError) {
      return [el('div', { class: 'source source--error' }, [
        el('b', { text: '来源原文' }),
        el('p', { text: '来源加载失败' }),
        el('button', {
          class: 'inline-retry',
          attrs: { type: 'button' },
          text: '重试',
          on: { click: () => { void loadDetailSources(detail.id); } },
        }),
      ])];
    }
    const sources = Array.isArray(detail.sources) ? detail.sources : [];
    if (!sources.length) {
      return [el('div', { class: 'source' }, [
        el('b', { text: '来源原文' }),
        el('p', { text: '暂无来源原文' }),
      ])];
    }
    return [el('div', { class: 'detail-sources' }, [
      /* 批次 4：来源标题补上条数（加载中 / 失败分支不显示） */
      el('p', { class: 'detail-sources-title', text: `来源原文 · ${sources.length} 条` }),
      ...sources.map((source) => {
        const content = source && typeof source.content === 'string' ? source.content : '（无内容）';
        const group = source && typeof source.group_name === 'string' && source.group_name.trim()
          ? source.group_name.trim()
          : '';
        const sender = source && typeof source.sender === 'string' ? source.sender.trim() : '';
        const time = formatSourceTime(source && source.ts);
        const meta = [sender, time].filter(Boolean).join(' · ');
        return el('div', { class: 'source' }, [
          group ? el('b', { text: group }) : null,
          el('p', { text: content }),
          meta ? el('p', { class: 'source-meta', text: meta }) : null,
        ]);
      }),
    ])];
  }

  function detailActionButton(label, className, action, handler) {
    return el('button', {
      class: `btn ${className}`.trim(),
      attrs: { type: 'button', 'data-action': action },
      text: label,
      on: { click: handler },
    });
  }

  function buildDesktopFoot(task) {
    const status = normalizeStatus(task.status);
    const buttons = [];
    if (status === 'pending') {
      buttons.push(detailActionButton('完成', 'btn-primary', 'done', () => completeTask(task)));
      buttons.push(detailActionButton('稍后', 'btn-ghost', 'snooze', () => snoozeTask(task)));
      buttons.push(detailActionButton('忽略', 'btn-ghost', 'ignore', () => ignoreTask(task)));
    } else {
      buttons.push(detailActionButton('恢复为待办', 'btn-primary', 'restore', () => restoreTask(task)));
    }
    buttons.push(detailActionButton('编辑', 'btn-ghost', 'edit', () => startEdit(task)));
    /* 批次 3：归档类视图（已完成 / 已忽略 / 稍后）的读态 footer 提供真删除 */
    if (!state.search && (state.tab === 'done' || state.tab === 'ignored' || state.tab === 'snoozed')) {
      buttons.push(detailActionButton('删除', 'btn-danger', 'delete', () => { void deleteTask(task); }));
    }
    return buttons;
  }

  function captureDetailAction() {
    const active = document.activeElement;
    if (!active || !dom.detailPanel || !dom.detailPanel.contains(active)) return null;
    return active.dataset ? active.dataset.action || null : null;
  }

  function restoreDetailAction(action) {
    if (!action || state.editing) return;
    const target = dom.detailFoot.querySelector(`[data-action="${cssEscape(action)}"]`);
    if (target && typeof target.focus === 'function') {
      target.focus({ preventScroll: true });
    }
  }

  function renderDesktopDetail() {
    if (!isDesktop()) return;
    const task = getDetailTask();
    setDetailHead(task);

    if (!task) {
      state.editing = null;
      dom.detailBody.replaceChildren(buildDetailEmpty());
      dom.detailFoot.replaceChildren();
      return;
    }

    if (state.editing && String(state.editing.id) === String(task.id)) {
      setDetailHead(task);
      if (!dom.detailBody.querySelector('.edit-form')) {
        dom.detailBody.replaceChildren(buildEditForm(task));
        window.cancelAnimationFrame(iconEditTitleFrame);
        iconEditTitleFrame = window.requestAnimationFrame(focusEditTitle);
      }
      if (!dom.detailFoot.querySelector('[data-action="save"]')) {
        dom.detailFoot.replaceChildren(...buildEditFoot());
      }
      return;
    }
    if (state.editing) state.editing = null;

    const previousAction = captureDetailAction();
    dom.detailBody.replaceChildren(buildDetailRead(task));
    dom.detailFoot.replaceChildren(...buildDesktopFoot(task));
    restoreDetailAction(previousAction);
  }

  /* ---------------- 内联编辑（桌面） ---------------- */

  function startEdit(task) {
    if (!isDesktop() || !task) return;
    state.editing = { id: String(task.id) };
    renderDesktopDetail();
  }

  function cancelEdit() {
    if (!state.editing) return;
    state.editing = null;
    renderDesktopDetail();
    restoreDetailAction('edit');
  }

  function focusEditTitle() {
    const input = dom.detailBody.querySelector('.edit-form input[name="title"]');
    if (!input) return;
    try {
      input.focus({ preventScroll: true });
      const length = input.value.length;
      input.setSelectionRange(length, length);
    } catch (error) {
      /* 忽略焦点失败 */
    }
  }

  function setEditError(message) {
    const error = dom.detailBody.querySelector('.edit-form .form-error');
    if (!error) return;
    error.textContent = message || '';
    error.hidden = !message;
  }

  function buildSeg(selected, label) {
    const seg = el('div', { class: 'seg', attrs: { role: 'group', 'aria-label': label } }, PRIORITY_OPTIONS.map(
      ([value, text]) => el('button', {
        class: 'seg-btn',
        attrs: { type: 'button', 'data-priority': value },
        text,
      }),
    ));
    bindSeg(seg);
    setSegValue(seg, selected);
    return seg;
  }

  function setSegValue(seg, value) {
    if (!seg) return;
    const normalized = normalizePriority(value);
    for (const button of seg.querySelectorAll('.seg-btn')) {
      const on = button.dataset.priority === normalized;
      button.classList.toggle('is-on', on);
      button.setAttribute('aria-pressed', on ? 'true' : 'false');
    }
  }

  function getSegValue(seg) {
    if (!seg) return 'normal';
    const active = seg.querySelector('.seg-btn.is-on') || seg.querySelector('.seg-btn[data-priority="normal"]');
    return normalizePriority(active ? active.dataset.priority : 'normal');
  }

  function bindSeg(seg) {
    if (!seg || seg.dataset.bound === '1') return;
    seg.dataset.bound = '1';
    seg.addEventListener('click', (event) => {
      const button = event.target.closest('.seg-btn');
      if (!button || !seg.contains(button)) return;
      setSegValue(seg, button.dataset.priority);
    });
  }

  function dueChoiceValue(choice) {
    const now = new Date();
    if (choice === 'today-18') {
      return new Date(now.getFullYear(), now.getMonth(), now.getDate(), 18, 0, 0, 0);
    }
    if (choice === 'tomorrow-09') {
      return new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1, 9, 0, 0, 0);
    }
    if (choice === 'friday-18') {
      const weekday = (now.getDay() + 6) % 7; // 0=周一
      let delta = (4 - weekday + 7) % 7;
      const date = new Date(now.getFullYear(), now.getMonth(), now.getDate() + delta, 18, 0, 0, 0);
      if (date.getTime() < now.getTime()) date.setDate(date.getDate() + 7);
      return date;
    }
    return null;
  }

  function buildDueChips(selectedValue) {
    const chips = el('div', { class: 'chips' }, DUE_CHOICES.map(([value, text]) => el('button', {
      class: 'chip',
      attrs: { type: 'button', 'data-due': value },
      text,
    })));
    markDueChips(chips, selectedValue);
    return chips;
  }

  function markDueChips(chips, value) {
    if (!chips) return;
    const key = dueKey(value);
    for (const chip of chips.querySelectorAll('.chip')) {
      const choice = chip.dataset.due;
      let on = false;
      // 「清除」是动作而不是状态，不显示选中态（避免误解为“已清除”被选中）
      if (choice !== 'clear') {
        const target = dueChoiceValue(choice);
        on = Boolean(key && target && dueKey(target) === key);
      }
      chip.classList.toggle('is-on', on);
      chip.setAttribute('aria-pressed', on ? 'true' : 'false');
    }
  }

  function bindDueChips(chips, input) {
    if (!chips || !input || chips.dataset.bound === '1') return;
    chips.dataset.bound = '1';
    chips.addEventListener('click', (event) => {
      const chip = event.target.closest('.chip');
      if (!chip || !chips.contains(chip)) return;
      event.preventDefault();
      const choice = chip.dataset.due;
      if (choice === 'clear') {
        input.value = '';
      } else {
        const target = dueChoiceValue(choice);
        if (target) input.value = toDateTimeLocal(target);
      }
      markDueChips(chips, input.value);
    });
    const sync = () => markDueChips(chips, input.value);
    input.addEventListener('input', sync);
    input.addEventListener('change', sync);
  }

  function buildEditForm(task) {
    const notes = typeof task.notes === 'string' ? task.notes : '';
    const titleInput = el('input', {
      class: 'input',
      attrs: {
        type: 'text',
        name: 'title',
        maxlength: '120',
        autocomplete: 'off',
        value: task.title,
      },
    });
    const titleField = el('div', { class: 'field' }, [
      el('label', { attrs: { for: 'editTitle' }, text: '标题' }),
      titleInput,
    ]);
    titleInput.id = 'editTitle';

    const priorityField = el('div', { class: 'field' }, [
      el('span', { class: 'field-label', text: '优先级' }),
      buildSeg(normalizePriority(task.priority), '优先级'),
    ]);

    const dueInput = el('input', {
      class: 'input',
      attrs: { type: 'datetime-local', name: 'due_at', value: dueKey(task.due_at) },
    });
    const chips = buildDueChips(dueKey(task.due_at));
    bindDueChips(chips, dueInput);
    const dueField = el('div', { class: 'field' }, [
      el('label', { attrs: { for: 'editDue' }, text: '截止时间' }),
      chips,
      dueInput,
    ]);
    dueInput.id = 'editDue';

    const notesInput = el('textarea', {
      class: 'input',
      attrs: { name: 'notes', rows: '3', maxlength: '2000' },
    });
    notesInput.value = notes;
    const notesField = el('div', { class: 'field' }, [
      el('label', { attrs: { for: 'editNotes' }, text: '备注' }),
      notesInput,
    ]);
    notesInput.id = 'editNotes';

    const formError = el('p', {
      class: 'form-error',
      attrs: { role: 'alert', hidden: 'hidden' },
    });

    const form = el('div', { class: 'edit-form', attrs: { 'data-id': String(task.id) } }, [
      titleField,
      priorityField,
      dueField,
      notesField,
      formError,
    ]);

    titleInput.addEventListener('input', () => setEditError(''));
    notesInput.addEventListener('input', () => setEditError(''));

    return form;
  }

  function collectEditPatch(task) {
    const form = dom.detailBody.querySelector('.edit-form');
    if (!form) return null;
    const titleInput = form.querySelector('input[name="title"]');
    const dueInput = form.querySelector('input[name="due_at"]');
    const notesInput = form.querySelector('textarea[name="notes"]');
    const seg = form.querySelector('.seg');

    const title = titleInput ? titleInput.value.trim() : '';
    if (!title) {
      setEditError('标题不能为空。');
      if (titleInput) titleInput.focus();
      return null;
    }
    const priority = getSegValue(seg);
    const dueValue = dueInput ? dueInput.value.trim() : '';
    const nextDue = dueKey(dueValue);
    if (dueValue && !nextDue) {
      setEditError('截止时间格式不正确。');
      if (dueInput) dueInput.focus();
      return null;
    }
    const notes = notesInput ? notesInput.value.trim().slice(0, 2000) : '';
    const originalNotes = typeof task.notes === 'string' ? task.notes.trim() : '';

    const patch = {};
    if (title !== task.title) patch.title = title;
    if (priority !== normalizePriority(task.priority)) patch.priority = priority;
    if (nextDue !== dueKey(task.due_at)) patch.due_at = nextDue ? `${nextDue}:00` : '';
    if (notes !== originalNotes) patch.notes = notes;
    return patch;
  }

  async function saveEdit() {
    if (!state.editing || state.saving) return;
    const task = getDetailTask();
    if (!task) {
      cancelEdit();
      return;
    }
    const patch = collectEditPatch(task);
    if (!patch) return;

    if (!Object.keys(patch).length) {
      state.editing = null;
      renderDesktopDetail();
      restoreDetailAction('edit');
      return;
    }

    const saveButton = dom.detailFoot.querySelector('[data-action="save"]');
    state.saving = true;
    if (saveButton) {
      saveButton.disabled = true;
      saveButton.textContent = '保存中…';
    }
    setEditError('');

    try {
      const data = await api(API.task(task.id), {
        method: 'PATCH',
        body: JSON.stringify(patch),
      });
      const saved = data && isTask(data.task) ? data.task : null;
      if (saved) mergeSavedTask(task.id, saved);
      state.counts = computeCounts(state.tasks);
      state.error = null;
      state.editing = null;
      renderAll();
      showToast('已保存', 'success');
      const editButton = dom.detailFoot.querySelector('[data-action="edit"]');
      if (editButton) editButton.focus({ preventScroll: true });
    } catch (error) {
      setEditError(describeError(error, '保存失败，请重试。'));
      if (error instanceof ApiError && error.status === 401) {
        openAuth('访问令牌无效，请重新输入。');
      }
    } finally {
      state.saving = false;
      const latestSaveButton = dom.detailFoot.querySelector('[data-action="save"]');
      if (latestSaveButton) {
        latestSaveButton.disabled = false;
        latestSaveButton.textContent = '保存修改';
      }
    }
  }

  function buildEditFoot() {
    return [
      detailActionButton('保存修改', 'btn-primary', 'save', () => { void saveEdit(); }),
      detailActionButton('取消', 'btn-ghost', 'cancel', () => cancelEdit()),
    ];
  }

  /* ---------------- 窄屏抽屉详情 ---------------- */

  function renderSheetDetail() {
    if (isDesktop() || state.sheet !== 'detail' || !state.detail) return;
    const task = getDetailTask();
    if (!task) {
      closeSheet();
      return;
    }

    const scrollChildren = [
      el('h2', { class: 'detail-title', attrs: { id: 'detailTitle' }, text: task.title }),
      buildSheetChips(task),
    ];
    const due = buildSheetDue(task);
    if (due) scrollChildren.push(due);
    const matchReason = typeof task.match_reason === 'string' ? task.match_reason.trim() : '';
    if (matchReason) {
      scrollChildren.push(el('section', { class: 'detail-section' }, [
        el('h3', { class: 'section-title', text: '判定依据' }),
        el('p', { class: 'notes', text: matchReason }),
      ]));
    }
    if (typeof task.notes === 'string' && task.notes.trim()) {
      scrollChildren.push(el('section', { class: 'detail-section' }, [
        el('h3', { class: 'section-title', text: '备注' }),
        el('p', { class: 'notes', text: task.notes }),
      ]));
    }
    const eventSection = buildSheetEvent();
    if (eventSection) scrollChildren.push(eventSection);
    scrollChildren.push(buildSheetSources());

    dom.detailScroll.replaceChildren(...scrollChildren);
    dom.detailFooter.replaceChildren(buildSheetActions(task));
  }

  function buildSheetChips(task) {
    const status = normalizeStatus(task.status);
    const priority = normalizePriority(task.priority);
    const chips = [
      el('span', {
        class: `chip chip--status status-${status}`,
        text: STATUS_LABELS[status],
      }),
      el('span', { class: 'chip chip--priority' }, [
        el('span', { class: `priority priority--${priority}`, attrs: { 'aria-hidden': 'true' } }),
        el('span', { text: PRIORITY_LABELS[priority] }),
      ]),
    ];
    const group = typeof task.group_name === 'string' ? task.group_name.trim() : '';
    if (group) {
      chips.push(el('span', { class: 'chip chip--group', attrs: { title: group }, text: group }));
    }
    return el('div', { class: 'detail-chips' }, chips);
  }

  function buildSheetDue(task) {
    const info = dueInfo(task);
    const due = parseDate(task.due_at);
    if (!info || !due) return null;
    return el('div', {
      class: `detail-due due due--${info.tone}`,
      attrs: { title: `截止时间：${formatDateTime(due)}` },
    }, [
      createIcon('clock', 14, 2),
      el('span', { text: info.text }),
    ]);
  }

  function buildSheetSources() {
    const detail = state.detail;
    const heading = el('h3', { class: 'section-title' }, [el('span', { text: '来源原文' })]);
    const section = el('section', { class: 'detail-section' }, [heading]);

    if (detail.sourcesLoading) {
      section.append(
        el('div', { class: 'source-skeleton' }),
        el('div', { class: 'source-skeleton' }),
      );
      return section;
    }

    if (detail.sourcesError) {
      section.append(el('div', { class: 'section-error' }, [
        el('span', { text: '来源加载失败' }),
        el('button', {
          class: 'inline-retry',
          attrs: { type: 'button' },
          text: '重试',
          on: { click: () => { void loadDetailSources(detail.id); } },
        }),
      ]));
      return section;
    }

    const sources = Array.isArray(detail.sources) ? detail.sources : [];
    if (!sources.length) {
      section.append(el('p', { class: 'section-muted', text: '暂无来源原文' }));
      return section;
    }

    heading.append(el('span', { class: 'section-count', text: String(sources.length) }));
    section.append(el('div', { class: 'sources' }, sources.map(createSourceCard)));
    return section;
  }

  function createSourceCard(source) {
    const content = source && typeof source.content === 'string'
      ? source.content
      : '（无内容）';
    const meta = [];
    if (source && typeof source.group_name === 'string' && source.group_name.trim()) {
      meta.push(source.group_name.trim());
    }
    if (source && typeof source.sender === 'string' && source.sender.trim()) {
      meta.push(source.sender.trim());
    }
    const time = formatSourceTime(source && source.ts);
    if (time) meta.push(time);

    return el('article', { class: 'source' }, [
      el('p', { class: 'source-content', text: content }),
      meta.length ? el('p', { class: 'source-meta', text: meta.join(' · ') }) : null,
    ]);
  }

  function buildSheetActions(task) {
    const status = normalizeStatus(task.status);
    /* 批次 4（需求 D）：归档视图（已完成/已忽略/稍后）提供恢复 + 真删除 */
    const archive = !state.search && isArchiveView();
    const actions = [];

    if (status === 'pending') {
      actions.push(el('button', {
        class: 'btn btn-primary sheet-action sheet-action--wide',
        attrs: { type: 'button', 'data-action': 'done' },
        on: {
          click: () => completeTask(task),
        },
      }, buttonLabel('check', '完成')));

      actions.push(el('button', {
        class: 'btn btn-secondary sheet-action',
        attrs: { type: 'button', 'data-action': 'snooze' },
        on: {
          click: () => snoozeTask(task),
        },
      }, buttonLabel('clock', '稍后 1 小时')));

      actions.push(el('button', {
        class: 'btn btn-secondary sheet-action',
        attrs: { type: 'button', 'data-action': 'ignore' },
        on: {
          click: () => ignoreTask(task),
        },
      }, buttonLabel('close', '忽略')));
    } else {
      /* 已完成 / 已忽略 / 稍后：主操作是恢复为待办 */
      actions.push(el('button', {
        class: 'btn btn-primary sheet-action sheet-action--wide',
        attrs: { type: 'button', 'data-action': 'restore' },
        on: {
          click: () => {
            void applyTaskPatch(task, { status: 'pending' }, { message: '已恢复' });
          },
        },
      }, buttonLabel('refresh', '恢复为待办')));
    }

    if (archive) {
      actions.push(el('button', {
        class: 'btn btn-danger sheet-action sheet-action--wide',
        attrs: { type: 'button', 'data-action': 'delete' },
        on: {
          click: () => { void deleteTask(task); },
        },
      }, buttonLabel('trash', '删除')));
    } else if (status === 'pending') {
      actions.push(el('button', {
        class: 'btn btn-ghost-danger sheet-action sheet-action--wide',
        attrs: { type: 'button' },
        on: {
          click: () => ignoreTask(task),
        },
      }, buttonLabel('trash', '删除（忽略）')));
    }

    return el('div', { class: 'sheet-actions' }, actions);
  }

  /* ---------------- 新建任务 ---------------- */

  function openNewTask() {
    if (state.authOpen) {
      void openAuth();
      return;
    }
    /* 批次 3：新建任务时退出批量选择 */
    if (state.selectMode) exitSelectMode();
    dom.newSheet.reset();
    dom.newError.hidden = true;
    dom.newError.textContent = '';
    dom.newTitleInput.removeAttribute('aria-invalid');
    setSegValue(dom.newPrioritySeg, 'normal');
    dom.newDueInput.value = '';
    markDueChips(dom.newDueChips, '');
    openSheet('new');
  }

  function showNewError(message) {
    dom.newError.textContent = message;
    dom.newError.hidden = false;
  }

  function setNewSubmitting(busy) {
    dom.newSubmit.disabled = busy;
    dom.newSubmit.textContent = busy ? '添加中…' : '添加任务';
  }

  async function handleNewSubmit(event) {
    event.preventDefault();
    if (state.submitting) return;

    const title = dom.newTitleInput.value.trim();
    if (!title) {
      dom.newTitleInput.setAttribute('aria-invalid', 'true');
      showNewError('请填写任务标题。');
      dom.newTitleInput.focus();
      return;
    }
    dom.newTitleInput.removeAttribute('aria-invalid');

    const dueValue = dom.newDueInput.value;
    let dueAt = '';
    if (dueValue) {
      const key = dueKey(dueValue);
      if (!key) {
        showNewError('截止时间格式不正确。');
        return;
      }
      dueAt = `${key}:00`;
    }

    const payload = {
      title,
      notes: dom.newNotesInput.value.trim(),
      priority: getSegValue(dom.newPrioritySeg),
    };
    if (dueAt) payload.due_at = dueAt;

    state.submitting = true;
    dom.newError.hidden = true;
    setNewSubmitting(true);

    try {
      const data = await api(API.tasks, {
        method: 'POST',
        body: JSON.stringify(payload),
      });
      const created = data && isTask(data.task) ? data.task : null;
      if (created) {
        state.tasks = [created, ...state.tasks.filter(
          (task) => String(task.id) !== String(created.id),
        )];
        state.loaded = true;
        state.error = null;
        state.counts = computeCounts(state.tasks);
      }
      closeSheet();
      dom.newSheet.reset();
      showToast('已添加任务', 'success');
      renderAll();
    } catch (error) {
      const message = describeError(error, '添加失败，请重试。');
      showNewError(message);
      showToast(message, 'error');
    } finally {
      state.submitting = false;
      setNewSubmitting(false);
    }
  }

  /* ---------------- 视图切换 ---------------- */

  function switchTab(tab) {
    if (!VIEWS.includes(tab)) return;
    /* 批次 3：切换视图自动退出批量选择 */
    const hadSelectMode = state.selectMode;
    if (hadSelectMode) clearSelectState();
    const changed = tab !== state.tab;
    state.tab = tab;
    /* 批次 4（需求 D）：手机端归档视图（已忽略/稍后）现在也可达，不再强制回「今天」 */
    if (state.editing) state.editing = null;
    if (state.search) {
      clearSearch({ render: false });
    }
    if (changed) {
      /* 切视图后旧的键盘选中项通常不在列表里，清掉避免 e/Enter 作用到不可见任务 */
      state.selectedId = null;
      renderAll();
      if (window.scrollY > 0) {
        window.scrollTo({ top: 0, behavior: prefersReducedMotion() ? 'auto' : 'smooth' });
      }
    } else if (hadSelectMode) {
      /* 点击当前视图也要把「退出选择」后的 UI 立刻重画 */
      renderAll();
    }
  }

  function handleRailKeydown(event) {
    if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const current = VIEWS.indexOf(state.tab);
    let next = current;
    if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') next = (current + VIEWS.length - 1) % VIEWS.length;
    if (event.key === 'ArrowRight' || event.key === 'ArrowDown') next = (current + 1) % VIEWS.length;
    if (event.key === 'Home') next = 0;
    if (event.key === 'End') next = VIEWS.length - 1;
    switchTab(VIEWS[next]);
    const target = dom.railButtons[VIEWS[next]];
    if (target) target.focus();
  }

  function handleTabsKeydown(event) {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    /* 批次 4（需求 D）：归档视图按「已完成」tab 的序号参与左右切换 */
    const activeTab = isArchiveView() ? 'done' : state.tab;
    const current = MOBILE_VIEWS.indexOf(activeTab);
    const base = current >= 0 ? current : 0;
    let next = base;
    if (event.key === 'ArrowLeft') next = (base + MOBILE_VIEWS.length - 1) % MOBILE_VIEWS.length;
    if (event.key === 'ArrowRight') next = (base + 1) % MOBILE_VIEWS.length;
    if (event.key === 'Home') next = 0;
    if (event.key === 'End') next = MOBILE_VIEWS.length - 1;
    switchTab(MOBILE_VIEWS[next]);
    const target = dom.tabButtons[MOBILE_VIEWS[next]];
    if (target) target.focus();
  }

  /* ---------------- 搜索 ---------------- */

  function scheduleSearch() {
    const raw = dom.searchInput.value;
    state.searchRaw = raw;
    window.clearTimeout(searchTimer);
    searchTimer = window.setTimeout(() => {
      state.search = raw.trim().toLowerCase();
      /* 批次 3：搜索会改变可见集合，直接退出批量选择 */
      if (state.selectMode) clearSelectState();
      if (raw.trim()) {
        if (!state.sheet) closeSheetIfDetail();
        renderAll();
      } else {
        renderAll();
      }
    }, 200);
  }

  function closeSheetIfDetail() {
    if (state.sheet === 'detail') closeSheet({ restoreFocus: false });
  }

  function clearSearch({ focus = false, render = true } = {}) {
    window.clearTimeout(searchTimer);
    state.search = '';
    state.searchRaw = '';
    if (dom.searchInput && dom.searchInput.value) dom.searchInput.value = '';
    if (render) renderAll();
    if (focus && dom.searchInput) {
      try {
        dom.searchInput.focus({ preventScroll: true });
      } catch (error) {
        /* 忽略焦点失败 */
      }
    }
  }

  /* ---------------- 视口切换 ---------------- */

  function handleViewportChange() {
    state.editing = null;
    /* 批次 3：批量选择仅桌面可用，跨断点时退出 */
    if (state.selectMode) clearSelectState();
    if (state.sheet === 'detail') closeSheet({ restoreFocus: false, keepDetail: true });
    updateStickyOffsets();
    renderAll();
    renderDetail();
  }

  /* ---------------- 事件绑定与启动 ---------------- */

  function cacheDom() {
    dom.appShell = document.querySelector('.app-shell');
    dom.syncStatus = document.getElementById('syncStatus');
    dom.syncDot = document.getElementById('syncDot');
    dom.syncText = document.getElementById('syncText');
    dom.refreshBtn = document.getElementById('refreshBtn');
    dom.tabs = document.getElementById('tabs');
    dom.rail = document.getElementById('rail');
    dom.tabButtons = {
      today: document.getElementById('tab-today'),
      week: document.getElementById('tab-week'),
      later: document.getElementById('tab-later'),
      done: document.getElementById('tab-done'),
    };
    dom.railButtons = {
      today: document.getElementById('nav-today'),
      week: document.getElementById('nav-week'),
      later: document.getElementById('nav-later'),
      done: document.getElementById('nav-done'),
      ignored: document.getElementById('nav-ignored'),
      snoozed: document.getElementById('nav-snoozed'),
    };
    dom.countNodes = {};
    for (const view of VIEWS) {
      dom.countNodes[view] = Array.from(document.querySelectorAll(`[data-count="${view}"]`));
    }
    dom.listTitle = document.getElementById('listTitle');
    dom.listSub = document.getElementById('listSub');
    dom.listTitle.tabIndex = -1;
    dom.weekProgressText = document.getElementById('weekProgressText');
    dom.weekProgressBar = document.getElementById('weekProgressBar');
    dom.searchBox = document.getElementById('searchBox');
    dom.searchInput = document.getElementById('searchInput');
    dom.newTaskBtn = document.getElementById('newTaskBtn');
    /* 批次 3：选择入口与批量操作条（index.html 已就位） */
    dom.selectBtn = document.getElementById('selectBtn');
    dom.bulkBar = document.getElementById('bulkBar');
    dom.bulkCount = document.getElementById('bulkCount');
    dom.bulkButtons = {};
    if (dom.bulkBar) {
      for (const button of dom.bulkBar.querySelectorAll('[data-bulk]')) {
        if (button.dataset.bulk) dom.bulkButtons[button.dataset.bulk] = button;
      }
      if (dom.bulkCount) dom.bulkCount.setAttribute('aria-live', 'polite');
    }
    dom.bannerSlot = document.getElementById('bannerSlot');
    /* 批次 4（需求 D）：手机端归档分段切换 */
    dom.appTop = document.querySelector('.app-top');
    dom.mobileArchiveBar = document.getElementById('mobileArchiveBar');
    dom.mobileArchiveButtons = {};
    dom.mobileArchiveCounts = {};
    if (dom.mobileArchiveBar) {
      for (const button of dom.mobileArchiveBar.querySelectorAll('[data-archive]')) {
        if (button.dataset.archive) dom.mobileArchiveButtons[button.dataset.archive] = button;
      }
      for (const badge of dom.mobileArchiveBar.querySelectorAll('[data-archive-count]')) {
        if (badge.dataset.archiveCount) dom.mobileArchiveCounts[badge.dataset.archiveCount] = badge;
      }
    }
    dom.taskList = document.getElementById('taskList');
    dom.skeleton = document.getElementById('skeleton');
    dom.stateView = document.getElementById('stateView');
    dom.addBtn = document.getElementById('addBtn');
    dom.toastRegion = document.getElementById('toastRegion');
    dom.detailPanel = document.getElementById('detailPanel');
    dom.detailBody = document.getElementById('detailBody');
    dom.detailFoot = document.getElementById('detailFoot');
    dom.detailState = document.getElementById('detailState');
    dom.detailCode = document.getElementById('detailCode');
    dom.detailOrbitCode = document.getElementById('detailOrbitCode');
    dom.sheetOverlay = document.getElementById('sheetOverlay');
    dom.sheetBackdrop = document.getElementById('sheetBackdrop');
    dom.detailSheet = document.getElementById('detailSheet');
    dom.detailClose = document.getElementById('detailClose');
    dom.detailScroll = document.getElementById('detailScroll');
    dom.detailFooter = document.getElementById('detailFooter');
    dom.newSheet = document.getElementById('newSheet');
    dom.newClose = document.getElementById('newClose');
    dom.newTitleInput = document.getElementById('newTitleInput');
    dom.newDueInput = document.getElementById('newDueInput');
    dom.newNotesInput = document.getElementById('newNotesInput');
    dom.newError = document.getElementById('newError');
    dom.newSubmit = document.getElementById('newSubmit');
    dom.newPrioritySeg = document.getElementById('newPrioritySeg');
    dom.newDueChips = document.getElementById('newDueChips');
    dom.authOverlay = document.getElementById('authOverlay');
    dom.authForm = document.getElementById('authForm');
    dom.tokenInput = document.getElementById('tokenInput');
    dom.tokenToggle = document.getElementById('tokenToggle');
    dom.authSubmit = document.getElementById('authSubmit');
    dom.authError = document.getElementById('authError');
    dom.authHealthDot = document.getElementById('authHealthDot');
    dom.authHealthText = document.getElementById('authHealthText');
  }

  function bindEvents() {
    if (dom.rail) {
      dom.rail.addEventListener('click', (event) => {
        const button = event.target.closest('.nav-item');
        if (button && button.dataset.tab) switchTab(button.dataset.tab);
      });
      dom.rail.addEventListener('keydown', handleRailKeydown);
    }

    dom.tabs.addEventListener('click', (event) => {
      const tab = event.target.closest('.tab');
      if (tab && tab.dataset.tab) switchTab(tab.dataset.tab);
    });
    dom.tabs.addEventListener('keydown', handleTabsKeydown);

    /* 批次 4（需求 D）：手机归档分段 —— 点击切换 + 方向键切换 */
    if (dom.mobileArchiveBar) {
      dom.mobileArchiveBar.addEventListener('click', (event) => {
        const button = event.target.closest('[data-archive]');
        if (!button || !dom.mobileArchiveBar.contains(button)) return;
        switchArchiveTab(button.dataset.archive);
      });
      dom.mobileArchiveBar.addEventListener('keydown', handleArchiveKeydown);
    }

    dom.refreshBtn.addEventListener('click', () => {
      if (state.loading) return;
      void loadTasks({ silent: state.loaded, notify: true });
      void checkHealth();
    });

    dom.addBtn.addEventListener('click', openNewTask);
    if (dom.newTaskBtn) dom.newTaskBtn.addEventListener('click', openNewTask);

    /* 批次 3：选择入口 + 批量条（事件委托到 data-bulk） */
    if (dom.selectBtn) dom.selectBtn.addEventListener('click', toggleSelectMode);
    if (dom.bulkBar) {
      dom.bulkBar.addEventListener('click', (event) => {
        const button = event.target.closest('[data-bulk]');
        if (!button || button.hidden || !dom.bulkBar.contains(button)) return;
        const action = button.dataset.bulk;
        if (action === 'all') {
          selectAllVisible();
          return;
        }
        if (action === 'cancel') {
          exitSelectMode({ focus: true });
          return;
        }
        void runBulkAction(action);
      });
    }

    dom.detailClose.addEventListener('click', () => closeSheet());
    dom.newClose.addEventListener('click', () => closeSheet());
    dom.sheetBackdrop.addEventListener('click', () => closeSheet());
    dom.sheetOverlay.addEventListener('keydown', trapFocus);

    dom.newSheet.addEventListener('submit', handleNewSubmit);
    dom.newTitleInput.addEventListener('input', () => {
      dom.newTitleInput.removeAttribute('aria-invalid');
      if (!dom.newError.hidden) {
        dom.newError.hidden = true;
        dom.newError.textContent = '';
      }
    });

    bindSeg(dom.newPrioritySeg);
    bindDueChips(dom.newDueChips, dom.newDueInput);
    markDueChips(dom.newDueChips, '');

    if (dom.searchInput) {
      dom.searchInput.addEventListener('input', scheduleSearch);
      dom.searchInput.addEventListener('search', scheduleSearch);
    }

    dom.authForm.addEventListener('submit', handleAuthSubmit);
    dom.tokenToggle.addEventListener('click', toggleTokenVisibility);
    dom.tokenInput.addEventListener('input', () => {
      if (!dom.authError.hidden) {
        dom.authError.hidden = true;
        dom.authError.textContent = '';
      }
    });

    document.addEventListener('keydown', (event) => {
      if (state.authOpen) {
        trapAuthFocus(event);
        return;
      }
      const typing = isTypingTarget(event.target);
      const plain = !event.metaKey && !event.ctrlKey && !event.altKey && !event.isComposing;

      if (event.key === '/' && plain) {
        if (!typing && !state.sheet && dom.searchInput && dom.searchInput.offsetParent !== null) {
          event.preventDefault();
          try {
            dom.searchInput.focus({ preventScroll: true });
            dom.searchInput.select();
          } catch (error) {
            /* 忽略焦点失败 */
          }
          return;
        }
      }

      /* Esc 是唯一在输入框里也生效的快捷键；批次 3：先退出批量选择模式 */
      if (event.key === 'Escape') {
        if (state.selectMode) {
          event.preventDefault();
          exitSelectMode({ focus: true });
          return;
        }
        if (state.search || (dom.searchInput && dom.searchInput.value)) {
          event.preventDefault();
          clearSearch({ focus: true });
          return;
        }
        if (state.editing) {
          event.preventDefault();
          cancelEdit();
          return;
        }
        if (state.sheet) {
          event.preventDefault();
          closeSheet();
          return;
        }
        if (!typing && (state.detail || state.selectedId != null)) {
          event.preventDefault();
          clearSelection();
        }
        return;
      }

      /* 批次 2：n / e / x / j / k / Enter，输入态、编辑态与弹层内不触发 */
      if (!plain || typing || state.sheet || state.editing) return;
      const key = event.key;
      if (key === 'j' || key === 'J') {
        event.preventDefault();
        moveSelection(1);
        return;
      }
      if (key === 'k' || key === 'K') {
        event.preventDefault();
        moveSelection(-1);
        return;
      }
      if (event.repeat) return;
      if (key === 'n' || key === 'N') {
        event.preventDefault();
        openNewTask();
        return;
      }
      if (key === 'e' || key === 'E') {
        event.preventDefault();
        editSelected();
        return;
      }
      if (key === 'x' || key === 'X') {
        event.preventDefault();
        toggleSelectedTask();
        return;
      }
      if (key === 'Enter') {
        /* 卡片自身 keydown 已处理 Enter；按钮/链接保留原生激活行为 */
        if (event.target instanceof Element && event.target.closest('[data-id], button, a')) return;
        if (!selectedTask()) return;
        event.preventDefault();
        openSelectedTask();
      }
    });

    const onViewportChange = () => handleViewportChange();
    if (typeof desktopQuery.addEventListener === 'function') {
      desktopQuery.addEventListener('change', onViewportChange);
    } else if (typeof desktopQuery.addListener === 'function') {
      desktopQuery.addListener(onViewportChange);
    }

    /* 批次 4（需求 D）：手机归档分段吸顶偏移跟随 sticky 头部实际高度 */
    updateStickyOffsets();
    if (dom.appTop && typeof ResizeObserver === 'function') {
      stickyObserver = new ResizeObserver(() => updateStickyOffsets());
      stickyObserver.observe(dom.appTop);
    }
    window.addEventListener('resize', updateStickyOffsets);

    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState !== 'visible') return;
      void checkHealth();
      if (!state.authOpen) void loadTasks({ silent: state.loaded });
    });

    window.addEventListener('online', () => {
      showToast('网络已恢复', 'success');
      void checkHealth();
      if (!state.authOpen) void loadTasks({ silent: state.loaded });
    });

    window.addEventListener('offline', () => {
      state.connection = 'fail';
      updateAuthHealth();
      renderStatus();
      renderBanner();
      showToast('网络已断开，恢复后会继续同步。', 'error');
    });

    window.setInterval(() => {
      if (document.visibilityState !== 'visible' || state.authOpen) return;
      void checkHealth();
      void loadTasks({ silent: true });
    }, REFRESH_INTERVAL);
  }

  function registerServiceWorker() {
    if (!('serviceWorker' in navigator)) return;
    if (window.location.protocol !== 'http:' && window.location.protocol !== 'https:') return;
    const run = () => {
      navigator.serviceWorker.register('./sw.js', { scope: './' }).catch(() => {
        /* 离线能力不可用时不阻塞页面 */
      });
    };
    /* 批次 2：延后到 window load 之后 + 至少 800ms 空闲再注册，避免和首屏渲染抢主线程 */
    const schedule = () => {
      window.setTimeout(() => {
        if (typeof window.requestIdleCallback === 'function') {
          window.requestIdleCallback(run, { timeout: 2500 });
        } else {
          run();
        }
      }, 800);
    };
    if (document.readyState === 'complete') {
      schedule();
    } else {
      window.addEventListener('load', schedule, { once: true });
    }
  }

  function init() {
    cacheDom();
    consumeUrlToken();
    bindEvents();
    dom.sheetOverlay.inert = true;
    setDetailHead(null);
    if (dom.detailBody && isDesktop()) dom.detailBody.replaceChildren(buildDetailEmpty());
    renderAll();

    if (!getToken()) openAuth();
    void checkHealth();
    void loadTasks({ silent: false });
    registerServiceWorker();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init, { once: true });
  } else {
    init();
  }
})();
