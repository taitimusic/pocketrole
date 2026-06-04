"""web の公開/秘密境界に関する回帰テスト。"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def _read(relpath: str) -> str:
    return (ROOT / relpath).read_text(encoding="utf-8")


def _css_block(css: str, selector: str) -> str:
    pattern = re.compile(re.escape(selector) + r"\s*\{(?P<body>[^}]*)\}", re.MULTILINE)
    match = pattern.search(css)
    assert match, f"{selector} block not found"
    return match.group("body")


def test_viewer_does_not_embed_write_auth_token() -> None:
    """viewer.php は書き込み用トークンをクライアントへ埋め込まない。"""
    viewer = _read("web/viewer.php")

    assert 'authToken:' not in viewer
    assert 'htmlspecialchars(AUTH_TOKEN' not in viewer


def test_public_client_polls_latest_without_auth_token() -> None:
    """公開 viewer の latest 取得は auth_token なしで行う。"""
    app_js = _read("web/assets/app.js")

    assert "const { storyId, authToken, apiUrl, statusUrl }" not in app_js
    assert "&action=latest&auth_token=" not in app_js
    assert "&action=latest" in app_js


def test_api_requires_auth_only_for_non_latest_actions() -> None:
    """api.php は latest / replay だけを公開し、history / characters は保護する。"""
    api_php = _read("web/api.php")

    assert "if (($_GET['auth_token'] ?? '') !== AUTH_TOKEN)" not in api_php
    assert "load_auth_token" in api_php

    auth_guard = re.compile(
        r"if\s*\(\s*\$action\s*!==\s*'latest'\s*&&\s*\$action\s*!==\s*'replay'\s*\)"
    )
    assert auth_guard.search(api_php)


def test_viewer_exposes_jump_to_latest_control() -> None:
    """viewer は未追従時に最新へ戻る導線を持つ。"""
    viewer = _read("web/viewer.php")

    assert 'id="jump-latest"' in viewer
    assert '>最新へ<' in viewer


def test_viewer_exposes_public_image_base_url() -> None:
    """viewer は公開画像のベースパスだけをクライアントへ渡す。"""
    viewer = _read("web/viewer.php")

    assert 'imageBaseUrl:' in viewer
    assert '"assets/character_images"' in viewer
    assert 'AUTH_TOKEN' not in viewer


def test_viewer_versions_public_assets_to_avoid_stale_linkify_js() -> None:
    """viewer は古い app.js / style.css キャッシュを避ける version query を付ける。"""
    viewer = _read("web/viewer.php")

    assert "function asset_url" in viewer
    assert "filemtime" in viewer
    assert "asset_url('assets/style.css')" in viewer
    assert "asset_url('assets/app.js')" in viewer


def test_public_news_link_title_keeps_visible_chip_background() -> None:
    """時事リンクのタイトルは暗い viewer 背景に同化しない背景色を持つ。"""
    css = _read("web/assets/style.css")
    block = _css_block(css, ".bubble .news-link-title,\n.narration-text .news-link-title")

    assert "background:" in block
    assert "linear-gradient" in block
    assert "background-image: none" not in block
    assert "color: #111827" in block


def test_admin_viewer_links_keep_dark_theme_override_for_os_dark_mode() -> None:
    """dashboard viewer は OS dark mode 由来の dark theme でもリンク色を上書きする。"""
    css = _read("admin/assets/admin_style.css")

    assert '[data-theme="dark"] .viewer-chat-bubble a' in css
    assert '[data-theme="dark"] .viewer-chat-narrator-text a' in css
    assert "color: #fbbf24;" in css


def test_public_client_tracks_initial_scroll_and_reader_position() -> None:
    """公開 viewer は初回最下部着地と非追従時の導線表示を持つ。"""
    app_js = _read("web/assets/app.js")

    assert "let initialScrollDone = false;" in app_js
    assert "let pendingWhileDetached = 0;" in app_js
    assert "timeline.addEventListener('scroll', handleTimelineScroll);" in app_js
    assert "jumpLatestBtn.addEventListener('click'" in app_js
    assert "scrollToBottom({ force: true });" in app_js
    assert "setJumpLatestVisible(true);" in app_js


def test_public_client_uses_character_images_with_safe_fallbacks() -> None:
    """公開 viewer は表情画像→neutral画像→絵文字の順でフォールバックする。"""
    app_js = _read("web/assets/app.js")

    assert "imageBaseUrl" in app_js
    assert "new Image()" in app_js
    assert "neutral.png" in app_js
    assert "EXPR_EMOJI[expr]" in app_js
    assert "action=characters" not in app_js


def test_api_enriches_logs_with_public_char_name_metadata() -> None:
    """api.php は公開 metadata を使って char_name を補完する。"""
    api_php = _read("web/api.php")

    assert "story_metadata" in api_php
    assert "char_name" in api_php
    assert "char_id" in api_php


def test_api_replay_does_not_depend_on_published_catalog() -> None:
    """replay は viewer と同じ公開面に揃え、catalog 依存にしない。"""
    api_php = _read("web/api.php")

    assert "published/catalog.json" not in api_php
    assert "is_story_public" not in api_php
    assert "replay" in api_php


def test_api_replay_declares_frontend_ready_payload_sections() -> None:
    """replay は story / places / characters / events をまとめて返す。"""
    api_php = _read("web/api.php")

    assert "'action'" in api_php
    assert "'replay'" in api_php
    assert "'story'" in api_php
    assert "'places'" in api_php
    assert "'characters'" in api_php
    assert "'events'" in api_php


def test_api_replay_normalizes_narrator_name_and_avatar() -> None:
    """replay は _narrator を公開向けの表示名と共通 avatar に変換する。"""
    api_php = _read("web/api.php")

    assert "NARRATOR_CHAR_ID" in api_php
    assert "NARRATOR_DISPLAY_NAME" in api_php
    assert "ナレーション" in api_php
    assert "assets/character_images/_system/narration/neutral.png" in api_php
    assert "normalize_replay_character" in api_php


def test_public_client_prefers_char_name_but_keeps_char_id_for_images() -> None:
    """viewer は表示名に char_name を使い、画像 lookup は char_id を使う。"""
    app_js = _read("web/assets/app.js")

    assert "log.char_name || log.char_id" in app_js
    assert "applyAvatarImage(avatarEl, log.char_id, expr)" in app_js


def test_web_config_loads_auth_token_from_protected_config_file() -> None:
    """web/config.php は config/auth_token.txt から token を読む。"""
    config_php = _read("web/config.php")

    assert "config/auth_token.txt" in config_php
    assert "WEB_AUTH_TOKEN" not in config_php
    assert "changeme" not in config_php
    assert "function load_auth_token" in config_php


def test_receiver_and_protected_api_use_token_loader() -> None:
    """receiver と保護 API は token loader を使う。"""
    receiver_php = _read("web/receiver.php")
    api_php = _read("web/api.php")

    assert "load_auth_token" in receiver_php
    assert "load_auth_token" in api_php
    assert "AUTH_TOKEN" not in receiver_php


def test_receiver_declares_temporary_debug_log_without_logging_secrets() -> None:
    """receiver は config/ 配下へ一時 debug log を出し、token 本文は書かない。"""
    receiver_php = _read("web/receiver.php")

    assert "receiver_debug.log" in receiver_php
    assert "function receiver_debug_log" in receiver_php
    debug_lines = [line for line in receiver_php.splitlines() if "receiver_debug_log(" in line]
    assert debug_lines
    assert all("auth_token" not in line for line in debug_lines)


def test_receiver_wraps_write_path_with_error_capture() -> None:
    """receiver は directory/write 区間の warning / exception を捕捉して 500 へ落とす。"""
    receiver_php = _read("web/receiver.php")

    assert "set_error_handler" in receiver_php
    assert "restore_error_handler" in receiver_php
    assert "ensure_directory_with_index_html" in receiver_php
    assert "upsert_day_logs" in receiver_php
    assert ": never" not in receiver_php
    assert "receiver_debug_log('write_failed'" in receiver_php or 'receiver_debug_log("write_failed"' in receiver_php


def test_web_php_surface_avoids_php8_only_and_php74_only_shortcuts() -> None:
    """shared hosting 向けに PHP 8 専用関数と 7.4 専用 arrow function を避ける。"""
    api_php = _read("web/api.php")
    chatlog_php = _read("web/chatlog_lib.php")

    assert "str_starts_with(" not in chatlog_php
    assert "fn(" not in api_php
    assert "fn (" not in api_php


def test_config_directory_is_blocked_from_direct_access() -> None:
    """.htaccess で web/config/ への外部アクセスを拒否する。"""
    htaccess = _read("web/config/.htaccess")

    assert "Require all denied" in htaccess
    assert "Deny from all" in htaccess


def test_auth_token_example_exists_and_real_token_is_gitignored() -> None:
    """実 token は未追跡、example は同梱する。"""
    example = _read("web/config/auth_token.txt.example")
    gitignore = ROOT.parent.joinpath(".gitignore").read_text(encoding="utf-8")

    assert "replace-with-your-secret-token" in example
    assert "pocketrole/web/config/auth_token.txt" in gitignore


def test_root_gitignore_excludes_agent_working_directory() -> None:
    """エージェント作業用 sagyou/ は配布対象から外す。"""
    gitignore = ROOT.parent.joinpath(".gitignore").read_text(encoding="utf-8")

    assert "/sagyou/" in gitignore


def test_chatlog_datetime_parser_does_not_emit_php_warning_for_valid_iso() -> None:
    """valid ISO datetime の正規化で PHP warning を出さない。"""
    script = (
        'require "web/chatlog_lib.php"; '
        'echo normalize_sim_datetime("2026-04-08T04:30:00"), "\\n";'
    )

    result = subprocess.run(
        ["php", "-d", "display_errors=1", "-r", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == "2026-04-08T04:30\n"
    assert result.stderr == ""


def test_agent_guide_matches_current_public_web_configuration_shape() -> None:
    """公開 Web 手順は現行 config/auth_token.txt と receiver_url/auth_token を案内する。"""
    guide = ROOT.parent.joinpath("AGENT_GUIDE.md").read_text(encoding="utf-8")

    assert "web/config/auth_token.txt" in guide
    assert "receiver_url:" in guide
    assert "auth_token:" in guide
    assert "RECEIVER_TOKEN" not in guide
    assert "\n    url:" not in guide
    assert "\n    token:" not in guide
