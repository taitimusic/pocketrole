"""Static regression checks for the vanilla admin UI assets."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
ADMIN_APP_JS = ROOT / "admin" / "assets" / "admin_app.js"
ADMIN_STYLE_CSS = ROOT / "admin" / "assets" / "admin_style.css"
LAYOUT_CSS = ROOT / "admin" / "assets" / "layout.css"
MAP_REPLAY_JS = ROOT / "web" / "assets" / "map_replay.js"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_topbar_logo_text_has_dedicated_mobile_selector() -> None:
    app_js = _read(ADMIN_APP_JS)
    layout_css = _read(LAYOUT_CSS)

    assert "admin-topbar-logo-text" in app_js
    assert ".admin-topbar-logo-text { display: none; }" in layout_css
    assert ".admin-topbar-logo span:last-child" not in layout_css


def test_login_inputs_have_labels_and_autocomplete() -> None:
    app_js = _read(ADMIN_APP_JS)

    assert "username.autocomplete = 'username'" in app_js
    assert "password.autocomplete = 'current-password'" in app_js
    assert "element('label', 'ユーザー名')" in app_js
    assert "element('label', 'パスワード')" in app_js


def test_dirty_guard_sets_beforeunload_return_value() -> None:
    app_js = _read(ADMIN_APP_JS)

    assert "e.returnValue = ''" in app_js


def test_viewer_renders_follow_guidance_above_and_below_timeline() -> None:
    app_js = _read(ADMIN_APP_JS)

    assert "function buildViewerFollowBanner(placement)" in app_js
    assert "main.append(buildViewerFollowBanner('top'))" in app_js
    assert "timelinePanel.append(timeline, buildViewerFollowBanner('bottom'))" in app_js
    assert "viewer-follow-banner-bottom" in app_js
    assert "viewer-follow-inline-toggle" in app_js
    assert "document.querySelectorAll('.viewer-follow-banner')" in app_js
    assert "document.querySelectorAll('.viewer-follow-toggle, .viewer-follow-inline-toggle')" in app_js


def test_viewer_map_replay_tab_survives_polling_refreshes() -> None:
    app_js = _read(ADMIN_APP_JS)

    assert "let viewerActiveTab = 'timeline'" in app_js
    assert "viewerActiveTab = 'map'" in app_js
    assert "viewerActiveTab = 'timeline'" in app_js
    assert "if (viewerActiveTab === 'map' && viewerMapIframeStoryId === storyId)" in app_js
    assert "activateViewerTab(viewerActiveTab)" in app_js
    assert "viewerAutoFollow && viewerActiveTab !== 'map'" in app_js
    assert "function refreshViewerUnlessMapActive(storyId" in app_js
    assert "if (isViewerMapReplayActive())" in app_js
    assert "refreshViewerUnlessMapActive(storyId, { silent: true })" in app_js
    assert "refreshViewerUnlessMapActive(storyId)" in app_js
    assert "void renderViewer(storyId);" not in app_js
    assert "viewerAutoFollow && options.scroll && !isViewerMapReplayActive()" in app_js


def test_place_image_upload_uses_submit_time_target_place_id() -> None:
    app_js = _read(ADMIN_APP_JS)

    assert "const uploadPlaceId = placePayload[idx].new_place_id || placePayload[idx].place_id" in app_js
    assert "requestBody.append(`place_image__${uploadPlaceId}`, file)" in app_js
    assert "const uploadPlaceId = place.new_place_id || place.place_id" not in app_js


def test_news_mode_dropdown_exposes_high_frequency_presets() -> None:
    app_js = _read(ADMIN_APP_JS)

    assert "['rate20', 'かなり高め（20%）']" in app_js
    assert "['rate30', '高頻度（30%）']" in app_js
    assert "['rate40', '超高頻度（40%）']" in app_js
    assert "['rate50', 'ほぼ毎回狙う（50%）']" in app_js


def test_web_post_token_ui_uses_write_only_token_contract() -> None:
    app_js = _read(ADMIN_APP_JS)

    assert "has_auth_token" in app_js
    assert "clear_auth_token" in app_js
    assert "wpData.auth_token" not in app_js
    assert "swpData.auth_token" not in app_js


def test_director_persona_management_owns_activation_controls() -> None:
    app_js = _read(ADMIN_APP_JS)

    assert "監督管理:" in app_js
    assert "現在の監督" in app_js
    assert "この監督に切り替え" in app_js
    assert "最近の監督交代履歴" in app_js
    assert "切替理由" in app_js
    assert "監督管理へ" in app_js
    assert "function activateDirectorPersona" not in app_js
    assert "章管理画面で扱います" not in app_js


def test_admin_redesign_keeps_editors_keyboard_accessible_and_state_safe() -> None:
    app_js = _read(ADMIN_APP_JS)
    admin_css = _read(ADMIN_STYLE_CSS)

    assert "toggleBtn.type = 'button'" in app_js
    assert "toggleBtn.setAttribute('aria-expanded'" in app_js
    assert "toggleBtn.setAttribute('aria-controls'" in app_js
    assert "header.addEventListener('click'" not in app_js

    assert "option.value = persona._cardId" in app_js
    assert "resolveDefaultActivePersonaId()" in app_js
    assert "default_active: resolveDefaultActivePersonaId()" in app_js

    assert "const obj = { ...aesthetic }" in app_js

    assert "function renderFlowOverview()" in app_js
    assert "renderFlowOverview();" in app_js

    assert ".aesthetic-track input[type=\"range\"]:focus-visible" in admin_css
    assert ".beat-phase-select:focus-visible" in admin_css
    assert "@media (max-width: 700px)" in admin_css
    assert ".persona-fields-row" in admin_css
    assert "grid-template-columns: 1fr;" in admin_css


def test_scene_script_list_exposes_structured_and_legacy_views() -> None:
    app_js = _read(ADMIN_APP_JS)
    layout_css = _read(LAYOUT_CSS)

    assert "details.open = index === 0" in app_js
    assert "シーンスクリプトを更新" in app_js
    assert "DBに保存済み scene script はありません" in app_js
    assert "scene-script-structured" in app_js
    assert "scene-script-character-card" in app_js
    assert "scene-script-raw-json" in app_js
    assert "旧形式または生成失敗由来" in app_js
    assert "white-space: pre-wrap;" in layout_css
    assert "overflow-wrap: anywhere;" in layout_css


def test_story_bgm_card_and_place_bgm_field() -> None:
    app_js = _read(ADMIN_APP_JS)
    admin_css = _read(ADMIN_STYLE_CSS)

    # ストーリー BGM カード
    assert "ストーリー BGM" in app_js
    assert "story-bgm-status" in app_js
    assert "has_story_bgm" in app_js
    assert "カスタム BGM 設定済み" in app_js
    assert "グローバルデフォルト使用中" in app_js
    assert "function resolveStoryBgmPreviewUrl(" in app_js
    assert "bgmData.story_default" in app_js
    assert "/story_bgm.mp3" not in app_js
    assert "bgm-config" in app_js
    assert "カスタム BGM を削除" in app_js
    assert "BGM を有効にする" in app_js
    assert "bgmSection" in app_js
    assert "bgmSection, storyWebPostSection" in app_js

    # 場所固有 BGM フィールド
    assert "function renderPlaceBgmField(" in app_js
    assert "place-bgm-section" in app_js
    assert "place_bgm:" in app_js
    assert "place_bgm__" in app_js
    assert "hasBgmUploads" in app_js
    assert "_bgmOverrides" in app_js
    assert "function resolvePlaceBgmPreviewUrl(" in app_js
    assert "placeOverrides[placeId]" in app_js
    assert "/place_bgm/${placeId}.mp3" not in app_js
    assert "bgmBody.append('config', JSON.stringify({ enabled: true }))" not in app_js
    assert "BGM の保存に失敗しました" in app_js

    # CSS
    assert ".story-bgm-status" in admin_css
    assert ".story-bgm-status.has-file" in admin_css
    assert ".story-bgm-status.no-file" in admin_css
    assert ".story-bgm-preview" in admin_css
    assert ".story-bgm-clear-btn" in admin_css
    assert ".place-bgm-section" in admin_css
    assert ".place-bgm-preview" in admin_css
    assert ".place-bgm-empty" in admin_css
    assert ".place-bgm-file-input" in admin_css


def test_map_replay_bgm_enabled_in_admin_and_place_switch_hook() -> None:
    app_js = _read(ADMIN_APP_JS)
    map_replay_js = _read(MAP_REPLAY_JS)

    # BGM ヒントに pocketrole_bgm.mp3 の説明があること
    assert "pocketrole_bgm.mp3" in app_js

    # map_replay.js: this.manifest を initialize() で保存すること
    assert "this.manifest = await this.fetchManifest()" in map_replay_js
    assert "this.manifest = null" in map_replay_js

    # map_replay.js: resolveTrackUrl が place_overrides を参照すること
    assert "resolveTrackUrl(manifest, placeId = null)" in map_replay_js
    assert "manifest.place_overrides" in map_replay_js

    # map_replay.js: switchToPlace メソッドが存在すること
    assert "async switchToPlace(placeId)" in map_replay_js
    assert "this.resolveTrackUrl(this.manifest, placeId)" in map_replay_js

    # map_replay.js: BGM 切替は placeImage 専用処理ではなく共通 event 経路で呼ぶこと
    assert "function switchBgmForPlace(placeId)" in map_replay_js
    assert "switchBgmForPlace(event.place_id)" in map_replay_js
    assert "audioController.switchToPlace(resolvedPlaceId)" not in map_replay_js
