// assets/i18n.js — PocketRole public web i18n helpers

(function () {
  'use strict';

  const PUBLIC_MESSAGES = {
    ja: {
      'viewer.mapReplay': 'マップ再生を見る',
      'viewer.timelineBack': 'タイムラインへ戻る',
      'viewer.storySwitcher': 'ストーリー切り替え',
      'viewer.connecting': '接続中...',
      'viewer.latestUpdated': '最終更新: {time}',
      'viewer.errorStatus': 'エラー {status}',
      'viewer.networkError': '通信エラー',
      'viewer.jumpLatest': '最新へ',
      'viewer.jumpLatestCount': '最新へ ({count})',
      'narration.scene': '情景',
      'narration.inner': '心理',
      'narration.transition': '場面転換',
      'narration.chapter': '章',
      'narration.foreshadow': '伏線',
      'narration.default': 'ナレーション',
      'map.play': '再生',
      'map.titleSuffix': 'マップ再生',
      'map.pause': '停止',
      'map.next': '1件送り',
      'map.reset': '先頭へ戻る',
      'map.bgmOn': 'BGM ON',
      'map.bgmOff': 'BGM OFF',
      'map.speed': '速度',
      'map.volume': '音量',
      'map.stageAria': 'マップ再生ステージ',
      'map.speaker': '話者',
      'map.place': '場所',
      'map.unknown': '-',
      'map.unsupportedStoryMap': 'この story はマップ再生未対応です。place 画像または背景画像と map.json を用意してください。',
      'map.notPublished': 'このストーリーの再生はまだ公開されていません。',
      'map.unavailable': 'マップ再生を読み込めませんでした。',
      'map.noLogs': '再生可能なログがありません。',
      'archive.indexTitle': '公開ストーリー一覧',
      'archive.storySuffix': 'の物語',
      'archive.empty': '公開ストーリーはまだありません。',
      'archive.noArchive': '公開アーカイブがありません。',
      'archive.pageLoadFailed': 'ページを読み込めませんでした。',
      'archive.meta': '{title} / {page} / {pageCount}',
    },
    en: {
      'viewer.mapReplay': 'Map Replay',
      'viewer.timelineBack': 'Back to Timeline',
      'viewer.storySwitcher': 'Switch story',
      'viewer.connecting': 'Connecting...',
      'viewer.latestUpdated': 'Updated: {time}',
      'viewer.errorStatus': 'Error {status}',
      'viewer.networkError': 'Network error',
      'viewer.jumpLatest': 'Latest',
      'viewer.jumpLatestCount': 'Latest ({count})',
      'narration.scene': 'Scene',
      'narration.inner': 'Inner Voice',
      'narration.transition': 'Transition',
      'narration.chapter': 'Chapter',
      'narration.foreshadow': 'Foreshadowing',
      'narration.default': 'Narration',
      'map.play': 'Play',
      'map.titleSuffix': 'Map Replay',
      'map.pause': 'Pause',
      'map.next': 'Next',
      'map.reset': 'Reset',
      'map.bgmOn': 'BGM ON',
      'map.bgmOff': 'BGM OFF',
      'map.speed': 'Speed',
      'map.volume': 'Volume',
      'map.stageAria': 'Map replay stage',
      'map.speaker': 'Speaker',
      'map.place': 'Place',
      'map.unknown': '-',
      'map.unsupportedStoryMap': 'This story does not support map replay yet. Add place images or a background image and map.json.',
      'map.notPublished': 'Replay for this story has not been published yet.',
      'map.unavailable': 'Could not load map replay.',
      'map.noLogs': 'No replayable logs are available.',
      'archive.indexTitle': 'Public Stories',
      'archive.storySuffix': 'Story',
      'archive.empty': 'No public stories yet.',
      'archive.noArchive': 'No public archive is available.',
      'archive.pageLoadFailed': 'Could not load this page.',
      'archive.meta': '{title} / {page} / {pageCount}',
    },
    'zh-TW': {
      'viewer.mapReplay': '地圖重播',
      'viewer.timelineBack': '返回時間軸',
      'viewer.storySwitcher': '切換故事',
      'viewer.connecting': '連線中...',
      'viewer.latestUpdated': '最後更新：{time}',
      'viewer.errorStatus': '錯誤 {status}',
      'viewer.networkError': '通訊錯誤',
      'viewer.jumpLatest': '最新',
      'viewer.jumpLatestCount': '最新 ({count})',
      'narration.scene': '情景',
      'narration.inner': '心理',
      'narration.transition': '場面轉換',
      'narration.chapter': '章',
      'narration.foreshadow': '伏筆',
      'narration.default': '旁白',
      'map.play': '播放',
      'map.titleSuffix': '地圖重播',
      'map.pause': '暫停',
      'map.next': '下一筆',
      'map.reset': '回到開頭',
      'map.bgmOn': 'BGM 開',
      'map.bgmOff': 'BGM 關',
      'map.speed': '速度',
      'map.volume': '音量',
      'map.stageAria': '地圖重播舞台',
      'map.speaker': '說話者',
      'map.place': '地點',
      'map.unknown': '-',
      'map.unsupportedStoryMap': '這個 story 尚未支援地圖重播。請準備 place 圖像，或背景圖與 map.json。',
      'map.notPublished': '這個故事的重播尚未公開。',
      'map.unavailable': '無法載入地圖重播。',
      'map.noLogs': '沒有可重播的紀錄。',
      'archive.indexTitle': '公開故事列表',
      'archive.storySuffix': '的故事',
      'archive.empty': '目前還沒有公開故事。',
      'archive.noArchive': '沒有公開封存。',
      'archive.pageLoadFailed': '無法讀取頁面。',
      'archive.meta': '{title} / {page} / {pageCount}',
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

  function runtimeLocale() {
    const configs = [
      window.POCKETROLE,
      window.POCKETROLE_MAP_REPLAY,
      window.POCKETROLE_ARCHIVE_INDEX,
      window.POCKETROLE_ARCHIVE,
    ];
    for (const config of configs) {
      const locale = normalizeLocale(config && config.locale);
      if (locale) return locale;
    }
    return '';
  }

  function resolveLocale(rawLocale) {
    return (
      normalizeLocale(rawLocale) ||
      normalizeLocale(window.localStorage && window.localStorage.getItem('pocketrole.public.locale')) ||
      normalizeLocale(window.navigator && window.navigator.language) ||
      'ja'
    );
  }

  function interpolate(template, params) {
    return String(template).replace(/\{([a-zA-Z0-9_]+)\}/g, (_, key) => (
      Object.prototype.hasOwnProperty.call(params || {}, key) ? String(params[key]) : `{${key}}`
    ));
  }

  let currentLocale = resolveLocale(runtimeLocale());

  function t(key, params = {}) {
    const template = (PUBLIC_MESSAGES[currentLocale] && PUBLIC_MESSAGES[currentLocale][key]) ||
      PUBLIC_MESSAGES.ja[key] ||
      key;
    return interpolate(template, params);
  }

  function setLocale(locale) {
    currentLocale = resolveLocale(locale);
    if (window.localStorage) {
      window.localStorage.setItem('pocketrole.public.locale', currentLocale);
    }
  }

  function formatTime(date) {
    return new Intl.DateTimeFormat(currentLocale, {
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
    }).format(date);
  }

  function bindLocaleSwitcher(selectId = 'locale-switcher') {
    const select = document.getElementById(selectId);
    if (!select) return;
    select.value = currentLocale;
    select.addEventListener('change', () => {
      setLocale(select.value);
      const url = new URL(window.location.href);
      url.searchParams.set('lang', currentLocale);
      window.location.href = url.toString();
    });
  }

  function applyStaticLabels(root = document) {
    root.querySelectorAll('[data-i18n]').forEach((node) => {
      node.textContent = t(node.getAttribute('data-i18n'));
    });
    root.querySelectorAll('[data-i18n-title]').forEach((node) => {
      node.setAttribute('title', t(node.getAttribute('data-i18n-title')));
    });
    root.querySelectorAll('[data-i18n-aria-label]').forEach((node) => {
      node.setAttribute('aria-label', t(node.getAttribute('data-i18n-aria-label')));
    });
  }

  window.PocketRoleI18n = {
    PUBLIC_MESSAGES,
    get locale() {
      return currentLocale;
    },
    resolveLocale,
    setLocale,
    t,
    formatTime,
    applyStaticLabels,
    bindLocaleSwitcher,
  };
})();
