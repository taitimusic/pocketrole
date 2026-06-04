const { apiBaseUrl } = window.POCKETROLE_ADMIN;
const adminI18n = window.PocketRoleAdminI18n || {
  t: (key) => ({
    'beat.setup': '導入',
    'beat.complication': '展開',
    'beat.turningPoint': '転機',
    'beat.resolution': '解決',
    'chapter.pending': '待機中',
    'chapter.active': '進行中',
    'chapter.closed': '完了',
    'proposal.pending': '審査待ち',
    'proposal.approved': '承認済',
    'proposal.rejected': '却下',
  }[key] || key),
  buildLocaleSwitcher: () => null,
};
const appRoot = document.getElementById('admin-app');
let viewerPollHandle = null;
let viewerAutoFollow = true;
let viewerActiveTab = 'timeline';
let viewerMapIframe = null;
let viewerMapIframeStoryId = null;
let _newPlaceCounter = 0;
let _newCharacterCounter = 0;
const CHARACTER_EXPRESSION_KEYS = [
  'neutral',
  'happy',
  'angry',
  'sad',
  'surprised',
  'worried',
  'content',
  'lonely',
  'tired',
  'determined'
];

/** Beat フェーズの表示ラベル（DB/API 値 → 日本語）。value は英語のまま API に送る。 */
const BEAT_PHASE_LABELS = {
  setup:         adminI18n.t('beat.setup'),
  complication:  adminI18n.t('beat.complication'),
  turning_point: adminI18n.t('beat.turningPoint'),
  resolution:    adminI18n.t('beat.resolution'),
};

/** Chapter ステータスの表示ラベル */
const CHAPTER_STATUS_LABELS = {
  pending: adminI18n.t('chapter.pending'),
  active:  adminI18n.t('chapter.active'),
  closed:  adminI18n.t('chapter.closed'),
};

/** Chapter Proposal ステータスの表示ラベル */
const PROPOSAL_STATUS_LABELS = {
  pending:  adminI18n.t('proposal.pending'),
  approved: adminI18n.t('proposal.approved'),
  rejected: adminI18n.t('proposal.rejected'),
};

/** 開始条件 (start_condition) を人間語に変換 */
function formatStartCondition(condition) {
  if (!condition || condition === '') return '即時自動起動';
  if (condition === 'manual') return '手動起動';
  const m = (condition || '').match(/^turn\s*>=\s*(\d+)$/);
  if (m) return `第${m[1]}ターンで自動起動`;
  return condition;
}

function element(tag, text = '') {
  const node = document.createElement(tag);
  if (text) {
    node.textContent = text;
  }
  return node;
}

function viewerEscapeHtml(str) {
  const d = document.createElement('div');
  d.textContent = str ?? '';
  return d.innerHTML;
}

function viewerLinkify(escapedText) {
  const markdownLinks = [];
  // Step 1: Markdown [text](url) は退避し、href 内 URL の二重変換を防ぐ。
  let result = escapedText.replace(/\[([^\]]*)\]\((https?:\/\/[^\s)]*)\)/g, (_, text, url) => {
    const token = `%%POCKETROLE_MARKDOWN_LINK_${markdownLinks.length}%%`;
    markdownLinks.push(`<a href="${url}" target="_blank" rel="noopener noreferrer">${text}</a>`);
    return token;
  });
  // Step 2: 残った裸の URL だけを変換
  result = result.replace(
    /(https?:\/\/[^\s<>"]+)/g,
    '<a href="$1" target="_blank" rel="noopener noreferrer">$1</a>'
  );
  markdownLinks.forEach((html, index) => {
    result = result.replace(`%%POCKETROLE_MARKDOWN_LINK_${index}%%`, html);
  });
  return result;
}

function normalizeStoryAssetRelativePath(relativePath) {
  const raw = String(relativePath || '').trim();
  if (!raw || raw.startsWith('/') || raw.includes('\\')) {
    return null;
  }
  const parts = raw.split('/');
  if (parts.some((part) => !part || part === '.' || part === '..')) {
    return null;
  }
  return parts.map((part) => encodeURIComponent(part)).join('/');
}

function resolveStoryBgmPreviewUrl(storyId, bgmData) {
  const safePath = normalizeStoryAssetRelativePath(bgmData && bgmData.story_default);
  if (!safePath) {
    return null;
  }
  return `/assets/story_maps/${encodeURIComponent(storyId)}/${safePath}?t=${Date.now()}`;
}

function resolvePlaceBgmPreviewUrl(storyId, placeId, placeOverrides) {
  const override = placeOverrides && placeId ? placeOverrides[placeId] : null;
  const safePath = normalizeStoryAssetRelativePath(override);
  if (!safePath) {
    return null;
  }
  return `/assets/story_maps/${encodeURIComponent(storyId)}/${safePath}?t=${Date.now()}`;
}

async function api(path, options = {}) {
  const headers = {
    ...(options.headers || {})
  };
  if (!(options.body instanceof FormData) && !headers['Content-Type']) {
    headers['Content-Type'] = 'application/json';
  }
  try {
    return await fetch(`${apiBaseUrl}${path}`, {
      credentials: 'same-origin',
      headers,
      ...options
    });
  } catch (err) {
    if (typeof window.showToast === 'function' && !options.silent) {
      const msg = err && err.message ? err.message : '接続に失敗しました';
      window.showToast('ネットワークエラー: ' + msg, 'error');
    }
    throw err;
  }
}

function setSavingButton(btn, label) {
  if (!btn) return;
  if (btn.dataset.savingOriginalLabel == null) {
    btn.dataset.savingOriginalLabel = btn.textContent;
  }
  btn.disabled = true;
  if (label) btn.textContent = label;
  btn.classList.add('is-saving');
}

function clearSavingButton(btn) {
  if (!btn) return;
  btn.disabled = false;
  if (btn.dataset.savingOriginalLabel != null) {
    btn.textContent = btn.dataset.savingOriginalLabel;
    delete btn.dataset.savingOriginalLabel;
  }
  btn.classList.remove('is-saving');
}

let editorDirtyHandler = null;

function clearEditorDirtyGuard() {
  if (editorDirtyHandler !== null) {
    window.removeEventListener('beforeunload', editorDirtyHandler);
    editorDirtyHandler = null;
  }
}

function attachEditorDirtyGuard() {
  clearEditorDirtyGuard();
  let dirty = false;
  editorDirtyHandler = (e) => {
    if (dirty) {
      e.preventDefault();
      e.returnValue = '';
    }
  };
  window.addEventListener('beforeunload', editorDirtyHandler);
  return {
    setDirty: () => { dirty = true; },
    clearDirty: () => { dirty = false; }
  };
}

function buildEditorSkeleton() {
  const wrap = element('div');
  wrap.className = 'editor-skeleton';
  const sizes = [
    ['skeleton-title', '320px'],
    ['skeleton-text', '520px'],
    ['skeleton-text', '400px'],
    ['skeleton-card', '100%'],
    ['skeleton-card', '100%'],
    ['skeleton-card', '100%']
  ];
  sizes.forEach(([cls, maxW]) => {
    const d = element('div');
    d.className = 'skeleton ' + cls;
    d.style.maxWidth = maxW;
    wrap.append(d);
  });
  return wrap;
}

function buildEditorSaveBar(saveBtn, links) {
  const bar = element('div');
  bar.className = 'admin-save-bar';
  const hint = element('span', '変更を保存してください');
  hint.className = 'save-hint';
  bar.append(hint);
  if (Array.isArray(links)) {
    links.forEach(({ href, label }) => {
      const a = link(href, label);
      a.className = 'btn btn-ghost btn-sm';
      bar.append(a);
    });
  }
  bar.append(saveBtn);
  return bar;
}

function card(title, body) {
  const section = element('section');
  section.className = 'admin-card';
  section.append(element('h2', title), element('p', body));
  return section;
}

function link(href, label) {
  const anchor = element('a', label);
  anchor.href = href;
  return anchor;
}

const NAV_SECTIONS = [
  {
    key: 'dashboard',
    label: adminI18n.t('nav.dashboard'),
    href: '/admin',
    icon: 'dashboard',
    matches: (p) => p === '/admin' || p === '/admin/'
  },
  {
    key: 'stories',
    label: adminI18n.t('nav.stories'),
    href: '/admin/stories',
    icon: 'stories',
    matches: (p) => (
      p === '/admin/stories'
      || p.startsWith('/admin/stories/')
      || p.startsWith('/admin/viewer/')
      || p.startsWith('/admin/onboarding/')
      || p.startsWith('/admin/characters/')
      || p.startsWith('/admin/places/')
      || p.startsWith('/admin/story-settings/')
      || p.startsWith('/admin/event-anomalies/')
      || p.startsWith('/admin/directors/')
      || p.startsWith('/admin/chapter-definitions/')
      || p.startsWith('/admin/chapters/')
      || p.startsWith('/admin/conversation-patterns/')
    )
  },
  {
    key: 'news-mode',
    label: adminI18n.t('nav.newsMode'),
    href: '/admin/news-mode',
    icon: 'settings',
    matches: (p) => p === '/admin/news-mode'
  },
  {
    key: 'settings',
    label: adminI18n.t('nav.settings'),
    href: '/admin/settings',
    icon: 'settings',
    matches: (p) => p === '/admin/settings'
  },
  {
    key: 'audit',
    label: adminI18n.t('nav.audit'),
    href: '/admin/audit-logs',
    icon: 'audit',
    matches: (p) => p.startsWith('/admin/audit-logs')
  }
];

const STORY_SCOPED_LABELS = {
  'viewer': 'Viewer',
  'onboarding': 'Onboarding',
  'characters': adminI18n.t('nav.characters'),
  'places': adminI18n.t('nav.places'),
  'story-settings': adminI18n.t('nav.storySettings'),
  'event-anomalies': adminI18n.t('nav.eventAnomalies'),
  'directors': adminI18n.t('nav.directors'),
  'chapter-definitions': adminI18n.t('nav.chapterDefinitions'),
  'chapters': adminI18n.t('nav.chapters'),
  'conversation-patterns': adminI18n.t('nav.conversationPatterns'),
  'scene-scripts': adminI18n.t('nav.sceneScripts')
};

function buildTopbar() {
  const bar = element('header');
  bar.className = 'admin-topbar';

  const sidebarToggle = element('button');
  sidebarToggle.type = 'button';
  sidebarToggle.className = 'admin-sidebar-toggle';
  sidebarToggle.setAttribute('aria-label', adminI18n.t('common.openMenu'));
  sidebarToggle.setAttribute('aria-expanded', 'false');
  sidebarToggle.setAttribute('aria-controls', 'admin-sidebar');
  sidebarToggle.innerHTML = (window.ADMIN_ICON && window.ADMIN_ICON.hamburger) || '';
  sidebarToggle.addEventListener('click', () => {
    const sb = document.querySelector('.admin-sidebar');
    const ov = document.querySelector('.admin-sidebar-overlay');
    if (sb) sb.classList.toggle('open');
    if (ov) ov.classList.toggle('open');
    const isOpen = sb ? sb.classList.contains('open') : false;
    sidebarToggle.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
    sidebarToggle.setAttribute('aria-label', isOpen ? adminI18n.t('common.closeMenu') : adminI18n.t('common.openMenu'));
  });

  const logo = element('a');
  logo.className = 'admin-topbar-logo';
  logo.href = '/admin';
  const logoIcon = element('span');
  logoIcon.innerHTML = (window.ADMIN_ICON && window.ADMIN_ICON.logo) || '';
  const logoText = element('span', 'PocketRole Admin');
  logoText.className = 'admin-topbar-logo-text';
  logo.append(logoIcon, logoText);

  const spacer = element('div');
  spacer.className = 'admin-topbar-spacer';

  const actions = element('div');
  actions.className = 'admin-topbar-actions';

  const themeBtn = element('button');
  themeBtn.type = 'button';
  themeBtn.className = 'admin-theme-toggle';
  const themeNow = (window.adminCurrentTheme && window.adminCurrentTheme()) || 'light';
  const setThemeBtnState = (t) => {
    themeBtn.setAttribute('data-theme-current', t);
    themeBtn.setAttribute('aria-label', t === 'dark' ? adminI18n.t('theme.toLight') : adminI18n.t('theme.toDark'));
    themeBtn.innerHTML = (window.ADMIN_ICON && (t === 'dark' ? window.ADMIN_ICON.sun : window.ADMIN_ICON.moon)) || '';
  };
  setThemeBtnState(themeNow);
  themeBtn.addEventListener('click', () => {
    if (window.adminToggleTheme) window.adminToggleTheme();
    setThemeBtnState((window.adminCurrentTheme && window.adminCurrentTheme()) || 'light');
  });
  const localeSwitcher = adminI18n.buildLocaleSwitcher();
  if (localeSwitcher) actions.append(localeSwitcher);
  actions.append(themeBtn);

  bar.append(sidebarToggle, logo, spacer, actions);
  return bar;
}

function buildSidebar(currentPath) {
  const aside = element('aside');
  aside.className = 'admin-sidebar';
  aside.id = 'admin-sidebar';
  aside.setAttribute('aria-label', 'メインナビゲーション');

  const heading = element('div', adminI18n.t('common.workspace'));
  heading.className = 'admin-sidebar-section';
  aside.append(heading);

  NAV_SECTIONS.forEach((nav) => {
    const a = element('a');
    a.className = 'admin-nav-item';
    a.href = nav.href;
    if (nav.matches(currentPath)) {
      a.classList.add('active');
      a.setAttribute('aria-current', 'page');
    }
    const icon = element('span');
    icon.innerHTML = (window.ADMIN_ICON && window.ADMIN_ICON[nav.icon]) || '';
    a.append(icon, document.createTextNode(nav.label));
    aside.append(a);
  });

  return aside;
}

function buildBreadcrumb(path) {
  const nav = element('nav');
  nav.className = 'admin-breadcrumb';
  nav.setAttribute('aria-label', 'breadcrumb');

  const crumbs = [{ label: 'Admin', href: '/admin' }];

  if (path === '/admin' || path === '/admin/') {
    crumbs.push({ label: adminI18n.t('nav.dashboard'), href: null });
  } else if (path === '/admin/stories') {
    crumbs.push({ label: adminI18n.t('nav.stories'), href: null });
  } else if (path.startsWith('/admin/stories/')) {
    const id = path.slice('/admin/stories/'.length);
    crumbs.push({ label: adminI18n.t('nav.stories'), href: '/admin/stories' });
    crumbs.push({ label: id, href: null });
  } else if (path === '/admin/settings') {
    crumbs.push({ label: adminI18n.t('nav.settings'), href: null });
  } else if (path.startsWith('/admin/audit-logs')) {
    crumbs.push({ label: adminI18n.t('nav.audit'), href: null });
  } else {
    const m = path.match(/^\/admin\/([^/]+)\/(.+)$/);
    if (m) {
      const section = m[1];
      const id = m[2];
      const sectionLabel = STORY_SCOPED_LABELS[section] || section;
      crumbs.push({ label: adminI18n.t('nav.stories'), href: '/admin/stories' });
      crumbs.push({ label: id, href: '/admin/stories/' + id });
      crumbs.push({ label: sectionLabel, href: null });
    } else {
      crumbs.push({ label: path, href: null });
    }
  }

  crumbs.forEach((crumb, idx) => {
    if (idx > 0) {
      const sep = element('span', '›');
      sep.className = 'crumb-sep';
      nav.append(sep);
    }
    if (crumb.href) {
      const a = element('a', crumb.label);
      a.href = crumb.href;
      nav.append(a);
    } else {
      const span = element('span', crumb.label);
      span.className = 'crumb-current';
      span.setAttribute('aria-current', 'page');
      nav.append(span);
    }
  });
  return nav;
}

function layout(title, options = {}) {
  clearViewerPolling();
  cancelDashboardRefresh();
  clearEditorDirtyGuard();
  appRoot.innerHTML = '';

  const path = window.location.pathname;
  if (!path.startsWith('/admin/viewer/')) {
    viewerActiveTab = 'timeline';
    viewerMapIframe = null;
    viewerMapIframeStoryId = null;
  }
  const app = element('div');
  app.className = 'admin-app';

  const topbar = buildTopbar();

  const body = element('div');
  body.className = 'admin-body';

  const sidebar = buildSidebar(path);
  const overlay = element('div');
  overlay.className = 'admin-sidebar-overlay';
  overlay.addEventListener('click', () => {
    sidebar.classList.remove('open');
    overlay.classList.remove('open');
    const toggle = document.querySelector('.admin-sidebar-toggle');
    if (toggle) {
      toggle.setAttribute('aria-expanded', 'false');
      toggle.setAttribute('aria-label', adminI18n.t('common.openMenu'));
    }
  });

  const mainArea = element('main');
  mainArea.className = 'admin-main';

  const breadcrumb = buildBreadcrumb(path);

  const content = element('div');
  content.className = 'admin-content';

  if (title) {
    const header = element('header');
    header.className = 'admin-page-header';
    const titleGroup = element('div');
    const titleEl = element('h1', title);
    titleEl.className = 'admin-page-title';
    titleGroup.append(titleEl);
    if (options && options.subtitle) {
      const sub = element('p', options.subtitle);
      sub.className = 'admin-page-subtitle';
      titleGroup.append(sub);
    }
    header.append(titleGroup);
    content.append(header);
  }

  mainArea.append(breadcrumb, content);
  body.append(sidebar, overlay, mainArea);
  app.append(topbar, body);
  appRoot.append(app);
  return content;
}

let viewerKeyHandler = null;

function clearViewerPolling() {
  if (viewerPollHandle !== null) {
    window.clearInterval(viewerPollHandle);
    viewerPollHandle = null;
  }
  if (viewerKeyHandler !== null) {
    window.removeEventListener('keydown', viewerKeyHandler);
    viewerKeyHandler = null;
  }
}

function renderLogin() {
  appRoot.innerHTML = '';
  const section = element('section');
  section.className = 'admin-login';
  section.append(element('h1', 'PocketRole Admin'));
  const form = element('form');
  const usernameLabel = element('label', 'ユーザー名');
  const username = element('input');
  username.name = 'username';
  username.autocomplete = 'username';
  username.placeholder = 'username';
  const passwordLabel = element('label', 'パスワード');
  const password = element('input');
  password.type = 'password';
  password.name = 'password';
  password.autocomplete = 'current-password';
  password.placeholder = 'password';
  const submit = element('button', 'ログイン');
  submit.type = 'submit';
  const message = element('p');
  usernameLabel.append(username);
  passwordLabel.append(password);
  form.append(usernameLabel, passwordLabel, submit);
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const response = await api('/session/login', {
      method: 'POST',
      body: JSON.stringify({ username: username.value, password: password.value })
    });
    if (response.ok) {
      await bootstrap();
      return;
    }
    message.textContent = 'ログインに失敗しました。';
  });
  section.append(form, message);
  appRoot.append(section);
}

let dashboardRefreshHandle = null;

function cancelDashboardRefresh() {
  if (dashboardRefreshHandle !== null) {
    window.clearInterval(dashboardRefreshHandle);
    dashboardRefreshHandle = null;
  }
}

function formatRelativeTime(ts) {
  if (!ts) return '-';
  const isoLike = typeof ts === 'string' && !ts.endsWith('Z') && ts.includes(' ')
    ? ts.replace(' ', 'T') + 'Z'
    : ts;
  const d = new Date(isoLike);
  if (isNaN(d.getTime())) return String(ts);
  const diffSec = Math.max(0, Math.round((Date.now() - d.getTime()) / 1000));
  if (diffSec < 60) return `${diffSec}秒前`;
  const diffMin = Math.round(diffSec / 60);
  if (diffMin < 60) return `${diffMin}分前`;
  const diffHr = Math.round(diffMin / 60);
  if (diffHr < 24) return `${diffHr}時間前`;
  const diffDay = Math.round(diffHr / 24);
  if (diffDay < 30) return `${diffDay}日前`;
  return d.toISOString().slice(0, 10);
}

function buildSparkline(counts) {
  const w = 120;
  const h = 32;
  const values = counts.map((c) => Number(c.count) || 0);
  const max = Math.max(1, ...values);
  const stepX = values.length > 1 ? w / (values.length - 1) : 0;
  const points = values
    .map((v, i) => {
      const x = i * stepX;
      const y = h - (v / max) * (h - 4) - 2;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(' ');
  const wrap = element('div');
  wrap.className = 'kpi-sparkline';
  wrap.innerHTML = `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true">`
    + `<polyline fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" points="${points}"/>`
    + `</svg>`;
  return wrap;
}

function buildKpiCard(label, value, options = {}) {
  const card = element('div');
  card.className = 'kpi-card card card-padded';
  const labelEl = element('div', label);
  labelEl.className = 'kpi-label';
  const valueEl = element('div', String(value));
  valueEl.className = 'kpi-value';
  card.append(labelEl, valueEl);
  if (options.sparklineData && options.sparklineData.length) {
    card.append(buildSparkline(options.sparklineData));
  }
  if (options.hint) {
    const hint = element('div', options.hint);
    hint.className = 'kpi-hint';
    card.append(hint);
  }
  return card;
}

function buildRuntimeCard(item) {
  const card = element('article');
  card.className = 'runtime-card';

  const head = element('div');
  head.className = 'runtime-card-head';
  const titleGroup = element('div');
  titleGroup.className = 'runtime-card-title-group';
  const titleEl = element('div', item.title || item.story_id);
  titleEl.className = 'runtime-card-title';
  const idEl = element('div', item.story_id);
  idEl.className = 'runtime-card-id';
  titleGroup.append(titleEl, idEl);
  const isRunning = item.state === 'running';
  const pill = element('span', isRunning ? '稼働中' : '停止中');
  pill.className = 'pill ' + (isRunning ? 'pill-running' : 'pill-stopped');
  head.append(titleGroup, pill);

  const meta = element('div');
  meta.className = 'runtime-card-meta';
  const llmText = item.llm_provider
    ? `${item.llm_provider}${item.llm_model ? ' / ' + item.llm_model : ''}`
    : '-';
  [
    ['LLM', llmText],
    ['公開', item.visibility === 'public' ? '公開中' : '非公開'],
    ['最終発話', formatRelativeTime(item.last_chat_at)]
  ].forEach(([label, value]) => {
    const row = element('div');
    row.className = 'runtime-card-meta-item';
    const l = element('span', label);
    l.className = 'runtime-card-meta-label';
    const v = element('span', value);
    v.className = 'runtime-card-meta-value';
    row.append(l, v);
    meta.append(row);
  });

  const actions = element('div');
  actions.className = 'runtime-card-actions';
  const viewerLink = link(`/admin/viewer/${item.story_id}`, 'Viewer');
  viewerLink.className = 'btn btn-secondary btn-sm';
  const detailLink = link(`/admin/stories/${item.story_id}`, '詳細');
  detailLink.className = 'btn btn-ghost btn-sm';
  actions.append(viewerLink, detailLink);

  if (isRunning) {
    const restartBtn = element('button', '再起動');
    restartBtn.type = 'button';
    restartBtn.className = 'btn btn-ghost btn-sm';
    restartBtn.addEventListener('click', () => dashboardRuntimeAction(restartBtn, item.story_id, 'restart'));
    const stopBtn = element('button', '停止');
    stopBtn.type = 'button';
    stopBtn.className = 'btn btn-danger btn-sm';
    stopBtn.addEventListener('click', () => dashboardRuntimeAction(stopBtn, item.story_id, 'stop'));
    actions.append(restartBtn, stopBtn);
  } else {
    const startBtn = element('button', '起動');
    startBtn.type = 'button';
    startBtn.className = 'btn btn-primary btn-sm';
    startBtn.addEventListener('click', () => dashboardRuntimeAction(startBtn, item.story_id, 'start'));
    actions.append(startBtn);
  }

  card.append(head, meta, actions);
  return card;
}

async function dashboardRuntimeAction(btn, storyId, action) {
  const labels = { start: '起動中…', stop: '停止中…', restart: '再起動中…' };
  const successMsg = { start: '起動しました', stop: '停止しました', restart: '再起動しました' };
  setSavingButton(btn, labels[action]);
  try {
    let response;
    if (action === 'restart') {
      response = await api(`/stories/${storyId}/runtime/restart`, { method: 'POST' });
    } else {
      const desired = action === 'start' ? 'running' : 'stopped';
      response = await api(`/stories/${storyId}/runtime`, {
        method: 'PUT',
        body: JSON.stringify({ desired_state: desired })
      });
    }
    if (response.ok) {
      window.showToast(successMsg[action], 'success');
      const grid = document.querySelector('.dashboard-bento');
      if (grid) await refreshDashboardContent(grid);
    } else {
      let detail = '';
      try { detail = (await response.json()).error || ''; } catch (_) {}
      window.showToast(detail || '操作に失敗しました', 'error');
      clearSavingButton(btn);
    }
  } catch (_) {
    clearSavingButton(btn);
  }
}

function buildChatPreview(chat) {
  const item = element('a');
  item.className = 'recent-chat-item';
  item.href = `/admin/viewer/${chat.story_id}`;

  const avatar = element('div');
  avatar.className = 'recent-chat-avatar';
  const initial = (chat.speaker_name || chat.char_id || '?').slice(0, 1).toUpperCase();
  avatar.textContent = initial;

  const body = element('div');
  body.className = 'recent-chat-body';

  const head = element('div');
  head.className = 'recent-chat-head';
  const speaker = element('span', chat.speaker_name || chat.char_id || '?');
  speaker.className = 'recent-chat-speaker';
  const story = element('span', chat.story_title || chat.story_id || '');
  story.className = 'recent-chat-story';
  const time = element('span', formatRelativeTime(chat.created_at));
  time.className = 'recent-chat-time';
  head.append(speaker, story, time);

  const message = element('p');
  message.className = 'recent-chat-message';
  const raw = String(chat.message || '');
  message.textContent = raw.length > 160 ? raw.slice(0, 160) + '…' : raw;

  body.append(head, message);
  item.append(avatar, body);
  return item;
}

function buildAuditLogItem(log) {
  const li = element('li');
  li.className = 'audit-stream-item';
  const head = element('div');
  head.className = 'audit-stream-head';
  const action = element('span', log.action || '');
  action.className = 'audit-stream-action';
  const result = log.result || '';
  const pill = element('span', result || '?');
  const pillKind = result === 'success' ? 'pill-running' : (result === 'error' ? 'pill-error' : 'pill-stopped');
  pill.className = 'pill ' + pillKind;
  head.append(action, pill);

  const meta = element('div');
  meta.className = 'audit-stream-meta';
  const actor = element('span', `${log.actor_label || '?'} (${log.actor_type || '?'})`);
  actor.className = 'audit-stream-actor';
  const story = element('span', log.story_id ? `story:${log.story_id}` : '-');
  story.className = 'audit-stream-story';
  const time = element('span', formatRelativeTime(log.created_at));
  time.className = 'audit-stream-time';
  meta.append(actor, story, time);

  li.append(head, meta);
  return li;
}

function renderDashboardKpis(data) {
  const section = element('section');
  section.className = 'dashboard-kpis';
  const kpis = data.kpis || {};
  const counts = data.daily_turn_counts || [];
  section.append(
    buildKpiCard('ストーリー', kpis.story_count ?? 0),
    buildKpiCard('公開中', kpis.public_count ?? 0),
    buildKpiCard('本日のターン', kpis.today_turn_count ?? 0, { sparklineData: counts, hint: '直近 7 日' }),
    buildKpiCard('稼働中 runtime', kpis.active_runtime_count ?? 0)
  );
  return section;
}

function renderDashboardRuntime(data) {
  const section = element('section');
  section.className = 'dashboard-runtime card card-padded';
  const head = element('div');
  head.className = 'dashboard-section-head';
  const heading = element('h2', 'Runtime 状態');
  heading.className = 'dashboard-section-title';
  head.append(heading);
  section.append(head);

  const summary = data.runtime_summary || [];
  if (summary.length === 0) {
    const empty = element('p', 'ストーリーが登録されていません。');
    empty.className = 'text-muted';
    section.append(empty);
    return section;
  }
  const list = element('div');
  list.className = 'runtime-card-grid';
  summary.forEach((item) => list.append(buildRuntimeCard(item)));
  section.append(list);
  return section;
}

function renderDashboardRecentChats(data) {
  const section = element('section');
  section.className = 'dashboard-chats card card-padded';
  const head = element('div');
  head.className = 'dashboard-section-head';
  const heading = element('h2', '直近の会話');
  heading.className = 'dashboard-section-title';
  head.append(heading);
  section.append(head);

  const chats = data.recent_chats || [];
  if (chats.length === 0) {
    const empty = element('p', 'まだ会話がありません。');
    empty.className = 'text-muted';
    section.append(empty);
    return section;
  }
  const list = element('div');
  list.className = 'recent-chat-list';
  chats.forEach((chat) => list.append(buildChatPreview(chat)));
  section.append(list);
  return section;
}

function renderDashboardAuditStream(data) {
  const section = element('section');
  section.className = 'dashboard-audit card card-padded';
  const head = element('div');
  head.className = 'dashboard-section-head';
  const heading = element('h2', '監査ログ');
  heading.className = 'dashboard-section-title';
  const moreLink = link('/admin/audit-logs', 'すべて表示 →');
  moreLink.className = 'dashboard-section-link';
  head.append(heading, moreLink);
  section.append(head);

  const logs = data.recent_audit_logs || [];
  if (logs.length === 0) {
    const empty = element('p', 'ログがありません。');
    empty.className = 'text-muted';
    section.append(empty);
    return section;
  }
  const list = element('ul');
  list.className = 'audit-stream-list';
  logs.forEach((log) => list.append(buildAuditLogItem(log)));
  section.append(list);
  return section;
}

async function refreshDashboardContent(grid) {
  let response;
  try {
    response = await api('/dashboard');
  } catch (_) {
    return;
  }
  if (!response.ok) {
    grid.innerHTML = '';
    const err = element('div', 'ダッシュボードの読み込みに失敗しました');
    err.className = 'notice notice-error';
    grid.append(err);
    return;
  }
  const payload = await response.json();
  const data = payload.data || {};

  grid.innerHTML = '';
  grid.append(renderDashboardKpis(data));
  grid.append(renderDashboardRuntime(data));

  const secondary = element('div');
  secondary.className = 'dashboard-secondary-row';
  secondary.append(renderDashboardRecentChats(data), renderDashboardAuditStream(data));
  grid.append(secondary);
}

async function renderDashboard() {
  cancelDashboardRefresh();
  const main = layout('ダッシュボード', { subtitle: 'runtime と直近のチャットを一望できます。' });
  const grid = element('div');
  grid.className = 'dashboard-bento';
  main.append(grid);

  await refreshDashboardContent(grid);

  dashboardRefreshHandle = window.setInterval(() => {
    if (document.hidden) return;
    if (!document.body.contains(grid)) {
      cancelDashboardRefresh();
      return;
    }
    refreshDashboardContent(grid).catch(() => {});
  }, 5000);
}

function buildStoryCard(story) {
  const cardEl = element('article');
  cardEl.className = 'story-card card card-interactive';

  const head = element('div');
  head.className = 'story-card-head';
  const isRunning = story.runtime_state === 'running';
  const statePill = element('span', isRunning ? '稼働中' : '停止中');
  statePill.className = 'pill ' + (isRunning ? 'pill-running' : 'pill-stopped');
  const visPill = element('span', story.visibility === 'public' ? '公開' : '非公開');
  visPill.className = 'pill ' + (story.visibility === 'public' ? 'pill-info' : 'pill-stopped');
  head.append(statePill, visPill);

  const body = element('div');
  body.className = 'story-card-body';
  const titleEl = element('h2', story.title || story.story_id);
  titleEl.className = 'story-card-title';
  const idEl = element('div', story.story_id);
  idEl.className = 'story-card-id';
  body.append(titleEl, idEl);

  const footer = element('div');
  footer.className = 'story-card-footer';
  const viewLink = link(`/admin/stories/${story.story_id}`, '詳細 →');
  viewLink.className = 'story-card-link';
  footer.append(viewLink);

  cardEl.append(head, body, footer);
  cardEl.addEventListener('click', (e) => {
    if (e.target.closest('a')) return;
    window.location.href = `/admin/stories/${story.story_id}`;
  });
  return cardEl;
}

async function renderStories() {
  const response = await api('/stories');
  if (!response.ok) {
    const main = layout('ストーリー');
    const err = element('div', 'ストーリーの読み込みに失敗しました。');
    err.className = 'notice notice-error';
    main.append(err);
    return;
  }
  const payload = await response.json();
  const stories = (payload.data && payload.data.stories) || [];
  const main = layout('ストーリー', { subtitle: `${stories.length} 件のストーリー` });

  if (stories.length === 0) {
    const empty = element('p', 'ストーリーが登録されていません。');
    empty.className = 'text-muted';
    main.append(empty);
    return;
  }
  const grid = element('div');
  grid.className = 'stories-grid';
  stories.forEach((story) => grid.append(buildStoryCard(story)));
  main.append(grid);
}

async function mutateStory(storyId, path, method, body) {
  const response = await api(`/stories/${storyId}${path}`, {
    method,
    body: body ? JSON.stringify(body) : undefined
  });
  if (response.ok && typeof window.showToast === 'function') {
    window.showToast('操作が完了しました', 'success');
  }
  await renderStory(storyId);
}

/**
 * type="password" の入力フィールドに表示/非表示トグルボタンを付けたコンポーネント。
 * Settings ページと Story ページの両方から再利用する。
 * @returns {{ element: HTMLElement, getValue: () => string, shouldClear: () => boolean }}
 */
function createTokenField(label, value, placeholder = 'pocketrole_...', hasExistingToken = false) {
  const wrap = element('div');
  wrap.className = 'settings-field';

  const lbl = element('label', label);
  lbl.className = 'form-label';

  const row = element('div');
  row.style.display = 'flex';
  row.style.gap = '6px';
  row.style.alignItems = 'stretch';

  const inp = element('input');
  inp.type = 'password';
  inp.className = 'input';
  inp.value = value;
  inp.placeholder = placeholder;
  inp.style.flex = '1';
  inp.setAttribute('autocomplete', 'off');

  const toggleBtn = element('button', '表示');
  toggleBtn.type = 'button';
  toggleBtn.className = 'btn btn-ghost btn-sm';
  toggleBtn.style.whiteSpace = 'nowrap';
  toggleBtn.addEventListener('click', () => {
    const showing = inp.type === 'text';
    inp.type = showing ? 'password' : 'text';
    toggleBtn.textContent = showing ? '表示' : '非表示';
  });

  row.append(inp, toggleBtn);
  wrap.append(lbl, row);

  let clearChk = null;
  if (hasExistingToken) {
    const note = element('div', 'Auth Token は設定済みです。変更する場合のみ入力してください。');
    note.className = 'form-help';
    const clearRow = element('label');
    clearRow.className = 'form-help';
    clearChk = document.createElement('input');
    clearChk.type = 'checkbox';
    clearChk.style.marginRight = '6px';
    clearRow.append(clearChk, document.createTextNode('保存時に Auth Token を削除する'));
    wrap.append(note, clearRow);
  }

  return {
    element: wrap,
    getValue: () => inp.value.trim(),
    shouldClear: () => Boolean(clearChk && clearChk.checked),
  };
}

/**
 * ストーリー完全削除の確認モーダルを表示する。
 * 既存モーダルがあれば閉じてから開き直す。
 */
function openDeleteModal(storyId) {
  document.getElementById('story-delete-modal')?.remove();

  const overlay = element('div');
  overlay.id = 'story-delete-modal';
  overlay.className = 'modal-overlay';

  const box = element('div');
  box.className = 'modal-box';

  const title = element('h2', 'ストーリーを完全削除');
  title.className = 'modal-title danger-zone-title';

  const warning = element('p');
  warning.className = 'modal-warning';
  warning.innerHTML =
    '<strong>この操作は取り消せません。</strong><br>'
    + '以下のデータがすべて削除されます:';

  const list = element('ul');
  list.className = 'modal-delete-list';
  [
    'DB: キャラクター・場所・チャットログ・記憶・関係性・進行データ（全テーブル）',
    'DB: ストーリー本体の設定',
    '（オプション）YAML 設定ファイル、キャラ画像、ストーリーマップ、チャットログファイル',
  ].forEach((text) => {
    const li = element('li', text);
    list.append(li);
  });

  const notDeletedNote = element('p', '削除されないもの: 管理者監査ログ（story_id が空欄に変更）、docs/・tests/ 以下のファイル');
  notDeletedNote.className = 'modal-note';

  const confirmLabel = element('label');
  confirmLabel.textContent = `確認のため、ストーリー ID「${storyId}」を入力してください:`;
  confirmLabel.className = 'modal-confirm-label';

  const confirmInput = element('input');
  confirmInput.type = 'text';
  confirmInput.className = 'text-input';
  confirmInput.placeholder = storyId;
  confirmInput.setAttribute('autocomplete', 'off');

  const deleteFilesRow = element('div');
  deleteFilesRow.className = 'form-row';
  deleteFilesRow.style.marginTop = '4px';
  const deleteFilesChk = element('input');
  deleteFilesChk.type = 'checkbox';
  const deleteFilesLabel = element('label', ' YAML・画像・チャットログファイルも削除する');
  deleteFilesLabel.style.marginLeft = '6px';
  deleteFilesRow.append(deleteFilesChk, deleteFilesLabel);

  const btnRow = element('div');
  btnRow.className = 'modal-btn-row';

  const cancelBtn = element('button', 'キャンセル');
  cancelBtn.className = 'btn btn-ghost';
  cancelBtn.addEventListener('click', () => overlay.remove());

  const execBtn = element('button', '完全削除する');
  execBtn.className = 'btn btn-danger';
  execBtn.disabled = true;

  confirmInput.addEventListener('input', () => {
    execBtn.disabled = confirmInput.value.trim() !== storyId;
  });

  execBtn.addEventListener('click', async () => {
    if (confirmInput.value.trim() !== storyId) return;
    execBtn.disabled = true;
    execBtn.textContent = '削除中...';
    try {
      const res = await api(`/stories/${storyId}`, {
        method: 'DELETE',
        body: JSON.stringify({
          confirm_story_id: storyId,
          delete_files: deleteFilesChk.checked,
        }),
      });
      if (res.ok) {
        overlay.remove();
        window.history.pushState({}, '', '/admin/stories');
        await renderStories();
      } else {
        const d = await res.json().catch(() => ({}));
        execBtn.textContent = '削除に失敗しました';
        execBtn.disabled = false;
        alert((d.error || {}).message || '削除に失敗しました');
      }
    } catch (err) {
      execBtn.textContent = '通信エラー';
      execBtn.disabled = false;
    }
  });

  btnRow.append(cancelBtn, execBtn);
  box.append(title, warning, list, notDeletedNote, confirmLabel, confirmInput, deleteFilesRow, btnRow);
  overlay.append(box);
  document.body.append(overlay);
  confirmInput.focus();

  // オーバーレイ背景クリックで閉じる
  overlay.addEventListener('click', (e) => {
    if (e.target === overlay) overlay.remove();
  });
}

/**
 * チップ式タグ入力コンポーネント。
 * Enter / カンマで追加、× で削除、Backspace で末尾削除。
 * <datalist> で既存タグの autocomplete サジェストを提供。
 * @returns {{ element: HTMLElement, getTags: () => string[], setTags: (arr: string[]) => void }}
 */
function createTagChipInput({ initialTags = [], suggestions = [], placeholder = 'タグを入力…', maxTags = 20 } = {}) {
  const root = element('div');
  root.className = 'tag-chip-input';

  const list = element('div');
  list.className = 'tag-chip-list';
  root.append(list);

  const inputWrap = element('div');
  inputWrap.className = 'tag-chip-input-wrap';

  const input = element('input');
  input.type = 'text';
  input.placeholder = placeholder;
  const dlId = `tagchip-dl-${Math.random().toString(36).slice(2)}`;
  input.setAttribute('list', dlId);
  input.setAttribute('autocomplete', 'off');

  const datalist = document.createElement('datalist');
  datalist.id = dlId;
  suggestions.forEach((s) => {
    const opt = document.createElement('option');
    opt.value = s;
    datalist.append(opt);
  });

  inputWrap.append(input, datalist);
  root.append(inputWrap);

  const state = { tags: [...initialTags] };

  const norm = (s) => s.replace(/\s+/g, ' ').trim();

  const render = () => {
    list.innerHTML = '';
    state.tags.forEach((tag, idx) => {
      const chip = element('span');
      chip.className = 'tag-chip';
      const label = element('span', tag);
      label.className = 'tag-chip-label';
      const close = element('button');
      close.type = 'button';
      close.className = 'tag-chip-remove';
      close.setAttribute('aria-label', `${tag} を削除`);
      close.textContent = '×';
      close.addEventListener('click', () => {
        state.tags.splice(idx, 1);
        render();
        input.focus();
      });
      chip.append(label, close);
      list.append(chip);
    });
  };

  const add = (raw) => {
    const v = norm(raw);
    if (!v || state.tags.some((t) => t.toLowerCase() === v.toLowerCase())) return false;
    if (state.tags.length >= maxTags) return false;
    state.tags.push(v);
    render();
    return true;
  };

  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ',') {
      e.preventDefault();
      if (add(input.value)) input.value = '';
    } else if (e.key === 'Backspace' && input.value === '' && state.tags.length > 0) {
      state.tags.pop();
      render();
    }
  });
  input.addEventListener('blur', () => {
    if (input.value.trim()) {
      if (add(input.value)) input.value = '';
    }
  });

  render();

  return {
    element: root,
    getTags: () => [...state.tags],
    setTags: (arr) => { state.tags = [...arr]; render(); },
  };
}

function storyRuntimeButton(label, className, disabled, handler) {
  const btn = element('button', label);
  btn.type = 'button';
  btn.className = 'btn ' + className;
  btn.disabled = disabled;
  btn.addEventListener('click', () => handler(btn));
  return btn;
}

async function storyRuntimeAction(btn, storyId, fetchArgs, successMsg) {
  setSavingButton(btn, '処理中…');
  try {
    const r = await api(`/stories/${storyId}${fetchArgs.path}`, {
      method: fetchArgs.method,
      body: fetchArgs.body ? JSON.stringify(fetchArgs.body) : undefined
    });
    if (r.ok) {
      if (typeof window.showToast === 'function') window.showToast(successMsg, 'success');
      await renderStory(storyId);
    } else {
      let detail = '';
      try { detail = (await r.json()).error || ''; } catch (_) {}
      if (typeof window.showToast === 'function') window.showToast(detail || '操作に失敗しました', 'error');
      clearSavingButton(btn);
    }
  } catch (_) {
    clearSavingButton(btn);
  }
}

async function resetStoryProgress(btn, storyId, storyTitle) {
  const label = storyTitle || storyId;
  const confirmed = window.confirm(
    `「${label}」の会話ログ、公開ログ、シーンスクリプト、記憶、関係値、キャラ現在状態、章進行を初期化します。`
    + 'キャラ設定・場所設定・ストーリー設定は残ります。この操作は元に戻せません。実行しますか?'
  );
  if (!confirmed) return;

  setSavingButton(btn, '初期化中…');
  try {
    const response = await api(`/stories/${storyId}/runtime/reset`, {
      method: 'POST',
      body: JSON.stringify({ confirm: true })
    });
    if (response.ok) {
      const payload = await response.json();
      const data = payload.data || {};
      const remote = data.remote_public_logs_reset || {};
      const remoteDeleted = remote.status === 'ok' && typeof remote.deleted === 'number'
        ? `リモート公開ログ ${remote.deleted} 件を削除しました`
        : '';
      const remoteSkipped = remote.status === 'skipped'
        ? 'リモート公開ログは未設定です'
        : '';
      const deleted = typeof data.public_logs_deleted === 'number'
        ? [`公開ログ ${data.public_logs_deleted} 件を削除しました`, remoteDeleted, remoteSkipped]
          .filter(Boolean)
          .join(' / ')
        : '進行ログを初期化しました';
      if (typeof window.showToast === 'function') window.showToast(deleted, 'success');
      await renderStory(storyId);
    } else {
      let detail = '';
      try {
        const payload = await response.json();
        detail = payload.error && payload.error.message ? payload.error.message : '';
      } catch (_) {}
      if (typeof window.showToast === 'function') window.showToast(detail || '初期化に失敗しました', 'error');
      clearSavingButton(btn);
    }
  } catch (_) {
    clearSavingButton(btn);
  }
}

async function renderStory(storyId) {
  const [storyResp, liveResp] = await Promise.all([
    api(`/stories/${storyId}`),
    api(`/stories/${storyId}/live`)
  ]);
  if (!storyResp.ok) {
    const main = layout(`ストーリー: ${storyId}`);
    const err = element('div', 'ストーリーが見つかりません。');
    err.className = 'notice notice-error';
    main.append(err);
    return;
  }
  const storyPayload = await storyResp.json();
  const livePayload = liveResp.ok ? await liveResp.json() : { data: { logs: [] } };
  const data = storyPayload.data || {};
  const story = data.story || {};
  const pub = data.publication || { visibility: 'draft' };
  const runtimeState = data.runtime_state || 'stopped';
  const logCount = ((livePayload.data || {}).logs || []).length;
  const isRunning = runtimeState === 'running';

  const main = layout(story.title || storyId, { subtitle: storyId });

  // ── Hero card ──
  const hero = element('section');
  hero.className = 'story-detail-hero card card-padded';

  const heroTop = element('div');
  heroTop.className = 'story-detail-hero-top';

  const pills = element('div');
  pills.className = 'story-detail-pills';
  const runtimePill = element('span', isRunning ? '稼働中' : '停止中');
  runtimePill.className = 'pill ' + (isRunning ? 'pill-running' : 'pill-stopped');
  const visPill = element('span', pub.visibility === 'public' ? '公開中' : '非公開');
  visPill.className = 'pill ' + (pub.visibility === 'public' ? 'pill-info' : 'pill-stopped');
  pills.append(runtimePill, visPill);

  const heroActions = element('div');
  heroActions.className = 'story-detail-hero-actions';
  const viewerBtn = link(`/admin/viewer/${storyId}`, 'Viewer');
  viewerBtn.className = 'btn btn-secondary';

  const startBtn = storyRuntimeButton('起動', 'btn-primary', isRunning,
    (btn) => storyRuntimeAction(btn, storyId, { path: '/runtime', method: 'PUT', body: { desired_state: 'running' } }, '起動しました'));
  const stopBtn = storyRuntimeButton('停止', 'btn-danger', !isRunning,
    (btn) => storyRuntimeAction(btn, storyId, { path: '/runtime', method: 'PUT', body: { desired_state: 'stopped' } }, '停止しました'));
  const restartBtn = storyRuntimeButton('再起動', 'btn-ghost', !isRunning,
    (btn) => storyRuntimeAction(btn, storyId, { path: '/runtime/restart', method: 'POST' }, '再起動しました'));
  const resetProgressBtn = storyRuntimeButton('ストーリー進行を初期化', 'btn-danger', isRunning,
    (btn) => resetStoryProgress(btn, storyId, story.title));

  heroActions.append(viewerBtn, startBtn, stopBtn, restartBtn, resetProgressBtn);
  heroTop.append(pills, heroActions);

  const metaGrid = element('div');
  metaGrid.className = 'story-detail-meta-grid';
  [
    ['最新ログ', `${logCount} 件`],
    ['LLM', story.llm_provider || '-'],
    ['LLM Model', story.llm_model || '-'],
  ].forEach(([label, val]) => {
    const item = element('div');
    item.className = 'story-detail-meta-item';
    const l = element('div', label);
    l.className = 'story-detail-meta-label';
    const v = element('div', val);
    v.className = 'story-detail-meta-value';
    item.append(l, v);
    metaGrid.append(item);
  });

  hero.append(heroTop, metaGrid);

  // ── Editor links ──
  const editorSection = element('section');
  editorSection.className = 'story-detail-editors card card-padded';
  const editorHeading = element('h2', '編集');
  editorHeading.className = 'story-detail-section-title';
  editorSection.append(editorHeading);

  const editorGrid = element('div');
  editorGrid.className = 'story-editor-grid';
  [
    [`/admin/characters/${storyId}`, 'キャラクター編集'],
    [`/admin/places/${storyId}`, '場所編集'],
    [`/admin/story-settings/${storyId}`, 'Story設定'],
    [`/admin/event-anomalies/${storyId}`, 'イベント/異変'],
    [`/admin/directors/${storyId}`, '監督管理'],
    [`/admin/chapter-definitions/${storyId}`, '章定義'],
    [`/admin/chapters/${storyId}`, '章管理'],
    [`/admin/conversation-patterns/${storyId}`, '会話パターン'],
    [`/admin/scene-scripts/${storyId}`, 'シーンスクリプト'],
    [`/admin/onboarding/${storyId}`, 'Story複製'],
  ].forEach(([href, label]) => {
    const a = link(href, label);
    a.className = 'btn btn-secondary story-editor-link';
    editorGrid.append(a);
  });
  editorSection.append(editorGrid);

  // ── Publication & archive ──
  const pubSection = element('section');
  pubSection.className = 'story-detail-pub card card-padded';
  const pubHeading = element('h2', '公開管理');
  pubHeading.className = 'story-detail-section-title';
  pubSection.append(pubHeading);

  const pubActions = element('div');
  pubActions.className = 'story-pub-actions';

  const publishBtn = storyRuntimeButton('公開する', 'btn-primary', pub.visibility === 'public',
    (btn) => storyRuntimeAction(btn, storyId, { path: '/publication', method: 'PUT', body: { visibility: 'public' } }, '公開しました'));
  const draftBtn = storyRuntimeButton('非公開にする', 'btn-danger', pub.visibility !== 'public',
    (btn) => storyRuntimeAction(btn, storyId, { path: '/publication', method: 'PUT', body: { visibility: 'draft' } }, '非公開にしました'));
  const rebuildBtn = storyRuntimeButton('archive 再生成', 'btn-secondary', false,
    (btn) => storyRuntimeAction(btn, storyId, { path: '/archive-builds', method: 'POST' }, 'archive を再生成しました'));
  const zipLink = link(`${apiBaseUrl}/stories/${storyId}/downloads/story.zip`, 'ZIP ダウンロード');
  zipLink.className = 'btn btn-ghost';

  pubActions.append(publishBtn, draftBtn, rebuildBtn, zipLink);
  pubSection.append(pubActions);

  // ── 時事モード（per-story） ──
  const newsModeSection = element('section');
  newsModeSection.className = 'story-detail-pub card card-padded';
  const newsModeHeading = element('h2', '時事モード');
  newsModeHeading.className = 'story-detail-section-title';
  newsModeSection.append(newsModeHeading);

  const newsModeBody = element('div');
  newsModeBody.className = 'story-pub-actions';
  newsModeBody.textContent = '読み込み中...';
  newsModeSection.append(newsModeBody);

  // 非同期で per-story 設定をロードして UI を構築
  api(`/stories/${storyId}/news-mode`).then(async (resp) => {
    if (!resp.ok) { newsModeBody.textContent = '設定の読み込みに失敗しました。'; return; }
    const payload = await resp.json();
    const current = payload.data || { enabled: false, intensity: 'low' };

    newsModeBody.innerHTML = '';

    // enabled トグル
    const row1 = element('div');
    row1.className = 'form-row';
    const enabledLabel = element('label', '有効');
    enabledLabel.style.marginRight = '8px';
    const enabledChk = element('input');
    enabledChk.type = 'checkbox';
    enabledChk.checked = Boolean(current.enabled);
    enabledChk.style.marginRight = '16px';
    row1.append(enabledLabel, enabledChk);

    // intensity ドロップダウン
    const intensityLabel = element('label', '頻度');
    intensityLabel.style.marginRight = '8px';
    const intensitySel = element('select');
    intensitySel.className = 'select-input';
    [
      ['off', 'オフ'],
      ['low', '控えめ（3%）'],
      ['medium', '中庸（7%）'],
      ['high', '派手目（12%）'],
      ['rate20', 'かなり高め（20%）'],
      ['rate30', '高頻度（30%）'],
      ['rate40', '超高頻度（40%）'],
      ['rate50', 'ほぼ毎回狙う（50%）'],
      ['rate80', '最大頻度（80%）🔥'],
    ].forEach(([val, label]) => {
      const opt = element('option', label);
      opt.value = val;
      if (val === (current.intensity || 'low')) opt.selected = true;
      intensitySel.append(opt);
    });
    row1.append(intensityLabel, intensitySel);

    // 保存ボタン
    const saveBtn = element('button', '保存');
    saveBtn.className = 'btn btn-primary';
    saveBtn.style.marginLeft = '16px';
    saveBtn.addEventListener('click', async () => {
      saveBtn.disabled = true;
      saveBtn.textContent = '保存中...';
      const res = await api(`/stories/${storyId}/news-mode`, {
        method: 'PUT',
        body: JSON.stringify({ enabled: enabledChk.checked, intensity: intensitySel.value }),
      });
      if (res.ok) {
        saveBtn.textContent = '保存しました ✓';
        setTimeout(() => { saveBtn.textContent = '保存'; saveBtn.disabled = false; }, 1800);
      } else {
        saveBtn.textContent = '失敗';
        saveBtn.disabled = false;
      }
    });
    row1.append(saveBtn);

    // RSS 管理ページへのリンク
    const rssLink = link('/admin/news-mode', 'RSS フィード管理 →');
    rssLink.className = 'btn btn-ghost';
    rssLink.style.marginLeft = '8px';

    newsModeBody.append(row1, rssLink);

    // ── タグフィルタ row ──
    let knownTagsForFilter = [];
    try {
      const tagsResp = await api('/news/tags');
      if (tagsResp.ok) {
        const tagsPayload = await tagsResp.json();
        knownTagsForFilter = (tagsPayload.data && tagsPayload.data.tags) || [];
      }
    } catch (_) { /* tags API 失敗は無視、autocomplete なしで続行 */ }

    let currentFilterTags = [];
    try {
      const filterResp = await api(`/stories/${storyId}/news-tag-filter`);
      if (filterResp.ok) {
        const filterPayload = await filterResp.json();
        currentFilterTags = (filterPayload.data && filterPayload.data.tags) || [];
      }
    } catch (_) { /* 失敗は無視して空で開始 */ }

    const filterRow = element('div');
    filterRow.className = 'form-row form-row-vertical';
    filterRow.style.marginTop = '12px';

    const filterTitle = element('div', 'タグフィルタ（OR）');
    filterTitle.style.fontWeight = '600';
    filterTitle.style.fontSize = '0.82rem';

    const filterHelp = element('div', '指定したタグのどれかに一致する記事のみを使用します。空のままにすると全記事が対象です。');
    filterHelp.className = 'form-help';

    const tagInput = createTagChipInput({
      initialTags: currentFilterTags,
      suggestions: knownTagsForFilter,
      placeholder: 'タグを入力して Enter…',
    });

    const filterSaveBtn = storyRuntimeButton('保存');
    filterSaveBtn.addEventListener('click', async () => {
      filterSaveBtn.disabled = true;
      filterSaveBtn.textContent = '保存中...';
      const res = await api(`/stories/${storyId}/news-tag-filter`, {
        method: 'PUT',
        body: JSON.stringify({ tags: tagInput.getTags() }),
      });
      if (res.ok) {
        filterSaveBtn.textContent = '保存しました ✓';
        setTimeout(() => { filterSaveBtn.textContent = '保存'; filterSaveBtn.disabled = false; }, 1800);
      } else {
        filterSaveBtn.textContent = '失敗';
        filterSaveBtn.disabled = false;
      }
    });

    filterRow.append(filterTitle, filterHelp, tagInput.element, filterSaveBtn);
    newsModeBody.append(filterRow);
  }).catch(() => { newsModeBody.textContent = '設定の読み込みに失敗しました。'; });

  // ── 発話の長さ設定（文字数・文数）───────────────────────────────────────────
  const utteranceSection = element('section');
  utteranceSection.className = 'story-detail-pub card card-padded';
  const utteranceHeading = element('h2', '発話の長さ');
  utteranceHeading.className = 'story-detail-section-title';
  utteranceSection.append(utteranceHeading);

  const utteranceBody = element('div');
  utteranceBody.className = 'story-pub-actions';
  utteranceBody.textContent = '読み込み中...';
  utteranceSection.append(utteranceBody);

  api(`/stories/${storyId}/utterance-settings`).then(async (resp) => {
    if (!resp.ok) { utteranceBody.textContent = '設定の読み込みに失敗しました。'; return; }
    const payload = await resp.json();
    const current = payload.data || { max_chars: 180, max_sentences: 3 };

    utteranceBody.innerHTML = '';

    // ── 文字数 row ──
    const row = element('div');
    row.className = 'form-row';

    const charLabel = element('label', '最大文字数');
    charLabel.className = 'form-label';
    const charSel = element('select');
    charSel.className = 'form-select';
    [
      [180, '標準（180字）'],
      [240, '長め（240字）'],
      [320, 'さらに長め（320字）'],
    ].forEach(([val, label]) => {
      const opt = element('option', label);
      opt.value = String(val);
      if (val === (current.max_chars || 180)) opt.selected = true;
      charSel.append(opt);
    });
    row.append(charLabel, charSel);

    const saveBtn = storyRuntimeButton('保存');
    saveBtn.addEventListener('click', async () => {
      saveBtn.disabled = true;
      saveBtn.textContent = '保存中...';
      const res = await api(`/stories/${storyId}/utterance-settings`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ max_chars: parseInt(charSel.value, 10) }),
      });
      if (res.ok) {
        saveBtn.textContent = '保存しました ✓';
        setTimeout(() => { saveBtn.textContent = '保存'; saveBtn.disabled = false; }, 1800);
      } else {
        saveBtn.textContent = '失敗';
        saveBtn.disabled = false;
      }
    });
    row.append(saveBtn);
    utteranceBody.append(row);

    // ── 文数 row ──
    const sentRow = element('div');
    sentRow.className = 'form-row';

    const sentLabel = element('label', '最大文数');
    sentLabel.className = 'form-label';
    const sentSel = element('select');
    sentSel.className = 'form-select';
    [
      [3, '標準（3文まで）'],
      [4, '長め（4文まで）'],
      [5, 'さらに長め（5文まで）'],
    ].forEach(([val, label]) => {
      const opt = element('option', label);
      opt.value = String(val);
      if (val === (current.max_sentences || 3)) opt.selected = true;
      sentSel.append(opt);
    });
    sentRow.append(sentLabel, sentSel);

    const sentSaveBtn = storyRuntimeButton('保存');
    sentSaveBtn.addEventListener('click', async () => {
      sentSaveBtn.disabled = true;
      sentSaveBtn.textContent = '保存中...';
      const res = await api(`/stories/${storyId}/utterance-settings`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ max_sentences: parseInt(sentSel.value, 10) }),
      });
      if (res.ok) {
        sentSaveBtn.textContent = '保存しました ✓';
        setTimeout(() => { sentSaveBtn.textContent = '保存'; sentSaveBtn.disabled = false; }, 1800);
      } else {
        sentSaveBtn.textContent = '失敗';
        sentSaveBtn.disabled = false;
      }
    });
    sentRow.append(sentSaveBtn);
    utteranceBody.append(sentRow);
  }).catch(() => { utteranceBody.textContent = '設定の読み込みに失敗しました。'; });

  // ── ストーリー BGM ──────────────────────────────────────────────────
  const bgmSection = element('section');
  bgmSection.className = 'story-detail-pub card card-padded';
  const bgmHeading = element('h2', 'ストーリー BGM');
  bgmHeading.className = 'story-detail-section-title';
  bgmSection.append(bgmHeading);

  const bgmBody = element('div');
  bgmBody.className = 'story-pub-actions';
  bgmBody.textContent = '読み込み中...';
  bgmSection.append(bgmBody);

  api(`/stories/${storyId}/bgm-config`).then(async (resp) => {
    if (!resp.ok) { bgmBody.textContent = '設定の読み込みに失敗しました。'; return; }
    const bgmData = (await resp.json()).data || {};
    bgmBody.innerHTML = '';

    const hint = element('div', '未設定の場合はグローバルデフォルト BGM（pocketrole_bgm.mp3）が使用されます。');
    hint.className = 'form-help';
    bgmBody.append(hint);

    const statusBadge = element('div');
    statusBadge.className = `story-bgm-status ${bgmData.has_story_bgm ? 'has-file' : 'no-file'}`;
    statusBadge.textContent = bgmData.has_story_bgm ? 'カスタム BGM 設定済み' : 'グローバルデフォルト使用中';
    bgmBody.append(statusBadge);

    const storyBgmPreviewUrl = resolveStoryBgmPreviewUrl(storyId, bgmData);
    if (bgmData.has_story_bgm && storyBgmPreviewUrl) {
      const preview = document.createElement('audio');
      preview.controls = true;
      preview.className = 'story-bgm-preview';
      preview.src = storyBgmPreviewUrl;
      bgmBody.append(preview);
    }

    const enabledRow = element('div');
    enabledRow.className = 'story-bgm-upload-row';
    const enabledLabel = element('label');
    enabledLabel.className = 'form-label';
    const enabledCheck = document.createElement('input');
    enabledCheck.type = 'checkbox';
    enabledCheck.checked = bgmData.enabled !== false;
    enabledLabel.append(enabledCheck, document.createTextNode(' BGM を有効にする'));
    enabledRow.append(enabledLabel);
    bgmBody.append(enabledRow);

    const uploadRow = element('div');
    uploadRow.className = 'story-bgm-upload-row';
    const uploadLabel = element('label', 'BGM ファイルを変更（MP3 / M4A / OGG）');
    uploadLabel.className = 'form-label';
    const bgmFileInput = document.createElement('input');
    bgmFileInput.type = 'file';
    bgmFileInput.accept = 'audio/mpeg,audio/mp4,audio/ogg,.mp3,.m4a,.ogg';
    bgmFileInput.className = 'place-img-file-input';
    uploadRow.append(uploadLabel, bgmFileInput);
    bgmBody.append(uploadRow);

    const saveBtnRow = element('div');
    saveBtnRow.className = 'story-bgm-upload-row';

    if (bgmData.has_story_bgm) {
      const clearBtn = storyRuntimeButton('カスタム BGM を削除');
      clearBtn.className += ' story-bgm-clear-btn';
      clearBtn.addEventListener('click', async () => {
        if (!confirm('カスタム BGM を削除してグローバルデフォルトに戻しますか？')) return;
        clearBtn.disabled = true;
        clearBtn.textContent = '削除中...';
        const body = new FormData();
        body.append('config', JSON.stringify({ clear_story_bgm: true }));
        const res = await api(`/stories/${storyId}/bgm-config`, { method: 'PUT', body });
        if (res.ok) {
          if (typeof window.showToast === 'function') window.showToast('BGM を削除しました', 'success');
          bgmBody.textContent = '読み込み中...';
          api(`/stories/${storyId}/bgm-config`).then(async (r2) => {
            if (!r2.ok) { bgmBody.textContent = '再読み込みに失敗しました。'; return; }
            bgmBody.innerHTML = '';
            bgmBody.textContent = 'ページをリロードして確認してください。';
          });
        } else {
          clearBtn.textContent = '失敗';
          clearBtn.disabled = false;
        }
      });
      saveBtnRow.append(clearBtn);
    }

    const saveBtn = storyRuntimeButton('保存');
    saveBtn.addEventListener('click', async () => {
      saveBtn.disabled = true;
      saveBtn.textContent = '保存中...';
      const body = new FormData();
      body.append('config', JSON.stringify({ enabled: enabledCheck.checked }));
      const file = bgmFileInput.files && bgmFileInput.files[0];
      if (file) body.append('story_bgm', file);
      const res = await api(`/stories/${storyId}/bgm-config`, { method: 'PUT', body });
      if (res.ok) {
        saveBtn.textContent = '保存しました ✓';
        const resData = (await res.json()).data || {};
        statusBadge.className = `story-bgm-status ${resData.has_story_bgm ? 'has-file' : 'no-file'}`;
        statusBadge.textContent = resData.has_story_bgm ? 'カスタム BGM 設定済み' : 'グローバルデフォルト使用中';
        bgmFileInput.value = '';
        setTimeout(() => { saveBtn.textContent = '保存'; saveBtn.disabled = false; }, 1800);
      } else {
        saveBtn.textContent = '失敗';
        saveBtn.disabled = false;
      }
    });
    saveBtnRow.append(saveBtn);
    bgmBody.append(saveBtnRow);
  }).catch(() => { bgmBody.textContent = '設定の読み込みに失敗しました。'; });

  // ── ストーリー別 Web 投稿先 ──────────────────────────────────────────────
  const storyWebPostSection = element('section');
  storyWebPostSection.className = 'story-detail-pub card card-padded';
  const swpHeading = element('h2', 'Web 投稿先');
  swpHeading.className = 'story-detail-section-title';
  storyWebPostSection.append(swpHeading);

  const swpBody = element('div');
  swpBody.className = 'story-pub-actions';
  swpBody.textContent = '読み込み中...';
  storyWebPostSection.append(swpBody);

  api(`/stories/${storyId}/web-post`).then(async (resp) => {
    const swpData = resp.ok ? ((await resp.json()).data || {}) : {};
    swpBody.innerHTML = '';

    const hint = element('div', '空欄の場合は「設定」ページのデフォルト投稿先を使用します。');
    hint.className = 'form-help';
    swpBody.append(hint);

    const urlRow = element('div');
    urlRow.className = 'form-row form-row-vertical';
    urlRow.style.marginTop = '8px';
    const urlLabel = element('label', 'Receiver URL（上書き）');
    urlLabel.className = 'form-label';
    const urlInput = element('input');
    urlInput.type = 'url';
    urlInput.className = 'text-input';
    urlInput.value = swpData.receiver_url || '';
    urlInput.placeholder = 'https://example.com/receiver.php（空欄でデフォルト使用）';
    urlRow.append(urlLabel, urlInput);
    swpBody.append(urlRow);

    const tokenRow = createTokenField(
      'Auth Token（上書き）',
      '',
      '変更する場合のみ入力',
      Boolean(swpData.has_auth_token),
    );
    swpBody.append(tokenRow.element);

    const saveBtn = storyRuntimeButton('保存');
    saveBtn.addEventListener('click', async () => {
      saveBtn.disabled = true;
      saveBtn.textContent = '保存中...';
      const res = await api(`/stories/${storyId}/web-post`, {
        method: 'PUT',
        body: JSON.stringify(Object.assign({
          receiver_url: urlInput.value.trim(),
          clear_auth_token: tokenRow.shouldClear(),
        }, tokenRow.getValue() ? { auth_token: tokenRow.getValue() } : {})),
      });
      if (res.ok) {
        saveBtn.textContent = '保存しました ✓';
        setTimeout(() => { saveBtn.textContent = '保存'; saveBtn.disabled = false; }, 1800);
      } else {
        saveBtn.textContent = '失敗';
        saveBtn.disabled = false;
      }
    });
    swpBody.append(saveBtn);
  }).catch(() => { swpBody.textContent = '設定の読み込みに失敗しました。'; });

  // ── 危険ゾーン ─────────────────────────────────────────────────────
  const dangerSection = element('section');
  dangerSection.className = 'story-detail-pub card card-padded danger-zone';
  const dangerHeading = element('h2', '危険ゾーン');
  dangerHeading.className = 'story-detail-section-title danger-zone-title';
  const dangerBody = element('div');
  dangerBody.className = 'story-pub-actions';
  const deleteStoryBtn = element('button', 'ストーリーを完全削除');
  deleteStoryBtn.className = 'btn btn-danger';
  deleteStoryBtn.addEventListener('click', () => openDeleteModal(storyId));
  dangerBody.append(deleteStoryBtn);
  dangerSection.append(dangerHeading, dangerBody);

  main.append(hero, editorSection, newsModeSection, utteranceSection,
              bgmSection, storyWebPostSection, pubSection, dangerSection);
}

function makeExpressionImage(storyId, log, charactersById) {
  const character = charactersById.get(log.char_id) || {};
  const expressions = Array.isArray(character.expressions_available)
    ? character.expressions_available
    : [];
  const imageBaseUrl = character.image_base_url || `/assets/character_images/${storyId}/${log.char_id}`;
  const candidates = [];
  if (log.expression) {
    candidates.push(log.expression);
  }
  if (!candidates.includes('neutral')) {
    candidates.push('neutral');
  }
  if (Array.isArray(expressions)) {
    expressions.forEach((expression) => {
      if (!candidates.includes(expression)) {
        candidates.push(expression);
      }
    });
  }

  const wrap = element('div');
  wrap.className = 'viewer-avatar';
  const placeholder = element('div', (log.speaker_name || log.char_id || '?').slice(0, 1));
  placeholder.className = 'viewer-avatar-fallback';
  wrap.append(placeholder);

  const img = element('img');
  img.alt = `${log.speaker_name || log.char_id} expression`;
  let candidateIndex = 0;
  const tryNext = () => {
    if (candidateIndex >= candidates.length) {
      img.remove();
      return;
    }
    img.src = `${imageBaseUrl}/${candidates[candidateIndex]}.png`;
    candidateIndex += 1;
  };
  img.addEventListener('load', () => {
    wrap.classList.add('has-image');
  });
  img.addEventListener('error', tryNext);
  wrap.prepend(img);
  tryNext();
  return wrap;
}

function renderViewerMeta(label, value) {
  const item = element('div');
  item.className = 'viewer-meta-item';
  item.append(element('span', label), element('strong', value || '-'));
  return item;
}

function renderOnboardingNotice(title, items) {
  const box = element('section');
  box.className = 'onboarding-notice';
  box.append(element('h3', title));
  const list = element('ul');
  items.forEach((item) => {
    const row = element('li', item);
    list.append(row);
  });
  box.append(list);
  return box;
}

function renderOnboardingError(status, message) {
  status.className = 'onboarding-status onboarding-status-error';
  status.innerHTML = '';
  status.append(element('strong', message || '複製に失敗しました。'));
  const list = element('ul');
  [
    'story_id が既存 story と重複していないか確認してください。',
    'story_id は小文字英数字と underscore だけにしてください。',
    'template story の world_config.yaml / characters.yaml が壊れていないか確認してください。',
    '詳細編集や既存 story 更新は YAML と update_story で行ってください。'
  ].forEach((text) => {
    list.append(element('li', text));
  });
  status.append(list);
}

function renderCharacterEditError(status, message) {
  status.className = 'onboarding-status onboarding-status-error';
  status.innerHTML = '';
  status.append(element('strong', message || '保存に失敗しました。'));
  const list = element('ul');
  [
    'story が停止中か確認してください。',
    'サンプル本体 ankoku_gakuen は直接編集せず、複製後の story を編集してください。',
    '表示名が空になっていないか確認してください。',
    'characters.yaml が validate_story を通る状態か確認してください。'
  ].forEach((text) => {
    list.append(element('li', text));
  });
  status.append(list);
}

function renderPlaceEditError(status, message) {
  status.className = 'onboarding-status onboarding-status-error';
  status.innerHTML = '';
  status.append(element('strong', message || '保存に失敗しました。'));
  const list = element('ul');
  [
    'story が停止中か確認してください。',
    'サンプル本体 ankoku_gakuen は直接編集せず、複製後の story を編集してください。',
    '場所名と zone が空になっていないか確認してください。',
    'zone は validate_story が許可する値にしてください。'
  ].forEach((text) => {
    list.append(element('li', text));
  });
  status.append(list);
}

function renderStorySettingsError(status, message) {
  status.className = 'onboarding-status onboarding-status-error';
  status.innerHTML = '';
  status.append(element('strong', message || '保存に失敗しました。'));
  const list = element('ul');
  [
    'story が停止中か確認してください。',
    'サンプル本体 ankoku_gakuen は直接編集せず、複製後の story を編集してください。',
    'タイトルが空になっていないか確認してください。',
    'world_config.yaml が validate_story を通る状態か確認してください。'
  ].forEach((text) => {
    list.append(element('li', text));
  });
  status.append(list);
}

function renderEventAnomalyEditError(status, message) {
  status.className = 'onboarding-status onboarding-status-error';
  status.innerHTML = '';
  status.append(element('strong', message || '保存に失敗しました。'));
  const list = element('ul');
  [
    'story が停止中か確認してください。',
    'サンプル本体 ankoku_gakuen は直接編集せず、複製後の story を編集してください。',
    'event_date は MM-DD、duration_days は正の整数にしてください。',
    'condition_json と emotion_impact は JSON object として入力してください。'
  ].forEach((text) => {
    list.append(element('li', text));
  });
  status.append(list);
}

function renderChapterError(status, message) {
  status.className = 'onboarding-status onboarding-status-error';
  status.innerHTML = '';
  status.append(element('strong', message || 'chapter 操作に失敗しました。'));
  const list = element('ul');
  [
    'story が起動中か停止中か、操作に必要な状態を確認してください。',
    '章案生成は chapter_generator が有効な story で使用してください。',
    '章案を承認すると pending chapter になり、有効化すると active chapter に切り替わります。',
    '同時に active にできる chapter は 1 つです。'
  ].forEach((text) => {
    list.append(element('li', text));
  });
  status.append(list);
}

function renderDirectorPersonaEditError(status, message) {
  status.className = 'onboarding-status onboarding-status-error';
  status.innerHTML = '';
  status.append(element('strong', message || '保存に失敗しました。'));
  const list = element('ul');
  [
    'story が停止中か確認してください。',
    'サンプル本体 ankoku_gakuen は直接編集せず、複製後の story を編集してください。',
    'persona_id は小文字英数字と underscore にしてください。',
    'aesthetic は JSON object、values と traits は1行1項目で入力してください。'
  ].forEach((text) => {
    list.append(element('li', text));
  });
  status.append(list);
}

function renderChapterDefinitionEditError(status, message) {
  status.className = 'onboarding-status onboarding-status-error';
  status.innerHTML = '';
  status.append(element('strong', message || '保存に失敗しました。'));
  const list = element('ul');
  [
    'story が停止中か確認してください。',
    'サンプル本体 ankoku_gakuen は直接編集せず、複製後の story を編集してください。',
    'chapter_id は小文字英数字と underscore にしてください。',
    'beats は1章につき1つ以上、events は JSON array で入力してください。'
  ].forEach((text) => {
    list.append(element('li', text));
  });
  status.append(list);
}

const VIEWER_BUBBLE_VARIANTS = 6;

function viewerBubbleVariant(charId) {
  let hash = 0;
  const s = String(charId || '');
  for (let i = 0; i < s.length; i += 1) {
    hash = (hash * 31 + s.charCodeAt(i)) | 0;
  }
  return Math.abs(hash) % VIEWER_BUBBLE_VARIANTS;
}

function attachViewerKeyboard(storyId) {
  if (viewerKeyHandler !== null) return;
  viewerKeyHandler = (e) => {
    const target = e.target;
    if (target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.isContentEditable)) return;
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === 'j' || e.key === 'ArrowDown') {
      e.preventDefault();
      window.scrollBy({ top: 240, behavior: 'smooth' });
    } else if (e.key === 'k' || e.key === 'ArrowUp') {
      e.preventDefault();
      window.scrollBy({ top: -240, behavior: 'smooth' });
    } else if (e.key === 'r') {
      e.preventDefault();
      refreshViewerUnlessMapActive(storyId);
    } else if (e.key === 'f') {
      e.preventDefault();
      setViewerAutoFollow(!viewerAutoFollow, { scroll: true, toast: true });
    }
  };
  window.addEventListener('keydown', viewerKeyHandler);
}

function isViewerMapReplayActive() {
  return viewerActiveTab === 'map';
}

function refreshViewerUnlessMapActive(storyId, options = {}) {
  if (isViewerMapReplayActive()) {
    if (!options.silent && typeof window.showToast === 'function') {
      window.showToast('MAP再生中はタイムラインへ戻ってから更新してください', 'info', { duration: 2200 });
    }
    return;
  }
  renderViewer(storyId).catch(() => {});
}

function setViewerAutoFollow(enabled, options = {}) {
  viewerAutoFollow = enabled;
  updateViewerFollowState();
  if (viewerAutoFollow && options.scroll && !isViewerMapReplayActive()) {
    window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' });
  }
  if (options.toast && typeof window.showToast === 'function') {
    window.showToast(
      viewerAutoFollow ? '最新追従を有効にしました' : '最新追従を無効にしました',
      'info',
      { duration: 1800 }
    );
  }
}

function updateViewerFollowState() {
  document.querySelectorAll('.viewer-follow-banner').forEach((banner) => {
    banner.dataset.active = viewerAutoFollow ? '1' : '0';
  });
  document.querySelectorAll('.viewer-follow-toggle, .viewer-follow-inline-toggle').forEach((btn) => {
    btn.textContent = viewerAutoFollow ? '追従: ON' : '追従: OFF';
    btn.classList.toggle('btn-primary', viewerAutoFollow);
    btn.classList.toggle('btn-secondary', !viewerAutoFollow);
  });
}

function buildViewerFollowBanner(placement) {
  const banner = element('div');
  banner.className = 'viewer-follow-banner viewer-follow-banner-' + placement;
  if (placement === 'bottom') {
    banner.classList.add('viewer-follow-banner-bottom');
  }
  banner.dataset.active = viewerAutoFollow ? '1' : '0';

  const dot = element('span');
  dot.className = 'viewer-follow-dot';

  const text = element('span', '最新メッセージに追従中');
  text.className = 'viewer-follow-text';

  const hint = element('span');
  hint.className = 'viewer-follow-hint';
  const fKey = element('kbd', 'f');
  fKey.className = 'kbd';
  const rKey = element('kbd', 'r');
  rKey.className = 'kbd';
  const jKey = element('kbd', 'j');
  jKey.className = 'kbd';
  const kKey = element('kbd', 'k');
  kKey.className = 'kbd';
  hint.append(
    fKey,
    document.createTextNode(' で切替・'),
    rKey,
    document.createTextNode(' で再読み込み・'),
    jKey,
    document.createTextNode(' / '),
    kKey,
    document.createTextNode(' でスクロール')
  );

  const toggle = element('button', viewerAutoFollow ? '追従: ON' : '追従: OFF');
  toggle.type = 'button';
  toggle.className = 'btn btn-sm viewer-follow-inline-toggle ' + (viewerAutoFollow ? 'btn-primary' : 'btn-secondary');
  toggle.addEventListener('click', () => {
    setViewerAutoFollow(!viewerAutoFollow, { scroll: true, toast: true });
  });

  banner.append(dot, text, hint, toggle);
  return banner;
}

function buildViewerChatRow(storyId, log, charactersById) {
  const isNarrator = log.char_id === '_narrator' || log.msg_type === 'narration';
  const row = element('article');
  row.className = 'viewer-chat-row';
  if (isNarrator) {
    row.classList.add('viewer-chat-row-narrator');
    const inner = element('div');
    inner.className = 'viewer-chat-narrator';
    const text = element('p');
    text.innerHTML = viewerLinkify(viewerEscapeHtml(log.message || ''));
    text.className = 'viewer-chat-narrator-text';
    const meta = element('div', `turn ${log.turn_number || '-'} · ${log.sim_datetime || '-'}`);
    meta.className = 'viewer-chat-narrator-meta';
    inner.append(text, meta);
    row.append(inner);
    return row;
  }

  const variant = viewerBubbleVariant(log.char_id);
  row.dataset.variant = String(variant);

  const avatar = makeExpressionImage(storyId, log, charactersById);
  avatar.classList.add('viewer-chat-avatar');
  row.append(avatar);

  const content = element('div');
  content.className = 'viewer-chat-content';

  const head = element('div');
  head.className = 'viewer-chat-head';
  const speaker = element('strong', log.speaker_name || log.char_id || '');
  speaker.className = 'viewer-chat-speaker';
  const metaText = `${log.place_label || log.place_id || '—'} · turn ${log.turn_number || '-'} · ${log.sim_datetime || '-'}`;
  const metaEl = element('span', metaText);
  metaEl.className = 'viewer-chat-meta';
  head.append(speaker, metaEl);

  const bubble = element('div');
  bubble.className = `viewer-chat-bubble viewer-chat-bubble-v${variant}`;
  bubble.innerHTML = viewerLinkify(viewerEscapeHtml(log.message || ''));

  const tail = element('div');
  tail.className = 'viewer-chat-tail';
  if (log.expression) {
    const exp = element('span', `expression: ${log.expression}`);
    tail.append(exp);
  }

  content.append(head, bubble);
  if (tail.children.length > 0) content.append(tail);
  row.append(content);
  return row;
}

async function renderViewer(storyId) {
  if (viewerMapIframeStoryId && viewerMapIframeStoryId !== storyId) {
    viewerActiveTab = 'timeline';
    viewerMapIframe = null;
    viewerMapIframeStoryId = null;
  }
  let reusableMapIframe = null;
  if (viewerActiveTab === 'map' && viewerMapIframeStoryId === storyId) {
    reusableMapIframe = viewerMapIframe || document.querySelector('.viewer-map-iframe');
  }
  const response = await api(`/stories/${storyId}/viewer`);
  if (!response.ok) {
    const main = layout(`viewer: ${storyId}`);
    const err = element('div', 'viewer の読み込みに失敗しました。story が存在し、viewer payload が取得できるか確認してください。');
    err.className = 'notice notice-error';
    main.append(err);
    return;
  }
  const payload = await response.json();
  const data = payload.data;
  const main = layout(data.story.title || `viewer: ${storyId}`, { subtitle: storyId });

  // ── Hero card ──
  const hero = element('section');
  hero.className = 'viewer-hero card card-padded';

  const heroTop = element('div');
  heroTop.className = 'viewer-hero-top';

  const pills = element('div');
  pills.className = 'viewer-hero-pills';
  const isRunning = (data.runtime && data.runtime.state) === 'running';
  const runtimePill = element('span', isRunning ? '稼働中' : '停止中');
  runtimePill.className = 'pill ' + (isRunning ? 'pill-running' : 'pill-stopped');
  const visPill = element('span', data.publication.visibility === 'public' ? '公開中' : '非公開');
  visPill.className = 'pill ' + (data.publication.visibility === 'public' ? 'pill-info' : 'pill-stopped');
  pills.append(runtimePill, visPill);

  const controls = element('div');
  controls.className = 'viewer-hero-actions';

  const start = storyRuntimeButton('起動', 'btn-primary btn-sm', isRunning,
    (btn) => storyRuntimeAction(btn, storyId, { path: '/runtime', method: 'PUT', body: { desired_state: 'running' } }, '起動しました'));
  const stop = storyRuntimeButton('停止', 'btn-danger btn-sm', !isRunning,
    (btn) => storyRuntimeAction(btn, storyId, { path: '/runtime', method: 'PUT', body: { desired_state: 'stopped' } }, '停止しました'));
  const restart = storyRuntimeButton('再起動', 'btn-ghost btn-sm', !isRunning,
    (btn) => storyRuntimeAction(btn, storyId, { path: '/runtime/restart', method: 'POST' }, '再起動しました'));
  const refresh = element('button', '再読み込み');
  refresh.type = 'button';
  refresh.className = 'btn btn-secondary btn-sm';
  refresh.addEventListener('click', () => { refreshViewerUnlessMapActive(storyId); });

  const followToggle = element('button', viewerAutoFollow ? '追従: ON' : '追従: OFF');
  followToggle.type = 'button';
  followToggle.className = 'btn btn-sm viewer-follow-toggle ' + (viewerAutoFollow ? 'btn-primary' : 'btn-secondary');
  followToggle.addEventListener('click', () => {
    setViewerAutoFollow(!viewerAutoFollow, { scroll: true, toast: true });
  });

  controls.append(start, stop, restart, refresh, followToggle);
  heroTop.append(pills, controls);

  const meta = element('div');
  meta.className = 'viewer-meta-grid';
  [
    ['runtime', data.runtime.state || '-'],
    ['provider', (data.llm && data.llm.provider) || '-'],
    ['model', (data.llm && data.llm.model) || '-'],
    ['latest turn', data.live && data.live.latest_turn ? String(data.live.latest_turn) : '-'],
    ['last update', (data.live && data.live.latest_sim_datetime) || '-'],
    ['visible', data.publication.visibility || 'draft']
  ].forEach(([label, value]) => {
    meta.append(renderViewerMeta(label, value));
  });

  hero.append(heroTop, meta);
  main.append(hero);

  // ── Auto-follow guidance ──
  main.append(buildViewerFollowBanner('top'));

  // ── タブ UI（タイムライン / MAP再生）──
  const tabBar = element('div');
  tabBar.className = 'viewer-tabs';

  const timelineTab = element('button', 'タイムライン');
  timelineTab.type = 'button';
  timelineTab.className = 'viewer-tab viewer-tab--active';

  const mapTab = element('button', 'MAP再生');
  mapTab.type = 'button';
  mapTab.className = 'viewer-tab';

  tabBar.append(timelineTab, mapTab);
  main.append(tabBar);

  // ── タイムラインパネル ──
  const timelinePanel = element('div');
  timelinePanel.className = 'viewer-tab-panel';

  const timeline = element('section');
  timeline.className = 'viewer-chat-list';
  const charactersById = new Map((data.characters || []).map((c) => [c.char_id, c]));
  const logs = data.logs || [];
  if (logs.length === 0) {
    const empty = element('p', 'まだ会話がありません。runtime を起動するとここに記録されます。');
    empty.className = 'text-muted viewer-chat-empty';
    timeline.append(empty);
  } else {
    logs.forEach((log) => timeline.append(buildViewerChatRow(storyId, log, charactersById)));
  }
  timelinePanel.append(timeline, buildViewerFollowBanner('bottom'));

  // ── MAP再生パネル ──
  const mapPanel = element('div');
  mapPanel.className = 'viewer-tab-panel';
  mapPanel.hidden = true;
  let mapIframe = reusableMapIframe;

  // タブ切り替えロジック
  const activateViewerTab = (tabName, options = {}) => {
    if (tabName === 'map') {
      viewerActiveTab = 'map';
      mapTab.className = 'viewer-tab viewer-tab--active';
      timelineTab.className = 'viewer-tab';
      mapPanel.hidden = false;
      timelinePanel.hidden = true;
      if (!mapIframe) {
        mapIframe = document.createElement('iframe');
        mapIframe.src = `/admin/map-replay/${storyId}`;
        mapIframe.className = 'viewer-map-iframe';
        mapIframe.title = 'MAP再生';
        viewerMapIframe = mapIframe;
        viewerMapIframeStoryId = storyId;
      }
      if (mapIframe.parentNode !== mapPanel) {
        mapPanel.append(mapIframe);
      }
      return;
    }
    viewerActiveTab = 'timeline';
    timelineTab.className = 'viewer-tab viewer-tab--active';
    mapTab.className = 'viewer-tab';
    timelinePanel.hidden = false;
    mapPanel.hidden = true;
    if (options.refresh) {
      refreshViewerUnlessMapActive(storyId, { silent: true });
    }
  };
  timelineTab.addEventListener('click', () => {
    activateViewerTab('timeline', { refresh: true });
  });
  mapTab.addEventListener('click', () => {
    activateViewerTab('map');
  });
  activateViewerTab(viewerActiveTab);

  main.append(timelinePanel, mapPanel);

  if (viewerAutoFollow && viewerActiveTab !== 'map') {
    window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' });
  }
  clearViewerPolling();
  attachViewerKeyboard(storyId);
  viewerPollHandle = window.setInterval(() => {
    refreshViewerUnlessMapActive(storyId, { silent: true });
  }, 5000);
}

/**
 * 1 表情分の画像カードを生成する。
 * ┌───────────┐
 * │ expression│ ← ラベル
 * │  [thumb]  │ ← 保存済みサムネイル or 「未設定」
 * │ [preview] │ ← ファイル選択後プレビュー
 * │ [参照...] │ ← file input
 * └───────────┘
 */
function renderExpressionImageField(storyId, charId, expression, fieldName) {
  const card = element('div');
  card.className = 'expr-card';

  const lbl = element('div', `${expression}.png`);
  lbl.className = 'expr-label';

  const thumbBox = element('div');
  thumbBox.className = 'expr-thumb-box';

  // 保存済み画像（静的ファイルとして /assets/ から配信）
  const savedImg = document.createElement('img');
  savedImg.className = 'expr-thumb-saved';
  savedImg.alt = expression;
  savedImg.style.display = 'none';

  // 未設定プレースホルダー
  const emptyMsg = element('div', '未設定');
  emptyMsg.className = 'expr-thumb-empty';

  // 選択後プレビュー（FileReader）
  const previewImg = document.createElement('img');
  previewImg.className = 'expr-thumb-preview';
  previewImg.style.display = 'none';

  if (charId) {
    const ts = Date.now();
    savedImg.src = `/assets/character_images/${storyId}/${charId}/${expression}.png?t=${ts}`;
    savedImg.style.display = '';
    savedImg.addEventListener('load', () => { emptyMsg.style.display = 'none'; });
    savedImg.addEventListener('error', () => {
      savedImg.style.display = 'none';
      emptyMsg.style.display = '';
    });
  }

  thumbBox.append(savedImg, emptyMsg, previewImg);

  const fileInput = document.createElement('input');
  fileInput.type = 'file';
  fileInput.accept = 'image/png';
  fileInput.name = fieldName;
  fileInput.className = 'expr-file-input';

  fileInput.addEventListener('change', () => {
    const file = fileInput.files && fileInput.files[0];
    if (file) {
      const reader = new FileReader();
      reader.onload = (e) => {
        previewImg.src = e.target.result;
        previewImg.style.display = '';
        savedImg.style.opacity = '0.3';
        emptyMsg.style.opacity = '0.3';
      };
      reader.readAsDataURL(file);
    } else {
      previewImg.style.display = 'none';
      savedImg.style.opacity = '';
      emptyMsg.style.opacity = '';
    }
  });

  card.append(lbl, thumbBox, fileInput);
  return card;
}

function renderPlaceImageField(storyId, placeId, fieldName) {
  const section = element('section');
  section.className = 'persona-section place-image-section';

  const heading = element('h3', '場所画像');
  const hint = element('p', `マップ再生時の背景として使用されます。PNG 推奨（目安: 320px 幅）。`);
  hint.className = 'field-hint';

  const previewBox = element('div');
  previewBox.className = 'place-img-preview-box';

  const savedImg = document.createElement('img');
  savedImg.className = 'place-img-thumb-saved';
  savedImg.alt = '場所画像';
  savedImg.style.display = 'none';

  const emptyMsg = element('div', '未設定');
  emptyMsg.className = 'place-img-empty';

  const previewImg = document.createElement('img');
  previewImg.className = 'place-img-thumb-preview';
  previewImg.style.display = 'none';

  if (placeId && storyId) {
    const ts = Date.now();
    savedImg.src = `/assets/story_maps/${storyId}/images/${placeId}_320.png?t=${ts}`;
    savedImg.style.display = '';
    savedImg.addEventListener('load', () => { emptyMsg.style.display = 'none'; });
    savedImg.addEventListener('error', () => {
      savedImg.style.display = 'none';
      emptyMsg.style.display = '';
    });
  }

  previewBox.append(savedImg, emptyMsg, previewImg);

  const fileInput = document.createElement('input');
  fileInput.type = 'file';
  fileInput.accept = 'image/png,image/jpeg,image/webp';
  fileInput.name = fieldName;
  fileInput.className = 'place-img-file-input';

  fileInput.addEventListener('change', () => {
    const file = fileInput.files && fileInput.files[0];
    if (file) {
      const reader = new FileReader();
      reader.onload = (e) => {
        previewImg.src = e.target.result;
        previewImg.style.display = '';
        savedImg.style.opacity = '0.3';
        emptyMsg.style.opacity = '0.3';
      };
      reader.readAsDataURL(file);
    } else {
      previewImg.style.display = 'none';
      savedImg.style.opacity = '';
      emptyMsg.style.opacity = '';
    }
  });

  section.append(heading, hint, previewBox, fileInput);
  return section;
}

function renderPlaceBgmField(storyId, placeId, placeOverrides, fieldName) {
  const section = element('section');
  section.className = 'persona-section place-bgm-section';

  const heading = element('h3', '場所 BGM');
  const hint = element('p', 'MAP 再生中にこの場所に移動すると BGM が切り替わります。MP3 / M4A / OGG 推奨。');
  hint.className = 'field-hint';

  const previewUrl = resolvePlaceBgmPreviewUrl(storyId, placeId, placeOverrides);

  const previewWrap = element('div');
  previewWrap.className = 'place-bgm-preview-wrap';

  if (previewUrl) {
    const audio = document.createElement('audio');
    audio.controls = true;
    audio.className = 'place-bgm-preview';
    audio.src = previewUrl;
    previewWrap.append(audio);
  } else {
    const emptyMsg = element('div', 'BGM 未設定');
    emptyMsg.className = 'place-bgm-empty';
    previewWrap.append(emptyMsg);
  }

  const fileInput = document.createElement('input');
  fileInput.type = 'file';
  fileInput.accept = 'audio/mpeg,audio/mp4,audio/ogg,.mp3,.m4a,.ogg';
  fileInput.name = fieldName;
  fileInput.className = 'place-bgm-file-input';

  section.append(heading, hint, previewWrap, fileInput);
  return section;
}

function renderOnboardingCharacterCard(storyId, character, options = {}) {
  const cardId = character._cardId || character.char_id;
  const cardNode = element('article');
  cardNode.className = 'admin-card onboarding-character-card';

  const header = element('div');
  header.className = 'onboarding-character-head';
  const avatar = element('div');
  avatar.append(
    makeExpressionImage(storyId, {
      char_id: character.char_id,
      speaker_name: character.name,
      expression: 'neutral'
    }, new Map([[character.char_id, {
      char_id: character.char_id,
      image_base_url: character.image_base_url,
      expressions_available: ['neutral']
    }]]))
  );
  const info = element('div');
  info.className = 'onboarding-character-title';
  info.append(
    element('strong', character.name || character.char_id),
    element('span', character.char_id ? character.char_id : '新規（character_id 必須）')
  );
  header.append(avatar, info);
  if (options.onDelete) {
    const delBtn = element('button', '削除');
    delBtn.type = 'button';
    delBtn.className = 'onboarding-delete-btn';
    delBtn.addEventListener('click', options.onDelete);
    header.append(delBtn);
  }

  const nameInput = element('input');
  nameInput.name = `name:${cardId}`;
  nameInput.value = character.name || '';

  const shortInput = element('input');
  shortInput.name = `short:${cardId}`;
  shortInput.value = character.short_description || '';

  const goalInput = element('input');
  goalInput.name = `goal:${cardId}`;
  goalInput.value = character.goal || '';

  const worryInput = element('input');
  worryInput.name = `worry:${cardId}`;
  worryInput.value = character.worry || '';

  const fields = element('div');
  fields.className = 'onboarding-field-grid';
  if (options.allowIdRename) {
    const newCharIdInput = element('input');
    newCharIdInput.name = `new_char:${cardId}`;
    newCharIdInput.value = character.new_char_id || character.char_id || '';
    newCharIdInput.pattern = '[a-z][a-z0-9_]*';
    fields.append(element('label', '変更後 character_id'), newCharIdInput);
  }
  fields.append(
    element('label', '表示名'),
    nameInput,
    element('label', '短い説明'),
    shortInput,
    element('label', 'goal'),
    goalInput,
    element('label', 'worry'),
    worryInput
  );

  const imageFields = element('div');
  if (options.allowExpressionUploads) {
    imageFields.className = 'expression-image-grid';
    const charIdForThumb = character.char_id || '';
    CHARACTER_EXPRESSION_KEYS.forEach((expression) => {
      imageFields.append(
        renderExpressionImageField(
          options.storyId || storyId,
          charIdForThumb,
          expression,
          `image:${cardId}:${expression}`
        )
      );
    });
  } else {
    // clone 画面: neutral 画像のみ
    imageFields.className = 'expression-upload-grid';
    const imageInput = element('input');
    imageInput.type = 'file';
    imageInput.accept = 'image/png';
    imageInput.name = `image:${cardId}`;
    imageFields.append(element('label', 'neutral画像 (PNG)'), imageInput);
  }

  cardNode.append(header, fields, imageFields);
  return cardNode;
}

function captureCharacterFormValues(form, workingCharacters) {
  workingCharacters.forEach((character) => {
    const cardId = character._cardId || character.char_id;
    const newCharIdEl = form.elements[`new_char:${cardId}`];
    const nameEl = form.elements[`name:${cardId}`];
    const shortEl = form.elements[`short:${cardId}`];
    const goalEl = form.elements[`goal:${cardId}`];
    const worryEl = form.elements[`worry:${cardId}`];
    if (newCharIdEl) {
      character.new_char_id = newCharIdEl.value.trim();
    }
    if (nameEl) {
      character.name = nameEl.value.trim();
    }
    if (shortEl) {
      character.short_description = shortEl.value.trim();
    }
    if (goalEl) {
      character.goal = goalEl.value.trim();
    }
    if (worryEl) {
      character.worry = worryEl.value.trim();
    }
  });
}

async function renderOnboarding(storyId) {
  const response = await api(`/stories/${storyId}/onboarding-template`);
  const main = layout(`story onboarding: ${storyId}`);
  if (!response.ok) {
    main.append(card('template 読み込み失敗', '複製元 story の YAML が見つかりませんでした。'));
    return;
  }

  const payload = await response.json();
  const data = payload.data;
  const hero = element('section');
  hero.className = 'viewer-hero';
  const summary = element('div');
  summary.className = 'viewer-summary';
  summary.append(
    element('h2', data.story.title || storyId),
    element('p', 'sample story を起点に、自分用の story を作ります。ここでは title / 説明 / キャラ名 / neutral画像だけを差し替えます。')
  );
  hero.append(summary);
  main.append(hero);

  const form = element('form');
  form.className = 'onboarding-form';

  const storyFields = element('section');
  storyFields.className = 'admin-card onboarding-story-card';
  storyFields.append(
    renderOnboardingNotice('作成モード', [
      'この画面は新規 story 作成専用です。既存 story の更新は行いません。',
      '同じ story_id が既にある場合は作成を止めます。',
      '作成後の詳細編集は YAML と update_story で行います。'
    ])
  );
  const newStoryId = element('input');
  newStoryId.name = 'story_id';
  newStoryId.value = `${storyId}_custom`;
  const titleInput = element('input');
  titleInput.name = 'title';
  titleInput.value = `${data.story.title || storyId} カスタム`;
  const descriptionInput = element('textarea');
  descriptionInput.name = 'description';
  descriptionInput.value = data.story.description || '';
  storyFields.append(
    element('h2', '新しい story'),
    element('label', 'story_id'),
    newStoryId,
    element('label', 'タイトル'),
    titleInput,
    element('label', '説明'),
    descriptionInput
  );

  const characterList = element('section');
  characterList.className = 'admin-list';
  (data.characters || []).forEach((character) => {
    characterList.append(renderOnboardingCharacterCard(storyId, character));
  });

  const actions = element('div');
  actions.className = 'admin-actions';
  const submit = element('button', '複製して作成');
  submit.type = 'submit';
  const openViewer = link(`/admin/viewer/${storyId}`, '元 story を viewer で見る');
  actions.append(submit, openViewer);

  const status = element('p');
  status.className = 'onboarding-status';

  form.append(storyFields, characterList, actions, status);
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    status.textContent = '複製を実行中です。';

    const characterPayload = (data.characters || []).map((character) => ({
      char_id: character.char_id,
      name: form.elements[`name:${character.char_id}`].value.trim(),
      short_description: form.elements[`short:${character.char_id}`].value.trim(),
      goal: form.elements[`goal:${character.char_id}`].value.trim(),
      worry: form.elements[`worry:${character.char_id}`].value.trim()
    }));

    const requestBody = new FormData();
    requestBody.append('payload', JSON.stringify({
      story_id: newStoryId.value.trim(),
      title: titleInput.value.trim(),
      description: descriptionInput.value.trim(),
      characters: characterPayload
    }));
    (data.characters || []).forEach((character) => {
      const fileInput = form.elements[`image:${character.char_id}`];
      const file = fileInput.files && fileInput.files[0];
      if (file) {
        requestBody.append(`character_image__${character.char_id}`, file);
      }
    });

    const cloneResponse = await api(`/stories/${storyId}/clone`, {
      method: 'POST',
      body: requestBody
    });
    const clonePayload = await cloneResponse.json();
    if (!cloneResponse.ok) {
      const message = clonePayload.error && clonePayload.error.message
        ? clonePayload.error.message
        : '複製に失敗しました。';
      renderOnboardingError(status, message);
      return;
    }

    const result = clonePayload.data;
    status.className = 'onboarding-status onboarding-status-ok';
    status.innerHTML = '';
    status.append(
      document.createTextNode(`作成完了: ${result.story_id}。`),
      document.createTextNode(' '),
      link(`/admin/stories/${result.story_id}`, '詳細へ'),
      document.createTextNode(' / '),
      link(`/admin/viewer/${result.story_id}`, 'viewerを開く')
    );
  });
  main.append(form);
}

async function renderCharacterEditor(storyId) {
  const main = layout(`キャラ編集: ${storyId}`);
  const loadingEl = buildEditorSkeleton();
  main.append(loadingEl);
  const response = await api(`/stories/${storyId}/characters/edit-template`);
  loadingEl.remove();
  if (!response.ok) {
    const err = element('div', 'story の読み込みに失敗しました。characters.yaml が見つからないか、DB に story が未登録です。');
    err.className = 'notice notice-error';
    main.append(err);
    return;
  }

  const payload = await response.json();
  const data = payload.data;
  const hero = element('section');
  hero.className = 'viewer-hero';
  const summary = element('div');
  summary.className = 'viewer-summary';
  summary.append(
    element('h2', data.story.title || storyId),
    element('p', `runtime: ${data.runtime_state || 'stopped'} / 編集: ${data.editable ? '可能' : '不可'}`)
  );
  hero.append(summary);
  main.append(hero);

  if (!data.editable) {
    const reason = data.read_only_reason || 'not_editable';
    const notices = {
      template_story: [
        'サンプル本体は直接変更しません。',
        '先に story を複製し、複製後の story でキャラを差し替えます。'
      ],
      story_running: [
        'エンジン稼働中の YAML/DB 更新は避けます。',
        'ストーリー詳細で停止してから、この画面を再読み込みしてください。'
      ],
      story_not_imported: [
        'YAML はありますが、DB に story が未登録です。',
        'import_story または onboarding の複製作成を先に完了してください。'
      ]
    };
    main.append(renderOnboardingNotice('編集できません', notices[reason] || ['この story は現在編集対象外です。']));
    const actions = element('div');
    actions.className = 'admin-actions';
    actions.append(
      link(`/admin/stories/${storyId}`, 'ストーリー詳細へ'),
      link(`/admin/onboarding/${storyId}`, 'この story を複製')
    );
    main.append(actions);
    return;
  }

  const form = element('form');
  form.className = 'onboarding-form';
  const notice = renderOnboardingNotice('編集範囲', [
    'キャラの追加・削除、character_id、表示名、短い説明、goal、worry、10 表情画像を更新します。',
    'character_id を変更するとキャラ画像フォルダも追従します。',
    '保存時に validate_story と update_story を実行し、DB と metadata に反映します。',
    '場所・章の編集は別画面で扱います。'
  ]);
  form.append(notice);

  const characterList = element('section');
  characterList.className = 'admin-list';
  const workingCharacters = (data.characters || []).map((character) => ({
    ...character,
    _cardId: character.char_id
  }));
  const renderCharacterList = () => {
    characterList.innerHTML = '';
    workingCharacters.forEach((character, index) => {
      characterList.append(
        renderOnboardingCharacterCard(storyId, character, {
          allowIdRename: true,
          allowExpressionUploads: true,
          storyId,
          onDelete: () => {
            captureCharacterFormValues(form, workingCharacters);
            workingCharacters.splice(index, 1);
            renderCharacterList();
          }
        })
      );
    });
  };
  renderCharacterList();

  const addCharacter = element('button', '＋ キャラを追加');
  addCharacter.type = 'button';
  addCharacter.className = 'btn btn-ghost btn-sm';
  addCharacter.addEventListener('click', () => {
    captureCharacterFormValues(form, workingCharacters);
    _newCharacterCounter += 1;
    workingCharacters.push({
      _cardId: `new-character-${_newCharacterCounter}`,
      char_id: '',
      new_char_id: `new_character_${_newCharacterCounter}`,
      name: '新しいキャラ',
      short_description: '',
      goal: '',
      worry: ''
    });
    renderCharacterList();
  });

  const inlineActions = element('div');
  inlineActions.className = 'editor-inline-actions';
  inlineActions.append(addCharacter);

  const submit = element('button', '保存');
  submit.type = 'submit';
  submit.className = 'btn btn-primary';
  const saveBar = buildEditorSaveBar(submit, [
    { href: `/admin/stories/${storyId}`, label: '詳細へ戻る' },
    { href: `/admin/viewer/${storyId}`, label: 'Viewer' }
  ]);

  const { setDirty, clearDirty } = attachEditorDirtyGuard();
  form.addEventListener('input', setDirty);
  form.addEventListener('change', setDirty);

  form.append(characterList, inlineActions, saveBar);
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    setSavingButton(submit, '保存中…');
    captureCharacterFormValues(form, workingCharacters);

    const characterPayload = workingCharacters.map((character) => ({
      char_id: character.char_id,
      new_char_id: character.new_char_id,
      name: character.name,
      short_description: character.short_description,
      goal: character.goal,
      worry: character.worry
    }));

    const requestBody = new FormData();
    requestBody.append('payload', JSON.stringify({ characters: characterPayload }));
    workingCharacters.forEach((character) => {
      const cardId = character._cardId || character.char_id;
      const uploadCharId = character.char_id || character.new_char_id;
      CHARACTER_EXPRESSION_KEYS.forEach((expression) => {
        const fileInput = form.elements[`image:${cardId}:${expression}`];
        const file = fileInput && fileInput.files && fileInput.files[0];
        if (file && uploadCharId) {
          requestBody.append(`character_image__${uploadCharId}__${expression}`, file);
        }
      });
      const legacyNeutralInput = form.elements[`image:${cardId}`];
      const legacyNeutralFile = legacyNeutralInput && legacyNeutralInput.files && legacyNeutralInput.files[0];
      if (legacyNeutralFile && uploadCharId) {
        requestBody.append(`character_image__${uploadCharId}`, legacyNeutralFile);
      }
    });

    const updateResponse = await api(`/stories/${storyId}/characters`, {
      method: 'PUT',
      body: requestBody
    });
    clearSavingButton(submit);
    const updatePayload = await updateResponse.json();
    if (!updateResponse.ok) {
      const errMsg = (updatePayload.error && updatePayload.error.message)
        ? updatePayload.error.message
        : '保存に失敗しました。';
      if (typeof window.showToast === 'function') window.showToast(errMsg, 'error');
      return;
    }

    clearDirty();
    const result = updatePayload.data;
    const successMsg = `保存完了: ${result.character_count} キャラを反映しました（追加 ${result.characters_added || 0} / 削除 ${result.characters_removed || 0} / ID変更 ${result.characters_renamed || 0}）`;
    if (typeof window.showToast === 'function') window.showToast(successMsg, 'success', { duration: 5000 });
    // サムネイルをキャッシュバストして再描画（保存後の画像を即反映）
    renderCharacterList();
  });
  main.append(form);
}

function effectivePlaceId(place) {
  return place.new_place_id || place.place_id || '';
}

function renderAdjRow(adjRow, rowIdx, cardId, workingPlaces, selfPlaceId, onDelete) {
  const row = element('div');
  row.className = 'adjacent-row';

  const adjSelect = element('select');
  adjSelect.name = `adj_id:${cardId}:${rowIdx}`;
  const blankOpt = element('option', '--- 選択 ---');
  blankOpt.value = '';
  adjSelect.append(blankOpt);
  workingPlaces
    .map((p) => effectivePlaceId(p))
    .filter((pid) => pid && pid !== selfPlaceId)
    .forEach((pid) => {
      const opt = element('option', pid);
      opt.value = pid;
      if (pid === adjRow.adj_id) opt.selected = true;
      adjSelect.append(opt);
    });

  const costInput = element('input');
  costInput.type = 'number';
  costInput.min = '1';
  costInput.style.width = '64px';
  costInput.name = `adj_cost:${cardId}:${rowIdx}`;
  costInput.value = String(adjRow.cost || 1);

  const bidirLabel = element('label');
  bidirLabel.style.cssText = 'display:flex;align-items:center;gap:4px;font-size:13px;cursor:pointer;';
  const bidirCheck = element('input');
  bidirCheck.type = 'checkbox';
  bidirCheck.name = `adj_bidir:${cardId}:${rowIdx}`;
  bidirCheck.checked = adjRow.bidir !== false;
  bidirLabel.append(bidirCheck, document.createTextNode('両方向'));

  const delBtn = element('button', '×');
  delBtn.type = 'button';
  delBtn.className = 'onboarding-delete-btn';
  delBtn.addEventListener('click', onDelete);

  row.append(adjSelect, costInput, bidirLabel, delBtn);
  return row;
}

function renderPlaceEditCard(place, cardId, workingPlaces, onDelete) {
  const cardNode = element('article');
  cardNode.className = 'place-accordion-card';

  const details = element('details');
  details.open = true;
  const summary = element('summary');
  summary.className = 'place-accordion-summary';

  const summaryInfo = element('div');
  summaryInfo.className = 'place-accordion-info';
  const titleEl = element('span', place.label || place.place_id || '新しい場所');
  titleEl.className = 'place-accordion-title';
  const idEl = element('span', place.place_id ? `id: ${place.place_id}` : '新規（place_id 必須）');
  idEl.className = 'place-accordion-id';
  summaryInfo.append(titleEl, idEl);

  summary.append(summaryInfo);

  if (onDelete) {
    const delBtn = element('button', '削除');
    delBtn.type = 'button';
    delBtn.className = 'btn btn-danger btn-sm';
    delBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      const label = place.label || place.place_id || '場所';
      if (window.confirm(`「${label}」を削除しますか?`)) {
        onDelete();
      }
    });
    summary.append(delBtn);
  }

  details.append(summary);
  const body = element('div');
  body.className = 'place-accordion-body';

  const placeIdInput = element('input');
  placeIdInput.name = `place_id:${cardId}`;
  if (place.place_id) {
    placeIdInput.value = place.place_id;
    placeIdInput.readOnly = true;
    placeIdInput.style.color = '#53677a';
  } else {
    placeIdInput.placeholder = 'snake_case 例: school_garden';
  }

  const newPlaceIdInput = element('input');
  newPlaceIdInput.name = `new_place_id:${cardId}`;
  newPlaceIdInput.value = place.new_place_id || place.place_id || '';
  newPlaceIdInput.placeholder = '変更後 place_id';

  const labelInput = element('input');
  labelInput.name = `label:${cardId}`;
  labelInput.value = place.label || '';

  const zoneInput = element('input');
  zoneInput.name = `zone:${cardId}`;
  zoneInput.value = place.zone || '';
  zoneInput.placeholder = 'school / town / home';

  const atmosphereInput = element('textarea');
  atmosphereInput.name = `atmosphere:${cardId}`;
  atmosphereInput.value = place.atmosphere || '';

  const whoGathersInput = element('textarea');
  whoGathersInput.name = `who_gathers:${cardId}`;
  whoGathersInput.value = Array.isArray(place.who_gathers)
    ? place.who_gathers.join('\n')
    : (place.who_gathers || '');

  const eventsLikelyInput = element('textarea');
  eventsLikelyInput.name = `events_likely:${cardId}`;
  eventsLikelyInput.value = Array.isArray(place.events_likely)
    ? place.events_likely.join('\n')
    : (place.events_likely || '');

  const accessNoteInput = element('textarea');
  accessNoteInput.name = `access_note:${cardId}`;
  accessNoteInput.value = place.access_note || '';

  const fields = element('div');
  fields.className = 'onboarding-field-grid';
  fields.append(
    element('label', 'place_id'),
    placeIdInput,
    element('label', '変更後 place_id'),
    newPlaceIdInput,
    element('label', '場所名'),
    labelInput,
    element('label', 'zone'),
    zoneInput,
    element('label', '雰囲気'),
    atmosphereInput,
    element('label', '集まりやすい人'),
    whoGathersInput,
    element('label', '起きやすいイベント'),
    eventsLikelyInput,
    element('label', 'アクセスメモ'),
    accessNoteInput
  );

  const adjSection = element('div');
  const adjTitle = element('p', '隣接する場所');
  adjTitle.style.cssText = 'margin: 12px 0 4px; font-size: 13px; font-weight: 600; color: #33506b;';
  adjSection.append(adjTitle);

  const adjList = element('div');
  adjList.className = 'adjacent-list';
  const _adjRows = place._adjRows;

  const captureLocalAdjRows = () => {
    _adjRows.forEach((adjRow, rowIdx) => {
      const adjSel = adjList.querySelector(`[name="adj_id:${cardId}:${rowIdx}"]`);
      const adjCost = adjList.querySelector(`[name="adj_cost:${cardId}:${rowIdx}"]`);
      const bidirEl = adjList.querySelector(`[name="adj_bidir:${cardId}:${rowIdx}"]`);
      if (adjSel) {
        adjRow.adj_id = adjSel.value;
      }
      if (adjCost) {
        adjRow.cost = parseInt(adjCost.value || '1', 10) || 1;
      }
      if (bidirEl) {
        adjRow.bidir = bidirEl.checked;
      }
    });
  };

  const renderAdjRows = () => {
    adjList.innerHTML = '';
    _adjRows.forEach((adjRow, rowIdx) => {
      adjList.append(
        renderAdjRow(adjRow, rowIdx, cardId, workingPlaces, effectivePlaceId(place), () => {
          captureLocalAdjRows();
          _adjRows.splice(rowIdx, 1);
          renderAdjRows();
        })
      );
    });
  };
  renderAdjRows();

  const addAdjBtn = element('button', '＋ 隣接を追加');
  addAdjBtn.type = 'button';
  addAdjBtn.className = 'onboarding-add-btn';
  addAdjBtn.addEventListener('click', () => {
    captureLocalAdjRows();
    _adjRows.push({ adj_id: '', cost: 1, bidir: true });
    renderAdjRows();
  });

  adjSection.append(adjList, addAdjBtn);

  const imgSection = renderPlaceImageField(
    place._storyId || '',
    place.place_id || '',
    `place_image:${cardId}`
  );

  const bgmField = renderPlaceBgmField(
    place._storyId || '',
    place.place_id || '',
    place._bgmOverrides || {},
    `place_bgm:${cardId}`
  );

  body.append(fields, adjSection, imgSection, bgmField);
  details.append(body);
  cardNode.append(details);
  return cardNode;
}

function readStringListField(form, name) {
  const field = form.elements[name];
  return ((field && field.value) || '')
    .split('\n')
    .map((item) => item.trim())
    .filter(Boolean);
}

function capturePlaceFormValues(form, workingPlaces) {
  workingPlaces.forEach((place) => {
    const cid = place._cardId;
    const placeIdEl = form.elements[`place_id:${cid}`];
    const newPlaceIdEl = form.elements[`new_place_id:${cid}`];
    const labelEl = form.elements[`label:${cid}`];
    const zoneEl = form.elements[`zone:${cid}`];
    const atmosphereEl = form.elements[`atmosphere:${cid}`];
    const accessNoteEl = form.elements[`access_note:${cid}`];

    if (placeIdEl) {
      place.place_id = placeIdEl.value.trim() || place.place_id || '';
    }
    if (newPlaceIdEl) {
      place.new_place_id = newPlaceIdEl.value.trim();
    }
    if (labelEl) {
      place.label = labelEl.value.trim();
    }
    if (zoneEl) {
      place.zone = zoneEl.value.trim();
    }
    if (atmosphereEl) {
      place.atmosphere = atmosphereEl.value;
    }
    place.who_gathers = readStringListField(form, `who_gathers:${cid}`);
    place.events_likely = readStringListField(form, `events_likely:${cid}`);
    if (accessNoteEl) {
      place.access_note = accessNoteEl.value;
    }

    place._adjRows = (place._adjRows || []).map((adjRow, rowIdx) => {
      const adjSel = form.elements[`adj_id:${cid}:${rowIdx}`];
      const adjCost = form.elements[`adj_cost:${cid}:${rowIdx}`];
      const bidirEl = form.elements[`adj_bidir:${cid}:${rowIdx}`];
      return {
        adj_id: adjSel ? adjSel.value : adjRow.adj_id,
        cost: parseInt((adjCost && adjCost.value) || String(adjRow.cost || 1), 10) || 1,
        bidir: bidirEl ? bidirEl.checked : adjRow.bidir
      };
    });
  });
}

async function renderPlaceEditor(storyId) {
  const main = layout(`場所編集: ${storyId}`);
  const loadingEl = buildEditorSkeleton();
  main.append(loadingEl);
  const [response, bgmResp] = await Promise.all([
    api(`/stories/${storyId}/places/edit-template`),
    api(`/stories/${storyId}/bgm-config`).catch(() => null)
  ]);
  loadingEl.remove();
  if (!response.ok) {
    const err = element('div', 'story の読み込みに失敗しました。world_config.yaml が見つからないか、DB に story が未登録です。');
    err.className = 'notice notice-error';
    main.append(err);
    return;
  }

  const payload = await response.json();
  const data = payload.data;
  const bgmOverrides = bgmResp && bgmResp.ok
    ? ((await bgmResp.json()).data || {}).place_overrides || {}
    : {};
  const hero = element('section');
  hero.className = 'viewer-hero';
  const summary = element('div');
  summary.className = 'viewer-summary';
  summary.append(
    element('h2', data.story.title || storyId),
    element('p', `runtime: ${data.runtime_state || 'stopped'} / 編集: ${data.editable ? '可能' : '不可'}`)
  );
  hero.append(summary);
  main.append(hero);

  if (!data.editable) {
    const reason = data.read_only_reason || 'not_editable';
    const notices = {
      template_story: [
        'サンプル本体は直接変更しません。',
        '先に story を複製し、複製後の story で場所設定を調整します。'
      ],
      story_running: [
        'エンジン稼働中の YAML/DB 更新は避けます。',
        'ストーリー詳細で停止してから、この画面を再読み込みしてください。'
      ],
      story_not_imported: [
        'YAML はありますが、DB に story が未登録です。',
        'import_story または onboarding の複製作成を先に完了してください。'
      ]
    };
    main.append(renderOnboardingNotice('編集できません', notices[reason] || ['この story は現在編集対象外です。']));
    const actions = element('div');
    actions.className = 'admin-actions';
    actions.append(
      link(`/admin/stories/${storyId}`, 'ストーリー詳細へ'),
      link(`/admin/onboarding/${storyId}`, 'この story を複製')
    );
    main.append(actions);
    return;
  }

  const workingPlaces = (data.places || []).map((p) => ({
    ...p,
    _cardId: p.place_id,
    _storyId: storyId,
    _bgmOverrides: bgmOverrides,
    _adjRows: Object.entries(p.adjacent_places || {}).map(([adj_id, cost]) => ({
      adj_id, cost, bidir: false
    }))
  }));

  const form = element('form');
  form.className = 'onboarding-form';
  form.append(renderOnboardingNotice('編集範囲', [
    '場所の追加・削除・place_id変更・隣接関係と基本項目の編集ができます。',
    'place_id を変更すると favorite_places や force_place などの参照も更新します。',
    '削除は参照がない場所のみ可能です（favorite_places や force_place など）。',
    '保存時に validate_story と update_story を実行し、DB に反映します。'
  ]));

  const placeList = element('section');
  placeList.className = 'admin-list';

  const renderPlaceCards = () => {
    while (placeList.firstChild) placeList.removeChild(placeList.firstChild);
    workingPlaces.forEach((place, idx) => {
      placeList.append(
        renderPlaceEditCard(place, place._cardId, workingPlaces, () => {
          capturePlaceFormValues(form, workingPlaces);
          workingPlaces.splice(idx, 1);
          renderPlaceCards();
        })
      );
    });
    const addBtn = element('button', '＋ 場所を追加');
    addBtn.type = 'button';
    addBtn.className = 'onboarding-add-btn';
    addBtn.addEventListener('click', () => {
      capturePlaceFormValues(form, workingPlaces);
      _newPlaceCounter += 1;
      workingPlaces.push({
        place_id: '',
        label: '',
        zone: '',
        adjacent_places: {},
        _cardId: `_new_place_${_newPlaceCounter}`,
        _storyId: storyId,
        _bgmOverrides: bgmOverrides,
        _adjRows: []
      });
      renderPlaceCards();
    });
    placeList.append(addBtn);
  };
  renderPlaceCards();

  const submit = element('button', '保存');
  submit.type = 'submit';
  submit.className = 'btn btn-primary';
  const saveBar = buildEditorSaveBar(submit, [
    { href: `/admin/stories/${storyId}`, label: '詳細へ戻る' },
    { href: `/admin/viewer/${storyId}`, label: 'Viewer' }
  ]);

  const { setDirty, clearDirty } = attachEditorDirtyGuard();
  form.addEventListener('input', setDirty);
  form.addEventListener('change', setDirty);

  form.append(placeList, saveBar);
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    setSavingButton(submit, '保存中…');

    const placePayload = workingPlaces.map((place) => {
      const cid = place._cardId;
      const adjMap = {};
      const newPlaceIdEl = form.elements[`new_place_id:${cid}`];
      place._adjRows.forEach((_, rowIdx) => {
        const adjSel = form.elements[`adj_id:${cid}:${rowIdx}`];
        const adjCost = form.elements[`adj_cost:${cid}:${rowIdx}`];
        if (adjSel && adjSel.value) {
          adjMap[adjSel.value] = parseInt((adjCost && adjCost.value) || '1') || 1;
        }
      });
      const placeIdEl = form.elements[`place_id:${cid}`];
      const originalId = placeIdEl
        ? (placeIdEl.value.trim() || place.place_id || '')
        : (place.place_id || '');
      const targetId = newPlaceIdEl
        ? (newPlaceIdEl.value.trim() || originalId)
        : originalId;
      return {
        place_id: originalId,
        new_place_id: targetId,
        label: ((form.elements[`label:${cid}`] || {}).value || '').trim(),
        zone: ((form.elements[`zone:${cid}`] || {}).value || '').trim(),
        atmosphere: ((form.elements[`atmosphere:${cid}`] || {}).value || '').trim(),
        who_gathers: ((form.elements[`who_gathers:${cid}`] || {}).value || '')
          .split('\n').map((s) => s.trim()).filter(Boolean),
        events_likely: ((form.elements[`events_likely:${cid}`] || {}).value || '')
          .split('\n').map((s) => s.trim()).filter(Boolean),
        access_note: ((form.elements[`access_note:${cid}`] || {}).value || '').trim(),
        adjacent_places: adjMap
      };
    });

    const pidIndex = {};
    placePayload.forEach((p) => { if (p.new_place_id) pidIndex[p.new_place_id] = p; });
    workingPlaces.forEach((place, idx) => {
      const cid = place._cardId;
      place._adjRows.forEach((_, rowIdx) => {
        const bidirEl = form.elements[`adj_bidir:${cid}:${rowIdx}`];
        const adjSel = form.elements[`adj_id:${cid}:${rowIdx}`];
        const adjCost = form.elements[`adj_cost:${cid}:${rowIdx}`];
        if (bidirEl && bidirEl.checked && adjSel && adjSel.value) {
          const selfId = placePayload[idx].new_place_id || placePayload[idx].place_id;
          const target = pidIndex[adjSel.value];
          if (selfId && target && !target.adjacent_places[selfId]) {
            target.adjacent_places[selfId] = parseInt((adjCost && adjCost.value) || '1') || 1;
          }
        }
      });
    });

    const requestBody = new FormData();
    requestBody.append('payload', JSON.stringify({ places: placePayload }));
    workingPlaces.forEach((place, idx) => {
      const cid = place._cardId;
      const uploadPlaceId = placePayload[idx].new_place_id || placePayload[idx].place_id;
      const fileInput = form.elements[`place_image:${cid}`];
      const file = fileInput && fileInput.files && fileInput.files[0];
      if (file && uploadPlaceId) {
        requestBody.append(`place_image__${uploadPlaceId}`, file);
      }
    });
    const updateResponse = await api(`/stories/${storyId}/places`, {
      method: 'PUT',
      body: requestBody
    });
    clearSavingButton(submit);
    const updatePayload = await updateResponse.json();
    if (!updateResponse.ok) {
      const errMsg = (updatePayload.error && updatePayload.error.message)
        ? updatePayload.error.message
        : '保存に失敗しました。';
      if (typeof window.showToast === 'function') window.showToast(errMsg, 'error');
      return;
    }

    clearDirty();

    const bgmBody = new FormData();
    let hasBgmUploads = false;
    workingPlaces.forEach((place, idx) => {
      const cid = place._cardId;
      const uploadPlaceId = placePayload[idx].new_place_id || placePayload[idx].place_id;
      const bgmInput = form.elements[`place_bgm:${cid}`];
      const bgmFile = bgmInput && bgmInput.files && bgmInput.files[0];
      if (bgmFile && uploadPlaceId) {
        bgmBody.append(`place_bgm__${uploadPlaceId}`, bgmFile);
        hasBgmUploads = true;
      }
    });
    if (hasBgmUploads) {
      const bgmResponse = await api(`/stories/${storyId}/bgm-config`, { method: 'PUT', body: bgmBody });
      if (!bgmResponse.ok) {
        let message = 'BGM の保存に失敗しました';
        try {
          const errorPayload = await bgmResponse.json();
          if (errorPayload && errorPayload.error && errorPayload.error.message) {
            message = `${message}: ${errorPayload.error.message}`;
          }
        } catch (_error) {
          // Ignore JSON parse errors and show the generic message.
        }
        if (typeof window.showToast === 'function') {
          window.showToast(message, 'error', { duration: 6000 });
        }
      }
    }

    const result = updatePayload.data;
    const addedInfo = (result.places_added || result.places_removed || result.places_renamed)
      ? `（+${result.places_added || 0}/-${result.places_removed || 0}/rename:${result.places_renamed || 0}）`
      : '';
    if (typeof window.showToast === 'function') {
      window.showToast(`保存完了: ${result.place_count} 箇所を反映しました${addedInfo}`, 'success', { duration: 5000 });
    }
  });
  main.append(form);
}

async function renderStorySettingsEditor(storyId) {
  const main = layout(`story設定: ${storyId}`);
  const loadingEl = buildEditorSkeleton();
  main.append(loadingEl);
  const response = await api(`/stories/${storyId}/definition/edit-template`);
  loadingEl.remove();
  if (!response.ok) {
    const err = element('div', 'story の読み込みに失敗しました。world_config.yaml が見つからないか、DB に story が未登録です。');
    err.className = 'notice notice-error';
    main.append(err);
    return;
  }

  const payload = await response.json();
  const data = payload.data;
  const hero = element('section');
  hero.className = 'viewer-hero';
  const summary = element('div');
  summary.className = 'viewer-summary';
  summary.append(
    element('h2', data.story.title || storyId),
    element('p', `runtime: ${data.runtime_state || 'stopped'} / 編集: ${data.editable ? '可能' : '不可'}`)
  );
  hero.append(summary);
  main.append(hero);

  if (!data.editable) {
    const reason = data.read_only_reason || 'not_editable';
    const notices = {
      template_story: [
        'サンプル本体は直接変更しません。',
        '先に story を複製し、複製後の story で基本設定を調整します。'
      ],
      story_running: [
        'エンジン稼働中の YAML/DB 更新は避けます。',
        'ストーリー詳細で停止してから、この画面を再読み込みしてください。'
      ],
      story_not_imported: [
        'YAML はありますが、DB に story が未登録です。',
        'import_story または onboarding の複製作成を先に完了してください。'
      ]
    };
    main.append(renderOnboardingNotice('編集できません', notices[reason] || ['この story は現在編集対象外です。']));
    const actions = element('div');
    actions.className = 'admin-actions';
    actions.append(
      link(`/admin/stories/${storyId}`, 'ストーリー詳細へ'),
      link(`/admin/onboarding/${storyId}`, 'この story を複製')
    );
    main.append(actions);
    return;
  }

  const form = element('form');
  form.className = 'onboarding-form';
  const storyFields = element('section');
  storyFields.className = 'admin-card onboarding-story-card';
  storyFields.append(renderOnboardingNotice('編集範囲', [
    'story のタイトル、説明、開始日、1ターンの時間、実行間隔、world_rules を更新します。',
    'story_id と LLM設定はここでは変更しません。',
    '保存時に validate_story と update_story を実行し、DB に反映します。'
  ]));

  const titleInput = element('input');
  titleInput.name = 'title';
  titleInput.value = data.story.title || '';

  const descriptionInput = element('textarea');
  descriptionInput.name = 'description';
  descriptionInput.value = data.story.description || '';

  const seasonStartInput = element('input');
  seasonStartInput.type = 'date';
  seasonStartInput.name = 'season_start';
  seasonStartInput.value = data.story.season_start || '';

  const turnMinutesInput = element('input');
  turnMinutesInput.type = 'number';
  turnMinutesInput.min = '1';
  turnMinutesInput.name = 'turn_minutes';
  turnMinutesInput.value = data.story.turn_minutes || 30;

  const turnIntervalInput = element('input');
  turnIntervalInput.type = 'number';
  turnIntervalInput.min = '1';
  turnIntervalInput.name = 'turn_interval_sec';
  turnIntervalInput.value = data.story.turn_interval_sec || 30;

  const worldRulesInput = element('textarea');
  worldRulesInput.name = 'world_rules';
  worldRulesInput.value = data.story.world_rules || '';

  storyFields.append(
    element('h2', 'story基本設定'),
    element('label', 'タイトル'),
    titleInput,
    element('label', '説明'),
    descriptionInput,
    element('label', 'season_start'),
    seasonStartInput,
    element('label', 'turn_minutes'),
    turnMinutesInput,
    element('label', 'turn_interval_sec'),
    turnIntervalInput,
    element('label', 'world_rules'),
    worldRulesInput
  );

  const submit = element('button', '保存');
  submit.type = 'submit';
  submit.className = 'btn btn-primary';
  const saveBar = buildEditorSaveBar(submit, [
    { href: `/admin/stories/${storyId}`, label: '詳細へ戻る' },
    { href: `/admin/viewer/${storyId}`, label: 'Viewer' }
  ]);

  const { setDirty, clearDirty } = attachEditorDirtyGuard();
  form.addEventListener('input', setDirty);
  form.addEventListener('change', setDirty);

  form.append(storyFields, saveBar);
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    setSavingButton(submit, '保存中…');

    const updateResponse = await api(`/stories/${storyId}/definition`, {
      method: 'PUT',
      body: JSON.stringify({
        story: {
          title: titleInput.value.trim(),
          description: descriptionInput.value.trim(),
          season_start: seasonStartInput.value.trim(),
          turn_minutes: parseInt(turnMinutesInput.value || '0', 10),
          turn_interval_sec: parseInt(turnIntervalInput.value || '0', 10),
          world_rules: worldRulesInput.value.trim()
        }
      })
    });
    clearSavingButton(submit);
    const updatePayload = await updateResponse.json();
    if (!updateResponse.ok) {
      const errMsg = (updatePayload.error && updatePayload.error.message)
        ? updatePayload.error.message
        : '保存に失敗しました。';
      if (typeof window.showToast === 'function') window.showToast(errMsg, 'error');
      return;
    }

    clearDirty();
    if (typeof window.showToast === 'function') window.showToast('保存完了: story 基本設定を反映しました。', 'success');
  });
  main.append(form);
}

function renderEventEditCard(eventItem, cardId, onDelete) {
  const cardNode = element('article');
  cardNode.className = 'admin-card onboarding-character-card';

  const header = element('div');
  header.className = 'onboarding-character-head';
  const info = element('div');
  info.className = 'onboarding-character-title';
  info.append(
    element('strong', eventItem.name || cardId || '(新規イベント)'),
    element('span', eventItem.event_date || '')
  );
  header.append(info);

  if (onDelete) {
    const deleteBtn = element('button', '削除');
    deleteBtn.type = 'button';
    deleteBtn.className = 'onboarding-delete-btn';
    deleteBtn.addEventListener('click', onDelete);
    header.append(deleteBtn);
  }

  const dateInput = element('input');
  dateInput.name = `event_date:${cardId}`;
  dateInput.value = eventItem.event_date || '';

  const nameInput = element('input');
  nameInput.name = `event_name:${cardId}`;
  nameInput.value = eventItem.name || '';

  const durationInput = element('input');
  durationInput.name = `event_duration:${cardId}`;
  durationInput.type = 'number';
  durationInput.min = '1';
  durationInput.value = String(eventItem.duration_days || 1);

  const atmosphereInput = element('textarea');
  atmosphereInput.name = `event_atmosphere:${cardId}`;
  atmosphereInput.value = eventItem.atmosphere || '';

  const emotionImpactInput = element('textarea');
  emotionImpactInput.name = `event_emotion:${cardId}`;
  emotionImpactInput.value = eventItem._emotionImpactText !== undefined
    ? eventItem._emotionImpactText
    : JSON.stringify(eventItem.emotion_impact || {}, null, 2);

  const forcePlaceInput = element('input');
  forcePlaceInput.name = `event_force_place:${cardId}`;
  forcePlaceInput.value = eventItem.force_place || '';

  const fields = element('div');
  fields.className = 'onboarding-field-grid';
  fields.append(
    element('label', '日付 MM-DD'),
    dateInput,
    element('label', 'イベント名'),
    nameInput,
    element('label', '日数'),
    durationInput,
    element('label', '雰囲気'),
    atmosphereInput,
    element('label', '感情影響 JSON'),
    emotionImpactInput,
    element('label', 'force_place'),
    forcePlaceInput
  );

  cardNode.append(header, fields);
  return cardNode;
}

function appendPlaceOptions(select, placeOptions, selectedValue) {
  select.append(element('option', '-'));
  let found = !selectedValue;
  (placeOptions || []).forEach((place) => {
    const option = element('option', `${place.label || place.place_id} (${place.place_id})`);
    option.value = place.place_id || '';
    option.selected = option.value === selectedValue;
    if (option.selected) found = true;
    select.append(option);
  });
  if (!found) {
    const option = element('option', selectedValue);
    option.value = selectedValue;
    option.selected = true;
    select.append(option);
  }
}

function conditionBuilderStateFromForm(form, cardId) {
  const booleanValue = (name) => {
    const value = ((form.elements[`${name}:${cardId}`] || {}).value || '').trim();
    if (value === 'true') return true;
    if (value === 'false') return false;
    return '';
  };
  return {
    place: ((form.elements[`condition_place:${cardId}`] || {}).value || '').trim(),
    expected_place: ((form.elements[`condition_expected_place:${cardId}`] || {}).value || '').trim(),
    zone: ((form.elements[`condition_zone:${cardId}`] || {}).value || '').trim(),
    time_from: ((form.elements[`condition_time_from:${cardId}`] || {}).value || '').trim(),
    time_to: ((form.elements[`condition_time_to:${cardId}`] || {}).value || '').trim(),
    min_tension: ((form.elements[`condition_min_tension:${cardId}`] || {}).value || '').trim(),
    required_event: ((form.elements[`condition_required_event:${cardId}`] || {}).value || '').trim(),
    multiple_characters: booleanValue('condition_multiple_characters'),
    alone: booleanValue('condition_alone'),
    stress_threshold_min: ((form.elements[`condition_stress_threshold_min:${cardId}`] || {}).value || '').trim()
  };
}

function parseConditionFromForm(form, cardId) {
  const conditionInput = form.elements[`anomaly_condition:${cardId}`];
  const condition = parseJsonObjectOrThrow(
    conditionInput ? conditionInput.value : '{}',
    'condition_json'
  );
  const builder = window.POCKETROLE_CONDITION_BUILDER;
  if (!builder) return condition;
  return builder.applyConditionBuilderState(condition, conditionBuilderStateFromForm(form, cardId));
}

function renderConditionBuilder(anomaly, cardId, conditionInput, placeOptions) {
  const builder = window.POCKETROLE_CONDITION_BUILDER;
  const tools = element('section');
  tools.className = 'onboarding-condition-builder';
  tools.append(element('h2', '条件ビルダー'));
  if (!builder) {
    return tools;
  }

  let state = builder.conditionJsonToBuilderState(anomaly.condition_json || {});
  const placeSelect = element('select');
  placeSelect.name = `condition_place:${cardId}`;
  appendPlaceOptions(placeSelect, placeOptions, state.place);

  const expectedPlaceSelect = element('select');
  expectedPlaceSelect.name = `condition_expected_place:${cardId}`;
  appendPlaceOptions(expectedPlaceSelect, placeOptions, state.expected_place);

  const zoneSelect = element('select');
  zoneSelect.name = `condition_zone:${cardId}`;
  ['', 'school', 'town', 'home'].forEach((zone) => {
    const option = element('option', zone || '-');
    option.value = zone;
    option.selected = zone === state.zone;
    zoneSelect.append(option);
  });

  const timeFromInput = element('input');
  timeFromInput.name = `condition_time_from:${cardId}`;
  timeFromInput.placeholder = '21:00';
  timeFromInput.value = state.time_from;

  const timeToInput = element('input');
  timeToInput.name = `condition_time_to:${cardId}`;
  timeToInput.placeholder = '23:00';
  timeToInput.value = state.time_to;

  const minTensionInput = element('input');
  minTensionInput.name = `condition_min_tension:${cardId}`;
  minTensionInput.type = 'number';
  minTensionInput.min = '0';
  minTensionInput.max = '1';
  minTensionInput.step = '0.05';
  minTensionInput.value = state.min_tension;

  const stressInput = element('input');
  stressInput.name = `condition_stress_threshold_min:${cardId}`;
  stressInput.type = 'number';
  stressInput.min = '0';
  stressInput.max = '1';
  stressInput.step = '0.05';
  stressInput.value = state.stress_threshold_min;

  const requiredEventInput = element('input');
  requiredEventInput.name = `condition_required_event:${cardId}`;
  requiredEventInput.value = state.required_event;

  const multipleSelect = element('select');
  multipleSelect.name = `condition_multiple_characters:${cardId}`;
  [
    ['', '-'],
    ['true', '必要'],
    ['false', '不要']
  ].forEach(([value, label]) => {
    const option = element('option', label);
    option.value = value;
    option.selected = (state.multiple_characters === true && value === 'true')
      || (state.multiple_characters === false && value === 'false')
      || (state.multiple_characters === '' && value === '');
    multipleSelect.append(option);
  });

  const aloneSelect = element('select');
  aloneSelect.name = `condition_alone:${cardId}`;
  [
    ['', '-'],
    ['true', 'ひとり'],
    ['false', 'ひとりではない']
  ].forEach(([value, label]) => {
    const option = element('option', label);
    option.value = value;
    option.selected = (state.alone === true && value === 'true')
      || (state.alone === false && value === 'false')
      || (state.alone === '' && value === '');
    aloneSelect.append(option);
  });

  const message = element('p');
  message.className = 'onboarding-status';

  const applyToJson = () => {
    try {
      const base = parseJsonObjectOrThrow(conditionInput.value, 'condition_json');
      const nextCondition = builder.applyConditionBuilderState(base, {
        place: placeSelect.value,
        expected_place: expectedPlaceSelect.value,
        zone: zoneSelect.value,
        time_from: timeFromInput.value,
        time_to: timeToInput.value,
        min_tension: minTensionInput.value,
        required_event: requiredEventInput.value,
        multiple_characters: multipleSelect.value === 'true'
          ? true
          : (multipleSelect.value === 'false' ? false : ''),
        alone: aloneSelect.value === 'true'
          ? true
          : (aloneSelect.value === 'false' ? false : ''),
        stress_threshold_min: stressInput.value
      });
      conditionInput.value = JSON.stringify(nextCondition, null, 2);
      message.className = 'onboarding-status onboarding-status-ok';
      message.textContent = 'JSONへ反映しました。';
    } catch (error) {
      message.className = 'onboarding-status onboarding-status-error';
      message.textContent = error.message;
    }
  };

  const loadFromJson = () => {
    try {
      const condition = parseJsonObjectOrThrow(conditionInput.value, 'condition_json');
      state = builder.conditionJsonToBuilderState(condition);
      placeSelect.value = state.place;
      expectedPlaceSelect.value = state.expected_place;
      zoneSelect.value = state.zone;
      timeFromInput.value = state.time_from;
      timeToInput.value = state.time_to;
      minTensionInput.value = state.min_tension;
      requiredEventInput.value = state.required_event;
      multipleSelect.value = state.multiple_characters === true
        ? 'true'
        : (state.multiple_characters === false ? 'false' : '');
      aloneSelect.value = state.alone === true ? 'true' : (state.alone === false ? 'false' : '');
      stressInput.value = state.stress_threshold_min;
      message.className = 'onboarding-status onboarding-status-ok';
      message.textContent = 'JSONから反映しました。';
    } catch (error) {
      message.className = 'onboarding-status onboarding-status-error';
      message.textContent = error.message;
    }
  };

  [
    placeSelect,
    expectedPlaceSelect,
    zoneSelect,
    timeFromInput,
    timeToInput,
    minTensionInput,
    requiredEventInput,
    multipleSelect,
    aloneSelect,
    stressInput
  ]
    .forEach((input) => {
      input.addEventListener('change', applyToJson);
      input.addEventListener('blur', applyToJson);
    });

  const fields = element('div');
  fields.className = 'onboarding-field-grid';
  fields.append(
    element('label', 'place'),
    placeSelect,
    element('label', 'expected_place'),
    expectedPlaceSelect,
    element('label', 'zone'),
    zoneSelect,
    element('label', 'time_from'),
    timeFromInput,
    element('label', 'time_to'),
    timeToInput,
    element('label', 'min_tension'),
    minTensionInput,
    element('label', 'required_event'),
    requiredEventInput,
    element('label', 'multiple_characters'),
    multipleSelect,
    element('label', 'alone'),
    aloneSelect,
    element('label', 'stress_threshold_min'),
    stressInput
  );

  const actions = element('div');
  actions.className = 'admin-actions';
  const applyBtn = element('button', 'JSONへ反映');
  applyBtn.type = 'button';
  applyBtn.addEventListener('click', applyToJson);
  const loadBtn = element('button', 'JSONから反映');
  loadBtn.type = 'button';
  loadBtn.addEventListener('click', loadFromJson);
  actions.append(applyBtn, loadBtn);

  tools.append(fields, actions, message);
  return tools;
}

function renderAnomalyEditCard(anomaly, cardId, onDelete, placeOptions = []) {
  const cardNode = element('article');
  cardNode.className = 'admin-card onboarding-character-card';

  const header = element('div');
  header.className = 'onboarding-character-head';
  const info = element('div');
  info.className = 'onboarding-character-title';
  info.append(
    element('strong', anomaly.label || cardId || '(新規異変)'),
    element('span', 'anomaly')
  );
  header.append(info);

  if (onDelete) {
    const deleteBtn = element('button', '削除');
    deleteBtn.type = 'button';
    deleteBtn.className = 'onboarding-delete-btn';
    deleteBtn.addEventListener('click', onDelete);
    header.append(deleteBtn);
  }

  const labelInput = element('input');
  labelInput.name = `anomaly_label:${cardId}`;
  labelInput.value = anomaly.label || '';

  const conditionInput = element('textarea');
  conditionInput.name = `anomaly_condition:${cardId}`;
  conditionInput.value = anomaly._conditionJsonText !== undefined
    ? anomaly._conditionJsonText
    : JSON.stringify(anomaly.condition_json || {}, null, 2);

  const dramaInput = element('textarea');
  dramaInput.name = `anomaly_drama:${cardId}`;
  dramaInput.value = anomaly.drama_potential || '';

  const reasonsInput = element('textarea');
  reasonsInput.name = `anomaly_reasons:${cardId}`;
  reasonsInput.value = Array.isArray(anomaly.suggested_reasons)
    ? anomaly.suggested_reasons.join('\n')
    : (anomaly.suggested_reasons || '');

  const fields = element('div');
  fields.className = 'onboarding-field-grid';
  fields.append(
    element('label', '異変ラベル'),
    labelInput,
    element('label', 'condition_json'),
    conditionInput,
    element('label', 'drama_potential'),
    dramaInput,
    element('label', 'suggested_reasons'),
    reasonsInput
  );

  cardNode.append(header, fields, renderConditionBuilder(anomaly, cardId, conditionInput, placeOptions));
  return cardNode;
}

function parseJsonObjectOrThrow(text, fieldName) {
  const parsed = JSON.parse(text || '{}');
  if (parsed === null || Array.isArray(parsed) || typeof parsed !== 'object') {
    throw new Error(`${fieldName} は JSON object で入力してください。`);
  }
  return parsed;
}

function parseJsonArrayOrThrow(text, fieldName) {
  const parsed = JSON.parse(text || '[]');
  if (!Array.isArray(parsed)) {
    throw new Error(`${fieldName} は JSON array で入力してください。`);
  }
  return parsed;
}

function captureEventAnomalyFormValues(form, workingEvents, workingAnomalies) {
  workingEvents.forEach((eventItem) => {
    const cardId = eventItem._cardId;
    const dateEl = form.elements[`event_date:${cardId}`];
    const nameEl = form.elements[`event_name:${cardId}`];
    const durationEl = form.elements[`event_duration:${cardId}`];
    const atmosphereEl = form.elements[`event_atmosphere:${cardId}`];
    const emotionEl = form.elements[`event_emotion:${cardId}`];
    const forcePlaceEl = form.elements[`event_force_place:${cardId}`];

    if (dateEl) eventItem.event_date = dateEl.value.trim();
    if (nameEl) eventItem.name = nameEl.value.trim();
    if (durationEl) eventItem.duration_days = Number.parseInt(durationEl.value, 10) || 1;
    if (atmosphereEl) eventItem.atmosphere = atmosphereEl.value;
    if (emotionEl) eventItem._emotionImpactText = emotionEl.value;
    if (forcePlaceEl) eventItem.force_place = forcePlaceEl.value.trim();
  });

  workingAnomalies.forEach((anomaly) => {
    const cardId = anomaly._cardId;
    const labelEl = form.elements[`anomaly_label:${cardId}`];
    const conditionEl = form.elements[`anomaly_condition:${cardId}`];
    const dramaEl = form.elements[`anomaly_drama:${cardId}`];
    const reasonsEl = form.elements[`anomaly_reasons:${cardId}`];

    if (labelEl) anomaly.label = labelEl.value.trim();
    if (conditionEl) anomaly._conditionJsonText = conditionEl.value;
    try {
      anomaly.condition_json = parseConditionFromForm(form, cardId);
      anomaly._conditionJsonText = JSON.stringify(anomaly.condition_json, null, 2);
    } catch (error) {
      if (conditionEl) anomaly._conditionJsonText = conditionEl.value;
    }
    if (dramaEl) anomaly.drama_potential = dramaEl.value;
    if (reasonsEl) {
      anomaly.suggested_reasons = reasonsEl.value
        .split('\n')
        .map((item) => item.trim())
        .filter(Boolean);
    }
  });
}

async function renderEventAnomalyEditor(storyId) {
  const main = layout(`イベント/異変編集: ${storyId}`);
  const loadingEl = buildEditorSkeleton();
  main.append(loadingEl);
  const response = await api(`/stories/${storyId}/event-anomalies/edit-template`);
  loadingEl.remove();
  if (!response.ok) {
    const err = element('div', 'story の読み込みに失敗しました。world_config.yaml が見つからないか、DB に story が未登録です。');
    err.className = 'notice notice-error';
    main.append(err);
    return;
  }

  const payload = await response.json();
  const data = payload.data;
  const hero = element('section');
  hero.className = 'viewer-hero';
  const summary = element('div');
  summary.className = 'viewer-summary';
  summary.append(
    element('h2', data.story.title || storyId),
    element('p', `runtime: ${data.runtime_state || 'stopped'} / 編集: ${data.editable ? '可能' : '不可'}`)
  );
  hero.append(summary);
  main.append(hero);

  if (!data.editable) {
    const reason = data.read_only_reason || 'not_editable';
    const notices = {
      template_story: [
        'サンプル本体は直接変更しません。',
        '先に story を複製し、複製後の story でイベントと異変を調整します。'
      ],
      story_running: [
        'エンジン稼働中の YAML/DB 更新は避けます。',
        'ストーリー詳細で停止してから、この画面を再読み込みしてください。'
      ],
      story_not_imported: [
        'YAML はありますが、DB に story が未登録です。',
        'import_story または onboarding の複製作成を先に完了してください。'
      ]
    };
    main.append(renderOnboardingNotice('編集できません', notices[reason] || ['この story は現在編集対象外です。']));
    const actions = element('div');
    actions.className = 'admin-actions';
    actions.append(
      link(`/admin/stories/${storyId}`, 'ストーリー詳細へ'),
      link(`/admin/onboarding/${storyId}`, 'この story を複製')
    );
    main.append(actions);
    return;
  }

  const form = element('form');
  form.className = 'onboarding-form';
  form.append(renderOnboardingNotice('編集範囲', [
    '既存 event_calendar と anomaly_rules の基本項目を更新できます。',
    '追加: 各セクション末尾の「＋」ボタンから新規エントリを追加します。',
    '削除: 各カードの「削除」ボタンで該当エントリをリストから除去します。',
    '保存時に validate_story と update_story を実行し、DB に反映します。'
  ]));

  let newCardCounter = 0;
  const workingEvents = (data.events || []).map((e) => ({ ...e, _cardId: e.event_key }));
  const workingAnomalies = (data.anomalies || []).map((a) => ({ ...a, _cardId: a.anomaly_key }));
  const placeOptions = data.places || [];

  const eventList = element('section');
  eventList.className = 'admin-list';

  const anomalyList = element('section');
  anomalyList.className = 'admin-list';

  function renderEventCards() {
    while (eventList.lastChild && eventList.lastChild.tagName !== 'H2') {
      eventList.removeChild(eventList.lastChild);
    }
    if (!eventList.firstChild) eventList.append(element('h2', 'イベント'));
    workingEvents.forEach((eventItem) => {
      const cardId = eventItem._cardId;
      eventList.append(renderEventEditCard(eventItem, cardId, () => {
        const label = eventItem.name || eventItem.event_key || 'イベント';
        if (!window.confirm(`「${label}」を削除しますか?`)) return;
        captureEventAnomalyFormValues(form, workingEvents, workingAnomalies);
        const idx = workingEvents.indexOf(eventItem);
        if (idx >= 0) workingEvents.splice(idx, 1);
        renderEventCards();
      }));
    });
    const addBtn = element('button', '＋ イベントを追加');
    addBtn.type = 'button';
    addBtn.className = 'onboarding-add-btn';
    addBtn.addEventListener('click', () => {
      captureEventAnomalyFormValues(form, workingEvents, workingAnomalies);
      const cardId = `_new_event_${newCardCounter++}`;
      workingEvents.push({ event_key: '', name: '', event_date: '', duration_days: 1, _cardId: cardId });
      renderEventCards();
    });
    eventList.append(addBtn);
  }

  function renderAnomalyCards() {
    while (anomalyList.lastChild && anomalyList.lastChild.tagName !== 'H2') {
      anomalyList.removeChild(anomalyList.lastChild);
    }
    if (!anomalyList.firstChild) anomalyList.append(element('h2', '異変'));
    workingAnomalies.forEach((anomaly) => {
      const cardId = anomaly._cardId;
      anomalyList.append(renderAnomalyEditCard(anomaly, cardId, () => {
        const label = anomaly.label || anomaly.anomaly_key || '異変';
        if (!window.confirm(`「${label}」を削除しますか?`)) return;
        captureEventAnomalyFormValues(form, workingEvents, workingAnomalies);
        const idx = workingAnomalies.indexOf(anomaly);
        if (idx >= 0) workingAnomalies.splice(idx, 1);
        renderAnomalyCards();
      }, placeOptions));
    });
    const addBtn = element('button', '＋ 異変を追加');
    addBtn.type = 'button';
    addBtn.className = 'onboarding-add-btn';
    addBtn.addEventListener('click', () => {
      captureEventAnomalyFormValues(form, workingEvents, workingAnomalies);
      const cardId = `_new_anomaly_${newCardCounter++}`;
      workingAnomalies.push({ anomaly_key: '', label: '', condition_json: {}, _cardId: cardId });
      renderAnomalyCards();
    });
    anomalyList.append(addBtn);
  }

  eventList.append(element('h2', 'イベント'));
  anomalyList.append(element('h2', '異変'));
  renderEventCards();
  renderAnomalyCards();

  const submit = element('button', '保存');
  submit.type = 'submit';
  submit.className = 'btn btn-primary';
  const saveBar = buildEditorSaveBar(submit, [
    { href: `/admin/stories/${storyId}`, label: '詳細へ戻る' },
    { href: `/admin/viewer/${storyId}`, label: 'Viewer' }
  ]);

  const { setDirty, clearDirty } = attachEditorDirtyGuard();
  form.addEventListener('input', setDirty);
  form.addEventListener('change', setDirty);

  form.append(eventList, anomalyList, saveBar);
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    setSavingButton(submit, '保存中…');

    let eventPayload = [];
    let anomalyPayload = [];
    try {
      eventPayload = workingEvents.map((eventItem) => {
        const cardId = eventItem._cardId;
        return {
          event_key: eventItem.event_key,
          event_date: form.elements[`event_date:${cardId}`].value.trim(),
          name: form.elements[`event_name:${cardId}`].value.trim(),
          duration_days: Number.parseInt(
            form.elements[`event_duration:${cardId}`].value,
            10
          ),
          atmosphere: form.elements[`event_atmosphere:${cardId}`].value.trim(),
          emotion_impact: parseJsonObjectOrThrow(
            form.elements[`event_emotion:${cardId}`].value,
            'emotion_impact'
          ),
          force_place: form.elements[`event_force_place:${cardId}`].value.trim() || null
        };
      });

      anomalyPayload = workingAnomalies.map((anomaly) => {
        const cardId = anomaly._cardId;
        return {
          anomaly_key: anomaly.anomaly_key,
          label: form.elements[`anomaly_label:${cardId}`].value.trim(),
          condition_json: parseConditionFromForm(form, cardId),
          drama_potential: form.elements[`anomaly_drama:${cardId}`].value.trim(),
          suggested_reasons: form.elements[`anomaly_reasons:${cardId}`].value
            .split('\n')
            .map((item) => item.trim())
            .filter(Boolean)
        };
      });
    } catch (error) {
      clearSavingButton(submit);
      if (typeof window.showToast === 'function') window.showToast(error.message, 'error');
      return;
    }

    const updateResponse = await api(`/stories/${storyId}/event-anomalies`, {
      method: 'PUT',
      body: JSON.stringify({ events: eventPayload, anomalies: anomalyPayload })
    });
    clearSavingButton(submit);
    const updatePayload = await updateResponse.json();
    if (!updateResponse.ok) {
      const errMsg = (updatePayload.error && updatePayload.error.message)
        ? updatePayload.error.message
        : '保存に失敗しました。';
      if (typeof window.showToast === 'function') window.showToast(errMsg, 'error');
      return;
    }

    clearDirty();
    const result = updatePayload.data;
    const addedInfo = result.events_added || result.anomalies_added
      ? `（追加: イベント${result.events_added || 0} / 異変${result.anomalies_added || 0}）`
      : '';
    if (typeof window.showToast === 'function') {
      window.showToast(
        `保存完了: ${result.event_count} イベント / ${result.anomaly_count} 異変を反映しました${addedInfo}`,
        'success',
        { duration: 5000 }
      );
    }
  });
  main.append(form);
}

function renderDirectorPersonaEditCard(persona, cardId, onDelete) {
  const aesthetic = persona.aesthetic || {};

  const AESTHETIC_PARAMS = [
    ['tension_preference', '緊張感の好み'],
    ['character_depth',    'キャラクター深度'],
    ['action_preference',  '行動志向'],
    ['dialogue_wit',       '台詞の機知'],
    ['atmosphere_weight',  '雰囲気重視'],
    ['curiosity',          '好奇心'],
  ];

  const cardNode = element('article');
  cardNode.className = 'admin-card persona-edit-card';

  // ── ヘッダー（タイトル + 削除ボタン） ───────────────────────────────────
  const header = element('div');
  header.className = 'persona-card-header';
  const titleEl = element('h2', persona.name || persona.persona_id || '新規監督');
  const deleteBtn = element('button', '削除');
  deleteBtn.type = 'button';
  deleteBtn.className = 'btn-ghost-danger';
  deleteBtn.addEventListener('click', onDelete);
  header.append(titleEl, deleteBtn);

  // ── hidden input（captureDirectorPersonaFormValues との後方互換） ─────────
  const aestheticHidden = element('input');
  aestheticHidden.type = 'hidden';
  aestheticHidden.name = `director_aesthetic:${cardId}`;
  aestheticHidden.value = JSON.stringify(aesthetic);

  const syncAestheticHidden = () => {
    const obj = { ...aesthetic };
    AESTHETIC_PARAMS.forEach(([key]) => {
      const s = cardNode.querySelector(`[data-aesthetic-key="${key}"]`);
      if (s) {
        const value = Number.parseFloat(s.value);
        obj[key] = Number.isFinite(value) ? Math.max(0, Math.min(1, value)) : 0.5;
      }
    });
    aestheticHidden.value = JSON.stringify(obj);
  };

  // ── セクション: 基本情報 ──────────────────────────────────────────────────
  const personaIdInput = element('input');
  personaIdInput.name = `director_persona_id:${cardId}`;
  personaIdInput.placeholder = 'snake_case_id（例: comedy_amplifier）';
  personaIdInput.value = persona.persona_id || '';

  const nameInput = element('input');
  nameInput.name = `director_name:${cardId}`;
  nameInput.placeholder = '監督の表示名（例: コメディ増幅監督）';
  nameInput.value = persona.name || '';

  const updateTitle = () => {
    titleEl.textContent = nameInput.value || personaIdInput.value || '新規監督';
  };
  nameInput.addEventListener('input', updateTitle);
  personaIdInput.addEventListener('input', () => { if (!nameInput.value) updateTitle(); });

  const idWrap = element('div');
  const idLbl = element('label', '識別子 (persona_id)');
  idLbl.className = 'form-label';
  idWrap.append(idLbl, personaIdInput);

  const nameWrap = element('div');
  const nameLbl = element('label', '表示名');
  nameLbl.className = 'form-label';
  nameWrap.append(nameLbl, nameInput);

  const basicFields = element('div');
  basicFields.className = 'persona-fields-row';
  basicFields.append(idWrap, nameWrap);

  const basicSection = element('section');
  basicSection.className = 'persona-section';
  const basicTitle = element('h3', '基本情報');
  basicSection.append(basicTitle, basicFields);

  // ── セクション: 美学パラメータ（スライダー） ──────────────────────────────
  const aestheticGrid = element('div');
  aestheticGrid.className = 'aesthetic-grid';

  AESTHETIC_PARAMS.forEach(([key, label]) => {
    const row = element('div');
    row.className = 'aesthetic-slider-row';

    const lbl = element('label', label);

    const track = element('div');
    track.className = 'aesthetic-track';
    const slider = element('input');
    slider.type = 'range';
    slider.min = '0';
    slider.max = '1';
    slider.step = '0.05';
    slider.dataset.aestheticKey = key;
    const initVal = typeof aesthetic[key] === 'number' ? aesthetic[key] : 0.5;
    slider.value = String(initVal);
    track.append(slider);

    const valDisplay = element('span', initVal.toFixed(2));
    valDisplay.className = 'aesthetic-value-display';

    slider.addEventListener('input', () => {
      valDisplay.textContent = parseFloat(slider.value).toFixed(2);
      syncAestheticHidden();
    });

    row.append(lbl, track, valDisplay);
    aestheticGrid.append(row);
  });

  const aestheticHint = element('p', '各パラメータは 0.0（低）〜 1.0（高）で設定します。');
  aestheticHint.className = 'field-hint';

  const aestheticSection = element('section');
  aestheticSection.className = 'persona-section';
  aestheticSection.append(element('h3', '美学パラメータ'), aestheticHint, aestheticGrid);

  // ── セクション: 価値観 ────────────────────────────────────────────────────
  const valuesInput = element('textarea');
  valuesInput.name = `director_values:${cardId}`;
  valuesInput.placeholder = '1行1項目\n例: 感情の起伏が大きい場面を引き出す\n例: ボケとツッコミのテンポを最優先';
  valuesInput.value = (persona.values || []).join('\n');
  valuesInput.rows = 4;

  const valuesHint = element('p', '演出哲学・優先事項（1行1項目）。LLM のシーン生成方針に反映されます。');
  valuesHint.className = 'field-hint';

  const valuesSection = element('section');
  valuesSection.className = 'persona-section';
  valuesSection.append(element('h3', '価値観'), valuesHint, valuesInput);

  // ── セクション: 演出特性 ──────────────────────────────────────────────────
  const traitsInput = element('textarea');
  traitsInput.name = `director_traits:${cardId}`;
  traitsInput.placeholder = '1行1項目\n例: 沈黙の演出\n例: 長回し好き\n例: 食事描写重視';
  traitsInput.value = (persona.traits || []).join('\n');
  traitsInput.rows = 4;

  const traitsHint = element('p', 'キーワードがシーン指示プロンプトに直接注入されます（1行1項目）。');
  traitsHint.className = 'field-hint';

  const traitsSection = element('section');
  traitsSection.className = 'persona-section';
  traitsSection.append(element('h3', '演出特性'), traitsHint, traitsInput);

  // ── 組み立て ──────────────────────────────────────────────────────────────
  cardNode.append(header, aestheticHidden, basicSection, aestheticSection, valuesSection, traitsSection);
  return cardNode;
}

function captureDirectorPersonaFormValues(form, workingPersonas) {
  workingPersonas.forEach((persona) => {
    const cardId = persona._cardId;
    const idEl = form.elements[`director_persona_id:${cardId}`];
    const nameEl = form.elements[`director_name:${cardId}`];
    const aestheticEl = form.elements[`director_aesthetic:${cardId}`];
    const valuesEl = form.elements[`director_values:${cardId}`];
    const traitsEl = form.elements[`director_traits:${cardId}`];
    if (idEl) persona.persona_id = idEl.value.trim();
    if (nameEl) persona.name = nameEl.value.trim();
    if (aestheticEl) persona._aestheticText = aestheticEl.value;
    if (valuesEl) {
      persona.values = valuesEl.value.split('\n').map((item) => item.trim()).filter(Boolean);
    }
    if (traitsEl) {
      persona.traits = traitsEl.value.split('\n').map((item) => item.trim()).filter(Boolean);
    }
  });
}

async function renderDirectorPersonaEditor(storyId) {
  const main = layout(`監督管理: ${storyId}`);
  const loadingEl = buildEditorSkeleton();
  main.append(loadingEl);
  const response = await api(`/stories/${storyId}/director-personas/edit-template`);
  loadingEl.remove();
  if (!response.ok) {
    const err = element('div', 'story の読み込みに失敗しました。director.yaml が見つからないか、DB に story が未登録です。');
    err.className = 'notice notice-error';
    main.append(err);
    return;
  }

  const payload = await response.json();
  const data = payload.data;
  const activePersona = data.active_director_persona || null;
  const hero = element('section');
  hero.className = 'viewer-hero';
  const summary = element('div');
  summary.className = 'viewer-summary';
  summary.append(
    element('h2', data.story.title || storyId),
    element(
      'p',
      `runtime: ${data.runtime_state || 'stopped'} / 現在の監督: ${
        activePersona ? (activePersona.name || activePersona.persona_id) : '未設定'
      } / 定義編集: ${data.editable ? '可能' : '不可'}`
    )
  );
  hero.append(summary);
  main.append(hero);

  const turnInput = element('input');
  turnInput.type = 'number';
  turnInput.min = '0';
  turnInput.value = '0';
  turnInput.className = 'input';
  turnInput.style.maxWidth = '120px';
  const reasonInput = element('input');
  reasonInput.placeholder = '監督交代理由 例: もっと明るいテンポにしたい';
  reasonInput.className = 'input';
  const mkField = (labelText, input) => {
    const wrap = element('div');
    const lbl = element('label', labelText);
    lbl.className = 'form-label';
    wrap.append(lbl, input);
    return wrap;
  };
  const switchDirectorPersona = async (personaId) => {
    const response = await api(`/stories/${storyId}/director-personas/${personaId}/activate`, {
      method: 'PUT',
      body: JSON.stringify({
        turn_number: Number.parseInt(turnInput.value, 10) || 0,
        reason: reasonInput.value.trim()
      })
    });
    const result = await response.json();
    if (!response.ok) {
      const errMsg = (result.error && result.error.message) || '監督ペルソナの切替に失敗しました。';
      if (typeof window.showToast === 'function') window.showToast(errMsg, 'error');
      return;
    }
    if (typeof window.showToast === 'function') window.showToast('監督ペルソナを切り替えました。', 'success');
    await renderDirectorPersonaEditor(storyId);
  };

  const activeSection = element('section');
  activeSection.className = 'chapter-section card card-padded';
  const activeHead = element('div');
  activeHead.className = 'chapter-section-head';
  const activeHeading = element('h2', '現在の監督');
  activeHeading.className = 'chapter-section-title';
  activeHead.append(activeHeading);
  activeSection.append(activeHead);
  const activeSummary = element(
    'p',
    activePersona
      ? `${activePersona.name || activePersona.persona_id} / persona_id: ${activePersona.persona_id}`
      : '現在 active な監督はありません。'
  );
  activeSection.append(activeSummary);
  const switchGrid = element('div');
  switchGrid.className = 'chapter-controls-grid';
  switchGrid.append(mkField('turn_number', turnInput), mkField('切替理由', reasonInput));
  activeSection.append(switchGrid);
  (data.director_personas || []).forEach((persona) => {
    activeSection.append(renderDirectorPersonaCard(persona, {
      onActivate: () => switchDirectorPersona(persona.persona_id)
    }));
  });
  if (Array.isArray(data.director_swap_history) && data.director_swap_history.length > 0) {
    const history = element('ul');
    data.director_swap_history.slice(0, 5).forEach((entry) => {
      const reason = entry.reason ? ` / ${entry.reason}` : '';
      history.append(element(
        'li',
        `turn ${entry.turn_number}: ${entry.from_persona_id || '-'} -> ${entry.to_persona_id}${reason}`
      ));
    });
    activeSection.append(element('h2', '最近の監督交代履歴'), history);
  }
  main.append(activeSection);

  if (!data.editable) {
    const reason = data.read_only_reason || 'not_editable';
    const notices = {
      template_story: [
        'サンプル本体は直接変更しません。',
        '先に story を複製し、複製後の story で監督ペルソナを調整します。'
      ],
      story_running: [
        'エンジン稼働中の YAML/DB 更新は避けます。',
        'ストーリー詳細で停止してから、この画面を再読み込みしてください。'
      ],
      story_not_imported: [
        'YAML はありますが、DB に story が未登録です。',
        'import_story または onboarding の複製作成を先に完了してください。'
      ]
    };
    main.append(renderOnboardingNotice('編集できません', notices[reason] || ['この story は現在編集対象外です。']));
    const actions = element('div');
    actions.className = 'admin-actions';
    actions.append(
      link(`/admin/stories/${storyId}`, 'ストーリー詳細へ'),
      link(`/admin/onboarding/${storyId}`, 'この story を複製')
    );
    main.append(actions);
    return;
  }

  const form = element('form');
  form.className = 'onboarding-form';
  form.append(renderOnboardingNotice('編集範囲', [
    'director.yaml の persona 定義を更新できます。',
    '保存時に director_personas DB へ反映します。',
    '現在の監督切替と交代履歴はこの画面上部で扱います。'
  ]));

  let newCardCounter = 0;
  const workingPersonas = (data.personas || []).map((p) => ({
    ...p,
    _cardId: p.persona_id,
    _aestheticText: JSON.stringify(p.aesthetic || {}, null, 2)
  }));

  const defaultSelect = element('select');
  defaultSelect.name = 'default_active';
  const personaList = element('section');
  personaList.className = 'admin-list';
  personaList.append(element('h2', '監督ペルソナ'));

  function resolveDefaultActivePersonaId() {
    const selectedPersona = workingPersonas.find((persona) => persona._cardId === defaultSelect.value);
    return selectedPersona ? (selectedPersona.persona_id || '').trim() : '';
  }

  function renderPersonaCards() {
    captureDirectorPersonaFormValues(form, workingPersonas);
    const previousDefaultCardId = defaultSelect.value;
    const defaultFromPayload = workingPersonas.find(
      (persona) => (persona.persona_id || '') === (data.default_active || '')
    );
    const currentDefaultCardId = workingPersonas.some(
      (persona) => persona._cardId === previousDefaultCardId
    )
      ? previousDefaultCardId
      : (defaultFromPayload ? defaultFromPayload._cardId : ((workingPersonas[0] || {})._cardId || ''));
    while (personaList.lastChild && personaList.lastChild.tagName !== 'H2') {
      personaList.removeChild(personaList.lastChild);
    }
    defaultSelect.innerHTML = '';
    workingPersonas.forEach((persona) => {
      const option = element('option', persona.name || persona.persona_id || '名称未設定');
      option.value = persona._cardId;
      if (persona._cardId === currentDefaultCardId) option.selected = true;
      defaultSelect.append(option);
      personaList.append(renderDirectorPersonaEditCard(persona, persona._cardId, () => {
        const label = persona.name || persona.persona_id || '監督';
        if (!window.confirm(`「${label}」を削除しますか?`)) return;
        captureDirectorPersonaFormValues(form, workingPersonas);
        const idx = workingPersonas.indexOf(persona);
        if (idx >= 0) workingPersonas.splice(idx, 1);
        renderPersonaCards();
      }));
    });
    const addBtn = element('button', '＋ 監督を追加');
    addBtn.type = 'button';
    addBtn.className = 'onboarding-add-btn';
    addBtn.addEventListener('click', () => {
      captureDirectorPersonaFormValues(form, workingPersonas);
      const cardId = `_new_director_${newCardCounter++}`;
      workingPersonas.push({
        persona_id: '',
        name: '',
        aesthetic: {},
        values: [],
        traits: [],
        _cardId: cardId,
        _aestheticText: '{}'
      });
      renderPersonaCards();
    });
    personaList.append(addBtn);
  }

  const defaultSection = element('div');
  defaultSection.className = 'director-default-section';
  const defaultTitle = element('h3', '起動時のデフォルト監督');
  const defaultHint = element('p',
    'エンジン起動・ストーリーリセット時に最初にアクティブになる監督を指定します。' +
    '現在の監督を切り替える場合は、上部の「監督切替」から行います。'
  );
  defaultHint.className = 'field-hint';
  defaultSection.append(defaultTitle, defaultHint, defaultSelect);
  form.append(defaultSection, personaList);
  renderPersonaCards();

  const submit = element('button', '保存');
  submit.type = 'submit';
  submit.className = 'btn btn-primary';
  const saveBar = buildEditorSaveBar(submit, [
    { href: `/admin/chapters/${storyId}`, label: '章管理へ' },
    { href: `/admin/stories/${storyId}`, label: '詳細へ戻る' }
  ]);

  const { setDirty, clearDirty } = attachEditorDirtyGuard();
  form.addEventListener('input', setDirty);
  form.addEventListener('change', setDirty);

  form.append(saveBar);
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    setSavingButton(submit, '保存中…');
    captureDirectorPersonaFormValues(form, workingPersonas);

    let personas = [];
    try {
      personas = workingPersonas.map((persona) => ({
        persona_id: persona.persona_id,
        name: persona.name,
        aesthetic: parseJsonObjectOrThrow(persona._aestheticText || '{}', 'aesthetic'),
        values: persona.values || [],
        traits: persona.traits || []
      }));
    } catch (error) {
      clearSavingButton(submit);
      if (typeof window.showToast === 'function') window.showToast(error.message, 'error');
      return;
    }

    const updateResponse = await api(`/stories/${storyId}/director-personas`, {
      method: 'PUT',
      body: JSON.stringify({ default_active: resolveDefaultActivePersonaId(), personas })
    });
    clearSavingButton(submit);
    const updatePayload = await updateResponse.json();
    if (!updateResponse.ok) {
      const errMsg = (updatePayload.error && updatePayload.error.message)
        ? updatePayload.error.message
        : '保存に失敗しました。';
      if (typeof window.showToast === 'function') window.showToast(errMsg, 'error');
      return;
    }
    clearDirty();
    const result = updatePayload.data;
    if (typeof window.showToast === 'function') {
      window.showToast(`保存完了: ${result.persona_count} 監督を反映しました。`, 'success');
    }
  });
  main.append(form);
}

function parseBeatEventsText(text) {
  const parsed = JSON.parse(text || '[]');
  if (!Array.isArray(parsed)) {
    throw new Error('events は JSON array で入力してください。');
  }
  return parsed;
}

function ensureBeatEventDrafts(beat) {
  if (Array.isArray(beat._eventDrafts)) return;
  try {
    beat._eventDrafts = window.POCKETROLE_CHAPTER_BEATS.createBeatEventDrafts(
      parseBeatEventsText(beat._eventsText || '[]')
    );
  } catch (_error) {
    beat._eventDrafts = [];
  }
}

function syncBeatEventsTextFromDrafts(beat) {
  const events = window.POCKETROLE_CHAPTER_BEATS.serializeBeatEventDrafts(beat._eventDrafts || []);
  beat._eventsText = JSON.stringify(events, null, 2);
}

function captureBeatEventFormValues(form, chapter, beat) {
  if (!form) return;
  ensureBeatEventDrafts(beat);
  (beat._eventDrafts || []).forEach((eventDraft) => {
    const eventId = eventDraft._eventId;
    const prefix = `${chapter._cardId}:${beat._beatId}:${eventId}`;
    const typeEl = form.elements[`beat_event_type:${prefix}`];
    const descEl = form.elements[`beat_event_desc:${prefix}`];
    const targetEl = form.elements[`beat_event_target:${prefix}`];
    const placeEl = form.elements[`beat_event_place:${prefix}`];
    const priorityEl = form.elements[`beat_event_priority:${prefix}`];
    const extraEl = form.elements[`beat_event_extra:${prefix}`];
    if (typeEl) eventDraft.type = typeEl.value.trim();
    if (descEl) eventDraft.desc = descEl.value;
    if (targetEl) eventDraft.target_char_id = targetEl.value.trim();
    if (placeEl) eventDraft.place_id = placeEl.value.trim();
    if (priorityEl) eventDraft.priority = priorityEl.value.trim();
    if (extraEl) eventDraft._extraJsonText = extraEl.value;
  });
}

function captureChapterBeatFormValues(form, chapter) {
  if (!form) return;
  (chapter._beatDrafts || []).forEach((beat) => {
    const beatId = beat._beatId;
    const phaseEl = form.elements[`beat_phase:${chapter._cardId}:${beatId}`];
    const descriptionEl = form.elements[`beat_description:${chapter._cardId}:${beatId}`];
    const goalEl = form.elements[`beat_goal:${chapter._cardId}:${beatId}`];
    const eventsEl = form.elements[`beat_events:${chapter._cardId}:${beatId}`];
    if (phaseEl) beat.phase = phaseEl.value.trim();
    if (descriptionEl) beat.description = descriptionEl.value;
    if (goalEl) beat.goal = goalEl.value;
    if (eventsEl) beat._eventsText = eventsEl.value;
    captureBeatEventFormValues(form, chapter, beat);
  });
}

function renderBeatEventCard(chapter, beat, eventDraft, onDelete) {
  const eventNode = element('article');
  eventNode.className = 'onboarding-beat-event-card';
  const prefix = `${chapter._cardId}:${beat._beatId}:${eventDraft._eventId}`;
  const typeInput = element('input');
  typeInput.name = `beat_event_type:${prefix}`;
  typeInput.placeholder = 'notice / director_intervention';
  typeInput.value = eventDraft.type || '';
  const descInput = element('textarea');
  descInput.name = `beat_event_desc:${prefix}`;
  descInput.placeholder = 'desc';
  descInput.value = eventDraft.desc || '';
  const targetInput = element('input');
  targetInput.name = `beat_event_target:${prefix}`;
  targetInput.placeholder = 'target_char_id';
  targetInput.value = eventDraft.target_char_id || '';
  const placeInput = element('input');
  placeInput.name = `beat_event_place:${prefix}`;
  placeInput.placeholder = 'place_id';
  placeInput.value = eventDraft.place_id || '';
  const priorityInput = element('input');
  priorityInput.type = 'number';
  priorityInput.name = `beat_event_priority:${prefix}`;
  priorityInput.placeholder = 'priority';
  priorityInput.value = eventDraft.priority || '';
  const extraInput = element('textarea');
  extraInput.name = `beat_event_extra:${prefix}`;
  extraInput.placeholder = 'extra JSON object';
  extraInput.value = eventDraft._extraJsonText || '{}';

  const fields = element('div');
  fields.className = 'onboarding-field-grid';
  fields.append(
    element('label', 'type'),
    typeInput,
    element('label', 'desc'),
    descInput,
    element('label', 'target_char_id'),
    targetInput,
    element('label', 'place_id'),
    placeInput,
    element('label', 'priority'),
    priorityInput,
    element('label', 'extra JSON'),
    extraInput
  );
  const deleteBtn = element('button', 'event削除');
  deleteBtn.type = 'button';
  deleteBtn.className = 'onboarding-delete-btn';
  deleteBtn.addEventListener('click', onDelete);
  eventNode.append(element('h4', eventDraft.type || 'new event'), fields, deleteBtn);
  return eventNode;
}

function renderChapterBeatCard(chapter, beat, onDelete, rerender) {
  ensureBeatEventDrafts(beat);
  const beatNode = element('article');
  beatNode.className = 'onboarding-beat-card';

  // ── ヘッダー（フェーズ select + 削除ボタン） ──────────────────────────
  const beatHeader = element('div');
  beatHeader.className = 'beat-card-header';

  const phaseSelect = element('select');
  phaseSelect.name = `beat_phase:${chapter._cardId}:${beat._beatId}`;
  phaseSelect.className = 'beat-phase-select';
  ['setup', 'complication', 'turning_point', 'resolution'].forEach((phase) => {
    const opt = element('option', BEAT_PHASE_LABELS[phase] || phase);
    opt.value = phase;
    if ((beat.phase || 'setup') === phase) opt.selected = true;
    phaseSelect.append(opt);
  });

  const deleteBtn = element('button', 'ビート削除');
  deleteBtn.type = 'button';
  deleteBtn.className = 'btn-ghost-danger';
  deleteBtn.addEventListener('click', onDelete);
  beatHeader.append(phaseSelect, deleteBtn);

  // ── フィールド ────────────────────────────────────────────────────────
  const descriptionInput = element('textarea');
  descriptionInput.name = `beat_description:${chapter._cardId}:${beat._beatId}`;
  descriptionInput.placeholder = 'このビートの場面説明';
  descriptionInput.value = beat.description || '';
  const goalInput = element('textarea');
  goalInput.name = `beat_goal:${chapter._cardId}:${beat._beatId}`;
  goalInput.placeholder = 'このビートの達成目標（LLM が判定して次のビートへ遷移）';
  goalInput.value = beat.goal || '';
  const eventsInput = element('textarea');
  eventsInput.name = `beat_events:${chapter._cardId}:${beat._beatId}`;
  eventsInput.placeholder = 'イベント定義 (JSON 配列)';
  eventsInput.value = beat._eventsText || '[]';

  const fields = element('div');
  fields.className = 'onboarding-field-grid';
  fields.append(
    element('label', '場面説明'),
    descriptionInput,
    element('label', '達成目標'),
    goalInput,
    element('label', 'イベント (JSON)'),
    eventsInput
  );

  // ── イベントアクション ────────────────────────────────────────────────
  const eventActions = element('div');
  eventActions.className = 'admin-actions admin-actions-compact';
  const importEventsBtn = element('button', 'JSONからeventカードへ反映');
  importEventsBtn.type = 'button';
  importEventsBtn.addEventListener('click', () => {
    captureChapterBeatFormValues(beatNode.closest('form'), chapter);
    try {
      beat._eventDrafts = window.POCKETROLE_CHAPTER_BEATS.createBeatEventDrafts(
        parseBeatEventsText(beat._eventsText || '[]')
      );
    } catch (error) {
      window.alert(error.message);
      return;
    }
    rerender({ skipCapture: true });
  });
  const exportEventsBtn = element('button', 'eventカードをJSONへ反映');
  exportEventsBtn.type = 'button';
  exportEventsBtn.addEventListener('click', () => {
    captureChapterBeatFormValues(beatNode.closest('form'), chapter);
    try {
      syncBeatEventsTextFromDrafts(beat);
    } catch (error) {
      window.alert(error.message);
      return;
    }
    rerender({ skipCapture: true });
  });
  eventActions.append(importEventsBtn, exportEventsBtn);

  const eventSection = element('section');
  eventSection.className = 'onboarding-beat-event-list';
  eventSection.append(element('h4', 'イベントカード'), eventActions);
  (beat._eventDrafts || []).forEach((eventDraft) => {
    eventSection.append(renderBeatEventCard(chapter, beat, eventDraft, () => {
      captureChapterBeatFormValues(beatNode.closest('form'), chapter);
      const idx = beat._eventDrafts.indexOf(eventDraft);
      if (idx >= 0) beat._eventDrafts.splice(idx, 1);
      try {
        syncBeatEventsTextFromDrafts(beat);
      } catch (_error) {
        // Keep the remaining card values visible; submit will show validation errors.
      }
      rerender({ skipCapture: true });
    }));
  });
  const addEventBtn = element('button', '＋ イベントを追加');
  addEventBtn.type = 'button';
  addEventBtn.className = 'onboarding-add-btn';
  addEventBtn.addEventListener('click', () => {
    captureChapterBeatFormValues(beatNode.closest('form'), chapter);
    ensureBeatEventDrafts(beat);
    beat._eventDrafts.push(
      window.POCKETROLE_CHAPTER_BEATS.createBeatEventDraft({}, beat._eventDrafts.length)
    );
    rerender({ skipCapture: true });
  });
  eventSection.append(addEventBtn);

  beatNode.append(beatHeader, fields, eventSection);
  return beatNode;
}

function renderChapterDefinitionCard(chapter, cardId, onDelete, rerender) {
  chapter._expanded = chapter._expanded ?? false;

  const cardNode = element('article');
  cardNode.className = 'admin-card chapter-def-card';

  // ── ヘッダー（常時表示・ボタンで展開/折りたたみ） ──────────────────
  const header = element('div');
  header.className = 'chapter-def-header';

  const headerInfo = element('div');
  headerInfo.className = 'chapter-def-header-info';

  const titleEl = element('h2', chapter.title || chapter.chapter_id || '新規章');
  titleEl.className = 'chapter-def-title';

  const conditionChip = element('span', formatStartCondition(chapter.start_condition));
  conditionChip.className = 'chapter-condition-chip';
  const beatCountEl = element('span', `ビート: ${(chapter._beatDrafts || []).length}本`);
  beatCountEl.className = 'chapter-beat-count';

  const metaRow = element('div');
  metaRow.className = 'chapter-def-meta';
  metaRow.append(conditionChip, beatCountEl);
  headerInfo.append(titleEl, metaRow);

  const toggleBtn = element('button');
  toggleBtn.type = 'button';
  toggleBtn.className = 'chapter-toggle-btn';
  toggleBtn.setAttribute('aria-label', '章定義を展開または折りたたみ');
  const toggleIcon = element('span', chapter._expanded ? '▲' : '▼');
  toggleIcon.className = 'chapter-toggle-icon';
  toggleBtn.append(toggleIcon);

  const deleteBtn = element('button', '削除');
  deleteBtn.type = 'button';
  deleteBtn.className = 'btn-ghost-danger';
  deleteBtn.addEventListener('click', onDelete);

  const headerActions = element('div');
  headerActions.className = 'chapter-def-header-actions';
  headerActions.append(toggleBtn, deleteBtn);
  header.append(headerInfo, headerActions);

  // ── 本体（折りたたみ可能） ────────────────────────────────────────────
  const body = element('div');
  const bodyId = `chapter-def-body-${String(cardId).replace(/[^a-zA-Z0-9_-]/g, '-')}`;
  body.id = bodyId;
  body.className = 'chapter-def-body' + (chapter._expanded ? '' : ' chapter-def-body--collapsed');
  toggleBtn.setAttribute('aria-controls', bodyId);
  toggleBtn.setAttribute('aria-expanded', String(chapter._expanded));

  toggleBtn.addEventListener('click', () => {
    chapter._expanded = !chapter._expanded;
    body.classList.toggle('chapter-def-body--collapsed', !chapter._expanded);
    toggleIcon.textContent = chapter._expanded ? '▲' : '▼';
    toggleBtn.setAttribute('aria-expanded', String(chapter._expanded));
  });

  // フィールド inputs
  const chapterId = element('input');
  chapterId.name = `chapter_id:${cardId}`;
  chapterId.placeholder = 'snake_case_id（例: ch1_invasion）';
  chapterId.value = chapter.chapter_id || '';

  const titleInput = element('input');
  titleInput.name = `chapter_title:${cardId}`;
  titleInput.placeholder = '第N章「...」';
  titleInput.value = chapter.title || '';

  const updateHeader = () => {
    titleEl.textContent = titleInput.value || chapterId.value || '新規章';
  };
  titleInput.addEventListener('input', updateHeader);
  chapterId.addEventListener('input', () => { if (!titleInput.value) updateHeader(); });

  const themeInput = element('input');
  themeInput.name = `chapter_theme:${cardId}`;
  themeInput.placeholder = '例: 秘密と対立 — 廃校通告から始まる混乱';
  themeInput.value = chapter.theme || '';

  const startInput = element('input');
  startInput.name = `chapter_start:${cardId}`;
  startInput.placeholder = 'manual または turn >= 10 形式';
  startInput.value = chapter.start_condition || 'manual';

  const startHint = element('p', formatStartCondition(chapter.start_condition));
  startHint.className = 'field-hint';
  startInput.addEventListener('input', () => {
    const label = formatStartCondition(startInput.value);
    startHint.textContent = label;
    conditionChip.textContent = label;
  });

  const injectionInput = element('textarea');
  injectionInput.name = `chapter_injection:${cardId}`;
  injectionInput.placeholder = '全キャラのプロンプトに注入される場面説明テキスト';
  injectionInput.value = chapter.world_injection || '';
  injectionInput.rows = 5;

  // ── セクション: 基本情報 ──────────────────────────────────────────────
  const mkLabeledField = (labelText, input, hint = '') => {
    const wrap = element('div');
    const lbl = element('label', labelText);
    lbl.className = 'form-label';
    wrap.append(lbl, input);
    if (hint) {
      const hintEl = element('p', hint);
      hintEl.className = 'field-hint';
      wrap.append(hintEl);
    }
    return wrap;
  };

  const basicFields = element('div');
  basicFields.className = 'persona-fields-row';
  basicFields.append(mkLabeledField('識別子 (chapter_id)', chapterId), mkLabeledField('タイトル', titleInput));

  const basicSection = element('section');
  basicSection.className = 'persona-section';
  basicSection.append(
    element('h3', '基本情報'),
    basicFields,
    mkLabeledField('テーマ', themeInput, '章全体の雰囲気・対立軸を一文で表現します。')
  );

  // ── セクション: 開始条件 ──────────────────────────────────────────────
  const startWrap = mkLabeledField('開始条件', startInput);
  startWrap.append(startHint);
  const startSection = element('section');
  startSection.className = 'persona-section';
  startSection.append(element('h3', '開始条件'), startWrap);

  // ── セクション: ワールド注入テキスト ──────────────────────────────────
  const injectionHint = element('p', 'この章がアクティブな間、全キャラの LLM プロンプトに自動挿入されます。現在のシーン・状況を具体的に書いてください。');
  injectionHint.className = 'field-hint';
  const injectionSection = element('section');
  injectionSection.className = 'persona-section';
  injectionSection.append(element('h3', 'ワールド注入テキスト'), injectionHint, injectionInput);

  // ── セクション: ビート構成 ────────────────────────────────────────────
  const beatSequenceBar = element('div');
  beatSequenceBar.className = 'beat-sequence-bar';
  const presentPhases = new Set((chapter._beatDrafts || []).map((b) => b.phase));
  ['setup', 'complication', 'turning_point', 'resolution'].forEach((phase, i, arr) => {
    const step = element('span', BEAT_PHASE_LABELS[phase] || phase);
    step.className = 'beat-seq-step' + (presentPhases.has(phase) ? ' beat-seq-step--present' : '');
    beatSequenceBar.append(step);
    if (i < arr.length - 1) {
      const arrow = element('span', '→');
      arrow.className = 'beat-seq-arrow';
      beatSequenceBar.append(arrow);
    }
  });

  const beatHint = element('p', 'ビートは 導入 → 展開 → 転機 → 解決 の順で進行します。各ビートの「達成目標」が LLM によって判定されると次のビートへ遷移します。');
  beatHint.className = 'field-hint';

  const beatsSection = element('section');
  beatsSection.className = 'persona-section';
  beatsSection.append(element('h3', 'ビート構成'), beatHint, beatSequenceBar);

  (chapter._beatDrafts || []).forEach((beat) => {
    beatsSection.append(renderChapterBeatCard(chapter, beat, () => {
      captureChapterBeatFormValues(cardNode.closest('form'), chapter);
      const idx = chapter._beatDrafts.indexOf(beat);
      if (idx >= 0) chapter._beatDrafts.splice(idx, 1);
      if (chapter._beatDrafts.length === 0) {
        chapter._beatDrafts.push(window.POCKETROLE_CHAPTER_BEATS.createBeatDraft({}, 0));
      }
      rerender();
    }, rerender));
  });

  const addBeatBtn = element('button', '＋ ビートを追加');
  addBeatBtn.type = 'button';
  addBeatBtn.className = 'onboarding-add-btn';
  addBeatBtn.addEventListener('click', () => {
    captureChapterBeatFormValues(cardNode.closest('form'), chapter);
    chapter._beatDrafts.push(window.POCKETROLE_CHAPTER_BEATS.createBeatDraft({}, chapter._beatDrafts.length));
    rerender();
  });
  beatsSection.append(addBeatBtn);

  // ── 組み立て ──────────────────────────────────────────────────────────
  body.append(basicSection, startSection, injectionSection, beatsSection);
  cardNode.append(header, body);
  return cardNode;
}

function captureChapterDefinitionFormValues(form, workingChapters) {
  workingChapters.forEach((chapter) => {
    const cardId = chapter._cardId;
    const idEl = form.elements[`chapter_id:${cardId}`];
    const titleEl = form.elements[`chapter_title:${cardId}`];
    const themeEl = form.elements[`chapter_theme:${cardId}`];
    const startEl = form.elements[`chapter_start:${cardId}`];
    const injectionEl = form.elements[`chapter_injection:${cardId}`];
    if (idEl) chapter.chapter_id = idEl.value.trim();
    if (titleEl) chapter.title = titleEl.value.trim();
    if (themeEl) chapter.theme = themeEl.value.trim();
    if (startEl) chapter.start_condition = startEl.value.trim();
    if (injectionEl) chapter.world_injection = injectionEl.value;
    captureChapterBeatFormValues(form, chapter);
  });
}

async function renderChapterDefinitionEditor(storyId) {
  const main = layout(`章定義編集: ${storyId}`);
  const loadingEl = buildEditorSkeleton();
  main.append(loadingEl);
  const response = await api(`/stories/${storyId}/chapter-definitions/edit-template`);
  loadingEl.remove();
  if (!response.ok) {
    const err = element('div', 'story の読み込みに失敗しました。chapters.yaml が見つからないか、DB に story が未登録です。');
    err.className = 'notice notice-error';
    main.append(err);
    return;
  }

  const payload = await response.json();
  const data = payload.data;
  const hero = element('section');
  hero.className = 'viewer-hero';
  const summary = element('div');
  summary.className = 'viewer-summary';
  summary.append(
    element('h2', data.story.title || storyId),
    element('p', `runtime: ${data.runtime_state || 'stopped'} / 編集: ${data.editable ? '可能' : '不可'}`)
  );
  hero.append(summary);
  main.append(hero);

  if (!data.editable) {
    const reason = data.read_only_reason || 'not_editable';
    const notices = {
      template_story: [
        'サンプル本体は直接変更しません。',
        '先に story を複製し、複製後の story で章定義を調整します。'
      ],
      story_running: [
        'エンジン稼働中の YAML/DB 更新は避けます。',
        'ストーリー詳細で停止してから、この画面を再読み込みしてください。'
      ],
      story_not_imported: [
        'YAML はありますが、DB に story が未登録です。',
        'import_story または onboarding の複製作成を先に完了してください。'
      ]
    };
    main.append(renderOnboardingNotice('編集できません', notices[reason] || ['この story は現在編集対象外です。']));
    const actions = element('div');
    actions.className = 'admin-actions';
    actions.append(
      link(`/admin/stories/${storyId}`, 'ストーリー詳細へ'),
      link(`/admin/onboarding/${storyId}`, 'この story を複製')
    );
    main.append(actions);
    return;
  }

  const form = element('form');
  form.className = 'onboarding-form';
  form.append(renderOnboardingNotice('編集範囲', [
    'chapters.yaml の章定義と beat 配列を更新できます。',
    '保存時に story_chapters / story_chapter_beats DB へ反映します。',
    'active/closed の runtime 履歴操作は章管理画面に分離します。'
  ]));

  let newCardCounter = 0;
  const workingChapters = (data.chapters || []).map((chapter) => ({
    ...chapter,
    _cardId: chapter.chapter_id,
    _beatDrafts: window.POCKETROLE_CHAPTER_BEATS.createBeatDrafts(chapter.beats || [])
  }));

  // ── ストーリーフロー概要（全章の横並び一覧） ────────────────────────
  const flowContainer = element('div');
  flowContainer.className = 'chapter-flow-overview';

  const chapterList = element('section');
  chapterList.className = 'admin-list';
  chapterList.append(element('h2', '章定義'));

  function renderFlowOverview() {
    flowContainer.innerHTML = '';
    workingChapters.forEach((ch, i) => {
      const block = element('div');
      block.className = 'chapter-flow-block';
      const numEl = element('span', String(i + 1));
      numEl.className = 'chapter-flow-num';
      const nameEl = element('div', ch.title || ch.chapter_id || '未設定');
      nameEl.className = 'chapter-flow-name';
      const condEl = element('div', formatStartCondition(ch.start_condition));
      condEl.className = 'chapter-flow-cond';
      block.append(numEl, nameEl, condEl);
      flowContainer.append(block);
      if (i < workingChapters.length - 1) {
        const arrow = element('span', '→');
        arrow.className = 'chapter-flow-arrow';
        flowContainer.append(arrow);
      }
    });
  }

  function renderChapterCards(options = {}) {
    if (!options.skipCapture) {
      captureChapterDefinitionFormValues(form, workingChapters);
    }

    renderFlowOverview();

    while (chapterList.lastChild && chapterList.lastChild.tagName !== 'H2') {
      chapterList.removeChild(chapterList.lastChild);
    }
    workingChapters.forEach((chapter) => {
      chapterList.append(renderChapterDefinitionCard(chapter, chapter._cardId, () => {
        const label = chapter.title || chapter.chapter_id || '章';
        if (!window.confirm(`「${label}」を削除しますか?`)) return;
        captureChapterDefinitionFormValues(form, workingChapters);
        const idx = workingChapters.indexOf(chapter);
        if (idx >= 0) workingChapters.splice(idx, 1);
        renderChapterCards();
      }, renderChapterCards));
    });
    const addBtn = element('button', '＋ 章を追加');
    addBtn.type = 'button';
    addBtn.className = 'onboarding-add-btn';
    addBtn.addEventListener('click', () => {
      captureChapterDefinitionFormValues(form, workingChapters);
      const cardId = `_new_chapter_${newCardCounter++}`;
      workingChapters.push({
        chapter_id: '',
        title: '',
        theme: '',
        world_injection: '',
        start_condition: 'manual',
        _cardId: cardId,
        _beatDrafts: window.POCKETROLE_CHAPTER_BEATS.createBeatDrafts([]),
        _expanded: true,
      });
      renderChapterCards();
    });
    chapterList.append(addBtn);
  }

  renderChapterCards();

  const submit = element('button', '保存');
  submit.type = 'submit';
  submit.className = 'btn btn-primary';
  const saveBar = buildEditorSaveBar(submit, [
    { href: `/admin/chapters/${storyId}`, label: '章管理へ' },
    { href: `/admin/stories/${storyId}`, label: '詳細へ戻る' }
  ]);

  const { setDirty, clearDirty } = attachEditorDirtyGuard();
  form.addEventListener('input', setDirty);
  form.addEventListener('input', () => {
    captureChapterDefinitionFormValues(form, workingChapters);
    renderFlowOverview();
  });
  form.addEventListener('change', setDirty);
  form.addEventListener('change', () => {
    captureChapterDefinitionFormValues(form, workingChapters);
    renderFlowOverview();
  });

  form.append(flowContainer, chapterList, saveBar);
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    setSavingButton(submit, '保存中…');
    captureChapterDefinitionFormValues(form, workingChapters);

    let chapters = [];
    try {
      chapters = workingChapters.map((chapter) => ({
        chapter_id: chapter.chapter_id,
        title: chapter.title,
        theme: chapter.theme,
        world_injection: chapter.world_injection,
        start_condition: chapter.start_condition,
        beats: window.POCKETROLE_CHAPTER_BEATS.serializeBeatDrafts(chapter._beatDrafts || [])
      }));
    } catch (error) {
      clearSavingButton(submit);
      if (typeof window.showToast === 'function') window.showToast(error.message, 'error');
      return;
    }

    const updateResponse = await api(`/stories/${storyId}/chapter-definitions`, {
      method: 'PUT',
      body: JSON.stringify({ chapters })
    });
    clearSavingButton(submit);
    const updatePayload = await updateResponse.json();
    if (!updateResponse.ok) {
      const errMsg = (updatePayload.error && updatePayload.error.message)
        ? updatePayload.error.message
        : '保存に失敗しました。';
      if (typeof window.showToast === 'function') window.showToast(errMsg, 'error');
      return;
    }
    clearDirty();
    const result = updatePayload.data;
    if (typeof window.showToast === 'function') {
      window.showToast(
        `保存完了: ${result.chapter_count} 章 / ${result.beat_count} beat を反映しました。`,
        'success',
        { duration: 5000 }
      );
    }
  });
  main.append(form);
}

async function renderSettings() {
  const main = layout('設定', { subtitle: 'アーカイブ公開・LLM runtime などのシステム設定を変更します。' });
  const loadingEl = buildEditorSkeleton();
  main.append(loadingEl);
  const response = await api('/settings');
  loadingEl.remove();
  if (!response.ok) {
    const err = element('div', '設定の読み込みに失敗しました。');
    err.className = 'notice notice-error';
    main.append(err);
    return;
  }
  const payload = await response.json();

  // ── Archive section ──
  const archiveSection = element('section');
  archiveSection.className = 'settings-card card card-padded';
  const archiveHead = element('div');
  archiveHead.className = 'settings-section-head';
  const archiveH = element('h2', 'アーカイブ公開');
  archiveH.className = 'settings-section-title';
  archiveHead.append(archiveH);
  archiveSection.append(archiveHead);

  const archiveForm = element('form');
  archiveForm.className = 'settings-form';
  const intervalWrap = element('div');
  intervalWrap.className = 'settings-field';
  const intervalLabel = element('label', 'archive更新間隔 (分)');
  intervalLabel.className = 'form-label';
  const intervalHint = element('div', '公開 archive の再生成をこの間隔で行います。推奨: 5〜30 分。');
  intervalHint.className = 'form-hint';
  const intervalInput = element('input');
  intervalInput.type = 'number';
  intervalInput.min = '1';
  intervalInput.className = 'input';
  intervalInput.style.maxWidth = '120px';
  intervalInput.value = String(payload.data.archive_publish_interval_minutes);
  intervalWrap.append(intervalLabel, intervalInput, intervalHint);
  archiveForm.append(intervalWrap);

  const archiveSave = element('button', '保存');
  archiveSave.type = 'submit';
  archiveSave.className = 'btn btn-primary btn-sm';
  const archiveBar = element('div');
  archiveBar.className = 'settings-form-footer';
  archiveBar.append(archiveSave);
  archiveForm.append(archiveBar);

  archiveForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    setSavingButton(archiveSave, '保存中…');
    const r = await api('/settings/archive-publish', {
      method: 'PUT',
      body: JSON.stringify({ interval_minutes: Number.parseInt(intervalInput.value, 10) || 5 })
    });
    clearSavingButton(archiveSave);
    if (typeof window.showToast === 'function') {
      window.showToast(r.ok ? 'archive 設定を保存しました。' : '保存に失敗しました。', r.ok ? 'success' : 'error');
    }
  });
  archiveSection.append(archiveForm);
  main.append(archiveSection);

  // ── LLM Runtime section ──
  const llmRuntime = payload.data.llm_runtime || {};
  if (llmRuntime.error) {
    const errNotice = element('div', `LLM runtime 設定を読み込めません: ${llmRuntime.error}`);
    errNotice.className = 'notice notice-error';
    main.append(errNotice);
    return;
  }

  const llmSection = element('section');
  llmSection.className = 'settings-card card card-padded';
  const llmHead = element('div');
  llmHead.className = 'settings-section-head';
  const llmH = element('h2', 'LLM Runtime');
  llmH.className = 'settings-section-title';
  if (llmRuntime.path) {
    const pathHint = element('span', `設定ファイル: ${llmRuntime.path}`);
    pathHint.className = 'text-muted text-xs';
    llmHead.append(llmH, pathHint);
  } else {
    llmHead.append(llmH);
  }
  llmSection.append(llmHead);

  const llmForm = element('form');
  llmForm.className = 'settings-form';

  const workingProfiles = (llmRuntime.profiles || []).map((profile) => ({ ...profile }));
  const captureProfiles = () => {
    workingProfiles.forEach((profile, index) => {
      const nameEl = llmForm.elements[`profile_name:${index}`];
      const providerEl = llmForm.elements[`provider:${index}`];
      const modelEl = llmForm.elements[`model:${index}`];
      if (nameEl) profile.profile_name = nameEl.value.trim();
      if (providerEl) profile.provider = providerEl.value.trim();
      if (modelEl) profile.model = modelEl.value.trim();
    });
  };

  let activeProfile = llmRuntime.active_profile || '';
  const profileListEl = element('div');
  profileListEl.className = 'settings-profile-list';

  const renderProfiles = () => {
    profileListEl.innerHTML = '';
    workingProfiles.forEach((profile, index) => {
      const card = element('label');
      card.className = 'settings-profile-card';
      const radio = element('input');
      radio.type = 'radio';
      radio.name = 'active_profile_radio';
      radio.value = profile.profile_name || '';
      radio.checked = (profile.profile_name || '') === activeProfile;
      radio.addEventListener('change', () => { activeProfile = radio.value; });

      const cardBody = element('div');
      cardBody.className = 'settings-profile-body';

      const profileName = element('input');
      profileName.name = `profile_name:${index}`;
      profileName.value = profile.profile_name || '';
      profileName.placeholder = 'profile_name';
      profileName.className = 'input settings-profile-name';

      const provider = element('input');
      provider.name = `provider:${index}`;
      provider.value = profile.provider || '';
      provider.placeholder = 'provider (openai / anthropic / ollama …)';
      provider.className = 'input';

      const model = element('input');
      model.name = `model:${index}`;
      model.value = profile.model || '';
      model.placeholder = 'model name';
      model.className = 'input';

      const delBtn = element('button', '削除');
      delBtn.type = 'button';
      delBtn.className = 'btn btn-danger btn-sm';
      delBtn.addEventListener('click', () => {
        const label = profile.profile_name || 'profile';
        if (!window.confirm(`「${label}」を削除しますか?`)) return;
        captureProfiles();
        workingProfiles.splice(index, 1);
        renderProfiles();
      });

      cardBody.append(profileName, provider, model, delBtn);
      card.append(radio, cardBody);
      profileListEl.append(card);
    });
  };
  renderProfiles();

  const addProfileBtn = element('button', '＋ profile を追加');
  addProfileBtn.type = 'button';
  addProfileBtn.className = 'btn btn-ghost btn-sm';
  addProfileBtn.addEventListener('click', () => {
    captureProfiles();
    workingProfiles.push({ profile_name: `new_profile_${workingProfiles.length + 1}`, provider: 'openai', model: '' });
    renderProfiles();
  });

  const overrideWrap = element('div');
  overrideWrap.className = 'settings-field';
  const overrideLabel = element('label', 'story ごとの LLM override');
  overrideLabel.className = 'form-label';
  const overrideHint = element('div', '1 行に story_id: profile_name の形式で。空行は無視されます。');
  overrideHint.className = 'form-hint';
  const overrideInput = element('textarea');
  overrideInput.name = 'story_overrides';
  overrideInput.className = 'textarea-field settings-override-textarea';
  overrideInput.rows = 4;
  overrideInput.value = Object.entries(llmRuntime.story_overrides || {})
    .map(([sid, pname]) => `${sid}: ${pname}`)
    .join('\n');
  overrideWrap.append(overrideLabel, overrideInput, overrideHint);

  const llmSave = element('button', 'LLM 設定を保存');
  llmSave.type = 'submit';
  llmSave.className = 'btn btn-primary btn-sm';
  const llmBar = element('div');
  llmBar.className = 'settings-form-footer';
  const llmHint = element('span', '起動中の story は再起動後に新しい profile を使います。');
  llmHint.className = 'text-muted text-xs';
  llmBar.append(llmHint, llmSave);

  llmForm.append(profileListEl, addProfileBtn, overrideWrap, llmBar);
  llmForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    setSavingButton(llmSave, '保存中…');
    captureProfiles();
    const storyOverrides = {};
    overrideInput.value.split('\n').forEach((line) => {
      const trimmed = line.trim();
      if (!trimmed) return;
      const colonIdx = trimmed.indexOf(':');
      if (colonIdx < 0) return;
      const sid = trimmed.slice(0, colonIdx).trim();
      const pname = trimmed.slice(colonIdx + 1).trim();
      if (sid && pname) storyOverrides[sid] = pname;
    });
    const finalActive = activeProfile || (workingProfiles[0] && workingProfiles[0].profile_name) || '';
    const updateResponse = await api('/settings/llm-runtime', {
      method: 'PUT',
      body: JSON.stringify({ active_profile: finalActive, profiles: workingProfiles, story_overrides: storyOverrides })
    });
    clearSavingButton(llmSave);
    const updatePayload = await updateResponse.json();
    if (!updateResponse.ok) {
      const errMsg = (updatePayload.error && updatePayload.error.message) || '保存に失敗しました。';
      if (typeof window.showToast === 'function') window.showToast(errMsg, 'error');
      return;
    }
    if (typeof window.showToast === 'function') {
      const d = updatePayload.data;
      window.showToast(`保存完了: ${d.profile_count} profiles / ${d.story_override_count} overrides`, 'success');
    }
  });
  llmSection.append(llmForm);
  main.append(llmSection);

  // ── デフォルト Web 投稿先 ──────────────────────────────────────────────────
  const webPostSection = element('section');
  webPostSection.className = 'settings-card card card-padded';
  const webPostHead = element('div');
  webPostHead.className = 'settings-section-head';
  const webPostH = element('h2', 'デフォルト Web 投稿先');
  webPostH.className = 'settings-section-title';
  const webPostHint = element('div',
    '各ストーリーで投稿先が未設定の場合に使用されます。ストーリーごとに上書きしたい場合は各ストーリーの管理ページで設定できます。');
  webPostHint.className = 'form-hint';
  webPostHead.append(webPostH, webPostHint);
  webPostSection.append(webPostHead);
  main.append(webPostSection);

  api('/settings/web-post').then(async (resp) => {
    const wpData = resp.ok ? ((await resp.json()).data || {}) : {};

    const webPostForm = element('form');
    webPostForm.className = 'settings-form';

    const urlWrap = element('div');
    urlWrap.className = 'settings-field';
    const urlLabel = element('label', 'Receiver URL');
    urlLabel.className = 'form-label';
    const urlInput = element('input');
    urlInput.type = 'url';
    urlInput.className = 'input';
    urlInput.value = wpData.receiver_url || '';
    urlInput.placeholder = 'https://example.com/receiver.php';
    urlWrap.append(urlLabel, urlInput);
    webPostForm.append(urlWrap);

    const tokenField = createTokenField('Auth Token', '', '変更する場合のみ入力', Boolean(wpData.has_auth_token));
    webPostForm.append(tokenField.element);

    const wpSave = element('button', '保存');
    wpSave.type = 'submit';
    wpSave.className = 'btn btn-primary btn-sm';
    const wpBar = element('div');
    wpBar.className = 'settings-form-footer';
    wpBar.append(wpSave);
    webPostForm.append(wpBar);

    webPostForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      setSavingButton(wpSave, '保存中…');
      const r = await api('/settings/web-post', {
        method: 'PUT',
        body: JSON.stringify(Object.assign({
          receiver_url: urlInput.value.trim(),
          clear_auth_token: tokenField.shouldClear(),
        }, tokenField.getValue() ? { auth_token: tokenField.getValue() } : {})),
      });
      clearSavingButton(wpSave);
      window.showToast?.(
        r.ok ? 'デフォルト Web 投稿先を保存しました。' : '保存に失敗しました。',
        r.ok ? 'success' : 'error',
      );
    });

    webPostSection.append(webPostForm);
  }).catch(() => {
    webPostSection.append(element('div', '設定の読み込みに失敗しました。'));
  });
}

function renderChapterCard(chapter, options = {}) {
  const node = element('article');
  node.className = 'admin-card';
  const title = chapter.title || chapter.chapter_id || `第${chapter.id}章`;
  const statusLabel = CHAPTER_STATUS_LABELS[chapter.status] ?? chapter.status;
  const beatLabel   = BEAT_PHASE_LABELS[chapter.current_beat] ?? chapter.current_beat ?? '-';
  node.append(
    element('h2', title),
    element('p', `状態: ${statusLabel} / ビート: ${beatLabel}`),
    element('p', `テーマ: ${chapter.theme || '-'} / 開始条件: ${chapter.start_condition || '-'}`)
  );
  if (chapter.world_injection) {
    node.append(element('p', chapter.world_injection));
  }
  if (Array.isArray(chapter.beats) && chapter.beats.length > 0) {
    const beats = element('ul');
    chapter.beats.forEach((beat) => {
      const phaseLabel  = BEAT_PHASE_LABELS[beat.phase] ?? beat.phase ?? '-';
      const statusMark  = beat.status === 'reached' ? ' ✓' : beat.status === 'skipped' ? ' —' : '';
      beats.append(element('li', `${phaseLabel}${statusMark}: ${beat.goal || beat.description || '-'}`));
    });
    node.append(beats);
  }
  if (options.onActivate || options.onClose || options.onReopen || options.onUpdateBeat) {
    const actions = element('div');
    actions.className = 'admin-actions';
    if (options.onActivate) {
      const button = element('button', '有効化');
      button.type = 'button';
      button.addEventListener('click', options.onActivate);
      actions.append(button);
    }
    if (options.onClose) {
      const button = element('button', '閉じる');
      button.type = 'button';
      button.addEventListener('click', options.onClose);
      actions.append(button);
    }
    if (options.onReopen) {
      const button = element('button', '再オープン');
      button.type = 'button';
      button.addEventListener('click', options.onReopen);
      actions.append(button);
    }
    if (options.onUpdateBeat && Array.isArray(chapter.beats) && chapter.beats.length > 0) {
      const beatSelect = element('select');
      chapter.beats.forEach((beat) => {
        // 表示は日本語、value は英語のまま（API に送る値が変わらない）
        const option = element('option', BEAT_PHASE_LABELS[beat.phase] ?? beat.phase ?? '-');
        option.value = beat.phase || '';
        option.selected = beat.phase === chapter.current_beat;
        beatSelect.append(option);
      });
      const button = element('button', 'ビート更新');
      button.type = 'button';
      button.addEventListener('click', () => options.onUpdateBeat(beatSelect.value));
      actions.append(beatSelect, button);
    }
    node.append(actions);
  }
  return node;
}

function renderChapterProposalCard(proposal, options) {
  const node = element('article');
  node.className = 'admin-card';
  const chapter = proposal.proposed_chapter_json || {};
  const pStatusLabel = PROPOSAL_STATUS_LABELS[proposal.admin_status] ?? proposal.admin_status;
  node.append(
    element('h2', chapter.title || proposal.theme || `章案 ${proposal.id}`),
    element('p', `状態: ${pStatusLabel} / テーマ: ${proposal.theme || '-'}`),
    element('p', chapter.world_injection || '')
  );
  const beats = chapter.beats || [];
  if (beats.length > 0) {
    const list = element('ul');
    beats.forEach((beat) => {
      const phaseLabel = BEAT_PHASE_LABELS[beat.phase] ?? beat.phase ?? '-';
      list.append(element('li', `${phaseLabel}: ${beat.goal || beat.description || '-'}`));
    });
    node.append(list);
  }
  if (proposal.admin_status === 'pending') {
    const actions = element('div');
    actions.className = 'admin-actions';
    const approve = element('button', '承認');
    approve.type = 'button';
    approve.addEventListener('click', () => options.onApprove(proposal.id));
    const reject = element('button', '却下');
    reject.type = 'button';
    reject.addEventListener('click', () => options.onReject(proposal.id));
    actions.append(approve, reject);
    node.append(actions);
  }
  return node;
}

function renderDirectorPersonaCard(persona, options = {}) {
  const node = element('article');
  node.className = 'admin-card';
  const active = Number.parseInt(persona.is_active, 10) === 1;
  const aesthetic = persona.aesthetic_json || {};
  node.append(
    element('h2', `${persona.name || persona.persona_id}${active ? ' / active' : ''}`),
    element('p', `persona_id: ${persona.persona_id}`),
    element('p', `tension: ${aesthetic.tension_preference ?? '-'} / pacing: ${aesthetic.pacing ?? '-'}`)
  );
  if (Array.isArray(persona.values_json) && persona.values_json.length > 0) {
    node.append(element('p', `values: ${persona.values_json.join(', ')}`));
  }
  if (Array.isArray(persona.traits_json) && persona.traits_json.length > 0) {
    node.append(element('p', `traits: ${persona.traits_json.join(', ')}`));
  }
  if (!active && options.onActivate) {
    const actions = element('div');
    actions.className = 'admin-actions';
    const button = element('button', 'この監督に切り替え');
    button.type = 'button';
    button.addEventListener('click', options.onActivate);
    actions.append(button);
    node.append(actions);
  }
  return node;
}

async function renderChapterManager(storyId) {
  const main = layout(`章管理: ${storyId}`);
  const loadingEl = buildEditorSkeleton();
  main.append(loadingEl);

  const [chapterResp, proposalResp] = await Promise.all([
    api(`/stories/${storyId}/chapters`),
    api(`/stories/${storyId}/chapter-proposals?status=pending`)
  ]);
  loadingEl.remove();

  if (!chapterResp.ok) {
    const err = element('div', 'story が見つからないか、chapter overview を取得できませんでした。');
    err.className = 'notice notice-error';
    main.append(err);
    return;
  }

  const chapterPayload = await chapterResp.json();
  const proposalPayload = proposalResp.ok ? await proposalResp.json() : { data: { proposals: [] } };
  const data = chapterPayload.data;

  const hero = element('section');
  hero.className = 'chapter-manager-hero card card-padded';

  const heroTop = element('div');
  heroTop.className = 'story-detail-hero-top';

  const pills = element('div');
  pills.className = 'story-detail-pills';
  const isRunning = data.runtime_state === 'running';
  const runtimePill = element('span', isRunning ? '稼働中' : '停止中');
  runtimePill.className = 'pill ' + (isRunning ? 'pill-running' : 'pill-stopped');
  pills.append(runtimePill);

  const persona = data.active_director_persona;
  if (persona) {
    const directorPill = element('span', `監督: ${persona.name}`);
    directorPill.className = 'pill pill-info';
    pills.append(directorPill);
  }
  const heroActions = element('div');
  heroActions.className = 'story-detail-hero-actions';
  const backLink = link(`/admin/stories/${storyId}`, '詳細へ戻る');
  backLink.className = 'btn btn-ghost btn-sm';
  heroActions.append(backLink);

  heroTop.append(pills, heroActions);
  hero.append(heroTop);
  main.append(hero);

  const turnInput = element('input');
  turnInput.type = 'number';
  turnInput.min = '0';
  turnInput.value = '0';
  turnInput.className = 'input';
  turnInput.style.maxWidth = '120px';
  const chapterCloseReasonInput = element('input');
  chapterCloseReasonInput.placeholder = '章を閉じる理由 例: 手動調整';
  chapterCloseReasonInput.className = 'input';

  const chapterOperationControls = element('section');
  chapterOperationControls.className = 'card card-padded chapter-controls-card';
  const ctrlNotice = element('div');
  ctrlNotice.className = 'chapter-controls-notice';
  ctrlNotice.innerHTML = '<strong>操作共通設定</strong> — turn_number は章案承認・有効化・close/reopen に使います。';
  const ctrlGrid = element('div');
  ctrlGrid.className = 'chapter-controls-grid';
  const mkField = (labelText, input) => {
    const wrap = element('div');
    const lbl = element('label', labelText);
    lbl.className = 'form-label';
    wrap.append(lbl, input);
    return wrap;
  };
  ctrlGrid.append(mkField('turn_number', turnInput), mkField('クローズ理由', chapterCloseReasonInput));
  chapterOperationControls.append(ctrlNotice, ctrlGrid);
  main.append(chapterOperationControls);

  const proposalForm = element('form');
  proposalForm.className = 'card card-padded chapter-generate-card';
  const themeInput = element('input');
  themeInput.placeholder = '章案テーマ 例: 文化祭';
  themeInput.className = 'input';
  const generateButton = element('button', '章案を生成');
  generateButton.type = 'submit';
  generateButton.className = 'btn btn-primary';
  const generateNotice = element('div');
  generateNotice.className = 'chapter-generate-notice';
  generateNotice.innerHTML = '<strong>章案生成</strong> — engine running かつ chapter_generator が有効な story で使用できます。生成には数十秒かかる場合があります。';
  const genGrid = element('div');
  genGrid.className = 'chapter-generate-grid';
  genGrid.append(mkField('テーマ', themeInput), generateButton);
  proposalForm.append(generateNotice, genGrid);
  proposalForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    setSavingButton(generateButton, '生成中… (数十秒)');
    const response = await api(`/stories/${storyId}/chapter-proposals/generate`, {
      method: 'POST',
      body: JSON.stringify({
        theme: themeInput.value.trim(),
        turn_number: Number.parseInt(turnInput.value, 10) || 0
      })
    });
    clearSavingButton(generateButton);
    if (!response.ok) {
      const p = await response.json();
      const errMsg = (p.error && p.error.message) || '章案生成に失敗しました。';
      if (typeof window.showToast === 'function') window.showToast(errMsg, 'error');
      return;
    }
    if (typeof window.showToast === 'function') window.showToast('章案を生成しました。', 'success');
    await renderChapterManager(storyId);
  });
  main.append(proposalForm);

  const chapterApiAction = async (path, method, body, successMsg) => {
    const response = await api(path, { method, body: JSON.stringify(body) });
    if (!response.ok) {
      const p = await response.json();
      const errMsg = (p.error && p.error.message) || '操作に失敗しました。';
      if (typeof window.showToast === 'function') window.showToast(errMsg, 'error');
      return;
    }
    if (typeof window.showToast === 'function') window.showToast(successMsg, 'success');
    await renderChapterManager(storyId);
  };

  const activateChapter = (chapterId) => chapterApiAction(
    `/stories/${storyId}/chapters/${chapterId}/activate`, 'POST',
    { turn_number: Number.parseInt(turnInput.value, 10) || 0 },
    '章を有効化しました。'
  );
  const closeChapter = (chapterId) => chapterApiAction(
    `/stories/${storyId}/chapters/${chapterId}/close`, 'POST',
    { turn_number: Number.parseInt(turnInput.value, 10) || 0, reason: chapterCloseReasonInput.value.trim() || 'manual_admin', carry_over: {} },
    '章を閉じました。'
  );
  const reopenChapter = (chapterId) => chapterApiAction(
    `/stories/${storyId}/chapters/${chapterId}/reopen`, 'POST',
    { turn_number: Number.parseInt(turnInput.value, 10) || 0 },
    '章を再オープンしました。'
  );
  const updateChapterBeat = (chapterId, currentBeat) => chapterApiAction(
    `/stories/${storyId}/chapters/${chapterId}/current-beat`, 'PUT',
    { current_beat: currentBeat },
    'current beat を更新しました。'
  );
  const approveProposal = (proposalId) => chapterApiAction(
    `/stories/${storyId}/chapter-proposals/${proposalId}/approve`, 'PUT',
    { turn_number: Number.parseInt(turnInput.value, 10) || 0 },
    '章案を承認しました。'
  );
  const rejectProposal = (proposalId) => chapterApiAction(
    `/stories/${storyId}/chapter-proposals/${proposalId}/reject`, 'PUT',
    {},
    '章案を却下しました。'
  );

  const directorSection = element('section');
  directorSection.className = 'chapter-section card card-padded';
  const directorHead = element('div');
  directorHead.className = 'chapter-section-head';
  const directorHeading = element('h2', '監督ペルソナ');
  directorHeading.className = 'chapter-section-title';
  directorHead.append(directorHeading);
  const directorActions = element('div');
  directorActions.className = 'admin-actions';
  const directorLink = link(`/admin/directors/${storyId}`, '監督管理へ');
  directorLink.className = 'btn btn-secondary btn-sm';
  directorActions.append(directorLink);
  directorHead.append(directorActions);
  const activeDirectorText = data.active_director_persona
    ? `${data.active_director_persona.name || data.active_director_persona.persona_id} / persona_id: ${data.active_director_persona.persona_id}`
    : '現在 active な監督はありません。';
  directorSection.append(directorHead, element('p', `現在の監督: ${activeDirectorText}`));
  main.append(directorSection);

  const mkChapterSection = (title, pillCls) => {
    const sec = element('section');
    sec.className = 'chapter-section card card-padded';
    const head = element('div');
    head.className = 'chapter-section-head';
    const h = element('h2', title);
    h.className = 'chapter-section-title';
    const pill = element('span');
    pill.className = 'pill ' + (pillCls || 'pill-stopped');
    head.append(h, pill);
    sec.append(head);
    return { sec, pill };
  };

  const { sec: activeSection, pill: activePill } = mkChapterSection('進行中の章', 'pill-running');
  if (data.active_chapter) {
    activePill.textContent = '1 章';
    activeSection.append(renderChapterCard(data.active_chapter, {
      onClose: () => closeChapter(data.active_chapter.id),
      onUpdateBeat: (currentBeat) => updateChapterBeat(data.active_chapter.id, currentBeat)
    }));
  } else {
    activePill.textContent = '0 章';
    const empty = element('p', '現在 active な chapter はありません。');
    empty.className = 'text-muted';
    activeSection.append(empty);
  }
  main.append(activeSection);

  const { sec: pendingSection, pill: pendingPill } = mkChapterSection('待機中の章', 'pill-warning');
  const pendingChapters = data.pending_chapters || [];
  pendingPill.textContent = `${pendingChapters.length} 章`;
  pendingChapters.forEach((chapter) => {
    pendingSection.append(renderChapterCard(chapter, {
      onActivate: () => activateChapter(chapter.id)
    }));
  });
  if (pendingChapters.length === 0) {
    const empty = element('p', '待機中の章はありません。');
    empty.className = 'text-muted';
    pendingSection.append(empty);
  }
  main.append(pendingSection);

  const { sec: proposalSection, pill: proposalPill } = mkChapterSection('章案 (proposals)', 'pill-info');
  const proposals = (proposalPayload.data && proposalPayload.data.proposals) || [];
  proposalPill.textContent = `${proposals.length} 件`;
  proposals.forEach((proposal) => {
    proposalSection.append(renderChapterProposalCard(proposal, {
      onApprove: approveProposal,
      onReject: rejectProposal
    }));
  });
  if (proposals.length === 0) {
    const empty = element('p', '未承認の章案はありません。');
    empty.className = 'text-muted';
    proposalSection.append(empty);
  }
  main.append(proposalSection);

  const { sec: closedSection, pill: closedPill } = mkChapterSection('完了した章', 'pill-stopped');
  const closedChapters = data.closed_chapters || [];
  closedPill.textContent = `${closedChapters.length} 章`;
  closedChapters.forEach((chapter) => {
    closedSection.append(renderChapterCard(chapter, {
      onReopen: () => reopenChapter(chapter.id)
    }));
  });
  if (closedChapters.length === 0) {
    const empty = element('p', '完了した章はありません。');
    empty.className = 'text-muted';
    closedSection.append(empty);
  }
  main.append(closedSection);
}

function buildAuditLogRow(log) {
  const item = element('li');
  item.className = 'audit-log-item';

  const head = element('div');
  head.className = 'audit-log-head';

  const action = element('span', log.action || '-');
  action.className = 'audit-log-action';

  const resultKind = log.result === 'success' ? 'pill-running' : (log.result === 'error' ? 'pill-error' : 'pill-stopped');
  const resultPill = element('span', log.result || '?');
  resultPill.className = 'pill ' + resultKind;

  const time = element('time', formatRelativeTime(log.created_at));
  time.className = 'audit-log-time';
  if (log.created_at) {
    const isoLike = !log.created_at.endsWith('Z') && log.created_at.includes(' ')
      ? log.created_at.replace(' ', 'T') + 'Z'
      : log.created_at;
    time.setAttribute('datetime', isoLike);
    time.title = isoLike;
  }

  head.append(action, resultPill, time);

  const meta = element('div');
  meta.className = 'audit-log-meta';

  const actor = element('span', `${log.actor_label || '?'} (${log.actor_type || '?'})`);
  actor.className = 'audit-log-actor';

  const story = element('span', log.story_id ? `story: ${log.story_id}` : '-');
  story.className = 'audit-log-story';

  meta.append(actor, story);

  if (log.payload_summary) {
    const summary = element('div', log.payload_summary);
    summary.className = 'audit-log-summary';
    item.append(head, meta, summary);
  } else {
    item.append(head, meta);
  }
  return item;
}

async function renderAuditLogs() {
  const main = layout('監査ログ', { subtitle: '管理操作の履歴を表示します。' });
  const loadingEl = buildEditorSkeleton();
  main.append(loadingEl);

  // Filter bar
  const filterBar = element('div');
  filterBar.className = 'audit-filter-bar card card-padded';
  const filterForm = element('form');
  filterForm.className = 'audit-filter-form';

  const storyFilter = element('input');
  storyFilter.placeholder = 'story_id でフィルタ';
  storyFilter.className = 'input';
  storyFilter.style.maxWidth = '200px';

  const actionFilter = element('input');
  actionFilter.placeholder = 'action で絞り込み';
  actionFilter.className = 'input';
  actionFilter.style.maxWidth = '200px';

  const filterBtn = element('button', '検索');
  filterBtn.type = 'submit';
  filterBtn.className = 'btn btn-secondary btn-sm';

  const clearBtn = element('button', 'クリア');
  clearBtn.type = 'button';
  clearBtn.className = 'btn btn-ghost btn-sm';

  filterForm.append(storyFilter, actionFilter, filterBtn, clearBtn);
  filterBar.append(filterForm);
  main.append(filterBar);

  const logList = element('ul');
  logList.className = 'audit-log-list';

  const footer = element('div');
  footer.className = 'audit-log-footer';

  let currentOffset = 0;
  const PAGE = 50;

  const fetchLogs = async (offset, replace) => {
    const qs = new URLSearchParams({ limit: String(PAGE), offset: String(offset) });
    const sid = storyFilter.value.trim();
    const act = actionFilter.value.trim();
    if (sid) qs.set('story_id', sid);
    if (act) qs.set('action', act);
    const resp = await api(`/audit-logs?${qs.toString()}`);
    if (!resp.ok) {
      if (typeof window.showToast === 'function') window.showToast('監査ログの取得に失敗しました。', 'error');
      return;
    }
    const data = (await resp.json()).data;
    const logs = data.logs || [];

    if (replace) {
      logList.innerHTML = '';
      currentOffset = 0;
    }

    if (logs.length === 0 && replace) {
      const empty = element('li', '条件に一致するログがありません。');
      empty.className = 'audit-log-empty';
      logList.append(empty);
    } else {
      logs.forEach((log) => logList.append(buildAuditLogRow(log)));
      currentOffset = offset + logs.length;
    }

    footer.innerHTML = '';
    if (logs.length === PAGE) {
      const more = element('button', '次の 50 件を読み込む');
      more.type = 'button';
      more.className = 'btn btn-secondary';
      more.addEventListener('click', async () => {
        setSavingButton(more, '読み込み中…');
        await fetchLogs(currentOffset, false);
        clearSavingButton(more);
      });
      footer.append(more);
    } else {
      const end = element('p', `全件表示中 (計 ${currentOffset} 件)`);
      end.className = 'text-muted text-sm';
      footer.append(end);
    }
  };

  loadingEl.remove();
  main.append(logList, footer);

  await fetchLogs(0, true);

  filterForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    logList.innerHTML = '';
    footer.innerHTML = '';
    await fetchLogs(0, true);
  });

  clearBtn.addEventListener('click', async () => {
    storyFilter.value = '';
    actionFilter.value = '';
    await fetchLogs(0, true);
  });
}

async function renderSceneScripts(storyId) {
  const main = layout(`シーンスクリプト: ${storyId}`, {
    subtitle: 'ターン前に監督ペルソナが生成した情景スクリプトの一覧と仕切り直し。',
  });
  const loadingEl = buildEditorSkeleton();
  main.append(loadingEl);

  const response = await api(`/stories/${storyId}/scene-scripts?limit=30`);
  loadingEl.remove();

  if (!response.ok) {
    const err = element('div', '読み込み失敗: シーンスクリプトを取得できませんでした。');
    err.className = 'notice notice-error';
    main.append(err);
    return;
  }

  const data = (await response.json()).data;
  const scripts = data.scripts || [];

  // ── 仕切り直しセクション ──
  const resetSection = element('section');
  resetSection.className = 'card card-padded';
  const resetTitle = element('h2', '仕切り直し');
  resetTitle.className = 'story-detail-section-title';
  const resetDesc = element('p', '指定したターン番号以降のシーンスクリプトを削除します。エンジンが次回起動時に新たに生成し直します。');
  resetDesc.className = 'text-muted';
  const resetRow = element('div');
  resetRow.className = 'scene-script-reset-row';
  const turnInput = element('input');
  turnInput.type = 'number';
  turnInput.min = '1';
  turnInput.placeholder = 'ターン番号';
  turnInput.className = 'scene-script-turn-input';
  const resetBtn = element('button', 'ターン N 以降を削除');
  resetBtn.type = 'button';
  resetBtn.className = 'btn btn-danger';
  resetRow.append(turnInput, resetBtn);
  resetSection.append(resetTitle, resetDesc, resetRow);
  main.append(resetSection);

  resetBtn.addEventListener('click', async () => {
    const turn = parseInt(turnInput.value, 10);
    if (!turn || turn < 1) {
      if (typeof window.showToast === 'function') window.showToast('ターン番号を入力してください。', 'error');
      return;
    }
    if (!confirm(`ターン ${turn} 以降のシーンスクリプトを削除しますか？`)) return;
    const res = await api(`/stories/${storyId}/scene-scripts/from-turn/${turn}`, { method: 'DELETE' });
    if (!res.ok) {
      if (typeof window.showToast === 'function') window.showToast('削除に失敗しました。', 'error');
      return;
    }
    const result = await res.json();
    if (typeof window.showToast === 'function') window.showToast(`${result.data.deleted} 件のスクリプトを削除しました。`, 'success');
    await renderSceneScripts(storyId);
  });

  // ── スクリプト一覧 ──
  const listSection = element('section');
  listSection.className = 'card card-padded';
  const listTitle = element('h2');
  listTitle.className = 'story-detail-section-title';
  const refreshBtn = element('button', 'シーンスクリプトを更新');
  refreshBtn.type = 'button';
  refreshBtn.className = 'btn btn-secondary btn-sm';
  refreshBtn.addEventListener('click', () => {
    refreshSceneScriptList(storyId, listSection, listTitle, refreshBtn).catch(() => {});
  });
  renderSceneScriptList(listSection, listTitle, scripts, refreshBtn);
  main.append(listSection);
}

function renderSceneScriptList(listSection, listTitle, scripts, refreshBtn = null) {
  listSection.innerHTML = '';
  const header = element('div');
  header.className = 'scene-script-list-header';
  listTitle.textContent = `スクリプト一覧（最新 ${scripts.length} 件）`;
  header.append(listTitle);
  if (refreshBtn) {
    header.append(refreshBtn);
  }
  listSection.append(header);

  if (scripts.length === 0) {
    const notice = element('p', 'DBに保存済み scene script はありません。engine 内 cache だけに残った script はこの一覧には表示されません。');
    notice.className = 'text-muted';
    listSection.append(notice);
    return;
  }

  scripts.forEach((s, index) => {
    const details = element('details');
    details.open = index === 0;
    details.className = 'scene-script-card';
    const summary = element('summary');
    summary.className = 'scene-script-summary';
    const meta = element('div');
    meta.className = 'scene-script-meta';
    const turnPill = element('span', `turn ${s.turn_number}`);
    turnPill.className = 'pill pill-info';
    meta.append(turnPill);
    if (s.director_persona_id) {
      const pp = element('span', `監督: ${s.director_persona_id}`);
      pp.className = 'pill';
      meta.append(pp);
    }
    if (s.chapter_id) {
      const cp = element('span', `ch: ${s.chapter_id}`);
      cp.className = 'pill';
      meta.append(cp);
    }
    if (s.beat_phase) {
      const bp = element('span', `beat: ${s.beat_phase}`);
      bp.className = 'pill';
      meta.append(bp);
    }
    summary.append(meta);
    const body = element('div');
    body.className = 'scene-script-body';
    const payload = getSceneScriptDirectorPayload(s);
    if (payload) {
      body.append(buildSceneScriptStructuredView(payload));
      body.append(buildSceneScriptRawJsonDetails(payload));
    } else {
      const legacy = element('p', '旧形式または生成失敗由来のスクリプトです。保存本文をそのまま表示します。');
      legacy.className = 'text-muted scene-script-legacy-note';
      const text = element('p', s.script_text || '(空)');
      text.className = 'scene-script-text';
      body.append(legacy, text);
    }
    details.append(summary, body);
    listSection.append(details);
  });
}

function getSceneScriptDirectorPayload(script) {
  const metadata = script && script.generation_metadata;
  if (!metadata || typeof metadata !== 'object') return null;
  const payload = metadata.director_payload;
  if (!payload || typeof payload !== 'object' || !payload.characters || typeof payload.characters !== 'object') {
    return null;
  }
  return payload;
}

function appendSceneScriptField(parent, label, value) {
  const text = value === undefined || value === null ? '' : String(value).trim();
  if (!text) return;
  const block = element('div');
  block.className = 'scene-script-field';
  block.append(element('h3', label), element('p', text));
  parent.append(block);
}

function buildSceneScriptStructuredView(payload) {
  const wrapper = element('div');
  wrapper.className = 'scene-script-structured';
  appendSceneScriptField(wrapper, 'シーン', payload.scene_frame);
  appendSceneScriptField(wrapper, 'ターン目的', payload.turn_goal);
  appendSceneScriptField(wrapper, '転機', payload.turn_shift);

  const characterSection = element('section');
  characterSection.className = 'scene-script-character-section';
  characterSection.append(element('h3', 'キャラ別実行カード'));
  Object.entries(payload.characters || {}).forEach(([charId, directive]) => {
    if (!directive || typeof directive !== 'object') return;
    characterSection.append(buildSceneScriptCharacterCard(charId, directive));
  });
  wrapper.append(characterSection);

  const banned = Array.isArray(payload.banned_surface_patterns)
    ? payload.banned_surface_patterns.map((item) => String(item).trim()).filter(Boolean)
    : [];
  if (banned.length) {
    const bannedSection = element('section');
    bannedSection.className = 'scene-script-banned';
    bannedSection.append(element('h3', '禁止パターン'));
    const list = element('ul');
    banned.forEach((item) => list.append(element('li', item)));
    bannedSection.append(list);
    wrapper.append(bannedSection);
  }
  return wrapper;
}

function buildSceneScriptCharacterCard(charId, directive) {
  const card = element('article');
  card.className = 'scene-script-character-card';
  const title = element('h4', String(directive.name || charId || 'キャラクター').trim());
  if (charId) {
    const idPill = element('span', charId);
    idPill.className = 'pill';
    title.append(idPill);
  }
  card.append(title);
  appendSceneScriptCardLine(card, '役割', directive.role);
  appendSceneScriptCardLine(card, '次の動き', directive.next_move);
  appendSceneScriptCardLine(card, '発話タスク', directive.speech_task);
  appendSceneScriptCardLine(card, '所作', directive.actable_behavior);
  appendSceneScriptCardLine(card, '相手', directive.target_char_id);
  const avoid = Array.isArray(directive.avoid)
    ? directive.avoid.map((item) => String(item).trim()).filter(Boolean).join(' / ')
    : '';
  appendSceneScriptCardLine(card, '避ける', avoid);
  return card;
}

function appendSceneScriptCardLine(parent, label, value) {
  const text = value === undefined || value === null ? '' : String(value).trim();
  if (!text) return;
  const row = element('p');
  row.append(element('strong', `${label}: `), document.createTextNode(text));
  parent.append(row);
}

function buildSceneScriptRawJsonDetails(payload) {
  const details = element('details');
  details.className = 'scene-script-raw-json';
  details.append(element('summary', 'pretty JSON 全文'), element('pre', JSON.stringify(payload, null, 2)));
  return details;
}

async function refreshSceneScriptList(storyId, listSection, listTitle, refreshBtn) {
  if (listSection.dataset.refreshing === 'true') return;
  listSection.dataset.refreshing = 'true';
  if (refreshBtn) refreshBtn.disabled = true;
  try {
    const response = await api(`/stories/${storyId}/scene-scripts?limit=30`, { silent: true });
    if (!response.ok) return;
    const data = (await response.json()).data;
    renderSceneScriptList(listSection, listTitle, data.scripts || [], refreshBtn);
  } finally {
    if (refreshBtn) refreshBtn.disabled = false;
    delete listSection.dataset.refreshing;
  }
}

async function renderConversationPatterns(storyId) {
  const main = layout(`会話パターン: ${storyId}`, { subtitle: '会話の発生パターンを切り替えます。' });
  const loadingEl = buildEditorSkeleton();
  main.append(loadingEl);

  const response = await api(`/stories/${storyId}/conversation-patterns`);
  loadingEl.remove();
  if (!response.ok) {
    const err = element('div', '読み込み失敗: 会話パターン設定を取得できませんでした。');
    err.className = 'notice notice-error';
    main.append(err);
    return;
  }
  const data = (await response.json()).data;
  const patterns = data.patterns || [];

  const form = element('form');
  form.className = 'conversation-pattern-form editor-form';

  const intro = element('section');
  intro.className = 'card card-padded';
  const title = element('h2', '会話輪舞モード');
  title.className = 'story-detail-section-title';
  const body = element('p', '独り言を別キャラが拾い、元のキャラへ戻して小さな話の種として会話を続ける組み込みパターンです。');
  body.className = 'text-muted';
  intro.append(title, body);
  form.append(intro);

  const list = element('div');
  list.className = 'conversation-pattern-list';
  patterns.forEach((pattern) => {
    const item = element('section');
    item.className = 'card card-padded conversation-pattern-card';
    item.dataset.motifId = pattern.motif_id;

    const header = element('div');
    header.className = 'conversation-pattern-header';
    const heading = element('h3', pattern.display_name || pattern.motif_id);
    const toggleLabel = element('label');
    toggleLabel.className = 'switch-row';
    const toggle = element('input');
    toggle.type = 'checkbox';
    toggle.checked = Boolean(pattern.enabled);
    toggle.name = 'enabled';
    toggleLabel.append(toggle, element('span', '有効にする'));
    header.append(heading, toggleLabel);

    const desc = element('p', pattern.description || '');
    desc.className = 'text-muted';

    const fields = element('div');
    fields.className = 'conversation-pattern-fields';
    const strengthLabel = element('label');
    strengthLabel.append(element('span', '強さ'));
    const strength = element('select');
    strength.name = 'strength';
    [
      ['subtle', '控えめ'],
      ['moderate', '標準'],
      ['strong', '強め'],
    ].forEach(([value, labelText]) => {
      const option = element('option', labelText);
      option.value = value;
      option.selected = pattern.strength === value;
      strength.append(option);
    });
    strengthLabel.append(strength);

    const cooldownLabel = element('label');
    cooldownLabel.append(element('span', 'クールダウンターン'));
    const cooldown = element('input');
    cooldown.type = 'number';
    cooldown.min = '0';
    cooldown.max = '50';
    cooldown.name = 'cooldown_turns';
    cooldown.value = String(pattern.cooldown_turns ?? 4);
    cooldownLabel.append(cooldown);

    fields.append(strengthLabel, cooldownLabel);
    item.append(header, desc, fields);
    list.append(item);
  });
  form.append(list);

  const saveBar = element('div');
  saveBar.className = 'sticky-save-bar';
  const saveButton = element('button', '保存');
  saveButton.type = 'submit';
  saveButton.className = 'btn btn-primary';
  saveBar.append(saveButton);
  form.append(saveBar);

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const payload = {
      patterns: Array.from(form.querySelectorAll('.conversation-pattern-card')).map((item) => ({
        motif_id: item.dataset.motifId,
        enabled: item.querySelector('input[name="enabled"]').checked,
        strength: item.querySelector('select[name="strength"]').value,
        cooldown_turns: Number(item.querySelector('input[name="cooldown_turns"]').value || 0),
      }))
    };
    setSavingButton(saveButton, '保存中…');
    const saveResponse = await api(`/stories/${storyId}/conversation-patterns`, {
      method: 'PUT',
      body: JSON.stringify(payload),
    });
    clearSavingButton(saveButton);
    if (!saveResponse.ok) {
      const errorPayload = await saveResponse.json().catch(() => ({ message: '保存に失敗しました。' }));
      if (typeof window.showToast === 'function') window.showToast(errorPayload.message || '保存に失敗しました。', 'error');
      return;
    }
    if (typeof window.showToast === 'function') window.showToast('会話パターンを保存しました。', 'success');
  });

  main.append(form);
}

async function bootstrap() {
  const session = await api('/session');
  if (!session.ok) {
    renderLogin();
    return;
  }
  const path = window.location.pathname;
  if (path === '/admin' || path === '/admin/') {
    await renderDashboard();
    return;
  }
  if (path === '/admin/stories') {
    await renderStories();
    return;
  }
  if (path.startsWith('/admin/stories/')) {
    await renderStory(path.split('/').pop());
    return;
  }
  if (path.startsWith('/admin/viewer/')) {
    await renderViewer(path.split('/').pop());
    return;
  }
  if (path.startsWith('/admin/onboarding/')) {
    await renderOnboarding(path.split('/').pop());
    return;
  }
  if (path.startsWith('/admin/characters/')) {
    await renderCharacterEditor(path.split('/').pop());
    return;
  }
  if (path.startsWith('/admin/places/')) {
    await renderPlaceEditor(path.split('/').pop());
    return;
  }
  if (path.startsWith('/admin/story-settings/')) {
    await renderStorySettingsEditor(path.split('/').pop());
    return;
  }
  if (path.startsWith('/admin/event-anomalies/')) {
    await renderEventAnomalyEditor(path.split('/').pop());
    return;
  }
  if (path.startsWith('/admin/directors/')) {
    await renderDirectorPersonaEditor(path.split('/').pop());
    return;
  }
  if (path.startsWith('/admin/chapter-definitions/')) {
    await renderChapterDefinitionEditor(path.split('/').pop());
    return;
  }
  if (path.startsWith('/admin/chapters/')) {
    await renderChapterManager(path.split('/').pop());
    return;
  }
  if (path.startsWith('/admin/conversation-patterns/')) {
    await renderConversationPatterns(path.split('/').pop());
    return;
  }
  if (path.startsWith('/admin/scene-scripts/')) {
    await renderSceneScripts(path.split('/').pop());
    return;
  }
  if (path === '/admin/news-mode') {
    await renderNewsModePage();
    return;
  }
  if (path === '/admin/settings') {
    await renderSettings();
    return;
  }
  await renderAuditLogs();
}

// ============================================================
// 時事モード RSS 管理ページ
// ============================================================

async function renderNewsModePage() {
  const main = layout('時事モード RSS 管理');

  // ── 記事統計 & 操作 ──
  const statsSection = element('section');
  statsSection.className = 'card card-padded';
  const statsHeading = element('h2', '記事 DB');
  statsHeading.className = 'story-detail-section-title';
  statsSection.append(statsHeading);

  const statsRow = element('div');
  statsRow.className = 'story-pub-actions';

  const countLabel = element('span', '蓄積記事: 読み込み中...');
  countLabel.style.marginRight = '16px';

  const fetchNowBtn = element('button', '今すぐ取得');
  fetchNowBtn.className = 'btn btn-primary';
  fetchNowBtn.addEventListener('click', async () => {
    fetchNowBtn.disabled = true;
    fetchNowBtn.textContent = '取得中...';
    const res = await api('/news/fetch-now', { method: 'POST' });
    if (res.ok) {
      const d = await res.json();
      const inserted = ((d.data || {}).inserted || 0);
      fetchNowBtn.textContent = `完了 (+${inserted}件)`;
      refreshArticleCount(countLabel);
    } else {
      fetchNowBtn.textContent = '失敗';
    }
    setTimeout(() => { fetchNowBtn.textContent = '今すぐ取得'; fetchNowBtn.disabled = false; }, 2500);
  });

  const resetArticlesBtn = element('button', '記事のみリセット');
  resetArticlesBtn.className = 'btn btn-ghost';
  resetArticlesBtn.style.marginLeft = '8px';
  resetArticlesBtn.addEventListener('click', async () => {
    if (!confirm('蓄積済み記事をすべて削除します。フィード設定は保持されます。よろしいですか？')) return;
    resetArticlesBtn.disabled = true;
    const res = await api('/news/reset', { method: 'POST' });
    if (res.ok) { refreshArticleCount(countLabel); }
    resetArticlesBtn.disabled = false;
  });

  const resetAllBtn = element('button', '全リセット（フィードも削除）');
  resetAllBtn.className = 'btn btn-danger';
  resetAllBtn.style.marginLeft = '8px';
  resetAllBtn.addEventListener('click', async () => {
    if (!confirm('フィードと記事をすべて削除します。よろしいですか？')) return;
    resetAllBtn.disabled = true;
    await api('/news/reset-all', { method: 'POST' });
    refreshArticleCount(countLabel);
    await refreshFeedList(feedListContainer);
    resetAllBtn.disabled = false;
  });

  statsRow.append(countLabel, fetchNowBtn, resetArticlesBtn, resetAllBtn);
  statsSection.append(statsRow);
  main.append(statsSection);

  // ── フィード一覧 ──
  const feedSection = element('section');
  feedSection.className = 'card card-padded';
  const feedHeading = element('h2', 'RSS フィード');
  feedHeading.className = 'story-detail-section-title';
  feedSection.append(feedHeading);

  const feedListContainer = element('div');
  feedListContainer.className = 'news-feed-list';
  feedSection.append(feedListContainer);

  // フィード追加フォーム
  const addForm = element('div');
  addForm.className = 'story-pub-actions';
  addForm.style.flexDirection = 'column';
  addForm.style.alignItems = 'flex-start';
  addForm.style.gap = '8px';
  addForm.style.marginTop = '16px';

  const addRow1 = element('div');
  addRow1.className = 'form-row';
  addRow1.style.width = '100%';

  const urlInput = element('input');
  urlInput.type = 'url';
  urlInput.placeholder = 'https://example.com/rss.xml';
  urlInput.className = 'text-input';
  urlInput.style.minWidth = '340px';
  urlInput.style.marginRight = '8px';
  const titleInput = element('input');
  titleInput.type = 'text';
  titleInput.placeholder = 'タイトル（任意）';
  titleInput.className = 'text-input';
  titleInput.style.marginRight = '8px';
  addRow1.append(urlInput, titleInput);
  addForm.append(addRow1);

  // タグ入力（既知タグを非同期ロード）
  let addFormKnownTags = [];
  try {
    const tagRes = await api('/news/tags');
    if (tagRes.ok) {
      const tagPayload = await tagRes.json();
      addFormKnownTags = (tagPayload.data && tagPayload.data.tags) || [];
    }
  } catch (_) { /* ignore */ }

  const addTagRow = element('div');
  addTagRow.className = 'form-row-vertical';
  addTagRow.style.width = '100%';
  const addTagLabel = element('div', 'タグ（Enter / カンマで追加）');
  addTagLabel.style.fontSize = '0.78rem';
  addTagLabel.style.opacity = '0.75';
  const addTagInput = createTagChipInput({ suggestions: addFormKnownTags, placeholder: 'タグを入力…' });
  addTagRow.append(addTagLabel, addTagInput.element);
  addForm.append(addTagRow);

  const addBtn = element('button', '追加');
  addBtn.className = 'btn btn-primary';
  addBtn.addEventListener('click', async () => {
    const url = urlInput.value.trim();
    if (!url) { alert('URL を入力してください'); return; }
    addBtn.disabled = true;
    addBtn.textContent = '追加中...';
    const res = await api('/news/feeds', {
      method: 'POST',
      body: JSON.stringify({ url, title: titleInput.value.trim() || null, tags: addTagInput.getTags() }),
    });
    if (res.ok) {
      urlInput.value = '';
      titleInput.value = '';
      addTagInput.setTags([]);
      await refreshFeedList(feedListContainer, addFormKnownTags);
    } else {
      const d = await res.json().catch(() => ({}));
      alert((d.error || {}).message || '追加に失敗しました');
    }
    addBtn.textContent = '追加';
    addBtn.disabled = false;
  });
  addForm.append(addBtn);
  feedSection.append(addForm);
  main.append(feedSection);

  // 初期ロード
  await Promise.all([
    refreshArticleCount(countLabel),
    refreshFeedList(feedListContainer, addFormKnownTags),
  ]);
}

async function refreshArticleCount(label) {
  const res = await api('/news/articles?limit=1');
  if (!res.ok) return;
  const d = await res.json();
  label.textContent = `蓄積記事: ${(d.data || {}).total ?? '?'} 件`;
}

async function refreshFeedList(container, knownTags = []) {
  const res = await api('/news/feeds');
  if (!res.ok) { container.textContent = 'フィード一覧の取得に失敗しました。'; return; }
  const d = await res.json();
  const feeds = (d.data || {}).feeds || [];
  container.innerHTML = '';
  if (feeds.length === 0) {
    const empty = element('p', 'フィードが登録されていません。');
    empty.className = 'notice notice-info';
    empty.style.margin = '8px 0';
    container.append(empty);
    return;
  }
  const tbl = element('table');
  tbl.style.width = '100%';
  tbl.style.borderCollapse = 'collapse';
  const thead = element('thead');
  thead.innerHTML = '<tr>'
    + '<th style="text-align:left;padding:6px 8px">URL</th>'
    + '<th style="padding:6px 8px">タイトル</th>'
    + '<th style="padding:6px 8px">タグ</th>'
    + '<th style="padding:6px 8px">有効</th>'
    + '<th style="padding:6px 8px">最終取得</th>'
    + '<th style="padding:6px 8px"></th>'
    + '</tr>';
  tbl.append(thead);
  const tbody = element('tbody');
  feeds.forEach((feed) => {
    const feedTags = feed.tags || [];
    const tr = element('tr');
    tr.style.borderTop = '1px solid rgba(128,128,128,0.18)';

    const tdUrl = element('td');
    tdUrl.style.padding = '6px 8px';
    tdUrl.style.wordBreak = 'break-all';
    tdUrl.style.maxWidth = '280px';
    tdUrl.textContent = feed.url;

    const tdTitle = element('td');
    tdTitle.style.padding = '6px 8px';
    tdTitle.textContent = feed.title || '-';

    // タグ列（チップ表示 + 編集展開）
    const tdTags = element('td');
    tdTags.style.padding = '6px 8px';
    tdTags.style.minWidth = '120px';

    const chipWrap = element('div');
    chipWrap.style.display = 'flex';
    chipWrap.style.flexWrap = 'wrap';
    chipWrap.style.gap = '4px';
    chipWrap.style.alignItems = 'center';

    feedTags.forEach((t) => {
      const chip = element('span', t);
      chip.className = 'tag-chip';
      chipWrap.append(chip);
    });
    const editTagBtn = element('button', feedTags.length === 0 ? 'タグを追加' : '編集');
    editTagBtn.className = 'btn btn-ghost';
    editTagBtn.style.fontSize = '0.72em';
    editTagBtn.style.padding = '1px 6px';
    editTagBtn.style.marginLeft = feedTags.length ? '4px' : '0';

    let tagEditor = null;
    editTagBtn.addEventListener('click', () => {
      if (tagEditor) {
        tagEditor.remove();
        tagEditor = null;
        editTagBtn.textContent = feedTags.length === 0 ? 'タグを追加' : '編集';
        return;
      }
      editTagBtn.textContent = '閉じる';
      tagEditor = element('div');
      tagEditor.style.marginTop = '8px';
      const ti = createTagChipInput({ initialTags: [...feedTags], suggestions: knownTags });
      const saveTagBtn = element('button', '保存');
      saveTagBtn.className = 'btn btn-primary';
      saveTagBtn.style.marginTop = '6px';
      saveTagBtn.style.fontSize = '0.8em';
      saveTagBtn.addEventListener('click', async () => {
        saveTagBtn.disabled = true;
        saveTagBtn.textContent = '保存中...';
        const putRes = await api(`/news/feeds/${feed.id}`, {
          method: 'PUT',
          body: JSON.stringify({ tags: ti.getTags() }),
        });
        if (putRes.ok) {
          await refreshFeedList(container, knownTags);
        } else {
          saveTagBtn.textContent = '失敗';
          saveTagBtn.disabled = false;
        }
      });
      tagEditor.append(ti.element, saveTagBtn);
      tdTags.append(tagEditor);
    });
    chipWrap.append(editTagBtn);
    tdTags.append(chipWrap);

    const tdEnabled = element('td');
    tdEnabled.style.padding = '6px 8px';
    tdEnabled.style.textAlign = 'center';
    const chk = element('input');
    chk.type = 'checkbox';
    chk.checked = feed.enabled;
    chk.addEventListener('change', async () => {
      await api(`/news/feeds/${feed.id}`, {
        method: 'PUT',
        body: JSON.stringify({ enabled: chk.checked }),
      });
    });
    tdEnabled.append(chk);

    const tdFetched = element('td');
    tdFetched.style.padding = '6px 8px';
    tdFetched.style.fontSize = '0.85em';
    tdFetched.style.opacity = '0.65';
    tdFetched.textContent = feed.last_fetched_at ? feed.last_fetched_at.slice(0, 16) : '未取得';

    const tdDel = element('td');
    tdDel.style.padding = '6px 8px';
    tdDel.style.textAlign = 'center';
    const delBtn = element('button', '削除');
    delBtn.className = 'btn btn-danger';
    delBtn.style.fontSize = '0.8em';
    delBtn.style.padding = '2px 8px';
    delBtn.addEventListener('click', async () => {
      if (!confirm(`フィードを削除します:\n${feed.url}`)) return;
      delBtn.disabled = true;
      await api(`/news/feeds/${feed.id}`, { method: 'DELETE' });
      await refreshFeedList(container, knownTags);
    });
    tdDel.append(delBtn);

    tr.append(tdUrl, tdTitle, tdTags, tdEnabled, tdFetched, tdDel);
    tbody.append(tr);
  });
  tbl.append(tbody);
  container.append(tbl);
}

void bootstrap();
