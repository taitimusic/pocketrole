"""UI i18n surface regression tests."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def _read(relpath: str) -> str:
    return (ROOT / relpath).read_text(encoding="utf-8")


def test_public_i18n_php_resolves_supported_locales_and_zh_tw_aliases() -> None:
    helper = _read("web/i18n.php")

    assert "function pocketrole_resolve_locale" in helper
    assert "zh-TW" in helper
    assert "zh-Hant" in helper
    assert "zh-HK" in helper
    assert "zh-MO" in helper
    assert "return 'ja';" in helper


def test_public_pages_expose_locale_to_shell_and_client_runtime() -> None:
    for relpath in ["web/viewer.php", "web/map_replay.php", "web/archive_index.php", "web/archive.php"]:
        page = _read(relpath)
        assert "require_once __DIR__ . '/i18n.php';" in page
        assert "$locale = pocketrole_resolve_locale" in page
        assert '<html lang="<?= htmlspecialchars($locale, ENT_QUOTES) ?>">' in page
        assert "locale:" in page
        assert "assets/i18n.js" in page


def test_public_js_catalog_has_ja_en_and_zh_tw_without_translating_story_content() -> None:
    catalog = _read("web/assets/i18n.js")

    assert "const PUBLIC_MESSAGES" in catalog
    assert "ja:" in catalog
    assert "en:" in catalog
    assert "'zh-TW':" in catalog
    assert "function resolveLocale" in catalog
    assert "function t" in catalog
    assert "window.PocketRoleI18n" in catalog

    app_js = _read("web/assets/app.js")
    assert "const i18n = window.PocketRoleI18n" in app_js
    assert "i18n.t('viewer.jumpLatest" in app_js
    assert "i18n.formatTime" in app_js
    assert "renderMessageHtml(esc(log.message))" in app_js


def test_map_replay_uses_translated_labels_but_keeps_replay_payload_values() -> None:
    app_js = _read("web/assets/map_replay.js")

    assert "const i18n = window.PocketRoleI18n" in app_js
    assert "i18n.t('map.play')" in app_js
    assert "i18n.t('map.pause')" in app_js
    assert "i18n.t('map.unsupportedStoryMap')" in app_js
    assert "event.place_id" in app_js
    assert "event.message" in app_js


def test_admin_shell_loads_i18n_runtime_and_admin_app_uses_translated_enum_labels() -> None:
    app_py = _read("admin/app.py")
    admin_i18n = _read("admin/assets/i18n.js")
    admin_app = _read("admin/assets/admin_app.js")

    assert "assets/i18n.js" in app_py
    assert "locale:" in app_py
    assert "const ADMIN_MESSAGES" in admin_i18n
    assert "'zh-TW':" in admin_i18n
    assert "window.PocketRoleAdminI18n" in admin_i18n
    assert "const adminI18n = window.PocketRoleAdminI18n" in admin_app
    assert "adminI18n.t('beat.setup')" in admin_app
    assert "setup:" in admin_app


def test_public_translation_catalog_contains_required_core_keys() -> None:
    catalog = _read("web/assets/i18n.messages.json")
    payload = json.loads(catalog)

    for locale in ["ja", "en", "zh-TW"]:
        messages = payload[locale]
        for key in [
            "viewer.mapReplay",
            "viewer.jumpLatest",
            "viewer.connecting",
            "map.play",
            "map.pause",
            "map.next",
            "map.reset",
            "archive.indexTitle",
            "archive.empty",
        ]:
            assert key in messages
            assert messages[key]
