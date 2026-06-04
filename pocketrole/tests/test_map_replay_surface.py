"""Phaser ベースの map replay 公開 surface 回帰テスト。"""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
NARRATION_AVATAR = ROOT / "web" / "assets" / "character_images" / "_system" / "narration" / "neutral.png"


def _read(relpath: str) -> str:
    return (ROOT / relpath).read_text(encoding="utf-8")


def test_map_replay_page_exists_and_loads_local_assets() -> None:
    page = _read("web/map_replay.php")

    assert "story_id is required" in page
    assert "assets/map_replay.css" in page
    assert "assets/phaser.min.js" in page
    assert "assets/map_replay.js" in page


def test_map_replay_page_exposes_public_runtime_config() -> None:
    page = _read("web/map_replay.php")

    assert "window.POCKETROLE_MAP_REPLAY" in page
    assert 'apiUrl: "api.php"' in page
    assert 'replayPageUrl: "map_replay.php"' in page
    assert 'storyMapBaseUrl: "assets/story_maps"' in page
    assert 'defaultBgmUrl: "assets/audio/pocketrole_bgm.mp3"' in page
    assert "placeSceneImageBaseUrl" not in page


def test_viewer_links_to_map_replay_page() -> None:
    viewer = _read("web/viewer.php")

    assert "map_replay.php?story_id=" in viewer
    assert "マップ再生を見る" in viewer
    assert "replay-cta" in viewer


def test_map_replay_client_fetches_replay_api() -> None:
    app_js = _read("web/assets/map_replay.js")

    assert "action=replay" in app_js
    assert "window.POCKETROLE_MAP_REPLAY" in app_js
    assert "fetch(" in app_js
    assert "assets/story_maps/images" not in app_js
    assert "images/" in app_js
    assert "storyId" in app_js
    assert "_320.png" in app_js
    assert "bgm_manifest.json" in app_js
    assert "defaultBgmUrl" in app_js


def test_map_replay_client_declares_playback_controls_and_fallbacks() -> None:
    app_js = _read("web/assets/map_replay.js")

    assert "play-toggle" in app_js
    assert "next-event" in app_js
    assert "reset-replay" in app_js
    assert "speed-select" in app_js
    assert "bgm-toggle" in app_js
    assert "bgm-volume" in app_js
    assert "unknown" in app_js
    assert "avatar_url" in app_js
    assert "再生可能なログがありません" in app_js
    assert "このストーリーの再生はまだ公開されていません" in app_js
    assert "home" in app_js
    assert "fallback" in app_js or "Fallback" in app_js
    assert "Focus" in app_js
    assert "Move" in app_js
    assert "Settle" in app_js
    assert "Speak" in app_js
    assert "Advance" in app_js
    assert "speaker row" in app_js or "support row" in app_js or "buildStageLayout" in app_js
    assert "MAX_SUPPORT_CHARACTERS" in app_js
    assert "recentSupportIds" in app_js or "supportCandidates" in app_js
    assert "ACTIVE_AVATAR_MAX_SIZE" in app_js
    assert "SUPPORT_AVATAR_MAX_SIZE" in app_js
    assert "getBubbleAnchorY" in app_js
    assert "displayHeight" in app_js
    assert "ReplayAudioController" in app_js
    assert ".loop = true" in app_js or "loop = true" in app_js
    assert "function resolveBgmAssetPath(relativePath)" in app_js
    assert "BGM disabled" in app_js
    assert "placeId !== UNKNOWN_PLACE_ID && placeId !== HOME_PLACE_ID" not in app_js
    assert "resolvedPlaceId === HOME_PLACE_ID || !hasTexture" not in app_js
    assert "home は共通 fallback 背景で再生します" not in app_js


def test_map_replay_client_normalizes_narrator_identity() -> None:
    app_js = _read("web/assets/map_replay.js")

    assert "const NARRATOR_CHAR_ID = '_narrator';" in app_js
    assert "const NARRATOR_DISPLAY_NAME = 'ナレーション';" in app_js
    assert "const NARRATOR_AVATAR_URL = 'assets/character_images/_system/narration/neutral.png';" in app_js
    assert "function normalizeReplayCharacter(character)" in app_js
    assert "function normalizeReplayEvent(event)" in app_js
    assert "normalizeReplayData(data)" in app_js


def test_map_replay_has_shared_narration_avatar_asset() -> None:
    assert NARRATION_AVATAR.is_file()


def test_map_replay_speed_controls_shift_default_baseline_slower() -> None:
    page = _read("web/map_replay.php")
    app_js = _read("web/assets/map_replay.js")

    assert 'value="0.5"' in page
    assert ">0.5x<" in page
    assert 'value="1" selected' in page
    assert ">1x<" in page
    assert 'value="2"' in page
    assert ">2x<" in page
    assert 'value="4"' in page
    assert ">4x<" in page
    assert 'value="8"' not in page
    assert ">8x<" not in page

    assert "new Set(['0.5', '1', '2', '4'])" in app_js
    assert "getEffectiveSpeedMultiplier" in app_js
    assert "getSpeedMultiplier() / 2" in app_js


def test_map_replay_bgm_switches_in_common_event_path() -> None:
    app_js = _read("web/assets/map_replay.js")

    assert "function switchBgmForPlace(placeId)" in app_js
    assert "switchBgmForPlace(event.place_id)" in app_js
    assert "audioController.switchToPlace(resolvedPlaceId)" not in app_js
    assert "function resolveBgmAssetPath(relativePath)" in app_js
    assert "parts.includes('..')" in app_js
    assert "raw.includes('\\\\')" in app_js


def test_map_replay_stop_button_cancels_in_flight_playback() -> None:
    app_js = _read("web/assets/map_replay.js")

    assert "class PlaybackCancelledError extends Error" in app_js
    assert "function cancelPlaybackWait()" in app_js
    assert "function playbackDelay(ms)" in app_js
    assert "function isPlaybackCancelled(error)" in app_js
    assert "if (isPlaying) {\n        stopPlayback();\n        return;\n      }" in app_js
    assert "cancelPlaybackWait();" in app_js
    assert "clearBubble();" in app_js
    assert "if (!isPlaying) {\n      return;\n    }" in app_js
    assert "if (!isPlaybackCancelled(error))" in app_js


def test_map_replay_styles_define_fullscreen_map_and_minimal_hud() -> None:
    css = _read("web/assets/map_replay.css")
    viewer_css = _read("web/assets/style.css")

    assert "#map-stage" in css
    assert ".replay-controls" in css
    assert ".minimal-hud" in css
    assert ".unsupported-state" in css
    assert ".bgm-volume" in css
    assert ".replay-cta" in viewer_css
