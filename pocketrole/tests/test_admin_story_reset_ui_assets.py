"""Static checks for story progress reset controls in the admin UI."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
ADMIN_APP_JS = ROOT / "admin" / "assets" / "admin_app.js"


def test_story_detail_has_progress_reset_confirmation_only_on_detail_view() -> None:
    app_js = ADMIN_APP_JS.read_text(encoding="utf-8")

    assert "ストーリー進行を初期化" in app_js
    assert "この操作は元に戻せません" in app_js
    assert "シーンスクリプト" in app_js
    assert "/runtime/reset" in app_js
    assert "JSON.stringify({ confirm: true })" in app_js
    assert "リモート公開ログ" in app_js
    assert "controls.append(start, stop, restart, refresh, followToggle)" in app_js


def test_scene_script_view_uses_manual_refresh_without_auto_reload() -> None:
    app_js = ADMIN_APP_JS.read_text(encoding="utf-8")

    assert "シーンスクリプトを更新" in app_js
    assert "refreshSceneScriptList(storyId, listSection, listTitle, refreshBtn)" in app_js
    assert "sceneScriptRefreshHandle" not in app_js
    assert "cancelSceneScriptRefresh()" not in app_js
