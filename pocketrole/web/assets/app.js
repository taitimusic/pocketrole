// assets/app.js — PocketRole タイムラインポーリングクライアント

(function () {
  'use strict';

  const { storyId, apiUrl, statusUrl, imageBaseUrl } = window.POCKETROLE;
  const i18n = window.PocketRoleI18n || {
    t: (key, params = {}) => {
      const fallback = {
        'viewer.jumpLatest': '最新へ',
        'viewer.jumpLatestCount': `最新へ (${params.count || 0})`,
        'viewer.errorStatus': `エラー ${params.status || ''}`,
        'viewer.latestUpdated': `最終更新: ${params.time || ''}`,
        'viewer.networkError': '通信エラー',
        'narration.scene': '情景',
        'narration.inner': '心理',
        'narration.transition': '場面転換',
        'narration.chapter': '章',
        'narration.foreshadow': '伏線',
        'narration.default': 'ナレーション',
      };
      return fallback[key] || key;
    },
    formatTime: (date) => date.toLocaleTimeString('ja-JP'),
    applyStaticLabels: () => {},
    bindLocaleSwitcher: () => {},
  };

  const EXPR_EMOJI = {
    happy: '😊', angry: '😠', sad: '😢', surprised: '😲',
    worried: '😟', content: '😌', lonely: '😔', tired: '😴', neutral: '😐'
  };
  const LIMIT = 50;
  const INTERVAL = 10000; // 10秒
  const BOTTOM_THRESHOLD = 100;

  const timeline = document.getElementById('timeline');
  const pollStatus = document.getElementById('poll-status');
  const jumpLatestBtn = document.getElementById('jump-latest');
  const renderedKeys = new Set(); // 重複防止
  let initialScrollDone = false;
  let pendingWhileDetached = 0;

  // キー生成: sim_datetime + char_id + turn_number
  function makeKey(log) {
    return `${log.sim_datetime}|${log.char_id}|${log.turn_number}`;
  }

  // XSS 対策
  function esc(str) {
    const d = document.createElement('div');
    d.textContent = str ?? '';
    return d.innerHTML;
  }

  // esc() 済みテキスト内のリンクを <a> に変換（時事モード対応）
  // esc() は <,>,&,",' のみをエスケープ。[],() はエスケープされないため Markdown リンクが保持される。
  function linkify(escapedText) {
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

  function renderCurrentAffairsLink(escapedText) {
    const match = escapedText.match(/^「\[([^\]]+)\]\((https?:\/\/[^\s)]*)\)」([\s\S]*)$/);
    if (!match) return null;

    const [, title, url, reaction] = match;
    const reactionHtml = linkify(reaction);
    const reactionPart = reactionHtml
      ? `<span class="news-link-reaction">${reactionHtml}</span>`
      : '';
    return (
      `<span class="news-link-message">` +
        `<span class="news-link-quote">「</span>` +
        `<a class="news-link-title" href="${url}" target="_blank" rel="noopener noreferrer">${title}</a>` +
        `<span class="news-link-quote">」</span>` +
        reactionPart +
      `</span>`
    );
  }

  function renderMessageHtml(escapedText) {
    return renderCurrentAffairsLink(escapedText) || linkify(escapedText);
  }

  function buildImageUrl(charId, expr) {
    return `${imageBaseUrl}/${encodeURIComponent(storyId)}`
      + `/${encodeURIComponent(charId)}`
      + `/${encodeURIComponent(expr)}.png`;
  }

  function buildNeutralImageUrl(charId) {
    return `${imageBaseUrl}/${encodeURIComponent(storyId)}`
      + `/${encodeURIComponent(charId)}/neutral.png`;
  }

  function applyAvatarImage(avatarEl, charId, expr) {
    const normalizedExpr = expr || 'neutral';
    const candidates = [buildImageUrl(charId, normalizedExpr)];
    if (normalizedExpr !== 'neutral') {
      candidates.push(buildNeutralImageUrl(charId));
    }
    let index = 0;

    function tryNext() {
      if (index >= candidates.length) {
        avatarEl.classList.remove('has-image');
        return;
      }
      const src = candidates[index++];
      const probe = new Image();
      probe.addEventListener('load', () => {
        avatarEl.classList.add('has-image');
        avatarEl.innerHTML = `<img class="avatar-image" src="${esc(src)}" alt="${esc(charId)}">`;
      }, { once: true });
      probe.addEventListener('error', tryNext, { once: true });
      probe.src = src;
    }

    tryNext();
  }

  // 1エントリ → DOM要素
  function renderLog(log) {
    const key = makeKey(log);
    if (renderedKeys.has(key)) return;
    renderedKeys.add(key);

    const el = document.createElement('div');
    const msgType = log.msg_type || 'monologue';
    const isNarration = msgType.startsWith('narration_');
    el.className = `log-entry ${msgType}${isNarration ? ' narration' : ''} new-entry`;

    if (msgType === 'move_comment') {
      el.innerHTML = `<span class="move-text">📍 ${esc(log.message)}</span>`;
    } else if (isNarration) {
      const TYPE_LABELS = {
        narration_scene:      i18n.t('narration.scene'),
        narration_inner:      i18n.t('narration.inner'),
        narration_transition: i18n.t('narration.transition'),
        narration_chapter:    i18n.t('narration.chapter'),
        narration_foreshadow: i18n.t('narration.foreshadow'),
      };
      const label = TYPE_LABELS[msgType] || i18n.t('narration.default');
      el.innerHTML =
        `<div class="narration-bubble">` +
          `<div class="narration-type-label">${esc(label)}</div>` +
          `<div class="narration-text">${renderMessageHtml(esc(log.message))}</div>` +
        `</div>`;
    } else {
      const initials = (log.char_id || '?').substring(0, 2).toUpperCase();
      const displayName = log.char_name || log.char_id;
      const expr = log.expression || 'neutral';
      const avatarContent = EXPR_EMOJI[expr] || initials;
      el.innerHTML = `
        <div class="avatar ${esc(expr)}" title="${esc(log.char_id)}">${avatarContent}</div>
        <div class="bubble-wrap">
          <div class="char-name">${esc(displayName)}</div>
          <div class="bubble">${renderMessageHtml(esc(log.message))}</div>
          <div class="meta">${esc(log.sim_datetime)} · ${esc(log.place_id || '')} · ${EXPR_EMOJI[expr] || ''} ${esc(expr)}</div>
        </div>`;
      const avatarEl = el.querySelector('.avatar');
      if (avatarEl && log.char_id) {
        applyAvatarImage(avatarEl, log.char_id, expr);
      }
    }
    timeline.appendChild(el);
  }

  function isNearBottom() {
    return timeline.scrollTop + timeline.clientHeight >= timeline.scrollHeight - BOTTOM_THRESHOLD;
  }

  function setJumpLatestVisible(visible) {
    if (!jumpLatestBtn) return;
    jumpLatestBtn.classList.toggle('visible', visible);
    jumpLatestBtn.textContent = pendingWhileDetached > 0
      ? i18n.t('viewer.jumpLatestCount', { count: pendingWhileDetached })
      : i18n.t('viewer.jumpLatest');
  }

  function scrollToBottom(options = {}) {
    const { force = false } = options;
    if (force || isNearBottom()) {
      timeline.scrollTop = timeline.scrollHeight;
    }
  }

  function clearPendingState() {
    pendingWhileDetached = 0;
    setJumpLatestVisible(false);
  }

  function handleTimelineScroll() {
    if (isNearBottom()) {
      clearPendingState();
    }
  }

  // ポーリング
  async function poll() {
    const url = `${apiUrl}?story_id=${encodeURIComponent(storyId)}`
              + `&action=latest`
              + `&limit=${LIMIT}`;
    try {
      const wasNearBottom = isNearBottom();
      const res = await fetch(url);
      if (!res.ok) {
        pollStatus.textContent = i18n.t('viewer.errorStatus', { status: res.status });
        return;
      }
      const data = await res.json();
      if (data.status !== 'ok') return;
      const before = renderedKeys.size;
      data.logs.forEach(renderLog);
      const added = renderedKeys.size - before;
      if (!initialScrollDone) {
        scrollToBottom({ force: true });
        initialScrollDone = true;
        clearPendingState();
      } else if (added > 0) {
        if (wasNearBottom) {
          scrollToBottom({ force: true });
          clearPendingState();
        } else {
          pendingWhileDetached += added;
          setJumpLatestVisible(true);
        }
      }
      pollStatus.textContent = i18n.t('viewer.latestUpdated', {
        time: i18n.formatTime(new Date()),
      });
    } catch (e) {
      pollStatus.textContent = i18n.t('viewer.networkError');
      console.error('poll error', e);
    }
  }

  async function initStorySelector() {
    const sel = document.getElementById('story-switcher');
    if (!sel) return;
    try {
      const res = await fetch(statusUrl);
      const data = await res.json();
      if (data.status !== 'ok') return;
      data.stories.forEach(s => {
        const opt = document.createElement('option');
        opt.value = s.story_id;
        opt.textContent = s.story_id;
        if (s.story_id === storyId) opt.selected = true;
        sel.appendChild(opt);
      });
    } catch (_) { /* selector は optional */ }
    sel.addEventListener('change', () => {
      location.href = `viewer.php?story_id=${encodeURIComponent(sel.value)}&lang=${encodeURIComponent(i18n.locale || 'ja')}`;
    });
  }

  document.addEventListener('DOMContentLoaded', () => {
    timeline.addEventListener('scroll', handleTimelineScroll);
    if (jumpLatestBtn) {
      jumpLatestBtn.addEventListener('click', () => {
        scrollToBottom({ force: true });
        clearPendingState();
      });
    }
    i18n.applyStaticLabels();
    i18n.bindLocaleSwitcher();
    initStorySelector();
    poll();
    setInterval(poll, INTERVAL);
  });
})();
