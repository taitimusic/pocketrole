// assets/map_replay.js — Phaser ベースの map replay クライアント

(function () {
  'use strict';

  const {
    storyId,
    apiUrl,
    imageBaseUrl,
    storyMapBaseUrl,
    defaultBgmUrl,
  } = window.POCKETROLE_MAP_REPLAY;
  const i18n = window.PocketRoleI18n || {
    t: (key) => ({
      'map.play': '再生',
      'map.pause': '停止',
      'map.next': '1件送り',
      'map.reset': '先頭へ戻る',
      'map.bgmOn': 'BGM ON',
      'map.bgmOff': 'BGM OFF',
      'map.speaker': '話者',
      'map.place': '場所',
      'map.unknown': '-',
      'map.unsupportedStoryMap': 'この story はマップ再生未対応です。place 画像または背景画像と map.json を用意してください。',
      'map.notPublished': 'このストーリーの再生はまだ公開されていません。',
      'map.unavailable': '再生データを読み込めませんでした。',
      'map.noLogs': '再生可能なログがありません。',
    }[key] || key),
    applyStaticLabels: () => {},
    bindLocaleSwitcher: () => {},
  };

  const UNKNOWN_PLACE_ID = 'unknown';
  const HOME_PLACE_ID = 'home';
  const NARRATOR_CHAR_ID = '_narrator';
  const NARRATOR_DISPLAY_NAME = 'ナレーション';
  const NARRATOR_AVATAR_URL = 'assets/character_images/_system/narration/neutral.png';
  const PLACE_IMAGE_SUFFIX = '_320.png';
  const BGM_MANIFEST_FILENAME = 'bgm_manifest.json';
  const DEFAULT_BGM_VOLUME = 0.55;
  const ACTIVE_AVATAR_MAX_SIZE = 156;
  const SUPPORT_AVATAR_MAX_SIZE = 132;
  const BUBBLE_SAFE_MARGIN = 52;
  const MAX_SUPPORT_CHARACTERS = 3;
  const ReplayPhase = {
    Focus: 'Focus',
    Move: 'Move',
    Settle: 'Settle',
    Speak: 'Speak',
    Advance: 'Advance',
  };
  const SPEED_VALUES = new Set(['0.5', '1', '2', '4']);

  const stageEl = document.getElementById('map-stage');
  const playToggleBtn = document.getElementById('play-toggle');
  const nextEventBtn = document.getElementById('next-event');
  const resetReplayBtn = document.getElementById('reset-replay');
  const bgmToggleBtn = document.getElementById('bgm-toggle');
  const bgmVolumeInput = document.getElementById('bgm-volume');
  const speedSelect = document.getElementById('speed-select');
  const statusSpeaker = document.getElementById('status-speaker');
  const statusPlace = document.getElementById('status-place');
  const statusProgress = document.getElementById('status-progress');
  const unsupportedStateEl = document.getElementById('unsupported-state');

  let game = null;
  let replayData = null;
  let storyMap = null;
  let eventIndex = -1;
  let isPlaying = false;
  let playbackTimer = null;
  let playbackWaitCancel = null;
  const motionCancelers = new Set();
  let playbackGeneration = 0;
  let isTransitioning = false;
  let activeBubble = null;
  let audioController = null;

  const runtime = {
    scene: null,
    mode: 'storyMap',
    characters: new Map(),
    positions: new Map(),
    currentPlaces: new Map(),
    placeLookup: new Map(),
    placeLabels: new Map(),
    placeTextureKeys: new Map(),
    recentPlaceCharacters: new Map(),
    activeCharId: null,
    activePlaceId: null,
    backgroundKey: 'story-map-background',
    backgroundImage: null,
    fallbackBackdrop: null,
    fallbackLabel: null,
    phase: ReplayPhase.Advance,
  };

  function esc(str) {
    return String(str ?? '');
  }

  class PlaybackCancelledError extends Error {
    constructor() {
      super('playback cancelled');
      this.name = 'PlaybackCancelledError';
    }
  }

  function isPlaybackCancelled(error) {
    return error instanceof PlaybackCancelledError;
  }

  function registerMotionCanceler(cancel) {
    motionCancelers.add(cancel);
    return () => {
      motionCancelers.delete(cancel);
    };
  }

  function cancelSceneMotion() {
    const cancelers = Array.from(motionCancelers);
    motionCancelers.clear();
    cancelers.forEach((cancel) => {
      cancel();
    });
  }

  function cancelPlaybackWait() {
    const cancel = playbackWaitCancel;
    playbackWaitCancel = null;
    if (playbackTimer !== null) {
      window.clearTimeout(playbackTimer);
      playbackTimer = null;
    }
    if (cancel) {
      cancel();
    }
  }

  function playbackDelay(ms) {
    cancelPlaybackWait();
    return new Promise((resolve, reject) => {
      let settled = false;
      playbackWaitCancel = () => {
        if (settled) {
          return;
        }
        settled = true;
        playbackWaitCancel = null;
        reject(new PlaybackCancelledError());
      };
      playbackTimer = window.setTimeout(() => {
        if (settled) {
          return;
        }
        settled = true;
        playbackTimer = null;
        playbackWaitCancel = null;
        resolve();
      }, ms);
    });
  }

  function assertPlaybackNotCancelled(runGeneration) {
    if (runGeneration !== playbackGeneration) {
      throw new PlaybackCancelledError();
    }
  }

  function getSpeedMultiplier() {
    const value = speedSelect && SPEED_VALUES.has(speedSelect.value) ? speedSelect.value : '1';
    return Number(value);
  }

  function getEffectiveSpeedMultiplier() {
    return getSpeedMultiplier() / 2;
  }

  function buildStoryAssetUrl(relativePath) {
    return `${storyMapBaseUrl}/${encodeURIComponent(storyId)}/${relativePath}`;
  }

  function resolveStoryAssetPath(relativePath) {
    const trimmed = String(relativePath || '').replace(/^\/+/, '');
    return buildStoryAssetUrl(trimmed.split('/').map((part) => encodeURIComponent(part)).join('/'));
  }

  function resolveBgmAssetPath(relativePath) {
    const raw = String(relativePath || '').trim();
    if (!raw || raw.startsWith('/') || raw.includes('\\')) {
      return null;
    }
    const parts = raw.split('/');
    if (parts.includes('..') || parts.includes('.') || parts.some((part) => !part)) {
      return null;
    }
    return buildStoryAssetUrl(parts.map((part) => encodeURIComponent(part)).join('/'));
  }

  function resolveNarratorAvatarUrl() {
    const baseUrl = String(imageBaseUrl || '').replace(/\/+$/, '');
    if (baseUrl) {
      return `${baseUrl}/_system/narration/neutral.png`;
    }
    return NARRATOR_AVATAR_URL;
  }

  function normalizeReplayCharacter(character) {
    const source = character || {};
    const charId = String(source.char_id || '');
    if (charId === NARRATOR_CHAR_ID) {
      return {
        ...source,
        char_id: NARRATOR_CHAR_ID,
        char_name: NARRATOR_DISPLAY_NAME,
        avatar_url: source.avatar_url || resolveNarratorAvatarUrl(),
      };
    }
    return {
      ...source,
      char_id: charId,
      char_name: source.char_name || charId,
    };
  }

  function normalizeReplayEvent(event) {
    const source = event || {};
    const charId = String(source.char_id || '');
    if (charId === NARRATOR_CHAR_ID) {
      return {
        ...source,
        char_id: NARRATOR_CHAR_ID,
        char_name: NARRATOR_DISPLAY_NAME,
      };
    }
    return {
      ...source,
      char_id: charId,
      char_name: source.char_name || charId,
    };
  }

  function normalizeReplayData(data) {
    const normalized = { ...data };
    normalized.events = (data.events || []).map(normalizeReplayEvent);
    normalized.characters = (data.characters || []).map(normalizeReplayCharacter);
    const hasNarratorEvent = normalized.events.some((event) => event.char_id === NARRATOR_CHAR_ID);
    const hasNarratorCharacter = normalized.characters.some((character) => (
      character.char_id === NARRATOR_CHAR_ID
    ));
    if (hasNarratorEvent && !hasNarratorCharacter) {
      normalized.characters.push(normalizeReplayCharacter({ char_id: NARRATOR_CHAR_ID }));
    }
    return normalized;
  }

  function updateBgmButtonLabel() {
    if (!bgmToggleBtn) {
      return;
    }
    if (!audioController) {
      bgmToggleBtn.textContent = i18n.t('map.bgmOn');
      return;
    }
    bgmToggleBtn.textContent = audioController.isEnabled() ? i18n.t('map.bgmOn') : i18n.t('map.bgmOff');
  }

  function updateBgmVolumeUi() {
    if (!bgmVolumeInput || !audioController) {
      return;
    }
    bgmVolumeInput.value = String(audioController.getVolume());
  }

  class ReplayAudioController {
    constructor(options) {
      this.storyMapBaseUrl = options.storyMapBaseUrl;
      this.defaultBgmUrl = options.defaultBgmUrl;
      this.enabled = true;
      this.unlocked = false;
      this.audio = null;
      this.currentUrl = null;
      this.fallbackUrl = null;
      this.volume = DEFAULT_BGM_VOLUME;
      this.shouldPlay = false;
      this.manifest = null;
    }

    async initialize() {
      this.manifest = await this.fetchManifest();
      this.enabled = this.manifest ? this.manifest.enabled !== false : true;
      this.currentUrl = this.resolveTrackUrl(this.manifest);
      this.fallbackUrl = this.currentUrl && this.currentUrl !== this.defaultBgmUrl ? this.defaultBgmUrl : null;
      if (this.currentUrl) {
        this.ensureAudio(this.currentUrl);
      }
      updateBgmButtonLabel();
      updateBgmVolumeUi();
    }

    async fetchManifest() {
      const url = buildStoryAssetUrl(BGM_MANIFEST_FILENAME);
      const response = await fetch(url);
      if (response.status === 404) {
        return null;
      }
      if (!response.ok) {
        throw new Error(`bgm manifest error: HTTP ${response.status}`);
      }
      return response.json();
    }

    resolveTrackUrl(manifest, placeId = null) {
      if (placeId && manifest && manifest.place_overrides) {
        const override = manifest.place_overrides[placeId];
        if (typeof override === 'string' && override.trim()) {
          const overrideUrl = resolveBgmAssetPath(override);
          if (overrideUrl) {
            return overrideUrl;
          }
        }
      }
      if (manifest && typeof manifest.story_default === 'string' && manifest.story_default.trim()) {
        const storyDefaultUrl = resolveBgmAssetPath(manifest.story_default);
        if (storyDefaultUrl) {
          return storyDefaultUrl;
        }
      }
      return this.defaultBgmUrl || null;
    }

    ensureAudio(trackUrl) {
      if (this.audio && this.audio.src === trackUrl) {
        return this.audio;
      }

      if (this.audio) {
        this.audio.pause();
      }

      this.audio = new Audio(trackUrl);
      this.audio.loop = true;
      this.audio.preload = 'auto';
      this.audio.volume = this.volume;
      this.audio.addEventListener('error', () => {
        if (this.fallbackUrl && this.audio && this.audio.src !== this.fallbackUrl) {
          const shouldResume = this.shouldPlay && this.unlocked && this.enabled;
          this.audio.src = this.fallbackUrl;
          this.audio.load();
          if (shouldResume) {
            this.audio.play().catch(() => {});
          }
          this.currentUrl = this.fallbackUrl;
          this.fallbackUrl = null;
        }
      });
      return this.audio;
    }

    isEnabled() {
      return this.enabled;
    }

    getVolume() {
      return this.volume;
    }

    setVolume(value) {
      const parsed = Number(value);
      this.volume = Number.isFinite(parsed) ? Math.max(0, Math.min(1, parsed)) : DEFAULT_BGM_VOLUME;
      if (this.audio) {
        this.audio.volume = this.volume;
      }
      updateBgmVolumeUi();
    }

    setEnabled(enabled) {
      this.enabled = Boolean(enabled);
      if (!this.enabled) {
        this.pause();
      } else if (this.shouldPlay) {
        this.syncPlayback(true);
      }
      updateBgmButtonLabel();
    }

    async unlock() {
      this.unlocked = true;
      if (!this.audio && this.currentUrl) {
        this.ensureAudio(this.currentUrl);
      }
      if (!this.audio) {
        return;
      }
      try {
        await this.audio.play();
        this.audio.pause();
        this.audio.currentTime = this.audio.currentTime || 0;
      } catch (_error) {
        // Browser autoplay policy may still block until the same gesture triggers real playback.
      }
    }

    async syncPlayback(shouldPlay) {
      this.shouldPlay = Boolean(shouldPlay);
      if (!this.enabled || !this.currentUrl) {
        this.pause();
        return;
      }
      if (!this.unlocked) {
        return;
      }
      if (!this.audio) {
        this.ensureAudio(this.currentUrl);
      }
      if (!this.audio) {
        return;
      }
      if (this.shouldPlay) {
        this.audio.play().catch(() => {});
      } else {
        this.pause();
      }
    }

    pause() {
      if (this.audio) {
        this.audio.pause();
      }
    }

    async switchToPlace(placeId) {
      if (!this.enabled || !this.manifest || !placeId) return;
      const trackUrl = this.resolveTrackUrl(this.manifest, placeId);
      if (!trackUrl || trackUrl === this.currentUrl) return;
      this.currentUrl = trackUrl;
      this.fallbackUrl = trackUrl !== this.defaultBgmUrl ? this.defaultBgmUrl : null;
      const wasPlaying = this.shouldPlay && this.unlocked;
      this.ensureAudio(trackUrl);
      if (wasPlaying && this.enabled && this.audio) {
        this.audio.play().catch(() => {});
      }
    }

    reset() {
      this.pause();
      this.shouldPlay = false;
      if (this.audio) {
        this.audio.currentTime = 0;
      }
    }

    destroy() {
      if (this.audio) {
        this.audio.pause();
        this.audio.src = '';
        this.audio.load();
      }
      this.audio = null;
    }
  }

  function scaledDuration(ms) {
    return Math.max(180, Math.round(ms / getEffectiveSpeedMultiplier()));
  }

  function tweenPromise(scene, config) {
    return new Promise((resolve, reject) => {
      let settled = false;
      let tween = null;
      let unregister = () => {};
      const finish = (fn, value) => {
        if (settled) {
          return;
        }
        settled = true;
        unregister();
        fn(value);
      };
      const cancel = () => {
        if (tween && typeof tween.stop === 'function') {
          tween.stop();
        }
        finish(reject, new PlaybackCancelledError());
      };
      unregister = registerMotionCanceler(cancel);
      tween = scene.tweens.add({
        ...config,
        onComplete: (...args) => {
          if (typeof config.onComplete === 'function') {
            config.onComplete(...args);
          }
          finish(resolve);
        },
      });
    });
  }

  function fetchReplayData() {
    const url = `${apiUrl}?story_id=${encodeURIComponent(storyId)}&action=replay`;
    return fetch(url).then((response) => {
      if (!response.ok) {
        const error = new Error(`HTTP ${response.status}`);
        error.status = response.status;
        throw error;
      }
      return response.json();
    }).then((data) => {
      if (data.status !== 'ok') {
        throw new Error(data.message || 'replay api error');
      }
      return normalizeReplayData(data);
    });
  }

  function fetchStoryMap() {
    const url = buildStoryAssetUrl('map.json');
    return fetch(url).then((response) => {
      if (!response.ok) {
        const error = new Error('story map is missing');
        error.kind = 'storyMap';
        error.status = response.status;
        throw error;
      }
      return response.json();
    }).then((data) => {
      if (!data || !Array.isArray(data.places) || !data.background) {
        const error = new Error('story map is invalid');
        error.kind = 'storyMap';
        throw error;
      }
      return data;
    });
  }

  function imageExists(url) {
    return new Promise((resolve) => {
      const img = new Image();
      img.onload = () => resolve(true);
      img.onerror = () => resolve(false);
      img.src = url;
    });
  }

  function isPlaceImageModeCandidate(placeId) {
    return Boolean(placeId) && placeId !== UNKNOWN_PLACE_ID;
  }

  function buildPlaceSceneUrl(placeId) {
    return buildStoryAssetUrl(`images/${encodeURIComponent(placeId)}${PLACE_IMAGE_SUFFIX}`);
  }

  async function probePlaceSceneAvailability(places) {
    const candidates = (places || [])
      .map((place) => String(place.place_id || ''))
      .filter(isPlaceImageModeCandidate);

    if (!candidates.length) {
      return false;
    }

    const uniqueCandidates = [...new Set(candidates)];
    const results = await Promise.all(
      uniqueCandidates.slice(0, 4).map((placeId) => imageExists(buildPlaceSceneUrl(placeId)))
    );
    return results.some(Boolean);
  }

  function getStageSize() {
    return {
      width: Math.max(820, Math.floor(stageEl.clientWidth || 1280)),
      height: Math.max(540, Math.floor(stageEl.clientHeight || 720)),
    };
  }

  function getPlaceLabel(placeId) {
    return runtime.placeLabels.get(placeId) || placeId || UNKNOWN_PLACE_ID;
  }

  function getPlace(placeId) {
    const resolvedId = placeId && runtime.placeLookup.has(placeId) ? placeId : UNKNOWN_PLACE_ID;
    return runtime.placeLookup.get(resolvedId);
  }

  function updateHud(event) {
    statusSpeaker.textContent = `${i18n.t('map.speaker')}: ${esc(event.char_name || event.char_id || i18n.t('map.unknown'))}`;
    statusPlace.textContent = `${i18n.t('map.place')}: ${esc(getPlaceLabel(event.place_id))}`;
    statusProgress.textContent = `${event.seq} / ${replayData.events.length}`;
  }

  function showUnsupportedState(message) {
    unsupportedStateEl.hidden = false;
    unsupportedStateEl.textContent = message;
    statusSpeaker.textContent = `${i18n.t('map.speaker')}: ${i18n.t('map.unknown')}`;
    statusPlace.textContent = `${i18n.t('map.place')}: ${i18n.t('map.unknown')}`;
    statusProgress.textContent = '0 / 0';
  }

  function hideUnsupportedState() {
    unsupportedStateEl.hidden = true;
    unsupportedStateEl.textContent = '';
  }

  function splitMessagePages(message) {
    const text = String(message || '').trim();
    if (!text) {
      return [];
    }

    const maxCharsPerPage = 54;
    const pages = [];
    let cursor = 0;
    while (cursor < text.length) {
      pages.push(text.slice(cursor, cursor + maxCharsPerPage));
      cursor += maxCharsPerPage;
    }
    return pages;
  }

  function clearBubble() {
    if (activeBubble) {
      activeBubble.destroy(true);
      activeBubble = null;
    }
  }

  function getAvatarBody(container) {
    return container && container.avatarBody ? container.avatarBody : null;
  }

  function getAvatarLabel(container) {
    return container && container.avatarLabel ? container.avatarLabel : null;
  }

  function fitDisplayObject(body, maxSize) {
    const width = Number(body.width || body.displayWidth || 1);
    const height = Number(body.height || body.displayHeight || 1);
    const scale = Math.min(maxSize / width, maxSize / height);
    if (typeof body.setScale === 'function') {
      body.setScale(scale);
    }
  }

  function applyAvatarPresentation(container, isActive) {
    const body = getAvatarBody(container);
    const label = getAvatarLabel(container);
    const maxSize = isActive ? ACTIVE_AVATAR_MAX_SIZE : SUPPORT_AVATAR_MAX_SIZE;
    const initials = container.list.find((child) => child.name === 'avatar-initials');

    if (body) {
      fitDisplayObject(body, maxSize);
      body.setY(isActive ? -4 : -10);
      if (initials) {
        initials.setY(body.y);
        initials.setScale(body.scaleX, body.scaleY);
      }
    }

    if (label && body) {
      const labelY = body.y + (body.displayHeight / 2) + 18;
      label.setY(labelY);
      label.setFontSize(isActive ? '12px' : '11px');
    }
  }

  function getBubbleAnchorY(container) {
    const body = getAvatarBody(container);
    if (!body) {
      return container.y - 120;
    }
    return container.y + body.y - (body.displayHeight / 2) - BUBBLE_SAFE_MARGIN;
  }

  function positionBubbleForContainer(container) {
    if (!activeBubble || !container) {
      return;
    }
    activeBubble.x = container.x;
    activeBubble.y = getBubbleAnchorY(container);
  }

  function showBubble(scene, container, message) {
    clearBubble();
    const pages = splitMessagePages(message);
    if (!pages.length) {
      return Promise.resolve();
    }

    const showPage = (index) => {
      if (index >= pages.length) {
        clearBubble();
        return Promise.resolve();
      }

      const textObj = scene.add.text(0, 0, pages[index], {
        fontFamily: 'sans-serif',
        fontSize: '17px',
        color: '#0f172a',
        align: 'center',
        wordWrap: { width: 300, useAdvancedWrap: true },
        lineSpacing: 6,
      }).setOrigin(0.5);

      const bubbleWidth = Math.max(170, textObj.width + 36);
      const bubbleHeight = Math.max(72, textObj.height + 30);
      const bubbleBg = scene.add.graphics();
      bubbleBg.fillStyle(0xf8fafc, 0.96);
      bubbleBg.fillRoundedRect(-bubbleWidth / 2, -bubbleHeight / 2, bubbleWidth, bubbleHeight, 16);
      bubbleBg.fillTriangle(-14, bubbleHeight / 2 - 8, 0, bubbleHeight / 2 + 12, 14, bubbleHeight / 2 - 8);
      textObj.setY(-6);

      activeBubble = scene.add.container(container.x, getBubbleAnchorY(container), [bubbleBg, textObj]);
      activeBubble.setDepth(120);

      return playbackDelay(scaledDuration(1400 + (pages[index].length * 38)))
        .then(() => {
          clearBubble();
          return showPage(index + 1);
        })
        .catch((error) => {
          clearBubble();
          throw error;
        });
    };

    return showPage(0);
  }

  function createCharacterSprite(scene, character) {
    const textureKey = `avatar-${character.char_id}`;
    const container = scene.add.container(0, 0);
    let body;

    if (character.avatar_url && scene.textures.exists(textureKey)) {
      body = scene.add.image(0, -4, textureKey);
    } else {
      body = scene.add.circle(0, -4, 28, 0xfb7185, 1);
      const initials = String(character.char_name || character.char_id || '?').slice(0, 2);
      const initialsText = scene.add.text(0, -4, initials, {
        fontFamily: 'sans-serif',
        fontSize: '16px',
        color: '#0f172a',
        fontStyle: 'bold',
      }).setOrigin(0.5);
      initialsText.setName('avatar-initials');
      container.add(initialsText);
    }

    const label = scene.add.text(0, 86, String(character.char_name || character.char_id || '?'), {
      fontFamily: 'sans-serif',
      fontSize: '12px',
      color: '#f8fafc',
      backgroundColor: 'rgba(15,23,42,0.78)',
      padding: { x: 7, y: 3 },
    }).setOrigin(0.5);

    container.add(body);
    container.add(label);
    container.avatarBody = body;
    container.avatarLabel = label;
    container.setDepth(100);
    container.setVisible(false);
    scene.add.existing(container);
    applyAvatarPresentation(container, true);
    runtime.characters.set(character.char_id, container);
    return container;
  }

  function getOrCreateCharacter(scene, eventOrId) {
    const charId = typeof eventOrId === 'string' ? eventOrId : eventOrId.char_id;
    if (runtime.characters.has(charId)) {
      return runtime.characters.get(charId);
    }

    const character = (replayData.characters || []).find((item) => item.char_id === charId)
      || {
        char_id: charId,
        char_name: typeof eventOrId === 'string' ? charId : (eventOrId.char_name || charId),
        avatar_url: null,
      };
    return createCharacterSprite(runtime.scene, character);
  }

  function hideNonOccupants(occupantIds) {
    runtime.characters.forEach((container, charId) => {
      if (occupantIds.has(charId)) {
        return;
      }
      container.setVisible(false);
      container.setAlpha(0);
    });
  }

  function rememberRecentPlaceCharacter(placeId, charId) {
    const resolvedPlaceId = placeId || UNKNOWN_PLACE_ID;
    const recentSupportIds = runtime.recentPlaceCharacters.get(resolvedPlaceId) || [];
    const filtered = recentSupportIds.filter((id) => id !== charId);
    filtered.push(charId);
    runtime.recentPlaceCharacters.set(resolvedPlaceId, filtered);
  }

  function getRecentSupportIds(placeId, activeCharId) {
    const resolvedPlaceId = placeId || UNKNOWN_PLACE_ID;
    const samePlaceIds = [];
    runtime.currentPlaces.forEach((currentPlaceId, charId) => {
      if ((currentPlaceId || UNKNOWN_PLACE_ID) === resolvedPlaceId && charId !== activeCharId) {
        samePlaceIds.push(charId);
      }
    });

    const recentSupportIds = runtime.recentPlaceCharacters.get(resolvedPlaceId) || [];
    const supportCandidates = recentSupportIds.filter((charId) => samePlaceIds.includes(charId) && charId !== activeCharId);
    return supportCandidates.slice(-MAX_SUPPORT_CHARACTERS);
  }

  function buildStageLayout(activeCharId, supportIds) {
    const size = getStageSize();
    const centerX = size.width / 2;
    const speakerRowY = size.height * 0.61;
    const supportRowY = size.height * 0.82;
    const stageLayout = [
      {
        charId: activeCharId,
        x: centerX,
        y: speakerRowY,
        scale: 1,
        alpha: 1,
        depth: 120,
      },
    ];

    const supportSlots = {
      1: [centerX],
      2: [centerX - 180, centerX + 180],
      3: [centerX - 220, centerX, centerX + 220],
    };
    const slotPositions = supportSlots[supportIds.length] || supportSlots[3];

    supportIds.forEach((charId, index) => {
      stageLayout.push({
        charId,
        x: slotPositions[index] ?? (centerX + ((index - 1) * 180)),
        y: supportRowY,
        scale: 0.9,
        alpha: 0.88,
        depth: 100 - index,
      });
    });

    return stageLayout;
  }

  async function transitionPlaceScene(scene, placeId) {
    if (runtime.mode !== 'placeImage') {
      return;
    }

    if (runtime.activePlaceId === placeId) {
      return;
    }

    const resolvedPlaceId = placeId || UNKNOWN_PLACE_ID;
    const textureKey = runtime.placeTextureKeys.get(resolvedPlaceId);
    const hasTexture = Boolean(textureKey && scene.textures.exists(textureKey));
    const shouldUseFallback = !hasTexture;

    if (runtime.backgroundImage) {
      await tweenPromise(scene, {
        targets: runtime.backgroundImage,
        alpha: 0.18,
        duration: scaledDuration(260),
        ease: 'Sine.easeInOut',
      });
    }

    if (shouldUseFallback) {
      if (runtime.backgroundImage) {
        runtime.backgroundImage.setVisible(false);
      }
      runtime.fallbackBackdrop.setVisible(true);
      runtime.fallbackLabel.setVisible(true);
      runtime.fallbackLabel.setText(
        `${getPlaceLabel(resolvedPlaceId)}\nbackground fallback`
      );
      runtime.fallbackBackdrop.setAlpha(0);
      runtime.fallbackLabel.setAlpha(0);
      await Promise.all([
        tweenPromise(scene, {
          targets: runtime.fallbackBackdrop,
          alpha: 1,
          duration: scaledDuration(280),
          ease: 'Sine.easeOut',
        }),
        tweenPromise(scene, {
          targets: runtime.fallbackLabel,
          alpha: 1,
          duration: scaledDuration(280),
          ease: 'Sine.easeOut',
        }),
      ]);
    } else {
      runtime.fallbackBackdrop.setVisible(false);
      runtime.fallbackLabel.setVisible(false);
      if (!runtime.backgroundImage) {
        runtime.backgroundImage = scene.add.image(0, 0, textureKey);
        runtime.backgroundImage.setDepth(0);
      }
      runtime.backgroundImage.setTexture(textureKey);
      runtime.backgroundImage.setVisible(true);
      runtime.backgroundImage.setAlpha(0.18);
      fitBackgroundToStage(scene);
      await tweenPromise(scene, {
        targets: runtime.backgroundImage,
        alpha: 1,
        duration: scaledDuration(300),
        ease: 'Sine.easeOut',
      });
    }

    runtime.activePlaceId = resolvedPlaceId;
  }

  function switchBgmForPlace(placeId) {
    if (audioController) {
      audioController.switchToPlace(placeId || UNKNOWN_PLACE_ID);
    }
  }

  async function arrangePlaceSceneCharacters(scene, event) {
    runtime.currentPlaces.set(event.char_id, event.place_id || UNKNOWN_PLACE_ID);
    runtime.activeCharId = event.char_id;
    rememberRecentPlaceCharacter(event.place_id, event.char_id);

    const recentSupportIds = getRecentSupportIds(event.place_id, event.char_id);
    const layout = buildStageLayout(event.char_id, recentSupportIds);
    const occupantIds = new Set([event.char_id, ...recentSupportIds]);
    hideNonOccupants(occupantIds);

    await Promise.all(layout.map(async (slot) => {
      const container = getOrCreateCharacter(scene, slot.charId);
      const isFirstAppearance = !container.visible;
      applyAvatarPresentation(container, slot.charId === event.char_id);
      container.setVisible(true);
      container.setDepth(slot.depth);
      if (isFirstAppearance) {
        container.setPosition(slot.x, slot.y + 40);
        container.setScale(slot.scale);
        container.setAlpha(0);
      }
      await tweenPromise(scene, {
        targets: container,
        x: slot.x,
        y: slot.y,
        scaleX: slot.scale,
        scaleY: slot.scale,
        alpha: slot.alpha,
        duration: scaledDuration(520),
        ease: 'Sine.easeInOut',
        onUpdate: () => {
          if (activeBubble && slot.charId === runtime.activeCharId) {
            positionBubbleForContainer(container);
          }
        },
      });
    }));
  }

  function cameraPanPromise(scene, x, y, duration) {
    return new Promise((resolve, reject) => {
      let settled = false;
      let unregister = () => {};
      const camera = scene.cameras.main;
      const finish = (fn, value) => {
        if (settled) {
          return;
        }
        settled = true;
        unregister();
        fn(value);
      };
      const cancel = () => {
        if (camera.panEffect && typeof camera.panEffect.reset === 'function') {
          camera.panEffect.reset();
        } else if (typeof camera.resetFX === 'function') {
          camera.resetFX();
        }
        finish(reject, new PlaybackCancelledError());
      };
      unregister = registerMotionCanceler(cancel);
      scene.cameras.main.pan(x, y, duration, 'Sine.easeInOut', true, (_camera, progress) => {
        if (activeBubble && runtime.characters.has(runtime.activeCharId)) {
          const activeContainer = runtime.characters.get(runtime.activeCharId);
          positionBubbleForContainer(activeContainer);
        }
        if (progress >= 1) {
          finish(resolve);
        }
      });
    });
  }

  function moveCharacterOnStoryMap(scene, event) {
    const character = getOrCreateCharacter(scene, event);
    const place = getPlace(event.place_id);
    const destination = { x: place.x, y: place.y };
    const previous = runtime.positions.get(event.char_id);

    runtime.positions.set(event.char_id, destination);
    runtime.activeCharId = event.char_id;
    character.setVisible(true);

    if (!previous) {
      character.setPosition(destination.x, destination.y);
      return cameraPanPromise(scene, destination.x, destination.y, scaledDuration(700));
    }

    if (previous.x === destination.x && previous.y === destination.y) {
      return cameraPanPromise(scene, destination.x, destination.y, scaledDuration(500));
    }

    runtime.phase = ReplayPhase.Move;
    return Promise.all([
      tweenPromise(scene, {
        targets: character,
        x: destination.x,
        y: destination.y,
        duration: scaledDuration(1200),
        ease: 'Sine.easeInOut',
        onUpdate: () => {
          if (activeBubble) {
            positionBubbleForContainer(character);
          }
        },
      }),
      cameraPanPromise(scene, destination.x, destination.y, scaledDuration(1200)),
    ]);
  }

  async function moveCharacter(scene, event) {
    if (runtime.mode === 'placeImage') {
      runtime.phase = ReplayPhase.Move;
      await transitionPlaceScene(scene, event.place_id);
      await arrangePlaceSceneCharacters(scene, event);
      return;
    }

    return moveCharacterOnStoryMap(scene, event);
  }

  function settlePromise() {
    runtime.phase = ReplayPhase.Settle;
    return playbackDelay(scaledDuration(420));
  }

  function speakPromise(scene, event) {
    runtime.phase = ReplayPhase.Speak;
    const character = runtime.characters.get(event.char_id);
    return showBubble(scene, character, event.message);
  }

  function advancePromise() {
    runtime.phase = ReplayPhase.Advance;
    return playbackDelay(scaledDuration(220));
  }

  function stopPlayback() {
    isPlaying = false;
    playbackGeneration += 1;
    cancelPlaybackWait();
    cancelSceneMotion();
    clearBubble();
    playToggleBtn.textContent = i18n.t('map.play');
    if (audioController) {
      audioController.syncPlayback(false);
    }
  }

  async function runEvent(scene, event) {
    const runGeneration = playbackGeneration;
    runtime.phase = ReplayPhase.Focus;
    updateHud(event);
    switchBgmForPlace(event.place_id);
    await moveCharacter(scene, event);
    assertPlaybackNotCancelled(runGeneration);
    await settlePromise();
    assertPlaybackNotCancelled(runGeneration);
    await speakPromise(scene, event);
    assertPlaybackNotCancelled(runGeneration);
    await advancePromise();
  }

  async function stepReplay() {
    if (!runtime.scene || !replayData || !replayData.events.length || isTransitioning) {
      return;
    }

    if (eventIndex >= replayData.events.length - 1) {
      stopPlayback();
      return;
    }

    isTransitioning = true;
    eventIndex += 1;
    const event = replayData.events[eventIndex];

    try {
      await runEvent(runtime.scene, event);
    } catch (error) {
      if (!isPlaybackCancelled(error)) {
        throw error;
      }
    } finally {
      isTransitioning = false;
    }

    if (isPlaying) {
      stepReplay();
    } else if (eventIndex >= replayData.events.length - 1) {
      stopPlayback();
    }
  }

  function resetReplay() {
    stopPlayback();
    if (audioController) {
      audioController.reset();
    }
    eventIndex = -1;
    runtime.phase = ReplayPhase.Advance;
    runtime.activeCharId = null;
    runtime.activePlaceId = null;
    runtime.currentPlaces.clear();
    runtime.recentPlaceCharacters.clear();

    runtime.characters.forEach((container) => {
      container.destroy(true);
    });
    runtime.characters.clear();
    runtime.positions.clear();

    if (runtime.mode === 'storyMap' && runtime.scene) {
      const defaultPlace = getPlace('classroom') || Array.from(runtime.placeLookup.values())[0];
      if (defaultPlace) {
        runtime.scene.cameras.main.centerOn(defaultPlace.x, defaultPlace.y);
      }
    }

    if (runtime.mode === 'placeImage' && runtime.scene) {
      if (runtime.backgroundImage) {
        runtime.backgroundImage.setVisible(false);
      }
      runtime.fallbackBackdrop.setVisible(true);
      runtime.fallbackBackdrop.setAlpha(1);
      runtime.fallbackLabel.setVisible(true);
      runtime.fallbackLabel.setAlpha(1);
      runtime.fallbackLabel.setText(i18n.t('map.play'));
    }

    statusSpeaker.textContent = `${i18n.t('map.speaker')}: ${i18n.t('map.unknown')}`;
    statusPlace.textContent = `${i18n.t('map.place')}: ${i18n.t('map.unknown')}`;
    statusProgress.textContent = `0 / ${replayData ? replayData.events.length : 0}`;
  }

  function continuePlaybackIfNeeded() {
    if (!isPlaying) {
      return;
    }
    stepReplay();
  }

  function initializeControls() {
    if (playToggleBtn) playToggleBtn.textContent = i18n.t('map.play');
    if (nextEventBtn) nextEventBtn.textContent = i18n.t('map.next');
    if (resetReplayBtn) resetReplayBtn.textContent = i18n.t('map.reset');
    updateBgmButtonLabel();
    i18n.applyStaticLabels();
    i18n.bindLocaleSwitcher();

    playToggleBtn.addEventListener('click', () => {
      if (!replayData || !replayData.events.length) {
        return;
      }
      if (isPlaying) {
        stopPlayback();
        return;
      }
      if (isTransitioning) {
        return;
      }

      isPlaying = true;
      playToggleBtn.textContent = i18n.t('map.pause');
      if (audioController) {
        audioController.unlock().finally(() => {
          if (!isPlaying) {
            return;
          }
          audioController.syncPlayback(true);
          continuePlaybackIfNeeded();
        });
        return;
      }
      continuePlaybackIfNeeded();
    });

    nextEventBtn.addEventListener('click', () => {
      if (!replayData || isTransitioning) {
        return;
      }
      stopPlayback();
      stepReplay();
    });

    resetReplayBtn.addEventListener('click', () => {
      resetReplay();
    });

    speedSelect.addEventListener('change', () => {
      if (!SPEED_VALUES.has(speedSelect.value)) {
        speedSelect.value = '1';
      }
    });

    if (bgmToggleBtn) {
      bgmToggleBtn.addEventListener('click', () => {
        if (!audioController) {
          return;
        }
        const nextEnabled = !audioController.isEnabled();
        const apply = () => audioController.setEnabled(nextEnabled);
        if (nextEnabled) {
          audioController.unlock().finally(apply);
        } else {
          apply();
        }
      });
    }

    if (bgmVolumeInput) {
      bgmVolumeInput.addEventListener('input', () => {
        if (!audioController) {
          return;
        }
        audioController.setVolume(bgmVolumeInput.value);
      });
    }
  }

  function createUnsupportedFallbackMessage(error) {
    if (error && error.kind === 'storyMap') {
      return i18n.t('map.unsupportedStoryMap');
    }
    if (error && error.status === 404) {
      return i18n.t('map.notPublished');
    }
    return i18n.t('map.unavailable');
  }

  function fitBackgroundToStage(scene) {
    if (runtime.mode !== 'placeImage' || !runtime.backgroundImage) {
      return;
    }

    const size = getStageSize();
    const texture = scene.textures.get(runtime.backgroundImage.texture.key);
    const source = texture && texture.getSourceImage();
    if (!source || !source.width || !source.height) {
      runtime.backgroundImage.setPosition(size.width / 2, size.height / 2);
      runtime.backgroundImage.setDisplaySize(size.width, size.height);
      return;
    }

    const scale = Math.max(size.width / source.width, size.height / source.height);
    runtime.backgroundImage.setPosition(size.width / 2, size.height / 2);
    runtime.backgroundImage.setDisplaySize(Math.round(source.width * scale), Math.round(source.height * scale));
  }

  function buildStoryMapSceneConfig() {
    const size = getStageSize();
    return {
      type: Phaser.AUTO,
      parent: 'map-stage',
      width: size.width,
      height: size.height,
      backgroundColor: '#0f172a',
      pixelArt: true,
      antialias: false,
      scene: {
        preload() {
          this.load.image(runtime.backgroundKey, resolveStoryAssetPath(storyMap.background));

          (replayData.characters || []).forEach((character) => {
            if (character.avatar_url) {
              this.load.image(`avatar-${character.char_id}`, character.avatar_url);
            }
          });
        },
        create() {
          runtime.scene = this;

          storyMap.places.forEach((place) => {
            runtime.placeLookup.set(String(place.place_id), {
              place_id: String(place.place_id),
              x: Number(place.x),
              y: Number(place.y),
              label: String(place.label || place.place_id),
            });
          });
          if (!runtime.placeLookup.has(UNKNOWN_PLACE_ID)) {
            runtime.placeLookup.set(UNKNOWN_PLACE_ID, {
              place_id: UNKNOWN_PLACE_ID,
              x: 80,
              y: 80,
              label: UNKNOWN_PLACE_ID,
            });
          }

          const background = this.add.image(0, 0, runtime.backgroundKey).setOrigin(0, 0);
          background.setDepth(0);

          const cam = this.cameras.main;
          cam.setBounds(0, 0, background.width, background.height);
          cam.setRoundPixels(true);
          cam.setZoom(Number((storyMap.camera && storyMap.camera.zoom) || 1));

          const defaultPlace = getPlace('classroom') || Array.from(runtime.placeLookup.values())[0];
          if (defaultPlace) {
            cam.centerOn(defaultPlace.x, defaultPlace.y);
          }

          statusProgress.textContent = `0 / ${replayData.events.length}`;
        },
      },
    };
  }

  function buildPlaceImageSceneConfig() {
    const size = getStageSize();
    return {
      type: Phaser.AUTO,
      parent: 'map-stage',
      width: size.width,
      height: size.height,
      backgroundColor: '#0f172a',
      pixelArt: true,
      antialias: false,
      scene: {
        preload() {
          (replayData.characters || []).forEach((character) => {
            if (character.avatar_url) {
              this.load.image(`avatar-${character.char_id}`, character.avatar_url);
            }
          });

          const placeIds = [...new Set((replayData.places || []).map((place) => String(place.place_id || '')))];
          placeIds.forEach((placeId) => {
            if (!isPlaceImageModeCandidate(placeId)) {
              return;
            }
            const textureKey = `place-scene-${placeId}`;
            runtime.placeTextureKeys.set(placeId, textureKey);
            this.load.image(textureKey, buildPlaceSceneUrl(placeId));
          });
        },
        create() {
          runtime.scene = this;
          const stageSize = getStageSize();
          const initialTextureKey = Array.from(runtime.placeTextureKeys.values()).find(
            (key) => this.textures.exists(key)
          );

          if (initialTextureKey) {
            runtime.backgroundImage = this.add.image(stageSize.width / 2, stageSize.height / 2, initialTextureKey);
            runtime.backgroundImage.setVisible(false);
            runtime.backgroundImage.setDepth(0);
          } else {
            runtime.backgroundImage = null;
          }

          runtime.fallbackBackdrop = this.add.rectangle(
            stageSize.width / 2,
            stageSize.height / 2,
            stageSize.width,
            stageSize.height,
            0x162033,
            1
          );
          runtime.fallbackBackdrop.setDepth(1);

          runtime.fallbackLabel = this.add.text(
            stageSize.width / 2,
            stageSize.height / 2,
            i18n.t('map.play'),
            {
              fontFamily: 'sans-serif',
              fontSize: '26px',
              color: '#e2e8f0',
              align: 'center',
              wordWrap: { width: Math.max(280, stageSize.width - 140), useAdvancedWrap: true },
              lineSpacing: 10,
            }
          ).setOrigin(0.5);
          runtime.fallbackLabel.setDepth(2);

          statusProgress.textContent = `0 / ${replayData.events.length}`;
        },
      },
    };
  }

  function initializePlaceLabels() {
    runtime.placeLabels.clear();
    (replayData.places || []).forEach((place) => {
      const placeId = String(place.place_id || '');
      if (!placeId) {
        return;
      }
      runtime.placeLabels.set(placeId, String(place.label || placeId));
    });
    runtime.placeLabels.set(HOME_PLACE_ID, HOME_PLACE_ID);
    runtime.placeLabels.set(UNKNOWN_PLACE_ID, UNKNOWN_PLACE_ID);
  }

  function resizeGame() {
    if (!game || !runtime.scene) {
      return;
    }

    const size = getStageSize();
    game.scale.resize(size.width, size.height);

    if (runtime.mode === 'placeImage') {
      fitBackgroundToStage(runtime.scene);
      runtime.fallbackBackdrop.setSize(size.width, size.height);
      runtime.fallbackBackdrop.setPosition(size.width / 2, size.height / 2);
      runtime.fallbackLabel.setPosition(size.width / 2, size.height / 2);
    }
  }

  async function bootstrap() {
    initializeControls();

    try {
      replayData = await fetchReplayData();
      initializePlaceLabels();
      try {
        audioController = new ReplayAudioController({
          storyMapBaseUrl,
          defaultBgmUrl,
        });
        await audioController.initialize();
      } catch (bgmError) {
        console.warn('BGM disabled', bgmError);
        audioController = null;
        updateBgmButtonLabel();
      }

      if (!replayData.events.length) {
        showUnsupportedState(i18n.t('map.noLogs'));
        return;
      }

      const hasPlaceImages = await probePlaceSceneAvailability(replayData.places || []);
      if (hasPlaceImages) {
        runtime.mode = 'placeImage';
      } else {
        runtime.mode = 'storyMap';
        storyMap = await fetchStoryMap();
      }

      hideUnsupportedState();
      game = new Phaser.Game(
        runtime.mode === 'placeImage' ? buildPlaceImageSceneConfig() : buildStoryMapSceneConfig()
      );
      window.addEventListener('resize', resizeGame);
    } catch (error) {
      console.error('map replay bootstrap error', error);
      showUnsupportedState(createUnsupportedFallbackMessage(error));
    }
  }

  document.addEventListener('DOMContentLoaded', bootstrap);
})();
