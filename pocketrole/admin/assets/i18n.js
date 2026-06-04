// admin/assets/i18n.js — PocketRole admin i18n helpers

(function () {
  'use strict';

  const ADMIN_MESSAGES = {
    ja: {
      'nav.dashboard': 'ダッシュボード',
      'nav.stories': 'ストーリー',
      'nav.newsMode': '時事モード',
      'nav.settings': '設定',
      'nav.audit': '監査ログ',
      'nav.characters': 'キャラ編集',
      'nav.places': '場所編集',
      'nav.storySettings': 'Story設定',
      'nav.eventAnomalies': 'イベント/異変',
      'nav.directors': '監督',
      'nav.chapterDefinitions': '章定義',
      'nav.chapters': '章管理',
      'nav.conversationPatterns': '会話パターン',
      'nav.sceneScripts': 'シーンスクリプト',
      'common.openMenu': 'メニューを開く',
      'common.closeMenu': 'メニューを閉じる',
      'common.workspace': 'Workspace',
      'theme.toLight': 'ライトモードに切替',
      'theme.toDark': 'ダークモードに切替',
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
    },
    en: {
      'nav.dashboard': 'Dashboard',
      'nav.stories': 'Stories',
      'nav.newsMode': 'News Mode',
      'nav.settings': 'Settings',
      'nav.audit': 'Audit Logs',
      'nav.characters': 'Characters',
      'nav.places': 'Places',
      'nav.storySettings': 'Story Settings',
      'nav.eventAnomalies': 'Events / Anomalies',
      'nav.directors': 'Directors',
      'nav.chapterDefinitions': 'Chapter Definitions',
      'nav.chapters': 'Chapter Management',
      'nav.conversationPatterns': 'Conversation Patterns',
      'nav.sceneScripts': 'Scene Scripts',
      'common.openMenu': 'Open menu',
      'common.closeMenu': 'Close menu',
      'common.workspace': 'Workspace',
      'theme.toLight': 'Switch to light mode',
      'theme.toDark': 'Switch to dark mode',
      'beat.setup': 'Setup',
      'beat.complication': 'Complication',
      'beat.turningPoint': 'Turning Point',
      'beat.resolution': 'Resolution',
      'chapter.pending': 'Pending',
      'chapter.active': 'Active',
      'chapter.closed': 'Closed',
      'proposal.pending': 'Pending Review',
      'proposal.approved': 'Approved',
      'proposal.rejected': 'Rejected',
    },
    'zh-TW': {
      'nav.dashboard': '儀表板',
      'nav.stories': '故事',
      'nav.newsMode': '時事模式',
      'nav.settings': '設定',
      'nav.audit': '稽核紀錄',
      'nav.characters': '角色編輯',
      'nav.places': '地點編輯',
      'nav.storySettings': 'Story 設定',
      'nav.eventAnomalies': '事件／異變',
      'nav.directors': '監督',
      'nav.chapterDefinitions': '章節定義',
      'nav.chapters': '章節管理',
      'nav.conversationPatterns': '對話模式',
      'nav.sceneScripts': '場景腳本',
      'common.openMenu': '開啟選單',
      'common.closeMenu': '關閉選單',
      'common.workspace': '工作區',
      'theme.toLight': '切換為淺色模式',
      'theme.toDark': '切換為深色模式',
      'beat.setup': '導入',
      'beat.complication': '展開',
      'beat.turningPoint': '轉折',
      'beat.resolution': '解決',
      'chapter.pending': '待機中',
      'chapter.active': '進行中',
      'chapter.closed': '完成',
      'proposal.pending': '待審核',
      'proposal.approved': '已核准',
      'proposal.rejected': '已退回',
    },
  };

  function normalizeLocale(rawLocale) {
    const value = String(rawLocale || '').trim().replace('_', '-');
    const lower = value.toLowerCase();
    if (!lower) return '';
    if (lower === 'ja' || lower.startsWith('ja-')) return 'ja';
    if (lower === 'en' || lower.startsWith('en-')) return 'en';
    if (
      lower === 'zh' ||
      lower === 'zh-tw' ||
      lower === 'zh-hant' ||
      lower.startsWith('zh-hant-') ||
      lower === 'zh-hk' ||
      lower === 'zh-mo'
    ) {
      return 'zh-TW';
    }
    return '';
  }

  function resolveLocale(rawLocale) {
    return (
      normalizeLocale(rawLocale) ||
      normalizeLocale(window.localStorage && window.localStorage.getItem('pocketrole.admin.locale')) ||
      normalizeLocale(window.navigator && window.navigator.language) ||
      'ja'
    );
  }

  let currentLocale = resolveLocale(window.POCKETROLE_ADMIN && window.POCKETROLE_ADMIN.locale);

  function interpolate(template, params) {
    return String(template).replace(/\{([a-zA-Z0-9_]+)\}/g, (_, key) => (
      Object.prototype.hasOwnProperty.call(params || {}, key) ? String(params[key]) : `{${key}}`
    ));
  }

  function t(key, params = {}) {
    const template = (ADMIN_MESSAGES[currentLocale] && ADMIN_MESSAGES[currentLocale][key]) ||
      ADMIN_MESSAGES.ja[key] ||
      key;
    return interpolate(template, params);
  }

  function setLocale(locale) {
    currentLocale = resolveLocale(locale);
    if (window.localStorage) {
      window.localStorage.setItem('pocketrole.admin.locale', currentLocale);
    }
  }

  function buildLocaleSwitcher() {
    const select = document.createElement('select');
    select.className = 'admin-locale-switcher';
    select.setAttribute('aria-label', 'Language');
    [
      ['ja', '日本語'],
      ['en', 'English'],
      ['zh-TW', '繁體中文'],
    ].forEach(([value, label]) => {
      const option = document.createElement('option');
      option.value = value;
      option.textContent = label;
      if (value === currentLocale) option.selected = true;
      select.appendChild(option);
    });
    select.addEventListener('change', () => {
      setLocale(select.value);
      const url = new URL(window.location.href);
      url.searchParams.set('lang', currentLocale);
      window.location.href = url.toString();
    });
    return select;
  }

  window.PocketRoleAdminI18n = {
    ADMIN_MESSAGES,
    get locale() {
      return currentLocale;
    },
    resolveLocale,
    setLocale,
    t,
    buildLocaleSwitcher,
  };
})();
