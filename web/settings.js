/* 设置页：配置 LLM（API Key / Base URL / 模型） */
(() => {
  'use strict';

  const TOKEN_KEY = 'wechat-task-hub.token';
  const $ = (selector) => document.querySelector(selector);

  const state = { token: '', loading: false };

  function toast(message) {
    const el = $('#toast');
    el.textContent = message;
    el.hidden = false;
    clearTimeout(toast._timer);
    toast._timer = setTimeout(() => { el.hidden = true; }, 2200);
  }

  function showAuth(message = '') {
    $('#authOverlay').hidden = false;
    $('#authError').textContent = message;
    $('#authError').hidden = !message;
    setTimeout(() => $('#tokenInput').focus(), 50);
  }

  function hideAuth() {
    $('#authOverlay').hidden = true;
  }

  /* ---------------- 未保存修改（吸底保存条） ---------------- */

  const DIRTY_FORMS = [
    { id: '#llmForm', label: '大模型' },
    { id: '#filterForm', label: '消息范围' },
    { id: '#identityForm', label: '身份与判定' },
  ];
  const dirtyForms = new Set();
  const cleanWaiters = new Map();
  let savingDirty = false;

  function refreshDirtyUI() {
    const active = DIRTY_FORMS.filter((item) => dirtyForms.has(item.id));
    const dirty = active.length > 0;
    const bar = $('#dirtyBar');
    if (bar) bar.hidden = !dirty;
    document.body.classList.toggle('is-dirty', dirty);
    const sections = $('#dirtySections');
    if (sections) {
      sections.textContent = dirty ? active.map((item) => item.label).join(' · ') : '';
    }
    const count = $('#dirtyCount');
    if (count) count.textContent = String(active.length);
    const saveButton = $('#dirtySaveBtn');
    if (saveButton) saveButton.disabled = savingDirty || state.loading;
  }

  function markDirty(formId) {
    if (!DIRTY_FORMS.some((item) => item.id === formId)) return;
    if (dirtyForms.has(formId)) return;
    dirtyForms.add(formId);
    refreshDirtyUI();
  }

  function markClean(formId) {
    if (!dirtyForms.delete(formId)) return;
    const waiter = cleanWaiters.get(formId);
    if (waiter) waiter();
    refreshDirtyUI();
  }

  function waitForClean(formId, timeoutMs = 8000) {
    return new Promise((resolve) => {
      const timer = setTimeout(() => {
        cleanWaiters.delete(formId);
        resolve(false);
      }, timeoutMs);
      cleanWaiters.set(formId, () => {
        clearTimeout(timer);
        cleanWaiters.delete(formId);
        resolve(true);
      });
    });
  }

  async function saveDirtyForms() {
    if (savingDirty || state.loading) return;
    savingDirty = true;
    refreshDirtyUI();
    try {
      for (const item of DIRTY_FORMS) {
        if (!dirtyForms.has(item.id)) continue;
        const form = $(item.id);
        if (!form) continue;
        const cleaned = waitForClean(item.id);
        form.requestSubmit();
        if (!(await cleaned)) break;
      }
    } finally {
      savingDirty = false;
      refreshDirtyUI();
    }
  }

  function discardDirty() {
    if (savingDirty || !dirtyForms.size) return;
    dirtyForms.clear();
    refreshDirtyUI();
    toast('已放弃未保存的修改');
    loadSettings();
    loadFilters();
    loadIdentity();
  }

  function handleBeforeUnload(event) {
    if (!dirtyForms.size) return;
    event.preventDefault();
    event.returnValue = '';
  }

  function saveToken(token) {
    state.token = token;
    try { localStorage.setItem(TOKEN_KEY, token); } catch (e) { /* ignore */ }
  }

  function consumeUrlToken() {
    try {
      const url = new URL(window.location.href);
      if (!url.searchParams.has('token')) return;
      const token = (url.searchParams.get('token') || '').trim();
      if (token) saveToken(token);
      url.searchParams.delete('token');
      window.history.replaceState(null, '', `${url.pathname}${url.search}${url.hash}`);
    } catch (e) { /* ignore */ }
  }

  async function api(path, options = {}) {
    const headers = Object.assign(
      { 'X-Auth-Token': state.token, 'Accept': 'application/json' },
      options.headers || {},
    );
    const response = await fetch(path, Object.assign({}, options, { headers }));
    if (response.status === 401) {
      showAuth('访问令牌无效，请重新输入');
      throw new Error('unauthorized');
    }
    let body = null;
    try { body = await response.json(); } catch (e) { body = null; }
    if (!response.ok) {
      const detail = body && body.detail ? body.detail : `请求失败（${response.status}）`;
      const error = new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
      error.status = response.status;
      throw error;
    }
    return body;
  }

  function renderSettings(data) {
    const enabled = Boolean(data.enabled);
    const configured = Boolean(data.configured);
    const pill = $('#llmStatus');
    pill.className = 'status-pill ' + (configured ? 'status-pill--ok' : 'status-pill--off');
    pill.textContent = configured ? 'LLM 已启用' : enabled ? '未配置 Key' : '未启用';

    $('#llmKeyHint').textContent = data.key_saved
      ? `已保存：${data.key_hint}`
      : '尚未保存 API Key';
    $('#baseUrl').value = data.base_url || '';
    $('#model').value = data.model || '';
    $('#enabled').checked = enabled;
    const keyInput = $('#apiKey');
    keyInput.value = '';
    keyInput.placeholder = data.key_saved
      ? `已保存 ${data.key_hint}，留空则保持不变`
      : '粘贴 sk-...（例如 DeepSeek / OpenAI / Ollama）';
    markClean('#llmForm');
  }

  async function loadSettings() {
    try {
      const data = await api('/api/settings/llm');
      renderSettings(data);
      hideAuth();
    } catch (error) {
      if (error.message !== 'unauthorized') toast(`读取失败：${error.message}`);
    }
  }

  function setBusy(busy) {
    state.loading = busy;
    ['#saveBtn', '#testBtn', '#clearBtn'].forEach((selector) => {
      const el = $(selector);
      if (el) el.disabled = busy;
    });
  }

  async function saveSettings(event) {
    event.preventDefault();
    if (state.loading) return;
    const payload = {
      base_url: $('#baseUrl').value.trim(),
      model: $('#model').value.trim(),
      enabled: $('#enabled').checked,
    };
    const key = $('#apiKey').value.trim();
    if (key) payload.api_key = key;

    const errorEl = $('#formError');
    errorEl.hidden = true;
    setBusy(true);
    try {
      const data = await api('/api/settings/llm', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      renderSettings(data);
      toast(data.configured ? '已保存，LLM 已启用' : '已保存');
    } catch (error) {
      if (error.message !== 'unauthorized') {
        errorEl.textContent = error.message;
        errorEl.hidden = false;
      }
    } finally {
      setBusy(false);
    }
  }

  async function testConnection() {
    if (state.loading) return;
    const box = $('#testResult');
    box.hidden = false;
    box.className = 'test-result';
    box.textContent = '正在测试连接…';
    setBusy(true);
    try {
      const result = await api('/api/settings/llm/test', { method: 'POST' });
      if (result.ok) {
        box.className = 'test-result ok';
        box.textContent = `连接成功${result.model ? `（${result.model}）` : ''}：${result.reply || 'OK'}`;
      } else {
        box.className = 'test-result err';
        box.textContent = `连接失败：${result.error || '未知错误'}`;
      }
    } catch (error) {
      if (error.message !== 'unauthorized') {
        box.className = 'test-result err';
        box.textContent = `连接失败：${error.message}`;
      }
    } finally {
      setBusy(false);
    }
  }

  async function clearKey() {
    if (state.loading) return;
    if (!window.confirm('确定清除已保存的 API Key 吗？之后会退回规则模式。')) return;
    setBusy(true);
    try {
      const data = await api('/api/settings/llm', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ api_key: '' }),
      });
      renderSettings(data);
      toast('已清除 API Key');
    } catch (error) {
      if (error.message !== 'unauthorized') toast(error.message);
    } finally {
      setBusy(false);
    }
  }

  function toggleKeyVisibility() {
    const input = $('#apiKey');
    const button = $('#toggleKey');
    const reveal = input.type === 'password';
    input.type = reveal ? 'text' : 'password';
    button.textContent = reveal ? '隐藏' : '显示';
  }

  /* ---------------- 消息范围 / 白名单 ---------------- */

  const MODE_LABELS = {
    all: '全部聊天',
    pinned: '仅置顶聊天',
    custom: '仅自定义名单',
    pinned_custom: '置顶 + 自定义',
  };

  function renderFilters(data) {
    const pill = $('#filterStatus');
    pill.className = 'status-pill status-pill--ok';
    pill.textContent = MODE_LABELS[data.whitelist_mode] || '自定义';
    $('#includePrivate').checked = Boolean(data.include_private);
    $('#whitelistMode').value = data.whitelist_mode || 'all';
    $('#whitelist').value = (data.group_whitelist || []).join('\n');
    $('#pinnedInfo').textContent = data.pinned_ready
      ? `已自动同步 ${data.pinned_count} 个置顶会话（每 3 分钟刷新）`
      : '置顶会话尚未同步，稍后会自动重试；期间为安全起见先不拦截消息';
    markClean('#filterForm');
  }

  async function loadFilters() {
    try {
      renderFilters(await api('/api/settings/filters'));
    } catch (error) {
      if (error.message !== 'unauthorized') toast(`读取消息范围失败：${error.message}`);
    }
  }

  async function saveFilters(event) {
    event.preventDefault();
    if (state.loading) return;
    const errorEl = $('#filterError');
    errorEl.hidden = true;
    const raw = $('#whitelist').value || '';
    const list = raw
      .split(/[\n,，]/)
      .map((item) => item.trim())
      .filter(Boolean);
    setBusy(true);
    try {
      const data = await api('/api/settings/filters', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          include_private: $('#includePrivate').checked,
          whitelist_mode: $('#whitelistMode').value,
          group_whitelist: list,
        }),
      });
      renderFilters(data);
      toast('消息范围已保存');
    } catch (error) {
      if (error.message !== 'unauthorized') {
        errorEl.textContent = error.message;
        errorEl.hidden = false;
      }
    } finally {
      setBusy(false);
    }
  }

  /* ---------------- 身份与判定 ---------------- */

  const ACCOUNT_SOURCE_LABELS = {
    auto: '自动识别',
    manual: '手动设置',
    none: '未识别',
  };

  const VERDICT_META = {
    me: { label: '需要我参与', className: 'identity-badge--me' },
    all: { label: '@所有人', className: 'identity-badge--all' },
    other: { label: '指向他人·丢弃', className: 'identity-badge--other' },
    none: { label: '未指定', className: 'identity-badge--none' },
  };

  const VERDICT_SOURCE_LABELS = {
    at: '微信 @ 名单',
    text_at: '正文 @ 昵称',
    vocative: '开头直呼',
    assign: '指派句式',
    quote: '回复我',
    none: '无',
  };

  let identityBusy = false;

  function identityEl(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function formatIdentityTime(value) {
    if (!value) return '—';
    const text = String(value).replace('T', ' ').replace(/\.\d+$/, '');
    return text.slice(0, 19);
  }

  function formatIdentityShortTime(value) {
    if (!value) return '—';
    const text = formatIdentityTime(value);
    return text === '—' ? text : text.slice(5, 16);
  }

  function formatIdentityClock(value) {
    if (!value) return '—';
    const text = formatIdentityTime(value);
    return text.length >= 19 ? text.slice(11, 19) : text;
  }

  function identityEllipsisCell(value) {
    const text = value ? String(value) : '';
    const cell = document.createElement('td');
    const span = identityEl('span', 'identity-cell-ellipsis', text || '—');
    if (text) span.title = text;
    cell.append(span);
    return cell;
  }

  function setIdentityBusy(busy) {
    identityBusy = busy;
    ['#identitySaveBtn', '#identityRefreshBtn', '#identityCheckBtn'].forEach((selector) => {
      const button = $(selector);
      if (button) button.disabled = busy;
    });
    const dirtySaveButton = $('#dirtySaveBtn');
    if (dirtySaveButton) dirtySaveButton.disabled = busy || savingDirty || state.loading;
    if (!busy) refreshDirtyUI();
  }

  function renderIdentityGroups(groups) {
    const table = $('#identityGroupsTable');
    const body = $('#identityGroupsBody');
    const empty = $('#identityGroupsEmpty');
    const hint = $('#identityGroupsHint');
    if (!table || !body) return;
    body.replaceChildren();
    if (!groups.length) {
      table.hidden = true;
      if (empty) empty.hidden = false;
      if (hint) hint.textContent = '';
      return;
    }
    table.hidden = false;
    if (empty) empty.hidden = true;
    if (hint) hint.textContent = `${groups.length} 个群`;

    const frag = document.createDocumentFragment();
    groups.forEach((group) => {
      const info = group && typeof group === 'object' ? group : {};
      const sessionId = info.session_id ? String(info.session_id) : '';
      const groupName = info.group_name ? String(info.group_name) : (sessionId || '未知群');
      const nickname = info.my_nickname ? String(info.my_nickname) : '';
      const memberCount = Number(info.members);
      const members = Number.isFinite(memberCount) ? `${memberCount} 人` : '—';
      const fetched = formatIdentityShortTime(info.fetched_at);
      const failed = Boolean(info.error);
      const stale = !failed && Boolean(info.stale);
      const statusText = failed ? '失败' : stale ? '已过期' : '正常';
      const statusClass = failed
        ? 'identity-badge--err'
        : stale ? 'identity-badge--muted' : 'identity-badge--ok';

      const row = document.createElement('tr');
      row.append(identityEllipsisCell(groupName));
      row.append(identityEllipsisCell(nickname));
      row.append(identityEl('td', 'identity-cell-mono', members));
      row.append(identityEl('td', 'identity-cell-mono', fetched));

      const statusCell = document.createElement('td');
      const badge = identityEl('span', `identity-badge ${statusClass}`, statusText);
      if (failed && info.error) badge.title = String(info.error);
      statusCell.append(badge);
      row.append(statusCell);

      frag.append(row);
    });
    body.append(frag);
  }

  function renderIdentityCheckGroups(groups) {
    const select = $('#identityCheckGroup');
    if (!select) return;
    const previous = select.value;
    select.replaceChildren();
    select.append(new Option('不指定群（只用全局别名）', ''));
    groups.forEach((group) => {
      const sessionId = group && group.session_id ? String(group.session_id) : '';
      if (!sessionId) return;
      const name = group.group_name ? String(group.group_name) : sessionId;
      select.append(new Option(name, sessionId));
    });
    if (previous && Array.from(select.options).some((option) => option.value === previous)) {
      select.value = previous;
    }
  }

  function renderIdentityDrops(data) {
    const drops = Array.isArray(data.last_drops) ? data.last_drops : [];
    const judgments = data.judgments && typeof data.judgments === 'object' ? data.judgments : {};
    const count = (key) => {
      const value = Number(judgments[key]);
      return Number.isFinite(value) ? value : 0;
    };
    const summary = [
      `需要我参与 ${count('kept_me')}`,
      `@所有人 ${count('kept_all')}`,
      `未指定 ${count('kept_none')}`,
      `指向他人·丢弃 ${count('dropped_other')}`,
    ];
    const since = formatIdentityTime(judgments.since);
    const summaryEl = $('#identityJudgments');
    if (summaryEl) {
      summaryEl.textContent = `本次启动以来：${summary.join(' · ')}` +
        (since === '—' ? '' : `（自 ${since}）`);
    }

    const countEl = $('#identityDropsCount');
    if (countEl) countEl.textContent = drops.length ? `最近 ${drops.length} 条` : '';

    const table = $('#identityDropsTable');
    const body = $('#identityDropsBody');
    const empty = $('#identityDropsEmpty');
    if (!table || !body) return;
    body.replaceChildren();
    if (!drops.length) {
      table.hidden = true;
      if (empty) empty.hidden = false;
      return;
    }
    table.hidden = false;
    if (empty) empty.hidden = true;

    const frag = document.createDocumentFragment();
    drops.forEach((drop) => {
      const info = drop && typeof drop === 'object' ? drop : {};
      const fullTime = formatIdentityTime(info.at);
      const targets = Array.isArray(info.targets)
        ? info.targets.map((item) => String(item)).filter(Boolean).join('、')
        : '';

      const row = document.createElement('tr');
      const timeCell = identityEl('td', 'identity-cell-mono', formatIdentityClock(info.at));
      if (fullTime !== '—') timeCell.title = fullTime;
      row.append(timeCell);
      row.append(identityEllipsisCell(info.group));
      row.append(identityEllipsisCell(info.sender));
      row.append(identityEllipsisCell(targets));
      row.append(identityEllipsisCell(info.content));
      frag.append(row);
    });
    body.append(frag);
  }

  function renderIdentityCheck(data) {
    const verdict = data && data.verdict && typeof data.verdict === 'object' ? data.verdict : {};
    const identity = data && data.identity && typeof data.identity === 'object' ? data.identity : {};
    const meta = VERDICT_META[verdict.kind] || { label: '未知结果', className: 'identity-badge--none' };

    const badge = $('#identityVerdictBadge');
    if (badge) {
      badge.className = `identity-badge ${meta.className}`;
      badge.textContent = meta.label;
    }
    const sourceEl = $('#identityVerdictSource');
    if (sourceEl) {
      const sourceLabel = VERDICT_SOURCE_LABELS[verdict.source]
        || (verdict.source ? String(verdict.source) : '');
      sourceEl.textContent = sourceLabel ? `识别方式：${sourceLabel}` : '';
    }
    const reasonEl = $('#identityVerdictReason');
    if (reasonEl) reasonEl.textContent = verdict.reason ? String(verdict.reason) : '—';
    const targetsEl = $('#identityVerdictTargets');
    if (targetsEl) {
      const targets = Array.isArray(verdict.targets)
        ? verdict.targets.map((item) => String(item)).filter(Boolean)
        : [];
      targetsEl.textContent = targets.length ? targets.join('、') : '—';
    }
    const nicknameEl = $('#identityVerdictNickname');
    if (nicknameEl) {
      nicknameEl.textContent = identity.group_nickname ? String(identity.group_nickname) : '—';
    }
    const membersEl = $('#identityVerdictMembers');
    if (membersEl) {
      const members = Number(identity.members);
      membersEl.textContent = Number.isFinite(members) ? `${members} 人` : '—';
    }
    const result = $('#identityCheckResult');
    if (result) result.hidden = false;
  }

  function renderIdentity(data, options = {}) {
    const keepForm = Boolean(options.keepForm);
    const filterEnabled = Boolean(data && data.filter_enabled);

    const pill = $('#identityStatus');
    if (pill) {
      pill.className = 'status-pill ' + (filterEnabled ? 'status-pill--ok' : 'status-pill--off');
      pill.textContent = filterEnabled ? '过滤已启用' : '过滤已关闭';
    }

    const errorBox = $('#identityError');
    if (errorBox) errorBox.hidden = true;
    const bodyBox = $('#identityBody');
    if (bodyBox) bodyBox.hidden = false;

    const account = data && typeof data.account === 'string' ? data.account.trim() : '';
    const accountEl = $('#identityAccount');
    if (accountEl) {
      accountEl.textContent = account || '未识别';
      accountEl.title = account;
    }
    const sourceEl = $('#identitySource');
    if (sourceEl) {
      sourceEl.textContent = ACCOUNT_SOURCE_LABELS[data && data.account_source] || '未识别';
    }

    const autoNames = data && Array.isArray(data.auto_names)
      ? data.auto_names.map((item) => String(item).trim()).filter(Boolean)
      : [];
    const chips = $('#identityAutoNames');
    if (chips) {
      chips.replaceChildren();
      if (!autoNames.length) {
        chips.append(identityEl('span', 'identity-empty', '暂无（收到消息后自动识别）'));
      } else {
        autoNames.forEach((name) => chips.append(identityEl('span', 'identity-chip', name)));
      }
    }

    if (!keepForm) {
      const manualNames = data && Array.isArray(data.manual_names)
        ? data.manual_names.map((item) => String(item).trim()).filter(Boolean)
        : [];
      const namesInput = $('#identityNames');
      if (namesInput) namesInput.value = manualNames.join('\n');
      const wxidInput = $('#identityWxid');
      if (wxidInput) {
        wxidInput.value = data && data.account_source === 'manual' ? account : '';
        wxidInput.placeholder = '留空表示自动识别（推荐）';
      }
      const filterInput = $('#identityFilter');
      if (filterInput) filterInput.checked = filterEnabled;
      const fuzzyInput = $('#identityFuzzy');
      if (fuzzyInput) fuzzyInput.checked = Boolean(data && data.fuzzy_enabled);
      markClean('#identityForm');
    }

    const groups = data && Array.isArray(data.groups) ? data.groups : [];
    renderIdentityGroups(groups);
    renderIdentityCheckGroups(groups);
    renderIdentityDrops(data || {});
  }

  function renderIdentityUnavailable(error) {
    const notFound = Boolean(error && error.status === 404);
    const pill = $('#identityStatus');
    if (pill) {
      pill.className = 'status-pill ' + (notFound ? 'status-pill--muted' : 'status-pill--off');
      pill.textContent = notFound ? '未就绪' : '读取失败';
    }
    const body = $('#identityBody');
    if (body) body.hidden = true;
    const text = $('#identityErrorText');
    if (text) {
      text.textContent = notFound
        ? '当前服务还没有身份判定接口（404）：更新代码并重启服务后，点「重试」即可加载。'
        : `读取身份设置失败：${(error && error.message) || '未知错误'}。点「重试」重新加载。`;
    }
    const box = $('#identityError');
    if (box) box.hidden = false;
  }

  async function loadIdentity(options = {}) {
    try {
      renderIdentity(await api('/api/settings/identity'), options);
    } catch (error) {
      if (error.message === 'unauthorized') return;
      renderIdentityUnavailable(error);
    }
  }

  async function saveIdentity(event) {
    event.preventDefault();
    if (identityBusy) return;
    const errorEl = $('#identityFormError');
    errorEl.hidden = true;

    const raw = $('#identityNames').value || '';
    const names = Array.from(new Set(
      raw
        .split(/[\n,，]/)
        .map((item) => item.trim())
        .filter(Boolean),
    ));
    const payload = {
      me_names: names,
      me_wxid: $('#identityWxid').value.trim(),
      identity_filter: $('#identityFilter').checked,
      identity_fuzzy: $('#identityFuzzy').checked,
    };

    setIdentityBusy(true);
    try {
      const data = await api('/api/settings/identity', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      renderIdentity(data);
      toast('身份与判定设置已保存');
    } catch (error) {
      if (error.message !== 'unauthorized') {
        errorEl.textContent = error.message;
        errorEl.hidden = false;
      }
    } finally {
      setIdentityBusy(false);
    }
  }

  async function refreshIdentity() {
    if (identityBusy) return;
    const button = $('#identityRefreshBtn');
    const original = button.textContent;
    button.textContent = '刷新中…';
    setIdentityBusy(true);
    try {
      await api('/api/identity/refresh', { method: 'POST' });
      toast('身份信息已刷新');
      await loadIdentity({ keepForm: true });
    } catch (error) {
      if (error.message !== 'unauthorized') {
        toast(error.status === 404
          ? '刷新接口不存在（404）：当前服务还是旧版本，更新并重启后再试。'
          : `刷新失败：${error.message}`);
      }
    } finally {
      button.textContent = original;
      setIdentityBusy(false);
    }
  }

  async function runIdentityCheck() {
    if (identityBusy) return;
    const errorEl = $('#identityCheckError');
    const result = $('#identityCheckResult');
    errorEl.hidden = true;
    result.hidden = true;

    const text = $('#identityCheckText').value.trim();
    if (!text) {
      errorEl.textContent = '请先输入要测试的消息。';
      errorEl.hidden = false;
      return;
    }

    const payload = { text };
    const sessionId = $('#identityCheckGroup').value;
    if (sessionId) payload.session_id = sessionId;

    setIdentityBusy(true);
    try {
      renderIdentityCheck(await api('/api/identity/check', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      }));
    } catch (error) {
      if (error.message !== 'unauthorized') {
        errorEl.textContent = error.status === 404
          ? '判定接口不存在（404）：当前服务还是旧版本，更新并重启后再试。'
          : `判定失败：${error.message}`;
        errorEl.hidden = false;
      }
    } finally {
      setIdentityBusy(false);
    }
  }

  /* ---------------- 运行状态（GET /api/health） ---------------- */

  const WHITELIST_MODE_LABELS = {
    all: '全部',
    pinned: '仅置顶',
    custom: '仅自定义',
    pinned_custom: '置顶+自定义',
  };

  const CATCHUP_MODE_LABELS = {
    watermark: '按会话水位',
    off: '已关闭',
  };

  /* 采集降级原因码 → 中文提示（未知码原样展示，避免吞信息） */
  const DEGRADED_REASON_LABELS = {
    llm_fallback: 'LLM 回退规则模式',
    extraction_cap: '抽取命中上限',
    partial_failure: '部分会话失败',
    sustained_failure: '连续多轮失败',
    sessions_unavailable: '会话列表不可用',
    collect_error: '采集轮异常',
  };

  function degradedReasons(value) {
    if (!value || typeof value !== 'object' || !Array.isArray(value.degraded_reasons)) return [];
    return value.degraded_reasons
      .map((code) => (code ? (DEGRADED_REASON_LABELS[code] || String(code)) : ''))
      .filter((text) => text !== '');
  }

  let statusBusy = false;
  const STATUS_BUTTONS = ['#pinnedRefreshBtn', '#statusRefreshBtn', '#retryFailedBtn'];

  function statusNumber(value) {
    const number = Number(value);
    return value !== null && value !== undefined && value !== '' && Number.isFinite(number)
      ? String(number)
      : '—';
  }

  function statusCountText(value, unit) {
    const text = statusNumber(value);
    return text === '—' ? text : `${text} ${unit}`;
  }

  function truncateStatusText(value, max = 80) {
    if (typeof value !== 'string' && typeof value !== 'number') return '';
    const text = String(value).replace(/\s+/g, ' ').trim();
    if (!text) return '';
    return text.length > max ? `${text.slice(0, max).trimEnd()}…` : text;
  }

  function statusTime(value) {
    if (!value) return '—';
    const text = String(value).replace('T', ' ').replace(/\.\d+$/, '');
    return text.slice(0, 19);
  }

  function statusClock(value) {
    const text = statusTime(value);
    return text === '—' ? text : (text.length >= 19 ? text.slice(11, 19) : text);
  }

  function statusMinutes(value) {
    if (value === null || value === undefined || value === '') return '—';
    const number = Number(value);
    if (!Number.isFinite(number)) return '—';
    if (number < 60) return `${number} 分钟`;
    if (number % 60 === 0) return `${number / 60} 小时`;
    return `${Math.floor(number / 60)} 小时 ${number % 60} 分钟`;
  }

  function statusSet(selector, text) {
    const el = $(selector);
    if (el) el.textContent = text;
  }

  function setStatusBusy(busy, activeButton, busyText) {
    statusBusy = busy;
    STATUS_BUTTONS.forEach((selector) => {
      const button = $(selector);
      if (button) button.disabled = busy;
    });
    if (!activeButton) return;
    if (busy) {
      if (!busyText) return;
      if (!activeButton.dataset.idleLabel) {
        activeButton.dataset.idleLabel = activeButton.textContent;
      }
      activeButton.textContent = busyText;
      activeButton.setAttribute('aria-busy', 'true');
      return;
    }
    if (activeButton.dataset.idleLabel) {
      activeButton.textContent = activeButton.dataset.idleLabel;
      delete activeButton.dataset.idleLabel;
    }
    activeButton.removeAttribute('aria-busy');
  }

  function renderStatus(data) {
    const health = data && typeof data === 'object' ? data : {};
    const source = health.source && typeof health.source === 'object' ? health.source : {};
    const collection = health.collection && typeof health.collection === 'object'
      && !Array.isArray(health.collection)
      ? health.collection
      : (health.catchup && typeof health.catchup === 'object' && !Array.isArray(health.catchup)
        ? health.catchup : {});
    const whitelist = health.whitelist && typeof health.whitelist === 'object' ? health.whitelist : {};
    const events = health.events && typeof health.events === 'object' ? health.events : {};
    const llm = health.llm && typeof health.llm === 'object' ? health.llm : {};
    /* 分片/启用信息按契约在 collection.llm（顶层 llm 只有 configured 的旧结构也能兼容） */
    const collectionLlm = collection.llm && typeof collection.llm === 'object'
      && !Array.isArray(collection.llm) ? collection.llm : {};
    const tasks = health.tasks && typeof health.tasks === 'object' ? health.tasks : {};

    const connected = source.connected;
    const pill = $('#statusPill');
    if (pill) {
      if (connected === true) {
        pill.className = 'status-pill status-pill--ok';
        pill.textContent = '正常';
      } else if (connected === false) {
        pill.className = 'status-pill status-pill--off';
        pill.textContent = '未连接';
      } else {
        pill.className = 'status-pill status-pill--muted';
        pill.textContent = '等待首轮采集';
      }
    }
    const errorBox = $('#statusError');
    if (errorBox) errorBox.hidden = true;
    const body = $('#statusBody');
    if (body) body.hidden = false;

    statusSet('#statusSourceType', health.data_source || source.type || '—');
    statusSet('#statusSourceConnected', connected === true
      ? '已连接'
      : connected === false ? '未连接' : '等待中');
    statusSet('#statusLastCollect', statusTime(source.last_collect_at));
    statusSet('#statusLastEvent', statusTime(source.last_event_at));

    const sourceErrorEl = $('#statusSourceError');
    if (sourceErrorEl) {
      const lastError = source.last_error ? String(source.last_error) : '';
      sourceErrorEl.hidden = !lastError;
      sourceErrorEl.textContent = lastError ? `最近错误：${lastError}` : '';
    }

    const collectionEnabled = collection.enabled === true && collection.mode !== 'off';
    statusSet('#statusCatchupHint', collectionEnabled ? '全自动' : '仅启动时');
    statusSet('#statusCatchupMode', CATCHUP_MODE_LABELS[collection.mode]
      || (collection.mode ? String(collection.mode) : '—'));
    const collectionSeconds = Number(collection.seconds);
    statusSet('#statusCatchupInterval', !Number.isFinite(collectionSeconds) || collectionSeconds <= 0
      ? '仅启动时'
      : formatInterval(collectionSeconds));
    statusSet('#statusCatchupLookback', statusMinutes(collection.lookback_minutes));
    statusSet('#statusCatchupMax', collection.max_hours === null || collection.max_hours === undefined
      ? '—'
      : statusMinutes(Number(collection.max_hours) * 60));
    statusSet('#statusCatchupEmpty',
      collection.empty_history_hours === null || collection.empty_history_hours === undefined
        ? '—'
        : statusMinutes(Number(collection.empty_history_hours) * 60));
    statusSet('#statusCatchupPageSize', collection.page_size === null || collection.page_size === undefined
      ? '—'
      : `${statusNumber(collection.page_size)} 条/页`);
    const collectionBudget = Number(collection.round_budget);
    statusSet('#statusCatchupBudget', !Number.isFinite(collectionBudget) || collectionBudget <= 0
      ? '不限制'
      : `${statusNumber(collectionBudget)} 条/轮`);
    const collectionBackfill = Number(collection.backfill_minutes);
    statusSet('#statusCatchupBackfill', Number.isFinite(collectionBackfill) && collectionBackfill > 0
      ? statusMinutes(collectionBackfill)
      : '不额外回补');
    const deepScanMinutes = Number(collection.deep_scan_minutes);
    const deepScanLookback = Number(collection.deep_scan_lookback_minutes);
    statusSet('#statusDeepScan', !Number.isFinite(deepScanMinutes) || deepScanMinutes <= 0
      ? '关闭'
      : `每 ${statusMinutes(deepScanMinutes)} · 回看 ${statusMinutes(deepScanLookback)}`);
    statusSet('#statusCatchupWatermarks',
      collection.watermarks === null || collection.watermarks === undefined
        ? '—'
        : `${statusNumber(collection.watermarks)} 个`);
    const cursorSessions = collection.cursor_sessions;
    statusSet('#statusCursorSessions',
      cursorSessions === null || cursorSessions === undefined
        ? '—'
        : `${statusNumber(cursorSessions)} 个`);
    statusSet('#statusCollectPendingMessages', statusNumber(collection.pending_messages));
    statusSet('#statusCollectPendingSessions', statusNumber(collection.pending_sessions));
    statusSet('#statusMaxAttempts', collection.max_attempts === null
      || collection.max_attempts === undefined
      ? '—'
      : `${statusNumber(collection.max_attempts)} 次`);
    const failedMessages = collection.failed_messages;
    statusSet('#statusFailedMessages', failedMessages === null || failedMessages === undefined
      ? '—'
      : `${statusNumber(failedMessages)} 条`);
    statusSet('#statusFailedSessions', collection.failed_sessions === null
      || collection.failed_sessions === undefined
      ? '—'
      : `${statusNumber(collection.failed_sessions)} 个`);

    const last = collection.last && typeof collection.last === 'object'
      && !Array.isArray(collection.last)
      ? collection.last
      : null;
    /* 失败会话数：后端 collection.last 里 sessions_failed 是会话 id 数组，
       数量在 sessions_failed_count（旧结构直接给数字时也兼容） */
    const lastSessionsFailed = last
      ? (last.sessions_failed_count !== undefined && last.sessions_failed_count !== null
        ? last.sessions_failed_count
        : (Array.isArray(last.sessions_failed)
          ? last.sessions_failed.length
          : last.sessions_failed))
      : undefined;
    /* 上一轮指标：优先读 collection 顶层，缺失时回退 collection.last（字段契约未就绪也不报错） */
    const statOf = (name) => {
      const value = collection[name];
      if (value !== undefined && value !== null) return value;
      return last ? last[name] : undefined;
    };

    const collectionReasons = degradedReasons(collection);
    const lastReasons = degradedReasons(last);
    const degradedReasonList = collectionReasons.length ? collectionReasons : lastReasons;
    const degraded = collection.degraded === true;
    statusSet('#statusDegraded', degraded
      ? `降级 · ${degradedReasonList.length ? degradedReasonList.join('、') : '原因未提供'}`
      : collection.degraded === false ? '正常' : '—');

    const llmFallbackBits = [];
    if (statusNumber(statOf('llm_batches')) !== '—') {
      llmFallbackBits.push(`本轮 ${statusNumber(statOf('llm_batches'))} 批`);
    }
    if (statusNumber(statOf('llm_fallbacks')) !== '—') {
      llmFallbackBits.push(`回退 ${statusNumber(statOf('llm_fallbacks'))} 次`);
    }
    if (statusNumber(collection.llm_fallback_total) !== '—') {
      llmFallbackBits.push(`累计 ${statusNumber(collection.llm_fallback_total)} 次`);
    }
    const fallbackStreak = Number(collection.llm_fallback_streak);
    if (Number.isFinite(fallbackStreak) && fallbackStreak > 0) {
      llmFallbackBits.push(`连续 ${statusNumber(fallbackStreak)} 轮`);
    }
    statusSet('#statusLlmFallback', llmFallbackBits.length ? llmFallbackBits.join(' · ') : '—');

    statusSet('#statusCapHits', statusCountText(statOf('extraction_cap_hits'), '次'));
    statusSet('#statusMessagesWithoutTask', statusCountText(statOf('messages_without_task'), '条'));

    const deepScanState = collection.deep_scan && typeof collection.deep_scan === 'object'
      && !Array.isArray(collection.deep_scan)
      ? collection.deep_scan
      : null;
    const deepScanLast = deepScanState && deepScanState.last && typeof deepScanState.last === 'object'
      && !Array.isArray(deepScanState.last)
      ? deepScanState.last
      : null;
    let deepScanLastText = '—';
    if (deepScanState) {
      if (deepScanState.disabled === true) {
        deepScanLastText = '已关闭';
      } else if (!deepScanLast) {
        deepScanLastText = deepScanState.last_at ? `上次 ${statusTime(deepScanState.last_at)}` : '尚未执行';
      } else {
        const deepScanError = truncateStatusText(deepScanLast.error, 60);
        if (deepScanError) {
          /* 深扫失败：last.error 有值、没有 last_at */
          deepScanLastText = `失败 · ${deepScanError}`;
        } else {
          const atText = statusTime(deepScanLast.at) !== '—'
            ? statusTime(deepScanLast.at)
            : statusTime(deepScanState.last_at);
          const deepScanBits = [atText !== '—' ? atText : '时间未知'];
          if (statusNumber(deepScanLast.inserted) !== '—') {
            deepScanBits.push(`补回 ${statusNumber(deepScanLast.inserted)} 条`);
          }
          if (statusNumber(deepScanLast.processed) !== '—') {
            deepScanBits.push(`处理 ${statusNumber(deepScanLast.processed)} 条`);
          }
          deepScanLastText = deepScanBits.join(' · ');
        }
      }
    }
    statusSet('#statusDeepScanLast', deepScanLastText);

    const readBits = [];
    if (statusNumber(statOf('read_rows')) !== '—') {
      readBits.push(`读取 ${statusNumber(statOf('read_rows'))} 行`);
    }
    if (statusNumber(statOf('duplicate_rows')) !== '—') {
      readBits.push(`重复 ${statusNumber(statOf('duplicate_rows'))} 行`);
    }
    const readRatio = Number(statOf('read_ratio'));
    if (Number.isFinite(readRatio) && readRatio > 0) {
      readBits.push(`放大 ${readRatio.toFixed(1)}×`);
    }
    statusSet('#statusReadAmplification', readBits.length ? readBits.join(' / ') : '—');

    const collectionLastEl = $('#statusCatchupLast');
    if (collectionLastEl) {
      if (!last) {
        collectionLastEl.textContent = '上次采集：尚未执行';
      } else if (last.ok === false) {
        const failureBits = [String(last.reason || last.error || '未知原因')];
        if (last.partial === true) failureBits.push('仅部分会话失败');
        if (statusNumber(lastSessionsFailed) !== '—' && Number(lastSessionsFailed) > 0) {
          failureBits.push(`失败会话 ${statusNumber(lastSessionsFailed)} 个`);
        }
        const lastLlmError = truncateStatusText(last.last_llm_error, 80);
        if (lastLlmError) failureBits.push(`LLM 错误：${lastLlmError}`);
        collectionLastEl.textContent = `上次采集失败：${failureBits.join(' · ')}`;
      } else {
        const modeLabel = last.deep_scan
          ? '晚到消息深扫'
          : last.mode === 'watermark' ? '按 localId 游标'
            : statusMinutes(last.minutes) !== '—' ? `回看 ${statusMinutes(last.minutes)}` : '采集';
        const parts = [
          modeLabel,
          `会话 ${statusNumber(last.sessions)}`,
          `扫描 ${statusNumber(last.scanned)}`,
          `新增消息 ${statusNumber(last.inserted)}`,
          `处理 ${statusNumber(last.processed)}`,
          `新建待办 ${statusNumber(last.tasks_created)}`,
          `更新待办 ${statusNumber(last.tasks_updated)}`,
        ];
        if (last.pages !== null && last.pages !== undefined) {
          parts.push(`分页 ${statusNumber(last.pages)}`);
        }
        if (last.budget_exhausted) parts.push('本轮预算用完，余量下轮继续');
        if (last.dead_lettered) parts.push(`失败入队 ${statusNumber(last.dead_lettered)} 条`);
        if (last.failed_queue) parts.push(`失败队列 ${statusNumber(last.failed_queue)} 条`);
        if (last.partial === true) parts.push('部分会话失败');
        if (statusNumber(lastSessionsFailed) !== '—' && Number(lastSessionsFailed) > 0) {
          parts.push(`失败会话 ${statusNumber(lastSessionsFailed)} 个`);
        }
        if (statusNumber(last.llm_fallbacks) !== '—' && Number(last.llm_fallbacks) > 0) {
          parts.push(`LLM 回退 ${statusNumber(last.llm_fallbacks)} 次`);
        }
        if (statusNumber(last.extraction_cap_hits) !== '—' && Number(last.extraction_cap_hits) > 0) {
          parts.push(`抽取上限 ${statusNumber(last.extraction_cap_hits)} 次`);
        }
        if (statusNumber(last.messages_without_task) !== '—' && Number(last.messages_without_task) > 0) {
          parts.push(`未生成待办 ${statusNumber(last.messages_without_task)} 条`);
        }
        const skipped = Array.isArray(last.skipped) ? last.skipped.length : 0;
        if (skipped > 0) parts.push(`跳过 ${skipped} 个会话`);
        const roundDegraded = last.degraded === true || (last.degraded === undefined && degraded);
        let prefix = '上次采集：成功';
        if (roundDegraded) {
          const reasonText = lastReasons.length
            ? lastReasons.join('、')
            : (last.last_llm_error
              ? truncateStatusText(last.last_llm_error, 60)
              : '原因未提供');
          prefix = `上次采集：降级（${reasonText}）`;
        }
        collectionLastEl.textContent = `${prefix} · ${parts.join(' · ')} · ${statusTime(last.at)}`;
      }
    }

    statusSet('#statusWhitelistMode',
      WHITELIST_MODE_LABELS[whitelist.mode] || (whitelist.mode ? String(whitelist.mode) : '—'));
    statusSet('#statusPinnedCount', whitelist.pinned_count === null || whitelist.pinned_count === undefined
      ? '—'
      : `${statusNumber(whitelist.pinned_count)} 个`);
    statusSet('#statusPinnedReady', whitelist.pinned_ready === true
      ? '已就绪'
      : whitelist.pinned_ready === false ? '未就绪' : '—');
    statusSet('#statusPinnedHint', Number(whitelist.custom_count) > 0
      ? `自定义 ${statusNumber(whitelist.custom_count)} 个`
      : '');

    statusSet('#statusEventsOpen', statusNumber(events.open));
    statusSet('#statusBufferedSessions', statusNumber(events.pending_sessions));
    statusSet('#statusBufferedMessages', statusNumber(events.pending_messages));

    /* 顶层 llm.configured 旧字段表示“是否启用”；collection.llm.configured 才表示“已配置 Key”，优先后者 */
    const llmConfiguredValue = collectionLlm.configured !== undefined
      ? collectionLlm.configured
      : (llm.configured !== undefined ? llm.configured : collectionLlm.enabled);
    const llmConfiguredText = llmConfiguredValue === true
      ? '已配置'
      : llmConfiguredValue === false ? '未配置' : '—';
    const llmBatchValue = llm.batch_messages !== undefined
      ? llm.batch_messages
      : (collectionLlm.batch_messages !== undefined
        ? collectionLlm.batch_messages
        : collection.llm_batch_messages);
    if (statusNumber(llmBatchValue) !== '—') {
      const batchMessages = Number(llmBatchValue);
      statusSet('#statusLlm', `${llmConfiguredText} · ${batchMessages > 0
        ? `${statusNumber(batchMessages)} 条/批`
        : '不分批'}`);
    } else {
      statusSet('#statusLlm', llmConfiguredText);
    }
    statusSet('#statusTasksPending', tasks.pending === null || tasks.pending === undefined
      ? '—'
      : `${statusNumber(tasks.pending)} 条`);
    statusSet('#statusVersion', health.version ? `v${health.version}` : '');
  }

  function renderStatusUnavailable(error) {
    const pill = $('#statusPill');
    if (pill) {
      pill.className = 'status-pill status-pill--muted';
      pill.textContent = '读取失败';
    }
    const body = $('#statusBody');
    if (body) body.hidden = true;
    const text = $('#statusErrorText');
    if (text) {
      text.textContent = `读取运行状态失败：${(error && error.message) || '未知错误'}。点「重试」重新读取。`;
    }
    const box = $('#statusError');
    if (box) box.hidden = false;
  }

  async function loadStatus() {
    try {
      renderStatus(await api('/api/health'));
      return true;
    } catch (error) {
      if (error.message === 'unauthorized') return false;
      renderStatusUnavailable(error);
      return false;
    }
  }

  async function refreshPinned() {
    if (statusBusy) return;
    const box = $('#pinnedResult');
    const button = $('#pinnedRefreshBtn');
    if (box) {
      box.hidden = false;
      box.className = 'test-result';
      box.textContent = '正在同步置顶…';
    }
    setStatusBusy(true, button, '同步中…');
    try {
      const result = await api('/api/pinned/refresh', { method: 'POST' });
      if (result && result.ok !== false) {
        const count = statusNumber(result.pinned_count);
        const added = Array.isArray(result.added)
          ? result.added.length
          : statusNumber(result.added);
        const detail = `置顶 ${count === '—' ? '0' : count} 个，新增 ${added === '—' ? '0' : added} 个`;
        if (box) {
          box.className = 'test-result ok';
          box.textContent = `同步完成：${detail}`;
        }
        toast(detail);
      } else {
        const reason = (result && (result.reason || result.error)) || '未知原因';
        if (box) {
          box.className = 'test-result err';
          box.textContent = `同步失败：${reason}`;
        }
        toast('同步置顶失败');
      }
      await loadStatus();
    } catch (error) {
      if (error.message !== 'unauthorized') {
        if (box) {
          box.className = 'test-result err';
          box.textContent = error.status === 404
            ? '置顶同步接口不存在（404）：当前服务还是旧版本，更新并重启后再试。'
            : `同步失败：${error.message}`;
        }
        toast('同步置顶失败');
      } else if (box) {
        box.hidden = true;
      }
    } finally {
      setStatusBusy(false, button);
    }
  }

  async function refreshStatus() {
    if (statusBusy) return;
    const button = $('#statusRefreshBtn');
    setStatusBusy(true, button, '刷新中…');
    try {
      const loaded = await loadStatus();
      toast(loaded ? '运行状态已刷新' : '读取运行状态失败');
    } finally {
      setStatusBusy(false, button);
    }
  }

  async function retryFailedQueue() {
    if (statusBusy) return;
    const box = $('#retryFailedResult');
    const button = $('#retryFailedBtn');
    if (box) {
      box.hidden = false;
      box.className = 'test-result';
      box.textContent = '正在把失败队列放回待处理…';
    }
    setStatusBusy(true, button, '重试中…');
    try {
      const result = await api('/api/collect/failed/retry', { method: 'POST' });
      const requeued = statusNumber(result && result.requeued);
      const remaining = statusNumber(result && result.remaining);
      const detail = `已放回 ${requeued} 条，失败队列剩余 ${remaining} 条`;
      if (box) {
        box.className = 'test-result ok';
        box.textContent = `重试已安排：${detail}（下一轮采集会自动处理）`;
      }
      toast(`失败队列：${detail}`);
      await loadStatus();
    } catch (error) {
      if (error.message !== 'unauthorized') {
        if (box) {
          box.className = 'test-result err';
          box.textContent = error.status === 404
            ? '失败队列接口不存在（404）：当前服务还是旧版本，更新并重启后再试。'
            : `重试失败队列失败：${error.message}`;
        }
        toast('重试失败队列失败');
      } else if (box) {
        box.hidden = true;
      }
    } finally {
      setStatusBusy(false, button);
    }
  }

  /* ---------------- 公共格式化 / 剪贴板 ---------------- */

  function formatInterval(seconds) {
    if (seconds === null || seconds === undefined || seconds === '') return '—';
    const value = Number(seconds);
    if (!Number.isFinite(value)) return '—';
    if (value <= 0) return '仅手动';
    if (value < 60) return `${value} 秒`;
    if (value % 60 === 0) return `${value / 60} 分钟`;
    return `${Math.floor(value / 60)} 分 ${value % 60} 秒`;
  }

  function formatTime(value) {
    if (!value) return '尚未同步';
    const text = String(value).replace('T', ' ').replace(/\.\d+$/, '');
    return text.slice(0, 19);
  }

  async function copyText(text) {
    const value = String(text || '');
    if (!value) { toast('没有可复制的内容'); return; }
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(value);
        toast('已复制');
        return;
      }
    } catch (e) { /* 走下面的兜底方案 */ }
    try {
      const area = document.createElement('textarea');
      area.value = value;
      area.setAttribute('readonly', '');
      area.style.position = 'fixed';
      area.style.top = '-1000px';
      area.style.opacity = '0';
      document.body.appendChild(area);
      area.select();
      area.setSelectionRange(0, value.length);
      const copied = document.execCommand('copy');
      document.body.removeChild(area);
      toast(copied ? '已复制' : '复制失败，请长按输入框手动复制');
    } catch (e) {
      toast('复制失败，请长按输入框手动复制');
    }
  }

  /* ---------------- 手机 App（直连同步） ---------------- */

  function renderAppPairing(data) {
    const pill = $('#appStatus');
    const code = (data && data.code) || '';
    const endpoints = Array.isArray(data && data.endpoints) ? data.endpoints : [];
    const remote = endpoints.filter((item) => item.kind === 'remote');
    if (!code) {
      pill.className = 'status-pill status-pill--off';
      pill.textContent = '未就绪';
      $('#appPairCode').value = '';
      $('#appEndpoints').textContent = '服务端没有提供配对信息，更新代码并重启后再试。';
      return;
    }
    pill.className = remote.length ? 'status-pill status-pill--ok' : 'status-pill status-pill--muted';
    pill.textContent = remote.length ? '本地 + 远程' : '仅局域网';
    $('#appPairCode').value = code;
    $('#appEndpoints').textContent = endpoints.length
      ? endpoints
        .map((item, index) => `${index + 1}. ${item.kind === 'lan' ? '局域网' : '远程'}  ${item.url}`)
        .join('\n')
      : '（没有可用地址）';
    $('#appLastSync').textContent = formatTime(data.app_last_sync);
    $('#appLastSyncClient').textContent = data.app_last_sync_client || '—';
    $('#appTaskCount').textContent = Number.isFinite(Number(data.task_count))
      ? `${Number(data.task_count)} 条待办`
      : '—';
    const hint = $('#appPairHint');
    if (!remote.length) {
      hint.textContent =
        '当前没有远程地址：ngrok 隧道未启动（TUNNEL_ENABLED=0 或未配置 authtoken）。' +
        '出门时 App 只能看缓存，回家连上同一网段后自动同步。';
    } else {
      hint.textContent =
        '远程地址已就绪（ngrok）。App 优先走局域网，失败自动切远程；地址按上面顺序尝试。';
    }
  }

  async function loadAppPairing() {
    try {
      renderAppPairing(await api('/api/app/pairing'));
    } catch (error) {
      if (error.message === 'unauthorized') return;
      const pill = $('#appStatus');
      pill.className = 'status-pill status-pill--off';
      pill.textContent = error.status === 404 ? '未就绪' : '读取失败';
      $('#appEndpoints').textContent = error.status === 404
        ? '当前运行的服务还是旧版本（/api/app/pairing 返回 404），更新代码并重启后再试。'
        : `读取失败：${error.message}`;
    }
  }

  /* ---------------- 分区锚点导航 ---------------- */

  const navItems = Array.from(document.querySelectorAll('.settings-nav-item'));
  const navSections = navItems
    .map((link) => ({ link, section: document.getElementById(link.dataset.target || '') }))
    .filter((item) => item.section);
  let activeSectionId = '';
  let navLockUntil = 0;

  function prefersReducedMotion() {
    return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  }

  function headerHeight() {
    const header = $('.settings-header');
    return header ? header.getBoundingClientRect().height : 56;
  }

  function setActiveSection(sectionId, force) {
    if (!sectionId) return;
    if (!force && activeSectionId === sectionId) return;
    activeSectionId = sectionId;
    navSections.forEach(({ link, section }) => {
      const active = section.id === sectionId;
      link.classList.toggle('is-active', active);
      if (active) link.setAttribute('aria-current', 'true');
      else link.removeAttribute('aria-current');
    });
  }

  function scrollToSection(sectionId, behavior) {
    const section = document.getElementById(sectionId);
    if (!section) return;
    const top = section.getBoundingClientRect().top + window.scrollY - headerHeight() - 18;
    window.scrollTo({ top: Math.max(0, top), behavior: behavior || 'smooth' });
  }

  function updateActiveFromScroll() {
    if (!navSections.length) return;
    if (performance.now() < navLockUntil) return;
    const offset = headerHeight() + 24;
    let current = navSections[0];
    navSections.forEach((item) => {
      if (item.section.getBoundingClientRect().top <= offset) current = item;
    });
    const doc = document.documentElement;
    if (window.innerHeight + window.scrollY >= doc.scrollHeight - 2) {
      current = navSections[navSections.length - 1];
    }
    setActiveSection(current.section.id);
  }

  function initNav() {
    if (!navSections.length) return;
    navItems.forEach((link) => {
      link.addEventListener('click', (event) => {
        event.preventDefault();
        const targetId = link.dataset.target;
        if (!targetId) return;
        navLockUntil = performance.now() + 900;
        setActiveSection(targetId, true);
        scrollToSection(targetId, prefersReducedMotion() ? 'auto' : 'smooth');
        try {
          window.history.replaceState(null, '', `#${targetId}`);
        } catch (e) { /* ignore */ }
      });
    });

    let ticking = false;
    const schedule = () => {
      if (ticking) return;
      ticking = true;
      window.requestAnimationFrame(() => {
        ticking = false;
        updateActiveFromScroll();
      });
    };
    window.addEventListener('scroll', schedule, { passive: true });
    window.addEventListener('resize', schedule);
    updateActiveFromScroll();

    const initial = (window.location.hash || '').replace(/^#/, '');
    if (initial && navSections.some((item) => item.section.id === initial)) {
      window.requestAnimationFrame(() => scrollToSection(initial, 'auto'));
    }
  }

  function loadAll() {
    loadSettings();
    loadFilters();
    loadIdentity();
    loadStatus();
    loadAppPairing();
  }

  function bindEvents() {
    $('#llmForm').addEventListener('submit', saveSettings);
    $('#testBtn').addEventListener('click', testConnection);
    $('#clearBtn').addEventListener('click', clearKey);
    $('#toggleKey').addEventListener('click', toggleKeyVisibility);
    $('#filterForm').addEventListener('submit', saveFilters);
    $('#identityForm').addEventListener('submit', saveIdentity);
    $('#identityRefreshBtn').addEventListener('click', refreshIdentity);
    $('#identityRetryBtn').addEventListener('click', () => { void loadIdentity(); });
    $('#identityCheckBtn').addEventListener('click', runIdentityCheck);
    document.querySelectorAll('[data-example]').forEach((button) => {
      button.addEventListener('click', () => {
        $('#identityCheckText').value = button.dataset.example || '';
        $('#identityCheckText').focus();
      });
    });
    $('#pinnedRefreshBtn').addEventListener('click', refreshPinned);
    $('#statusRefreshBtn').addEventListener('click', refreshStatus);
    $('#retryFailedBtn').addEventListener('click', retryFailedQueue);
    $('#statusRetryBtn').addEventListener('click', () => { void loadStatus(); });
    $('#appCopyCode').addEventListener('click', () => copyText($('#appPairCode').value));
    $('#appPairReloadBtn').addEventListener('click', () => { void loadAppPairing(); });

    $('#authForm').addEventListener('submit', (event) => {
      event.preventDefault();
      const token = $('#tokenInput').value.trim();
      if (!token) return;
      saveToken(token);
      hideAuth();
      loadAll();
    });

    DIRTY_FORMS.forEach((item) => {
      const form = $(item.id);
      if (!form) return;
      const onEdit = () => markDirty(item.id);
      form.addEventListener('input', onEdit);
      form.addEventListener('change', onEdit);
    });
    const dirtySaveBtn = $('#dirtySaveBtn');
    if (dirtySaveBtn) dirtySaveBtn.addEventListener('click', saveDirtyForms);
    const dirtyDiscardBtn = $('#dirtyDiscardBtn');
    if (dirtyDiscardBtn) dirtyDiscardBtn.addEventListener('click', discardDirty);
    window.addEventListener('beforeunload', handleBeforeUnload);
  }

  function init() {
    consumeUrlToken();
    if (!state.token) {
      try { state.token = localStorage.getItem(TOKEN_KEY) || ''; } catch (e) { state.token = ''; }
    }
    bindEvents();
    initNav();
    refreshDirtyUI();
    if (!state.token) {
      showAuth('需要访问令牌才能修改设置');
      return;
    }
    hideAuth();
    loadAll();
  }

  document.addEventListener('DOMContentLoaded', init);
})();
