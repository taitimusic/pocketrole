"""tests/test_admin_service.py — aiohttp 管理サービスのテスト。"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from aiohttp import FormData, web
from aiohttp.test_utils import TestClient, TestServer
import pytest
import yaml

from db.db_manager import DatabaseManager

from admin.app import create_app
from admin.app import NEWS_STORE_KEY
from tools.import_story import import_story
from engine.config import NewsModeConfig, StoryWebPostTargetConfig
from tests.test_validate_story import MINIMAL_CHARACTERS, MINIMAL_WORLD_CONFIG


MIGRATIONS_DIR = Path(__file__).parent.parent / "db" / "migrations"
STORY_ID = "admin_story"


class FakeRuntimeController:
    def __init__(self) -> None:
        self.states = {STORY_ID: "stopped"}
        self.director_reload_story_ids: list[str] = []
        self.applied_llm_runtime = None

    def list_states(self) -> dict[str, str]:
        return dict(self.states)

    async def set_story_state(self, story_id: str, desired_state: str) -> dict[str, str]:
        self.states[story_id] = desired_state
        return {"story_id": story_id, "desired_state": desired_state}

    async def restart_story(self, story_id: str) -> dict[str, str]:
        self.states[story_id] = "running"
        return {"story_id": story_id, "desired_state": "running"}

    def register_story(self, story_id: str) -> None:
        self.states.setdefault(story_id, "stopped")

    async def reload_director_persona(self, story_id: str) -> bool:
        self.director_reload_story_ids.append(story_id)
        return self.states.get(story_id) == "running"

    def apply_llm_runtime_config(self, llm_runtime) -> None:
        self.applied_llm_runtime = llm_runtime


async def _create_admin_db_with_token() -> tuple[DatabaseManager, str]:
    db = DatabaseManager(":memory:", migrations_dir=MIGRATIONS_DIR)
    await db.initialize()
    assert db._conn is not None
    await db._conn.execute(
        """
        INSERT INTO stories (id, title, llm_provider, llm_model)
        VALUES (?, ?, ?, ?)
        """,
        (STORY_ID, "管理UIテスト", "openai", "gpt-4o"),
    )
    await db._conn.commit()
    await db.ensure_admin_user("admin", "secret-pass")
    token = await db.create_admin_api_token("default-agent")
    return db, token


def _write_template_story_bundle(stories_root: Path, story_id: str) -> None:
    story_dir = stories_root / story_id
    story_dir.mkdir(parents=True, exist_ok=True)

    world = copy.deepcopy(MINIMAL_WORLD_CONFIG)
    world["story"]["id"] = story_id
    world["story"]["title"] = "テンプレート"
    world["story"]["description"] = "テンプレート説明"
    (story_dir / "world_config.yaml").write_text(
        yaml.safe_dump(world, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    chars = copy.deepcopy(MINIMAL_CHARACTERS)
    chars["characters"][0]["story_id"] = story_id
    chars["characters"][0]["name"] = "テンプレキャラ"
    chars["characters"][0]["personality"] = {"type": "静かな主人公"}
    chars["characters"][0]["goal"] = "毎日を無事に終える"
    chars["characters"][0]["worry"] = "少し緊張しやすい"
    chars["characters"][0]["expressions"] = ["neutral", "happy"]
    (story_dir / "characters.yaml").write_text(
        yaml.safe_dump(chars, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    (story_dir / "chapters.yaml").write_text(
        yaml.safe_dump(
            {
                "chapters": [
                    {
                        "id": "ch001",
                        "title": "導入",
                        "beats": [{"phase": "setup", "description": "開始", "goal": "顔合わせ", "events": []}],
                    }
                ]
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (story_dir / "director.yaml").write_text(
        yaml.safe_dump(
            {
                "default_active": "default_director",
                "personas": {
                    "default_director": {
                        "name": "標準監督",
                        "aesthetic": {"tension_preference": 0.5},
                        "values": ["誠実"],
                        "traits": ["観察型"],
                    }
                },
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    image_sources = story_dir / "image_sources"
    image_sources.mkdir(exist_ok=True)
    (image_sources / "test_char.png").write_bytes(b"template-source")


def _write_template_assets(
    character_images_root: Path,
    story_maps_root: Path,
    story_metadata_root: Path,
    story_id: str,
) -> None:
    char_dir = character_images_root / story_id / "test_char"
    char_dir.mkdir(parents=True, exist_ok=True)
    (char_dir / "neutral.png").write_bytes(b"template-neutral")
    (char_dir / "happy.png").write_bytes(b"template-happy")

    map_dir = story_maps_root / story_id
    (map_dir / "images").mkdir(parents=True, exist_ok=True)
    (map_dir / "bgm").mkdir(parents=True, exist_ok=True)
    (map_dir / "images" / "classroom_320.png").write_bytes(b"place-image")
    (map_dir / "bgm" / "theme.ogg").write_bytes(b"bgm")
    (map_dir / "map.json").write_text("{}", encoding="utf-8")
    (map_dir / "background.svg").write_text("<svg></svg>", encoding="utf-8")
    story_metadata_root.mkdir(parents=True, exist_ok=True)
    (story_metadata_root / f"{story_id}.json").write_text(
        json.dumps({"story_id": story_id}, ensure_ascii=False),
        encoding="utf-8",
    )


async def _start_imported_story_admin_client(
    tmp_path: Path,
    *,
    max_bgm_upload_bytes: int | None = None,
    max_image_upload_bytes: int | None = None,
) -> tuple[TestClient, DatabaseManager, str, Path, Path, Path, Path]:
    db_path = tmp_path / "admin.db"
    db = DatabaseManager(db_path, migrations_dir=MIGRATIONS_DIR)
    await db.initialize()
    token = await db.create_admin_api_token("default-agent")

    stories_root = tmp_path / "stories"
    character_images_root = tmp_path / "character_images"
    story_maps_root = tmp_path / "story_maps"
    story_metadata_root = tmp_path / "story_metadata"
    _write_template_story_bundle(stories_root, STORY_ID)
    _write_template_assets(character_images_root, story_maps_root, story_metadata_root, STORY_ID)
    await import_story(
        stories_root / STORY_ID,
        db_path=db.db_path,
        force_replace=False,
        story_maps_root=story_maps_root,
    )

    app_kwargs: dict[str, object] = {}
    if max_bgm_upload_bytes is not None:
        app_kwargs["max_bgm_upload_bytes"] = max_bgm_upload_bytes
    if max_image_upload_bytes is not None:
        app_kwargs["max_image_upload_bytes"] = max_image_upload_bytes

    app = await create_app(
        db=db,
        runtime_controller=FakeRuntimeController(),
        archive_root=tmp_path,
        admin_base_url="/admin",
        stories_root=stories_root,
        character_images_root=character_images_root,
        story_maps_root=story_maps_root,
        story_metadata_root=story_metadata_root,
        **app_kwargs,
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    return client, db, token, stories_root, character_images_root, story_maps_root, story_metadata_root


@pytest.mark.asyncio
async def test_admin_login_sets_session_cookie(tmp_path: Path) -> None:
    db = DatabaseManager(":memory:", migrations_dir=MIGRATIONS_DIR)
    await db.initialize()
    assert db._conn is not None
    await db._conn.execute(
        """
        INSERT INTO stories (id, title, llm_provider, llm_model)
        VALUES (?, ?, ?, ?)
        """,
        (STORY_ID, "管理UIテスト", "openai", "gpt-4o"),
    )
    await db._conn.commit()
    await db.ensure_admin_user("admin", "secret-pass")

    app = await create_app(
        db=db,
        runtime_controller=FakeRuntimeController(),
        archive_root=tmp_path,
        admin_base_url="/admin",
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.post(
            "/admin/api/v1/session/login",
            json={"username": "admin", "password": "secret-pass"},
        )
        assert response.status == 200
        assert "admin_session" in response.cookies
        assert response.cookies["admin_session"]["httponly"]
        assert response.cookies["admin_session"]["secure"]
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_admin_bearer_token_can_list_stories(tmp_path: Path) -> None:
    db = DatabaseManager(":memory:", migrations_dir=MIGRATIONS_DIR)
    await db.initialize()
    assert db._conn is not None
    await db._conn.execute(
        """
        INSERT INTO stories (id, title, llm_provider, llm_model)
        VALUES (?, ?, ?, ?)
        """,
        (STORY_ID, "管理UIテスト", "openai", "gpt-4o"),
    )
    await db._conn.commit()
    await db.ensure_admin_user("admin", "secret-pass")
    token = await db.create_admin_api_token("default-agent")

    app = await create_app(
        db=db,
        runtime_controller=FakeRuntimeController(),
        archive_root=tmp_path,
        admin_base_url="/admin",
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.get(
            "/admin/api/v1/stories",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status == 200
        payload = await response.json()
        assert payload["data"]["stories"][0]["story_id"] == STORY_ID
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_audit_logs_reject_invalid_pagination_params(tmp_path: Path) -> None:
    db = DatabaseManager(":memory:", migrations_dir=MIGRATIONS_DIR)
    await db.initialize()
    assert db._conn is not None
    await db._conn.execute(
        """
        INSERT INTO stories (id, title, llm_provider, llm_model)
        VALUES (?, ?, ?, ?)
        """,
        (STORY_ID, "管理UIテスト", "openai", "gpt-4o"),
    )
    await db._conn.commit()
    await db.ensure_admin_user("admin", "secret-pass")
    token = await db.create_admin_api_token("default-agent")

    app = await create_app(
        db=db,
        runtime_controller=FakeRuntimeController(),
        archive_root=tmp_path,
        admin_base_url="/admin",
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        headers = {"Authorization": f"Bearer {token}"}
        for query in ("limit=abc", "offset=abc", "limit=-1", "offset=-1"):
            response = await client.get(f"/admin/api/v1/audit-logs?{query}", headers=headers)
            assert response.status == 400
            payload = await response.json()
            assert payload["status"] == "error"

        response = await client.get(
            "/admin/api/v1/audit-logs?limit=1&offset=0",
            headers=headers,
        )
        assert response.status == 200
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_news_feed_api_rejects_unsafe_urls_and_missing_feed(tmp_path: Path) -> None:
    db, token = await _create_admin_db_with_token()
    app = await create_app(
        db=db,
        runtime_controller=FakeRuntimeController(),
        archive_root=tmp_path,
        admin_base_url="/admin",
        news_store_path=tmp_path / "news.db",
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        headers = {"Authorization": f"Bearer {token}"}
        for url in ("http://127.0.0.1/rss", "https://user@example.com/rss"):
            response = await client.post(
                "/admin/api/v1/news/feeds",
                headers=headers,
                json={"url": url},
            )
            assert response.status == 400

        missing_response = await client.put(
            "/admin/api/v1/news/feeds/999",
            headers=headers,
            json={"title": "missing"},
        )
        assert missing_response.status == 404
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_story_news_mode_accepts_high_frequency_intensity(tmp_path: Path) -> None:
    db, token = await _create_admin_db_with_token()
    app = await create_app(
        db=db,
        runtime_controller=FakeRuntimeController(),
        archive_root=tmp_path,
        admin_base_url="/admin",
        news_store_path=tmp_path / "news.db",
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        headers = {"Authorization": f"Bearer {token}"}
        response = await client.put(
            f"/admin/api/v1/stories/{STORY_ID}/news-mode",
            headers=headers,
            json={"enabled": True, "intensity": "rate50"},
        )

        assert response.status == 200
        payload = await response.json()
        assert payload["data"]["intensity"] == "rate50"

        invalid = await client.put(
            f"/admin/api/v1/stories/{STORY_ID}/news-mode",
            headers=headers,
            json={"enabled": True, "intensity": "rate99"},
        )
        assert invalid.status == 400
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_news_fetch_now_uses_injected_news_mode_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db, token = await _create_admin_db_with_token()
    config = NewsModeConfig(max_feed_bytes=123, max_entries_per_feed=4)
    seen: dict[str, object] = {}

    class FakeFetcher:
        def __init__(self, store: object, fetch_config: NewsModeConfig) -> None:
            seen["store"] = store
            seen["config"] = fetch_config

        async def fetch_all_now(self) -> int:
            return 7

    monkeypatch.setattr("engine.news_fetcher.NewsFetcher", FakeFetcher)
    app = await create_app(
        db=db,
        runtime_controller=FakeRuntimeController(),
        archive_root=tmp_path,
        admin_base_url="/admin",
        news_store_path=tmp_path / "news.db",
        news_mode_config=config,
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.post(
            "/admin/api/v1/news/fetch-now",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status == 200
        payload = await response.json()
        assert payload["data"]["inserted"] == 7
        assert seen["config"] is config
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_admin_cleanup_closes_news_store(tmp_path: Path) -> None:
    db, token = await _create_admin_db_with_token()
    app = await create_app(
        db=db,
        runtime_controller=FakeRuntimeController(),
        archive_root=tmp_path,
        admin_base_url="/admin",
        news_store_path=tmp_path / "news.db",
    )
    store = app[NEWS_STORE_KEY]
    assert store._conn is not None
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.get(
            "/admin/api/v1/news/feeds",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status == 200
    finally:
        await client.close()
        await db.close()
    assert store._conn is None


@pytest.mark.asyncio
async def test_conversation_patterns_api_returns_defaults_and_updates_settings(
    tmp_path: Path,
) -> None:
    db = DatabaseManager(":memory:", migrations_dir=MIGRATIONS_DIR)
    await db.initialize()
    assert db._conn is not None
    await db._conn.execute(
        """
        INSERT INTO stories (id, title, llm_provider, llm_model)
        VALUES (?, ?, ?, ?)
        """,
        (STORY_ID, "管理UIテスト", "openai", "gpt-4o"),
    )
    await db._conn.commit()
    await db.ensure_admin_user("admin", "secret-pass")
    token = await db.create_admin_api_token("default-agent")

    app = await create_app(
        db=db,
        runtime_controller=FakeRuntimeController(),
        archive_root=tmp_path,
        admin_base_url="/admin",
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        headers = {"Authorization": f"Bearer {token}"}
        response = await client.get(
            f"/admin/api/v1/stories/{STORY_ID}/conversation-patterns",
            headers=headers,
        )
        assert response.status == 200
        payload = await response.json()
        assert payload["data"]["patterns"][0]["motif_id"] == "solo_seed_rondo"
        assert payload["data"]["patterns"][0]["enabled"] is False

        update_response = await client.put(
            f"/admin/api/v1/stories/{STORY_ID}/conversation-patterns",
            headers=headers,
            json={
                "patterns": [
                    {
                        "motif_id": "solo_seed_rondo",
                        "enabled": True,
                        "strength": "strong",
                        "cooldown_turns": 3,
                    }
                ]
            },
        )
        assert update_response.status == 200
        updated = await update_response.json()
        assert updated["data"]["patterns"][0]["enabled"] is True
        assert updated["data"]["patterns"][0]["strength"] == "strong"
        assert updated["data"]["patterns"][0]["cooldown_turns"] == 3
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_scene_scripts_api_get_and_delete(tmp_path: Path) -> None:
    """GET /scene-scripts と DELETE /scene-scripts/from-turn/{turn} が正常動作する。"""
    db = DatabaseManager(":memory:", migrations_dir=MIGRATIONS_DIR)
    await db.initialize()
    assert db._conn is not None
    await db._conn.execute(
        "INSERT INTO stories (id, title, llm_provider, llm_model) VALUES (?, ?, ?, ?);",
        (STORY_ID, "シーンスクリプトテスト", "openai", "gpt-4o"),
    )
    await db._conn.commit()
    await db.insert_scene_script(
        STORY_ID,
        {
            "turn_number": 1,
            "script_text": "ターン1の場面。",
            "generation_metadata": {
                "director_payload": {
                    "scene_frame": "廊下で紙片が揺れる。",
                    "turn_goal": "紙片に注意を集める。",
                    "turn_shift": "ルナが一歩遅れる。",
                    "characters": {
                        "hoshikaze_runa": {
                            "name": "星風ルナ",
                            "role": "秘密を守る側",
                            "next_move": "紙片から視線を外す",
                            "speech_task": "理由をぼかす",
                            "actable_behavior": "紙片を隠す",
                            "target_char_id": "chururun",
                            "avoid": ["直接命令"],
                        }
                    },
                }
            },
        },
    )
    for turn in [2, 3, 5]:
        await db.insert_scene_script(STORY_ID, {"turn_number": turn, "script_text": f"ターン{turn}の場面。"})
    await db.ensure_admin_user("admin", "secret-pass")
    token = await db.create_admin_api_token("default-agent")

    app = await create_app(
        db=db,
        runtime_controller=FakeRuntimeController(),
        archive_root=tmp_path,
        admin_base_url="/admin",
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        headers = {"Authorization": f"Bearer {token}"}

        # GET: 全件取得
        resp = await client.get(f"/admin/api/v1/stories/{STORY_ID}/scene-scripts", headers=headers)
        assert resp.status == 200
        payload = await resp.json()
        assert len(payload["data"]["scripts"]) == 4
        script_by_turn = {
            script["turn_number"]: script for script in payload["data"]["scripts"]
        }
        director_payload = script_by_turn[1]["generation_metadata"]["director_payload"]
        assert isinstance(director_payload, dict)
        assert director_payload["characters"]["hoshikaze_runa"]["speech_task"] == "理由をぼかす"

        # DELETE: ターン 3 以降を削除
        del_resp = await client.delete(
            f"/admin/api/v1/stories/{STORY_ID}/scene-scripts/from-turn/3",
            headers=headers,
        )
        assert del_resp.status == 200
        del_payload = await del_resp.json()
        assert del_payload["data"]["deleted"] == 2  # ターン 3, 5

        # 残り確認
        resp2 = await client.get(f"/admin/api/v1/stories/{STORY_ID}/scene-scripts", headers=headers)
        assert resp2.status == 200
        payload2 = await resp2.json()
        assert len(payload2["data"]["scripts"]) == 2
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_publication_and_runtime_mutations_are_audited(tmp_path: Path) -> None:
    db = DatabaseManager(":memory:", migrations_dir=MIGRATIONS_DIR)
    await db.initialize()
    assert db._conn is not None
    await db._conn.execute(
        """
        INSERT INTO stories (id, title, llm_provider, llm_model)
        VALUES (?, ?, ?, ?)
        """,
        (STORY_ID, "管理UIテスト", "openai", "gpt-4o"),
    )
    await db._conn.commit()
    await db.ensure_admin_user("admin", "secret-pass")
    token = await db.create_admin_api_token("default-agent")

    app = await create_app(
        db=db,
        runtime_controller=FakeRuntimeController(),
        archive_root=tmp_path,
        admin_base_url="/admin",
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        runtime_response = await client.put(
            f"/admin/api/v1/stories/{STORY_ID}/runtime",
            json={"desired_state": "running"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert runtime_response.status == 200

        publication_response = await client.put(
            f"/admin/api/v1/stories/{STORY_ID}/publication",
            json={"visibility": "public"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert publication_response.status == 200

        audit_logs = await db.list_admin_audit_logs(limit=10)
        actions = [row["action"] for row in audit_logs]
        assert "story.runtime.set" in actions
        assert "story.publication.set" in actions
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_admin_healthz_is_public(tmp_path: Path) -> None:
    db = DatabaseManager(":memory:", migrations_dir=MIGRATIONS_DIR)
    await db.initialize()

    app = await create_app(
        db=db,
        runtime_controller=FakeRuntimeController(),
        archive_root=tmp_path,
        admin_base_url="/admin",
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.get("/admin/healthz")
        assert response.status == 200
        payload = await response.json()
        assert payload["status"] == "ok"
        assert payload["data"]["db"]["status"] == "ok"
        assert payload["data"]["db"]["backend"] == "aiosqlite"
        assert payload["data"]["publisher"]["status"] == "idle"
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_admin_viewer_shell_route_is_public(tmp_path: Path) -> None:
    db = DatabaseManager(":memory:", migrations_dir=MIGRATIONS_DIR)
    await db.initialize()
    assert db._conn is not None
    await db._conn.execute(
        """
        INSERT INTO stories (id, title, llm_provider, llm_model)
        VALUES (?, ?, ?, ?)
        """,
        (STORY_ID, "管理UIテスト", "openai", "gpt-4o"),
    )
    await db._conn.commit()

    app = await create_app(
        db=db,
        runtime_controller=FakeRuntimeController(),
        archive_root=tmp_path,
        admin_base_url="/admin",
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.get(f"/admin/viewer/{STORY_ID}")
        assert response.status == 200
        text = await response.text()
        assert '<div id="admin-app"></div>' in text
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_admin_onboarding_shell_route_is_public(tmp_path: Path) -> None:
    db = DatabaseManager(":memory:", migrations_dir=MIGRATIONS_DIR)
    await db.initialize()
    assert db._conn is not None
    await db._conn.execute(
        """
        INSERT INTO stories (id, title, llm_provider, llm_model)
        VALUES (?, ?, ?, ?)
        """,
        (STORY_ID, "管理UIテスト", "openai", "gpt-4o"),
    )
    await db._conn.commit()

    app = await create_app(
        db=db,
        runtime_controller=FakeRuntimeController(),
        archive_root=tmp_path,
        admin_base_url="/admin",
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.get(f"/admin/onboarding/{STORY_ID}")
        assert response.status == 200
        text = await response.text()
        assert '<div id="admin-app"></div>' in text
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_admin_character_editor_shell_route_is_public(tmp_path: Path) -> None:
    db = DatabaseManager(":memory:", migrations_dir=MIGRATIONS_DIR)
    await db.initialize()
    assert db._conn is not None
    await db._conn.execute(
        """
        INSERT INTO stories (id, title, llm_provider, llm_model)
        VALUES (?, ?, ?, ?)
        """,
        (STORY_ID, "管理UIテスト", "openai", "gpt-4o"),
    )
    await db._conn.commit()

    app = await create_app(
        db=db,
        runtime_controller=FakeRuntimeController(),
        archive_root=tmp_path,
        admin_base_url="/admin",
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.get(f"/admin/characters/{STORY_ID}")
        assert response.status == 200
        text = await response.text()
        assert '<div id="admin-app"></div>' in text
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_admin_place_editor_shell_route_is_public(tmp_path: Path) -> None:
    db = DatabaseManager(":memory:", migrations_dir=MIGRATIONS_DIR)
    await db.initialize()
    assert db._conn is not None
    await db._conn.execute(
        """
        INSERT INTO stories (id, title, llm_provider, llm_model)
        VALUES (?, ?, ?, ?)
        """,
        (STORY_ID, "管理UIテスト", "openai", "gpt-4o"),
    )
    await db._conn.commit()

    app = await create_app(
        db=db,
        runtime_controller=FakeRuntimeController(),
        archive_root=tmp_path,
        admin_base_url="/admin",
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.get(f"/admin/places/{STORY_ID}")
        assert response.status == 200
        text = await response.text()
        assert '<div id="admin-app"></div>' in text
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_admin_story_settings_shell_route_is_public(tmp_path: Path) -> None:
    db = DatabaseManager(":memory:", migrations_dir=MIGRATIONS_DIR)
    await db.initialize()
    assert db._conn is not None
    await db._conn.execute(
        """
        INSERT INTO stories (id, title, llm_provider, llm_model)
        VALUES (?, ?, ?, ?)
        """,
        (STORY_ID, "管理UIテスト", "openai", "gpt-4o"),
    )
    await db._conn.commit()

    app = await create_app(
        db=db,
        runtime_controller=FakeRuntimeController(),
        archive_root=tmp_path,
        admin_base_url="/admin",
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.get(f"/admin/story-settings/{STORY_ID}")
        assert response.status == 200
        text = await response.text()
        assert '<div id="admin-app"></div>' in text
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_admin_director_persona_editor_shell_route_is_public(tmp_path: Path) -> None:
    db = DatabaseManager(":memory:", migrations_dir=MIGRATIONS_DIR)
    await db.initialize()
    assert db._conn is not None
    await db._conn.execute(
        """
        INSERT INTO stories (id, title, llm_provider, llm_model)
        VALUES (?, ?, ?, ?)
        """,
        (STORY_ID, "管理UIテスト", "openai", "gpt-4o"),
    )
    await db._conn.commit()

    app = await create_app(
        db=db,
        runtime_controller=FakeRuntimeController(),
        archive_root=tmp_path,
        admin_base_url="/admin",
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.get(f"/admin/directors/{STORY_ID}")
        assert response.status == 200
        text = await response.text()
        assert '<div id="admin-app"></div>' in text
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_admin_chapter_definition_editor_shell_route_is_public(tmp_path: Path) -> None:
    db = DatabaseManager(":memory:", migrations_dir=MIGRATIONS_DIR)
    await db.initialize()
    assert db._conn is not None
    await db._conn.execute(
        """
        INSERT INTO stories (id, title, llm_provider, llm_model)
        VALUES (?, ?, ?, ?)
        """,
        (STORY_ID, "管理UIテスト", "openai", "gpt-4o"),
    )
    await db._conn.commit()

    app = await create_app(
        db=db,
        runtime_controller=FakeRuntimeController(),
        archive_root=tmp_path,
        admin_base_url="/admin",
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.get(f"/admin/chapter-definitions/{STORY_ID}")
        assert response.status == 200
        text = await response.text()
        assert '<div id="admin-app"></div>' in text
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_admin_chapter_manager_shell_route_is_public(tmp_path: Path) -> None:
    db = DatabaseManager(":memory:", migrations_dir=MIGRATIONS_DIR)
    await db.initialize()
    assert db._conn is not None
    await db._conn.execute(
        """
        INSERT INTO stories (id, title, llm_provider, llm_model)
        VALUES (?, ?, ?, ?)
        """,
        (STORY_ID, "管理UIテスト", "openai", "gpt-4o"),
    )
    await db._conn.commit()

    app = await create_app(
        db=db,
        runtime_controller=FakeRuntimeController(),
        archive_root=tmp_path,
        admin_base_url="/admin",
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.get(f"/admin/chapters/{STORY_ID}")
        assert response.status == 200
        text = await response.text()
        assert '<div id="admin-app"></div>' in text
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_admin_event_anomaly_editor_shell_route_is_public(tmp_path: Path) -> None:
    db = DatabaseManager(":memory:", migrations_dir=MIGRATIONS_DIR)
    await db.initialize()
    assert db._conn is not None
    await db._conn.execute(
        """
        INSERT INTO stories (id, title, llm_provider, llm_model)
        VALUES (?, ?, ?, ?)
        """,
        (STORY_ID, "管理UIテスト", "openai", "gpt-4o"),
    )
    await db._conn.commit()

    app = await create_app(
        db=db,
        runtime_controller=FakeRuntimeController(),
        archive_root=tmp_path,
        admin_base_url="/admin",
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.get(f"/admin/event-anomalies/{STORY_ID}")
        assert response.status == 200
        text = await response.text()
        assert '<div id="admin-app"></div>' in text
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_admin_serves_web_assets_for_local_viewer_images(tmp_path: Path) -> None:
    db = DatabaseManager(":memory:", migrations_dir=MIGRATIONS_DIR)
    await db.initialize()
    web_assets_root = tmp_path / "web_assets"
    image_path = web_assets_root / "character_images" / STORY_ID / "hero" / "happy.png"
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(b"hero-happy")

    app = await create_app(
        db=db,
        runtime_controller=FakeRuntimeController(),
        archive_root=tmp_path,
        admin_base_url="/admin",
        web_assets_root=web_assets_root,
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.get(
            f"/assets/character_images/{STORY_ID}/hero/happy.png"
        )
        assert response.status == 200
        assert await response.read() == b"hero-happy"
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_admin_viewer_api_returns_local_viewer_payload(tmp_path: Path) -> None:
    db = DatabaseManager(":memory:", migrations_dir=MIGRATIONS_DIR)
    await db.initialize()
    assert db._conn is not None
    await db._conn.execute(
        """
        INSERT INTO stories (id, title, llm_provider, llm_model)
        VALUES (?, ?, ?, ?)
        """,
        (STORY_ID, "管理UIテスト", "openai", "gpt-4o"),
    )
    await db._conn.execute(
        """
        INSERT INTO characters (id, story_id, name_ja, expressions_available, is_active)
        VALUES (?, ?, ?, ?, 1)
        """,
        ("hero", STORY_ID, "主人公", '["neutral", "happy"]'),
    )
    await db._conn.execute(
        """
        INSERT INTO places (id, story_id, label, zone, is_active)
        VALUES (?, ?, ?, ?, 1)
        """,
        ("classroom", STORY_ID, "教室", "school"),
    )
    await db.insert_chat_log(
        STORY_ID,
        {
            "sim_datetime": "2026-04-25T10:00:00",
            "turn_number": 42,
            "char_id": "hero",
            "msg_type": "talk",
            "place_id": "classroom",
            "expression": "happy",
            "message": "おはよう。",
            "llm_provider": "openai",
            "llm_model": "gpt-5.4-nano",
        },
    )
    token = await db.create_admin_api_token("default-agent")

    app = await create_app(
        db=db,
        runtime_controller=FakeRuntimeController(),
        archive_root=tmp_path,
        admin_base_url="/admin",
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.get(
            f"/admin/api/v1/stories/{STORY_ID}/viewer",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status == 200
        payload = await response.json()
        assert payload["data"]["story"]["story_id"] == STORY_ID
        assert payload["data"]["runtime"]["state"] == "stopped"
        assert payload["data"]["llm"]["provider"] == "openai"
        assert payload["data"]["llm"]["model"] == "gpt-5.4-nano"
        assert payload["data"]["publication"]["visibility"] == "draft"
        assert payload["data"]["live"]["latest_turn"] == 42
        assert payload["data"]["characters"][0]["char_id"] == "hero"
        assert payload["data"]["characters"][0]["expressions_available"] == ["neutral", "happy"]
        assert payload["data"]["logs"][0]["speaker_name"] == "主人公"
        assert payload["data"]["logs"][0]["place_label"] == "教室"
        assert payload["data"]["logs"][0]["expression"] == "happy"
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_story_runtime_reset_clears_progress_without_deleting_settings(tmp_path: Path) -> None:
    db = DatabaseManager(":memory:", migrations_dir=MIGRATIONS_DIR)
    await db.initialize()
    assert db._conn is not None
    await db._conn.execute(
        """
        INSERT INTO stories (
            id, title, season_start, turn_minutes, llm_provider, llm_model, last_sim_time
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (STORY_ID, "管理UIテスト", "2026-04-01", 10, "openai", "gpt-4o", "2026-04-02T12:00:00"),
    )
    await db._conn.execute(
        """
        INSERT INTO places (id, story_id, label, zone, is_active)
        VALUES (?, ?, ?, ?, 1)
        """,
        ("classroom", STORY_ID, "教室", "school"),
    )
    await db._conn.execute(
        """
        INSERT INTO characters (
            id, story_id, name_ja, emotion_default, favorite_places,
            expressions_available, is_active
        )
        VALUES (?, ?, ?, ?, ?, ?, 1)
        """,
        (
            "hero",
            STORY_ID,
            "主人公",
            '{"stress": 0.11, "motivation": 0.82, "loneliness": 0.23, "excitement": 0.44}',
            '["classroom"]',
            '["neutral", "happy"]',
        ),
    )
    await db._conn.execute(
        """
        INSERT INTO characters (
            id, story_id, name_ja, emotion_default, favorite_places,
            expressions_available, is_active
        )
        VALUES (?, ?, ?, ?, ?, ?, 1)
        """,
        (
            "rival",
            STORY_ID,
            "ライバル",
            '{"stress": 0.2, "motivation": 0.6}',
            '["classroom"]',
            '["neutral"]',
        ),
    )
    await db._conn.execute(
        """
        INSERT INTO story_publications (story_id, visibility, latest_published_turn)
        VALUES (?, 'public', 99)
        """,
        (STORY_ID,),
    )
    await db._conn.execute(
        """
        INSERT INTO director_personas (
            story_id, persona_id, name, aesthetic_json, values_json, traits_json, is_active
        )
        VALUES (?, ?, ?, '{}', '[]', '[]', 1)
        """,
        (STORY_ID, "default_director", "標準監督"),
    )
    await db._conn.execute(
        """
        INSERT INTO character_states (
            char_id, story_id, sim_datetime, turn_number, current_place,
            stress, motivation, loneliness, excitement
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("hero", STORY_ID, "2026-04-02T12:00:00", 99, "old_room", 0.9, 0.1, 0.8, 0.2),
    )
    await db._conn.execute(
        """
        INSERT INTO relationships (
            story_id, char_id_from, char_id_to, trust, affinity, tension, familiarity, last_event_turn
        )
        VALUES (?, ?, ?, 0.1, 0.2, 0.9, 0.8, 99)
        """,
        (STORY_ID, "hero", "rival"),
    )
    await db.insert_chat_log(
        STORY_ID,
        {
            "sim_datetime": "2026-04-02T12:00:00",
            "turn_number": 99,
            "char_id": "hero",
            "msg_type": "talk",
            "place_id": "old_room",
            "expression": "happy",
            "message": "続きのログ",
        },
    )
    await db.insert_scene_script(
        STORY_ID,
        {
            "turn_number": 99,
            "script_text": "初期化前のシーンスクリプト",
        },
    )
    await db._conn.execute(
        """
        INSERT INTO story_memory (story_id, memory_type, summary, trigger_turn)
        VALUES (?, 'event', '古い記憶', 99)
        """,
        (STORY_ID,),
    )
    await db._conn.execute(
        """
        INSERT INTO story_chapters (
            story_id, chapter_id, title, status, start_condition, current_beat, opened_turn
        )
        VALUES (?, 'ch001', '導入', 'active', NULL, 'complication', 42)
        """,
        (STORY_ID,),
    )
    chapter_id = (await (await db._conn.execute("SELECT id FROM story_chapters")).fetchone())[0]
    await db._conn.execute(
        """
        INSERT INTO story_chapter_beats (chapter_db_id, phase, description, status, reached_turn)
        VALUES (?, 'setup', '開始', 'reached', 42)
        """,
        (chapter_id,),
    )
    await db._conn.commit()
    token = await db.create_admin_api_token("default-agent")
    chatlog_root = tmp_path / "chatlog"
    story_log_dir = chatlog_root / STORY_ID
    story_log_dir.mkdir(parents=True)
    (story_log_dir / "20260508.dat").write_text('{"message":"old"}\n', encoding="utf-8")
    (story_log_dir / "index.html").write_text("", encoding="utf-8")

    app = await create_app(
        db=db,
        runtime_controller=FakeRuntimeController(),
        archive_root=tmp_path,
        admin_base_url="/admin",
        chatlog_root=chatlog_root,
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.post(
            f"/admin/api/v1/stories/{STORY_ID}/runtime/reset",
            headers={"Authorization": f"Bearer {token}"},
            json={"confirm": True},
        )
        assert response.status == 200
        payload = await response.json()
        assert payload["data"]["public_logs_deleted"] == 1
        assert payload["data"]["db_counts"]["scene_scripts"] == 1

        story = await db.get_story(STORY_ID)
        assert story is not None
        assert story["last_sim_time"] is None
        assert await db.get_recent_chat_logs(STORY_ID, limit=10) == []
        assert await db.get_recent_scene_scripts(STORY_ID, limit=10) == []
        state = await db.get_latest_character_state(STORY_ID, "hero")
        assert state is not None
        assert state["sim_datetime"] == "2026-04-01T00:00:00"
        assert state["current_place"] == "classroom"
        assert state["stress"] == pytest.approx(0.11)
        rel = await (
            await db._conn.execute(
                """
                SELECT trust, affinity, tension, familiarity, last_event_turn
                FROM relationships
                WHERE story_id = ? AND char_id_from = ? AND char_id_to = ?
                """,
                (STORY_ID, "hero", "rival"),
            )
        ).fetchone()
        assert dict(rel) == {
            "trust": 0.5,
            "affinity": 0.5,
            "tension": 0.0,
            "familiarity": 0.5,
            "last_event_turn": None,
        }
        chapter = await db.get_story_chapter_by_chapter_id(STORY_ID, "ch001")
        assert chapter is not None
        assert chapter["status"] == "pending"
        assert chapter["current_beat"] == "setup"
        beat = (await db.get_chapter_beats(chapter["id"]))[0]
        assert beat["status"] == "pending"
        assert beat["reached_turn"] is None
        assert not (story_log_dir / "20260508.dat").exists()
        assert (story_log_dir / "index.html").exists()
        assert await db.get_characters(STORY_ID)
        assert await db.get_active_director_persona(STORY_ID) is not None
        publication = await db.get_story_publication(STORY_ID)
        assert publication["visibility"] == "public"
        audit_rows = await db.list_admin_audit_logs(action="story.runtime.reset")
        assert len(audit_rows) == 1
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_story_runtime_reset_sends_remote_public_log_reset_before_local_reset(
    tmp_path: Path,
) -> None:
    db, token = await _create_admin_db_with_token()
    await db.insert_chat_log(
        STORY_ID,
        {
            "sim_datetime": "2026-05-15T10:00:00",
            "turn_number": 3,
            "char_id": "hero",
            "msg_type": "talk",
            "message": "remote reset should happen before local delete",
        },
    )

    remote_calls: list[tuple[str, str, str]] = []

    async def fake_remote_reset(target: StoryWebPostTargetConfig, story_id: str) -> dict[str, object]:
        remote_calls.append((story_id, target.receiver_url, target.auth_token))
        return {"status": "ok", "deleted": 7}

    app = await create_app(
        db=db,
        runtime_controller=FakeRuntimeController(),
        archive_root=tmp_path,
        admin_base_url="/admin",
        story_web_post_targets={
            STORY_ID: StoryWebPostTargetConfig(
                enabled=True,
                receiver_url="https://remote.example/receiver.php",
                auth_token="remote-token",
            )
        },
        remote_reset_sender=fake_remote_reset,
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.post(
            f"/admin/api/v1/stories/{STORY_ID}/runtime/reset",
            headers={"Authorization": f"Bearer {token}"},
            json={"confirm": True},
        )

        assert response.status == 200
        payload = await response.json()
        assert remote_calls == [(STORY_ID, "https://remote.example/receiver.php", "remote-token")]
        assert payload["data"]["remote_public_logs_reset"]["status"] == "ok"
        assert payload["data"]["remote_public_logs_reset"]["deleted"] == 7
        assert await db.get_recent_chat_logs(STORY_ID, limit=10) == []
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_story_runtime_reset_aborts_local_reset_when_remote_public_reset_fails(
    tmp_path: Path,
) -> None:
    db, token = await _create_admin_db_with_token()
    await db.insert_chat_log(
        STORY_ID,
        {
            "sim_datetime": "2026-05-15T10:00:00",
            "turn_number": 3,
            "char_id": "hero",
            "msg_type": "talk",
            "message": "must remain if remote reset fails",
        },
    )

    async def failing_remote_reset(
        target: StoryWebPostTargetConfig,
        story_id: str,
    ) -> dict[str, object]:
        raise RuntimeError("remote receiver rejected reset")

    app = await create_app(
        db=db,
        runtime_controller=FakeRuntimeController(),
        archive_root=tmp_path,
        admin_base_url="/admin",
        story_web_post_targets={
            STORY_ID: StoryWebPostTargetConfig(
                enabled=True,
                receiver_url="https://remote.example/receiver.php",
                auth_token="remote-token",
            )
        },
        remote_reset_sender=failing_remote_reset,
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.post(
            f"/admin/api/v1/stories/{STORY_ID}/runtime/reset",
            headers={"Authorization": f"Bearer {token}"},
            json={"confirm": True},
        )

        assert response.status == 500
        remaining = await db.get_recent_chat_logs(STORY_ID, limit=10)
        assert [row["message"] for row in remaining] == ["must remain if remote reset fails"]
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_web_post_settings_do_not_echo_auth_token_and_preserve_empty_token(
    tmp_path: Path,
) -> None:
    db, token = await _create_admin_db_with_token()
    app = await create_app(
        db=db,
        runtime_controller=FakeRuntimeController(),
        archive_root=tmp_path,
        admin_base_url="/admin",
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        headers = {"Authorization": f"Bearer {token}"}
        put_response = await client.put(
            "/admin/api/v1/settings/web-post",
            headers=headers,
            json={
                "receiver_url": "https://remote.example/receiver.php",
                "auth_token": "secret-token",
            },
        )
        assert put_response.status == 200
        put_payload = await put_response.json()
        assert "auth_token" not in put_payload["data"]
        assert put_payload["data"]["has_auth_token"] is True

        get_response = await client.get("/admin/api/v1/settings/web-post", headers=headers)
        assert get_response.status == 200
        get_payload = await get_response.json()
        assert get_payload["data"] == {
            "receiver_url": "https://remote.example/receiver.php",
            "has_auth_token": True,
        }

        empty_token_response = await client.put(
            "/admin/api/v1/settings/web-post",
            headers=headers,
            json={
                "receiver_url": "https://other.example/receiver.php",
                "auth_token": "",
            },
        )
        assert empty_token_response.status == 200
        assert await db.get_system_setting("default_web_auth_token") == "secret-token"

        clear_response = await client.put(
            "/admin/api/v1/settings/web-post",
            headers=headers,
            json={"receiver_url": "", "clear_auth_token": True},
        )
        assert clear_response.status == 200
        clear_payload = await clear_response.json()
        assert clear_payload["data"]["has_auth_token"] is False
        assert await db.get_system_setting("default_web_auth_token") == ""
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_story_web_post_settings_do_not_echo_auth_token_and_preserve_empty_token(
    tmp_path: Path,
) -> None:
    db, token = await _create_admin_db_with_token()
    app = await create_app(
        db=db,
        runtime_controller=FakeRuntimeController(),
        archive_root=tmp_path,
        admin_base_url="/admin",
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        headers = {"Authorization": f"Bearer {token}"}
        put_response = await client.put(
            f"/admin/api/v1/stories/{STORY_ID}/web-post",
            headers=headers,
            json={
                "receiver_url": "https://story.example/receiver.php",
                "auth_token": "story-secret",
            },
        )
        assert put_response.status == 200
        put_payload = await put_response.json()
        assert "auth_token" not in put_payload["data"]
        assert put_payload["data"]["has_auth_token"] is True

        get_response = await client.get(
            f"/admin/api/v1/stories/{STORY_ID}/web-post",
            headers=headers,
        )
        assert get_response.status == 200
        get_payload = await get_response.json()
        assert get_payload["data"] == {
            "story_id": STORY_ID,
            "receiver_url": "https://story.example/receiver.php",
            "has_auth_token": True,
        }

        empty_token_response = await client.put(
            f"/admin/api/v1/stories/{STORY_ID}/web-post",
            headers=headers,
            json={
                "receiver_url": "https://story2.example/receiver.php",
                "auth_token": "",
            },
        )
        assert empty_token_response.status == 200
        stored = await db.get_story_web_post_settings(STORY_ID)
        assert stored["auth_token"] == "story-secret"

        clear_response = await client.put(
            f"/admin/api/v1/stories/{STORY_ID}/web-post",
            headers=headers,
            json={"receiver_url": "", "clear_auth_token": True},
        )
        assert clear_response.status == 200
        clear_payload = await clear_response.json()
        assert clear_payload["data"]["has_auth_token"] is False
        stored = await db.get_story_web_post_settings(STORY_ID)
        assert stored["auth_token"] == ""
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_web_post_settings_reject_private_receiver_urls(tmp_path: Path) -> None:
    db, token = await _create_admin_db_with_token()
    app = await create_app(
        db=db,
        runtime_controller=FakeRuntimeController(),
        archive_root=tmp_path,
        admin_base_url="/admin",
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        headers = {"Authorization": f"Bearer {token}"}
        global_response = await client.put(
            "/admin/api/v1/settings/web-post",
            headers=headers,
            json={"receiver_url": "http://127.0.0.1/receiver.php", "auth_token": "secret"},
        )
        assert global_response.status == 400

        story_response = await client.put(
            f"/admin/api/v1/stories/{STORY_ID}/web-post",
            headers=headers,
            json={"receiver_url": "https://user:pass@example.com/receiver.php"},
        )
        assert story_response.status == 400
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_per_story_settings_return_404_for_missing_story(tmp_path: Path) -> None:
    db, token = await _create_admin_db_with_token()
    app = await create_app(
        db=db,
        runtime_controller=FakeRuntimeController(),
        archive_root=tmp_path,
        admin_base_url="/admin",
        news_store_path=tmp_path / "news.db",
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        headers = {"Authorization": f"Bearer {token}"}
        cases = [
            ("GET", "/admin/api/v1/stories/missing_story/web-post", None),
            ("PUT", "/admin/api/v1/stories/missing_story/web-post", {"receiver_url": ""}),
            ("GET", "/admin/api/v1/stories/missing_story/news-tag-filter", None),
            ("PUT", "/admin/api/v1/stories/missing_story/news-tag-filter", {"tags": ["tech"]}),
            ("GET", "/admin/api/v1/stories/missing_story/utterance-settings", None),
            ("PUT", "/admin/api/v1/stories/missing_story/utterance-settings", {"max_chars": 180}),
            ("GET", "/admin/api/v1/stories/missing_story/news-mode", None),
            ("PUT", "/admin/api/v1/stories/missing_story/news-mode", {"enabled": True, "intensity": "low"}),
        ]
        for method, path, body in cases:
            response = await client.request(method, path, headers=headers, json=body)
            assert response.status == 404, f"{method} {path}"
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_story_delete_sends_remote_public_log_reset_before_db_delete(
    tmp_path: Path,
) -> None:
    db, token = await _create_admin_db_with_token()
    await db.set_story_active(STORY_ID, False)
    remote_calls: list[tuple[str, str, str]] = []

    async def fake_remote_reset(target: StoryWebPostTargetConfig, story_id: str) -> dict[str, object]:
        assert await db.get_story(story_id) is not None
        remote_calls.append((story_id, target.receiver_url, target.auth_token))
        return {"status": "ok", "deleted": 3}

    app = await create_app(
        db=db,
        runtime_controller=FakeRuntimeController(),
        archive_root=tmp_path,
        admin_base_url="/admin",
        story_web_post_targets={
            STORY_ID: StoryWebPostTargetConfig(
                enabled=True,
                receiver_url="https://remote.example/receiver.php",
                auth_token="remote-token",
            )
        },
        remote_reset_sender=fake_remote_reset,
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.delete(
            f"/admin/api/v1/stories/{STORY_ID}",
            headers={"Authorization": f"Bearer {token}"},
            json={"confirm_story_id": STORY_ID, "delete_files": False},
        )

        assert response.status == 200
        payload = await response.json()
        assert remote_calls == [(STORY_ID, "https://remote.example/receiver.php", "remote-token")]
        assert payload["data"]["remote_public_logs_reset"]["status"] == "ok"
        assert payload["data"]["remote_public_logs_reset"]["deleted"] == 3
        assert await db.get_story(STORY_ID) is None
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_story_delete_aborts_local_delete_when_remote_public_reset_fails(
    tmp_path: Path,
) -> None:
    db, token = await _create_admin_db_with_token()
    await db.set_story_active(STORY_ID, False)

    async def failing_remote_reset(
        target: StoryWebPostTargetConfig,
        story_id: str,
    ) -> dict[str, object]:
        raise RuntimeError("remote receiver rejected delete reset")

    app = await create_app(
        db=db,
        runtime_controller=FakeRuntimeController(),
        archive_root=tmp_path,
        admin_base_url="/admin",
        story_web_post_targets={
            STORY_ID: StoryWebPostTargetConfig(
                enabled=True,
                receiver_url="https://remote.example/receiver.php",
                auth_token="remote-token",
            )
        },
        remote_reset_sender=failing_remote_reset,
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.delete(
            f"/admin/api/v1/stories/{STORY_ID}",
            headers={"Authorization": f"Bearer {token}"},
            json={"confirm_story_id": STORY_ID, "delete_files": False},
        )

        assert response.status == 500
        assert await db.get_story(STORY_ID) is not None
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_story_runtime_reset_requires_confirmation_and_stopped_runtime(tmp_path: Path) -> None:
    db = DatabaseManager(":memory:", migrations_dir=MIGRATIONS_DIR)
    await db.initialize()
    assert db._conn is not None
    await db._conn.execute(
        """
        INSERT INTO stories (id, title, llm_provider, llm_model)
        VALUES (?, ?, ?, ?)
        """,
        (STORY_ID, "管理UIテスト", "openai", "gpt-4o"),
    )
    await db.insert_chat_log(
        STORY_ID,
        {
            "sim_datetime": "2026-04-25T10:00:00",
            "turn_number": 42,
            "char_id": "hero",
            "msg_type": "talk",
            "message": "消えてはいけないログ",
        },
    )
    await db._conn.commit()
    token = await db.create_admin_api_token("default-agent")
    chatlog_root = tmp_path / "chatlog"
    story_log_dir = chatlog_root / STORY_ID
    story_log_dir.mkdir(parents=True)
    (story_log_dir / "20260508.dat").write_text('{"message":"old"}\n', encoding="utf-8")
    runtime = FakeRuntimeController()
    runtime.states[STORY_ID] = "running"
    app = await create_app(
        db=db,
        runtime_controller=runtime,
        archive_root=tmp_path,
        admin_base_url="/admin",
        chatlog_root=chatlog_root,
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        missing_confirm = await client.post(
            f"/admin/api/v1/stories/{STORY_ID}/runtime/reset",
            headers={"Authorization": f"Bearer {token}"},
            json={},
        )
        assert missing_confirm.status == 400

        running_response = await client.post(
            f"/admin/api/v1/stories/{STORY_ID}/runtime/reset",
            headers={"Authorization": f"Bearer {token}"},
            json={"confirm": True},
        )
        assert running_response.status == 409
        assert len(await db.get_recent_chat_logs(STORY_ID, limit=10)) == 1
        assert (story_log_dir / "20260508.dat").exists()
        audit_rows = await db.list_admin_audit_logs(action="story.runtime.reset")
        assert audit_rows == []
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_admin_onboarding_template_api_returns_template_payload(tmp_path: Path) -> None:
    db_path = tmp_path / "admin.db"
    db = DatabaseManager(db_path, migrations_dir=MIGRATIONS_DIR)
    await db.initialize()
    token = await db.create_admin_api_token("default-agent")

    stories_root = tmp_path / "stories"
    character_images_root = tmp_path / "character_images"
    story_maps_root = tmp_path / "story_maps"
    story_metadata_root = tmp_path / "story_metadata"
    _write_template_story_bundle(stories_root, STORY_ID)
    _write_template_assets(character_images_root, story_maps_root, story_metadata_root, STORY_ID)

    app = await create_app(
        db=db,
        runtime_controller=FakeRuntimeController(),
        archive_root=tmp_path,
        admin_base_url="/admin",
        stories_root=stories_root,
        character_images_root=character_images_root,
        story_maps_root=story_maps_root,
        story_metadata_root=story_metadata_root,
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.get(
            f"/admin/api/v1/stories/{STORY_ID}/onboarding-template",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status == 200
        payload = await response.json()
        assert payload["data"]["story"]["story_id"] == STORY_ID
        assert payload["data"]["story"]["title"] == "テンプレート"
        assert payload["data"]["characters"][0]["char_id"] == "test_char"
        assert payload["data"]["characters"][0]["name"] == "テンプレキャラ"
        assert payload["data"]["characters"][0]["short_description"] == "静かな主人公"
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_admin_clone_api_creates_story_from_template(tmp_path: Path) -> None:
    db_path = tmp_path / "admin.db"
    db = DatabaseManager(db_path, migrations_dir=MIGRATIONS_DIR)
    await db.initialize()
    token = await db.create_admin_api_token("default-agent")

    stories_root = tmp_path / "stories"
    character_images_root = tmp_path / "character_images"
    story_maps_root = tmp_path / "story_maps"
    story_metadata_root = tmp_path / "story_metadata"
    _write_template_story_bundle(stories_root, STORY_ID)
    _write_template_assets(character_images_root, story_maps_root, story_metadata_root, STORY_ID)

    runtime = FakeRuntimeController()
    app = await create_app(
        db=db,
        runtime_controller=runtime,
        archive_root=tmp_path,
        admin_base_url="/admin",
        stories_root=stories_root,
        character_images_root=character_images_root,
        story_maps_root=story_maps_root,
        story_metadata_root=story_metadata_root,
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.post(
            f"/admin/api/v1/stories/{STORY_ID}/clone",
            json={
                "story_id": "my_story",
                "title": "自分の物語",
                "description": "差し替えた説明",
                "characters": [
                    {
                        "char_id": "test_char",
                        "name": "自分の主人公",
                        "short_description": "少し不器用な観測者",
                        "goal": "うまく友達を作る",
                        "worry": "自分だけ空回りしそう",
                    }
                ],
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status == 201
        payload = await response.json()
        assert payload["data"]["story_id"] == "my_story"
        assert payload["data"]["import_mode"] == "created"
        assert runtime.list_states()["my_story"] == "stopped"

        story_row = await db.get_story("my_story")
        assert story_row is not None
        assert story_row["title"] == "自分の物語"

        audit_logs = await db.list_admin_audit_logs(limit=10)
        assert "story.clone.create" in [row["action"] for row in audit_logs]
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_admin_clone_api_rejects_oversized_character_image_upload(tmp_path: Path) -> None:
    db_path = tmp_path / "admin.db"
    db = DatabaseManager(db_path, migrations_dir=MIGRATIONS_DIR)
    await db.initialize()
    token = await db.create_admin_api_token("default-agent")

    stories_root = tmp_path / "stories"
    character_images_root = tmp_path / "character_images"
    story_maps_root = tmp_path / "story_maps"
    story_metadata_root = tmp_path / "story_metadata"
    _write_template_story_bundle(stories_root, STORY_ID)
    _write_template_assets(character_images_root, story_maps_root, story_metadata_root, STORY_ID)

    app = await create_app(
        db=db,
        runtime_controller=FakeRuntimeController(),
        archive_root=tmp_path,
        admin_base_url="/admin",
        stories_root=stories_root,
        character_images_root=character_images_root,
        story_maps_root=story_maps_root,
        story_metadata_root=story_metadata_root,
        max_image_upload_bytes=4,
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        form = FormData()
        form.add_field(
            "payload",
            json.dumps(
                {
                    "story_id": "my_story",
                    "title": "自分の物語",
                    "description": "差し替えた説明",
                    "characters": [{"char_id": "test_char", "name": "自分の主人公"}],
                },
                ensure_ascii=False,
            ),
            content_type="application/json",
        )
        form.add_field(
            "character_image__test_char",
            b"12345",
            filename="neutral.png",
            content_type="image/png",
        )

        response = await client.post(
            f"/admin/api/v1/stories/{STORY_ID}/clone",
            data=form,
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status == 413
        assert await db.get_story("my_story") is None
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_admin_settings_api_returns_llm_runtime_payload(tmp_path: Path) -> None:
    db_path = tmp_path / "admin.db"
    db = DatabaseManager(db_path, migrations_dir=MIGRATIONS_DIR)
    await db.initialize()
    token = await db.create_admin_api_token("default-agent")
    llm_runtime_path = tmp_path / "llm_runtime.local.yaml"
    llm_runtime_path.write_text(
        yaml.safe_dump(
            {
                "active_profile": "openai_nano",
                "profiles": {
                    "openai_nano": {"provider": "openai", "model": "gpt-5.4-nano"},
                    "ollama_local": {"provider": "ollama", "model": "gemma4:e4b"},
                },
                "story_overrides": {STORY_ID: "ollama_local"},
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    app = await create_app(
        db=db,
        runtime_controller=FakeRuntimeController(),
        archive_root=tmp_path,
        admin_base_url="/admin",
        llm_runtime_path=llm_runtime_path,
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.get(
            "/admin/api/v1/settings",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status == 200
        payload = await response.json()
        llm_runtime = payload["data"]["llm_runtime"]
        assert llm_runtime["active_profile"] == "openai_nano"
        assert llm_runtime["story_overrides"][STORY_ID] == "ollama_local"
        assert {
            "profile_name": "ollama_local",
            "provider": "ollama",
            "model": "gemma4:e4b",
        } in llm_runtime["profiles"]
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_admin_llm_runtime_settings_api_updates_yaml_and_runtime(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "admin.db"
    db = DatabaseManager(db_path, migrations_dir=MIGRATIONS_DIR)
    await db.initialize()
    token = await db.create_admin_api_token("default-agent")
    llm_runtime_path = tmp_path / "llm_runtime.local.yaml"
    runtime = FakeRuntimeController()

    app = await create_app(
        db=db,
        runtime_controller=runtime,
        archive_root=tmp_path,
        admin_base_url="/admin",
        llm_runtime_path=llm_runtime_path,
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.put(
            "/admin/api/v1/settings/llm-runtime",
            json={
                "active_profile": "openai_nano",
                "profiles": [
                    {
                        "profile_name": "openai_nano",
                        "provider": "openai",
                        "model": "gpt-5.4-nano",
                    },
                    {
                        "profile_name": "ollama_local",
                        "provider": "ollama",
                        "model": "gemma4:e4b",
                    },
                ],
                "story_overrides": {STORY_ID: "ollama_local"},
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status == 200
        payload = await response.json()
        assert payload["data"]["profile_count"] == 2
        assert payload["data"]["story_override_count"] == 1
        assert payload["data"]["applied_to_runtime"] is True

        saved = yaml.safe_load(llm_runtime_path.read_text(encoding="utf-8"))
        assert saved["active_profile"] == "openai_nano"
        assert saved["profiles"]["ollama_local"]["model"] == "gemma4:e4b"
        assert runtime.applied_llm_runtime is not None
        assert runtime.applied_llm_runtime.story_overrides[STORY_ID] == "ollama_local"
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_admin_llm_runtime_settings_api_rejects_bad_profile(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "admin.db"
    db = DatabaseManager(db_path, migrations_dir=MIGRATIONS_DIR)
    await db.initialize()
    token = await db.create_admin_api_token("default-agent")

    app = await create_app(
        db=db,
        runtime_controller=FakeRuntimeController(),
        archive_root=tmp_path,
        admin_base_url="/admin",
        llm_runtime_path=tmp_path / "llm_runtime.local.yaml",
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.put(
            "/admin/api/v1/settings/llm-runtime",
            json={
                "active_profile": "missing",
                "profiles": [
                    {
                        "profile_name": "openai_nano",
                        "provider": "openai",
                        "model": "gpt-5.4-nano",
                    }
                ],
                "story_overrides": {},
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status == 400
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_admin_character_edit_template_api_returns_editable_story_payload(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "admin.db"
    db = DatabaseManager(db_path, migrations_dir=MIGRATIONS_DIR)
    await db.initialize()
    token = await db.create_admin_api_token("default-agent")

    stories_root = tmp_path / "stories"
    character_images_root = tmp_path / "character_images"
    story_maps_root = tmp_path / "story_maps"
    story_metadata_root = tmp_path / "story_metadata"
    _write_template_story_bundle(stories_root, STORY_ID)
    _write_template_assets(character_images_root, story_maps_root, story_metadata_root, STORY_ID)
    await import_story(
        stories_root / STORY_ID,
        db_path=db.db_path,
        force_replace=False,
        story_maps_root=story_maps_root,
    )

    app = await create_app(
        db=db,
        runtime_controller=FakeRuntimeController(),
        archive_root=tmp_path,
        admin_base_url="/admin",
        stories_root=stories_root,
        character_images_root=character_images_root,
        story_maps_root=story_maps_root,
        story_metadata_root=story_metadata_root,
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.get(
            f"/admin/api/v1/stories/{STORY_ID}/characters/edit-template",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status == 200
        payload = await response.json()
        assert payload["data"]["story"]["story_id"] == STORY_ID
        assert payload["data"]["editable"] is True
        assert payload["data"]["runtime_state"] == "stopped"
        assert payload["data"]["characters"][0]["char_id"] == "test_char"
        assert payload["data"]["characters"][0]["name"] == "テンプレキャラ"
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_admin_character_edit_api_updates_story_and_audits(tmp_path: Path) -> None:
    db_path = tmp_path / "admin.db"
    db = DatabaseManager(db_path, migrations_dir=MIGRATIONS_DIR)
    await db.initialize()
    token = await db.create_admin_api_token("default-agent")

    stories_root = tmp_path / "stories"
    character_images_root = tmp_path / "character_images"
    story_maps_root = tmp_path / "story_maps"
    story_metadata_root = tmp_path / "story_metadata"
    _write_template_story_bundle(stories_root, STORY_ID)
    _write_template_assets(character_images_root, story_maps_root, story_metadata_root, STORY_ID)
    await import_story(
        stories_root / STORY_ID,
        db_path=db.db_path,
        force_replace=False,
        story_maps_root=story_maps_root,
    )

    app = await create_app(
        db=db,
        runtime_controller=FakeRuntimeController(),
        archive_root=tmp_path,
        admin_base_url="/admin",
        stories_root=stories_root,
        character_images_root=character_images_root,
        story_maps_root=story_maps_root,
        story_metadata_root=story_metadata_root,
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.put(
            f"/admin/api/v1/stories/{STORY_ID}/characters",
            json={
                "characters": [
                    {
                        "char_id": "test_char",
                        "name": "編集後キャラ",
                        "short_description": "明るい観察者",
                        "goal": "友達と物語を進める",
                        "worry": "失敗が怖い",
                    }
                ]
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status == 200
        payload = await response.json()
        assert payload["data"]["update_counts"]["characters_updated"] == 1

        character_row = await db.get_character(STORY_ID, "test_char")
        assert character_row is not None
        assert character_row["name_ja"] == "編集後キャラ"
        assert character_row["personality_core"] == "明るい観察者"

        audit_logs = await db.list_admin_audit_logs(limit=10)
        assert "story.characters.update" in [row["action"] for row in audit_logs]
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_admin_character_edit_api_renames_character_id(tmp_path: Path) -> None:
    db_path = tmp_path / "admin.db"
    db = DatabaseManager(db_path, migrations_dir=MIGRATIONS_DIR)
    await db.initialize()
    token = await db.create_admin_api_token("default-agent")

    stories_root = tmp_path / "stories"
    character_images_root = tmp_path / "character_images"
    story_maps_root = tmp_path / "story_maps"
    story_metadata_root = tmp_path / "story_metadata"
    _write_template_story_bundle(stories_root, STORY_ID)
    _write_template_assets(character_images_root, story_maps_root, story_metadata_root, STORY_ID)
    await import_story(
        stories_root / STORY_ID,
        db_path=db.db_path,
        force_replace=False,
        story_maps_root=story_maps_root,
    )

    app = await create_app(
        db=db,
        runtime_controller=FakeRuntimeController(),
        archive_root=tmp_path,
        admin_base_url="/admin",
        stories_root=stories_root,
        character_images_root=character_images_root,
        story_maps_root=story_maps_root,
        story_metadata_root=story_metadata_root,
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.put(
            f"/admin/api/v1/stories/{STORY_ID}/characters",
            json={
                "characters": [
                    {
                        "char_id": "test_char",
                        "new_char_id": "main_hero",
                        "name": "編集後キャラ",
                        "short_description": "明るい観察者",
                        "goal": "友達と物語を進める",
                        "worry": "失敗が怖い",
                    }
                ]
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status == 200
        payload = await response.json()
        assert payload["data"]["characters_renamed"] == 1

        assert db._conn is not None
        cursor = await db._conn.execute(
            "SELECT id, is_active FROM characters WHERE story_id = ? AND id IN (?, ?)",
            (STORY_ID, "test_char", "main_hero"),
        )
        rows = [dict(row) for row in await cursor.fetchall()]
        assert {"id": "test_char", "is_active": 0} in rows
        assert {"id": "main_hero", "is_active": 1} in rows

        audit_logs = await db.list_admin_audit_logs(limit=10)
        matching = [row for row in audit_logs if row["action"] == "story.characters.update"]
        assert matching
        assert "rename:1" in matching[0]["payload_summary"]
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_admin_character_edit_api_adds_and_removes_characters(tmp_path: Path) -> None:
    db_path = tmp_path / "admin.db"
    db = DatabaseManager(db_path, migrations_dir=MIGRATIONS_DIR)
    await db.initialize()
    token = await db.create_admin_api_token("default-agent")

    stories_root = tmp_path / "stories"
    character_images_root = tmp_path / "character_images"
    story_maps_root = tmp_path / "story_maps"
    story_metadata_root = tmp_path / "story_metadata"
    _write_template_story_bundle(stories_root, STORY_ID)
    _write_template_assets(character_images_root, story_maps_root, story_metadata_root, STORY_ID)
    await import_story(
        stories_root / STORY_ID,
        db_path=db.db_path,
        force_replace=False,
        story_maps_root=story_maps_root,
    )

    app = await create_app(
        db=db,
        runtime_controller=FakeRuntimeController(),
        archive_root=tmp_path,
        admin_base_url="/admin",
        stories_root=stories_root,
        character_images_root=character_images_root,
        story_maps_root=story_maps_root,
        story_metadata_root=story_metadata_root,
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.put(
            f"/admin/api/v1/stories/{STORY_ID}/characters",
            json={
                "characters": [
                    {
                        "char_id": "",
                        "new_char_id": "new_friend",
                        "name": "新しい友人",
                        "short_description": "明るい新キャラ",
                        "goal": "主人公を助ける",
                        "worry": "まだ場所に慣れない",
                    }
                ]
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status == 200
        payload = await response.json()
        assert payload["data"]["characters_added"] == 1
        assert payload["data"]["characters_removed"] == 1

        assert db._conn is not None
        cursor = await db._conn.execute(
            "SELECT id, is_active FROM characters WHERE story_id = ? AND id IN (?, ?)",
            (STORY_ID, "test_char", "new_friend"),
        )
        rows = [dict(row) for row in await cursor.fetchall()]
        assert {"id": "test_char", "is_active": 0} in rows
        assert {"id": "new_friend", "is_active": 1} in rows
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_admin_character_edit_api_accepts_expression_image_uploads(tmp_path: Path) -> None:
    db_path = tmp_path / "admin.db"
    db = DatabaseManager(db_path, migrations_dir=MIGRATIONS_DIR)
    await db.initialize()
    token = await db.create_admin_api_token("default-agent")

    stories_root = tmp_path / "stories"
    character_images_root = tmp_path / "character_images"
    story_maps_root = tmp_path / "story_maps"
    story_metadata_root = tmp_path / "story_metadata"
    _write_template_story_bundle(stories_root, STORY_ID)
    _write_template_assets(character_images_root, story_maps_root, story_metadata_root, STORY_ID)
    await import_story(
        stories_root / STORY_ID,
        db_path=db.db_path,
        force_replace=False,
        story_maps_root=story_maps_root,
    )

    app = await create_app(
        db=db,
        runtime_controller=FakeRuntimeController(),
        archive_root=tmp_path,
        admin_base_url="/admin",
        stories_root=stories_root,
        character_images_root=character_images_root,
        story_maps_root=story_maps_root,
        story_metadata_root=story_metadata_root,
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        form = FormData()
        form.add_field(
            "payload",
            json.dumps(
                {
                    "characters": [
                        {
                            "char_id": "test_char",
                            "new_char_id": "test_char",
                            "name": "テンプレキャラ",
                            "short_description": "静かな主人公",
                            "goal": "毎日を無事に終える",
                            "worry": "少し緊張しやすい",
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            content_type="application/json",
        )
        form.add_field(
            "character_image__test_char__angry",
            b"edited-angry",
            filename="angry.png",
            content_type="image/png",
        )

        response = await client.put(
            f"/admin/api/v1/stories/{STORY_ID}/characters",
            data=form,
            headers={"Authorization": f"Bearer {token}"},
        )
        response_text = await response.text()
        assert response.status == 200, response_text
        assert (character_images_root / STORY_ID / "test_char" / "angry.png").read_bytes() == (
            b"edited-angry"
        )
        character_row = await db.get_character(STORY_ID, "test_char")
        assert character_row is not None
        assert "angry" in json.loads(character_row["expressions_available"])
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_admin_character_edit_api_rejects_oversized_expression_image_upload(
    tmp_path: Path,
) -> None:
    client, db, token, _stories_root, character_images_root, _story_maps_root, _metadata_root = (
        await _start_imported_story_admin_client(tmp_path, max_image_upload_bytes=4)
    )
    try:
        form = FormData()
        form.add_field(
            "payload",
            json.dumps(
                {
                    "characters": [
                        {
                            "char_id": "test_char",
                            "new_char_id": "test_char",
                            "name": "テンプレキャラ",
                            "short_description": "静かな主人公",
                            "goal": "毎日を無事に終える",
                            "worry": "少し緊張しやすい",
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            content_type="application/json",
        )
        form.add_field(
            "character_image__test_char__angry",
            b"12345",
            filename="angry.png",
            content_type="image/png",
        )

        response = await client.put(
            f"/admin/api/v1/stories/{STORY_ID}/characters",
            data=form,
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status == 413
        assert not (character_images_root / STORY_ID / "test_char" / "angry.png").exists()
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_admin_character_edit_api_rejects_running_story(tmp_path: Path) -> None:
    db_path = tmp_path / "admin.db"
    db = DatabaseManager(db_path, migrations_dir=MIGRATIONS_DIR)
    await db.initialize()
    token = await db.create_admin_api_token("default-agent")

    stories_root = tmp_path / "stories"
    character_images_root = tmp_path / "character_images"
    story_maps_root = tmp_path / "story_maps"
    story_metadata_root = tmp_path / "story_metadata"
    _write_template_story_bundle(stories_root, STORY_ID)
    _write_template_assets(character_images_root, story_maps_root, story_metadata_root, STORY_ID)
    await import_story(
        stories_root / STORY_ID,
        db_path=db.db_path,
        force_replace=False,
        story_maps_root=story_maps_root,
    )

    runtime = FakeRuntimeController()
    runtime.states[STORY_ID] = "running"
    app = await create_app(
        db=db,
        runtime_controller=runtime,
        archive_root=tmp_path,
        admin_base_url="/admin",
        stories_root=stories_root,
        character_images_root=character_images_root,
        story_maps_root=story_maps_root,
        story_metadata_root=story_metadata_root,
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.put(
            f"/admin/api/v1/stories/{STORY_ID}/characters",
            json={"characters": []},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status == 409
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_admin_place_edit_template_api_returns_editable_story_payload(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "admin.db"
    db = DatabaseManager(db_path, migrations_dir=MIGRATIONS_DIR)
    await db.initialize()
    token = await db.create_admin_api_token("default-agent")

    stories_root = tmp_path / "stories"
    character_images_root = tmp_path / "character_images"
    story_maps_root = tmp_path / "story_maps"
    story_metadata_root = tmp_path / "story_metadata"
    _write_template_story_bundle(stories_root, STORY_ID)
    _write_template_assets(character_images_root, story_maps_root, story_metadata_root, STORY_ID)
    await import_story(
        stories_root / STORY_ID,
        db_path=db.db_path,
        force_replace=False,
        story_maps_root=story_maps_root,
    )

    app = await create_app(
        db=db,
        runtime_controller=FakeRuntimeController(),
        archive_root=tmp_path,
        admin_base_url="/admin",
        stories_root=stories_root,
        character_images_root=character_images_root,
        story_maps_root=story_maps_root,
        story_metadata_root=story_metadata_root,
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.get(
            f"/admin/api/v1/stories/{STORY_ID}/places/edit-template",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status == 200
        payload = await response.json()
        assert payload["data"]["story"]["story_id"] == STORY_ID
        assert payload["data"]["editable"] is True
        assert payload["data"]["runtime_state"] == "stopped"
        assert payload["data"]["places"][0]["place_id"] == "classroom"
        assert payload["data"]["places"][0]["label"] == "教室"
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_admin_place_edit_api_updates_story_and_audits(tmp_path: Path) -> None:
    db_path = tmp_path / "admin.db"
    db = DatabaseManager(db_path, migrations_dir=MIGRATIONS_DIR)
    await db.initialize()
    token = await db.create_admin_api_token("default-agent")

    stories_root = tmp_path / "stories"
    character_images_root = tmp_path / "character_images"
    story_maps_root = tmp_path / "story_maps"
    story_metadata_root = tmp_path / "story_metadata"
    _write_template_story_bundle(stories_root, STORY_ID)
    _write_template_assets(character_images_root, story_maps_root, story_metadata_root, STORY_ID)
    await import_story(
        stories_root / STORY_ID,
        db_path=db.db_path,
        force_replace=False,
        story_maps_root=story_maps_root,
    )

    app = await create_app(
        db=db,
        runtime_controller=FakeRuntimeController(),
        archive_root=tmp_path,
        admin_base_url="/admin",
        stories_root=stories_root,
        character_images_root=character_images_root,
        story_maps_root=story_maps_root,
        story_metadata_root=story_metadata_root,
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.put(
            f"/admin/api/v1/stories/{STORY_ID}/places",
            json={
                "places": [
                    {
                        "place_id": "classroom",
                        "label": "朝の教室",
                        "zone": "school",
                        "atmosphere": "朝日が差し込む静かな空気",
                        "who_gathers": ["主人公", "友人"],
                        "events_likely": ["内緒話", "小さな衝突"],
                        "access_note": "授業前だけ人が少ない",
                    },
                    {"place_id": "corridor"},
                    {"place_id": "home"},
                ]
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status == 200
        payload = await response.json()
        assert payload["data"]["update_counts"]["places_updated"] == 3
        assert payload["data"]["places_added"] == 0
        assert payload["data"]["places_removed"] == 0

        assert db._conn is not None
        cursor = await db._conn.execute(
            """
            SELECT label, atmosphere
            FROM places
            WHERE story_id = ? AND id = ?
            """,
            (STORY_ID, "classroom"),
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row["label"] == "朝の教室"
        assert row["atmosphere"] == "朝日が差し込む静かな空気"

        audit_logs = await db.list_admin_audit_logs(limit=10)
        assert "story.places.update" in [row["action"] for row in audit_logs]
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_admin_place_edit_api_rejects_unknown_uploaded_place_image_key(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "admin.db"
    db = DatabaseManager(db_path, migrations_dir=MIGRATIONS_DIR)
    await db.initialize()
    token = await db.create_admin_api_token("default-agent")

    stories_root = tmp_path / "stories"
    character_images_root = tmp_path / "character_images"
    story_maps_root = tmp_path / "story_maps"
    story_metadata_root = tmp_path / "story_metadata"
    _write_template_story_bundle(stories_root, STORY_ID)
    _write_template_assets(character_images_root, story_maps_root, story_metadata_root, STORY_ID)
    await import_story(
        stories_root / STORY_ID,
        db_path=db.db_path,
        force_replace=False,
        story_maps_root=story_maps_root,
    )

    app = await create_app(
        db=db,
        runtime_controller=FakeRuntimeController(),
        archive_root=tmp_path,
        admin_base_url="/admin",
        stories_root=stories_root,
        character_images_root=character_images_root,
        story_maps_root=story_maps_root,
        story_metadata_root=story_metadata_root,
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        form = FormData()
        form.add_field(
            "payload",
            json.dumps(
                {
                    "places": [
                        {"place_id": "classroom"},
                        {"place_id": "corridor"},
                        {"place_id": "home"},
                    ]
                },
                ensure_ascii=False,
            ),
            content_type="application/json",
        )
        form.add_field(
            "place_image__ghost_place",
            b"not-a-real-place",
            filename="ghost.png",
            content_type="image/png",
        )

        response = await client.put(
            f"/admin/api/v1/stories/{STORY_ID}/places",
            data=form,
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status == 400
        payload = await response.json()
        assert "unknown uploaded place image id" in payload["error"]["message"]
        assert not (story_maps_root / STORY_ID / "images" / "ghost_place_320.png").exists()
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_admin_place_edit_api_saves_uploaded_image_under_renamed_place_id(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "admin.db"
    db = DatabaseManager(db_path, migrations_dir=MIGRATIONS_DIR)
    await db.initialize()
    token = await db.create_admin_api_token("default-agent")

    stories_root = tmp_path / "stories"
    character_images_root = tmp_path / "character_images"
    story_maps_root = tmp_path / "story_maps"
    story_metadata_root = tmp_path / "story_metadata"
    _write_template_story_bundle(stories_root, STORY_ID)
    _write_template_assets(character_images_root, story_maps_root, story_metadata_root, STORY_ID)
    await import_story(
        stories_root / STORY_ID,
        db_path=db.db_path,
        force_replace=False,
        story_maps_root=story_maps_root,
    )

    app = await create_app(
        db=db,
        runtime_controller=FakeRuntimeController(),
        archive_root=tmp_path,
        admin_base_url="/admin",
        stories_root=stories_root,
        character_images_root=character_images_root,
        story_maps_root=story_maps_root,
        story_metadata_root=story_metadata_root,
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        form = FormData()
        form.add_field(
            "payload",
            json.dumps(
                {
                    "places": [
                        {
                            "place_id": "classroom",
                            "new_place_id": "homeroom",
                            "label": "朝の教室",
                            "zone": "school",
                            "adjacent_places": {"corridor": 1},
                        },
                        {
                            "place_id": "corridor",
                            "label": "廊下",
                            "zone": "school",
                            "adjacent_places": {"homeroom": 1},
                        },
                        {"place_id": "home", "label": "自宅", "zone": "home"},
                    ]
                },
                ensure_ascii=False,
            ),
            content_type="application/json",
        )
        form.add_field(
            "place_image__homeroom",
            b"renamed-place-image",
            filename="homeroom.png",
            content_type="image/png",
        )

        response = await client.put(
            f"/admin/api/v1/stories/{STORY_ID}/places",
            data=form,
            headers={"Authorization": f"Bearer {token}"},
        )

        response_text = await response.text()
        assert response.status == 200, response_text
        assert (story_maps_root / STORY_ID / "images" / "homeroom_320.png").read_bytes() == (
            b"renamed-place-image"
        )
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_admin_place_edit_api_rejects_oversized_place_image_upload(
    tmp_path: Path,
) -> None:
    client, db, token, _stories_root, _char_root, story_maps_root, _metadata_root = (
        await _start_imported_story_admin_client(tmp_path, max_image_upload_bytes=4)
    )
    try:
        form = FormData()
        form.add_field(
            "payload",
            json.dumps(
                {
                    "places": [
                        {"place_id": "classroom"},
                        {"place_id": "corridor"},
                        {"place_id": "home"},
                    ]
                },
                ensure_ascii=False,
            ),
            content_type="application/json",
        )
        form.add_field(
            "place_image__classroom",
            b"12345",
            filename="classroom.png",
            content_type="image/png",
        )

        response = await client.put(
            f"/admin/api/v1/stories/{STORY_ID}/places",
            data=form,
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status == 413
        assert (story_maps_root / STORY_ID / "images" / "classroom_320.png").read_bytes() == (
            b"place-image"
        )
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_admin_place_edit_api_rejects_running_story(tmp_path: Path) -> None:
    db_path = tmp_path / "admin.db"
    db = DatabaseManager(db_path, migrations_dir=MIGRATIONS_DIR)
    await db.initialize()
    token = await db.create_admin_api_token("default-agent")

    stories_root = tmp_path / "stories"
    character_images_root = tmp_path / "character_images"
    story_maps_root = tmp_path / "story_maps"
    story_metadata_root = tmp_path / "story_metadata"
    _write_template_story_bundle(stories_root, STORY_ID)
    _write_template_assets(character_images_root, story_maps_root, story_metadata_root, STORY_ID)
    await import_story(
        stories_root / STORY_ID,
        db_path=db.db_path,
        force_replace=False,
        story_maps_root=story_maps_root,
    )

    runtime = FakeRuntimeController()
    runtime.states[STORY_ID] = "running"
    app = await create_app(
        db=db,
        runtime_controller=runtime,
        archive_root=tmp_path,
        admin_base_url="/admin",
        stories_root=stories_root,
        character_images_root=character_images_root,
        story_maps_root=story_maps_root,
        story_metadata_root=story_metadata_root,
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.put(
            f"/admin/api/v1/stories/{STORY_ID}/places",
            json={"places": []},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status == 409
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_admin_story_definition_template_api_returns_editable_payload(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "admin.db"
    db = DatabaseManager(db_path, migrations_dir=MIGRATIONS_DIR)
    await db.initialize()
    token = await db.create_admin_api_token("default-agent")

    stories_root = tmp_path / "stories"
    character_images_root = tmp_path / "character_images"
    story_maps_root = tmp_path / "story_maps"
    story_metadata_root = tmp_path / "story_metadata"
    _write_template_story_bundle(stories_root, STORY_ID)
    _write_template_assets(character_images_root, story_maps_root, story_metadata_root, STORY_ID)
    await import_story(
        stories_root / STORY_ID,
        db_path=db.db_path,
        force_replace=False,
        story_maps_root=story_maps_root,
    )

    app = await create_app(
        db=db,
        runtime_controller=FakeRuntimeController(),
        archive_root=tmp_path,
        admin_base_url="/admin",
        stories_root=stories_root,
        character_images_root=character_images_root,
        story_maps_root=story_maps_root,
        story_metadata_root=story_metadata_root,
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.get(
            f"/admin/api/v1/stories/{STORY_ID}/definition/edit-template",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status == 200
        payload = await response.json()
        assert payload["data"]["story"]["story_id"] == STORY_ID
        assert payload["data"]["story"]["title"] == "テンプレート"
        assert payload["data"]["story"]["season_start"] == "2025-04-01"
        assert payload["data"]["story"]["turn_minutes"] == 30
        assert payload["data"]["story"]["turn_interval_sec"] == 45
        assert payload["data"]["editable"] is True
        assert payload["data"]["runtime_state"] == "stopped"
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_admin_story_definition_update_api_updates_story_and_audits(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "admin.db"
    db = DatabaseManager(db_path, migrations_dir=MIGRATIONS_DIR)
    await db.initialize()
    token = await db.create_admin_api_token("default-agent")

    stories_root = tmp_path / "stories"
    character_images_root = tmp_path / "character_images"
    story_maps_root = tmp_path / "story_maps"
    story_metadata_root = tmp_path / "story_metadata"
    _write_template_story_bundle(stories_root, STORY_ID)
    _write_template_assets(character_images_root, story_maps_root, story_metadata_root, STORY_ID)
    await import_story(
        stories_root / STORY_ID,
        db_path=db.db_path,
        force_replace=False,
        story_maps_root=story_maps_root,
    )

    app = await create_app(
        db=db,
        runtime_controller=FakeRuntimeController(),
        archive_root=tmp_path,
        admin_base_url="/admin",
        stories_root=stories_root,
        character_images_root=character_images_root,
        story_maps_root=story_maps_root,
        story_metadata_root=story_metadata_root,
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.put(
            f"/admin/api/v1/stories/{STORY_ID}/definition",
            json={
                "story": {
                    "title": "編集後タイトル",
                    "description": "編集後の説明",
                    "season_start": "2025-05-01",
                    "turn_minutes": 15,
                    "turn_interval_sec": 5,
                    "world_rules": "編集後の世界ルール",
                }
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status == 200
        payload = await response.json()
        assert payload["data"]["update_counts"]["stories"] == 1

        story_row = await db.get_story(STORY_ID)
        assert story_row is not None
        assert story_row["title"] == "編集後タイトル"
        assert story_row["description"] == "編集後の説明"
        assert story_row["season_start"] == "2025-05-01"
        assert story_row["turn_minutes"] == 15
        assert story_row["turn_interval_sec"] == 5
        assert json.loads(story_row["world_rules"]) == "編集後の世界ルール"

        audit_logs = await db.list_admin_audit_logs(limit=10)
        assert "story.definition.update" in [row["action"] for row in audit_logs]
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_admin_story_definition_update_api_rejects_running_story(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "admin.db"
    db = DatabaseManager(db_path, migrations_dir=MIGRATIONS_DIR)
    await db.initialize()
    token = await db.create_admin_api_token("default-agent")

    stories_root = tmp_path / "stories"
    character_images_root = tmp_path / "character_images"
    story_maps_root = tmp_path / "story_maps"
    story_metadata_root = tmp_path / "story_metadata"
    _write_template_story_bundle(stories_root, STORY_ID)
    _write_template_assets(character_images_root, story_maps_root, story_metadata_root, STORY_ID)
    await import_story(
        stories_root / STORY_ID,
        db_path=db.db_path,
        force_replace=False,
        story_maps_root=story_maps_root,
    )

    runtime = FakeRuntimeController()
    runtime.states[STORY_ID] = "running"
    app = await create_app(
        db=db,
        runtime_controller=runtime,
        archive_root=tmp_path,
        admin_base_url="/admin",
        stories_root=stories_root,
        character_images_root=character_images_root,
        story_maps_root=story_maps_root,
        story_metadata_root=story_metadata_root,
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.put(
            f"/admin/api/v1/stories/{STORY_ID}/definition",
            json={"story": {"title": "稼働中更新"}},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status == 409
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_admin_event_anomaly_template_api_returns_editable_payload(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "admin.db"
    db = DatabaseManager(db_path, migrations_dir=MIGRATIONS_DIR)
    await db.initialize()
    token = await db.create_admin_api_token("default-agent")

    stories_root = tmp_path / "stories"
    character_images_root = tmp_path / "character_images"
    story_maps_root = tmp_path / "story_maps"
    story_metadata_root = tmp_path / "story_metadata"
    _write_template_story_bundle(stories_root, STORY_ID)
    _write_template_assets(character_images_root, story_maps_root, story_metadata_root, STORY_ID)
    await import_story(
        stories_root / STORY_ID,
        db_path=db.db_path,
        force_replace=False,
        story_maps_root=story_maps_root,
    )

    app = await create_app(
        db=db,
        runtime_controller=FakeRuntimeController(),
        archive_root=tmp_path,
        admin_base_url="/admin",
        stories_root=stories_root,
        character_images_root=character_images_root,
        story_maps_root=story_maps_root,
        story_metadata_root=story_metadata_root,
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.get(
            f"/admin/api/v1/stories/{STORY_ID}/event-anomalies/edit-template",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status == 200
        payload = await response.json()
        assert payload["data"]["story"]["story_id"] == STORY_ID
        assert payload["data"]["editable"] is True
        assert payload["data"]["runtime_state"] == "stopped"
        assert payload["data"]["events"][0]["event_key"] == "04-01:入学式"
        assert payload["data"]["events"][0]["name"] == "入学式"
        assert payload["data"]["anomalies"][0]["anomaly_key"] == "深夜の廊下"
        assert payload["data"]["places"][0]["place_id"] == "classroom"
        assert payload["data"]["places"][0]["zone"] == "school"
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_admin_event_anomaly_update_api_updates_story_and_audits(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "admin.db"
    db = DatabaseManager(db_path, migrations_dir=MIGRATIONS_DIR)
    await db.initialize()
    token = await db.create_admin_api_token("default-agent")

    stories_root = tmp_path / "stories"
    character_images_root = tmp_path / "character_images"
    story_maps_root = tmp_path / "story_maps"
    story_metadata_root = tmp_path / "story_metadata"
    _write_template_story_bundle(stories_root, STORY_ID)
    _write_template_assets(character_images_root, story_maps_root, story_metadata_root, STORY_ID)
    await import_story(
        stories_root / STORY_ID,
        db_path=db.db_path,
        force_replace=False,
        story_maps_root=story_maps_root,
    )

    app = await create_app(
        db=db,
        runtime_controller=FakeRuntimeController(),
        archive_root=tmp_path,
        admin_base_url="/admin",
        stories_root=stories_root,
        character_images_root=character_images_root,
        story_maps_root=story_maps_root,
        story_metadata_root=story_metadata_root,
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.put(
            f"/admin/api/v1/stories/{STORY_ID}/event-anomalies",
            json={
                "events": [
                    {
                        "event_key": "04-01:入学式",
                        "event_date": "04-02",
                        "name": "部活動紹介",
                        "duration_days": 2,
                        "atmosphere": "新しい出会いで校内が浮き立つ",
                        "emotion_impact": {"excitement": 0.4, "stress": 0.1},
                        "force_place": "classroom",
                    }
                ],
                "anomalies": [
                    {
                        "anomaly_key": "深夜の廊下",
                        "label": "深夜の教室",
                        "condition_json": {"place": "classroom", "time_from": "21:00"},
                        "drama_potential": "誰かの本音が漏れる",
                        "suggested_reasons": ["秘密の相談", "忘れ物を探している"],
                    }
                ],
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status == 200
        payload = await response.json()
        assert payload["data"]["update_counts"]["event_calendar"] == 1
        assert payload["data"]["update_counts"]["anomaly_rules"] == 1

        assert db._conn is not None
        cursor = await db._conn.execute(
            """
            SELECT event_date, name
            FROM event_calendar
            WHERE story_id = ?
            """,
            (STORY_ID,),
        )
        event_row = await cursor.fetchone()
        assert event_row is not None
        assert event_row["event_date"] == "04-02"
        assert event_row["name"] == "部活動紹介"

        audit_logs = await db.list_admin_audit_logs(limit=10)
        assert "story.event_anomalies.update" in [row["action"] for row in audit_logs]
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_admin_event_anomaly_update_api_rejects_running_story(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "admin.db"
    db = DatabaseManager(db_path, migrations_dir=MIGRATIONS_DIR)
    await db.initialize()
    token = await db.create_admin_api_token("default-agent")

    stories_root = tmp_path / "stories"
    character_images_root = tmp_path / "character_images"
    story_maps_root = tmp_path / "story_maps"
    story_metadata_root = tmp_path / "story_metadata"
    _write_template_story_bundle(stories_root, STORY_ID)
    _write_template_assets(character_images_root, story_maps_root, story_metadata_root, STORY_ID)
    await import_story(
        stories_root / STORY_ID,
        db_path=db.db_path,
        force_replace=False,
        story_maps_root=story_maps_root,
    )

    runtime = FakeRuntimeController()
    runtime.states[STORY_ID] = "running"
    app = await create_app(
        db=db,
        runtime_controller=runtime,
        archive_root=tmp_path,
        admin_base_url="/admin",
        stories_root=stories_root,
        character_images_root=character_images_root,
        story_maps_root=story_maps_root,
        story_metadata_root=story_metadata_root,
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.put(
            f"/admin/api/v1/stories/{STORY_ID}/event-anomalies",
            json={"events": [], "anomalies": []},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status == 409
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_admin_chapter_overview_api_returns_chapters_beats_and_director(
    tmp_path: Path,
) -> None:
    db = DatabaseManager(":memory:", migrations_dir=MIGRATIONS_DIR)
    await db.initialize()
    assert db._conn is not None
    await db._conn.execute(
        """
        INSERT INTO stories (id, title, llm_provider, llm_model)
        VALUES (?, ?, ?, ?)
        """,
        (STORY_ID, "管理UIテスト", "openai", "gpt-4o"),
    )
    await db._conn.commit()
    token = await db.create_admin_api_token("default-agent")

    await db.upsert_director_persona(
        STORY_ID,
        {
            "persona_id": "default_director",
            "name": "標準監督",
            "aesthetic_json": {"tension_preference": 0.5},
            "values_json": ["誠実"],
            "traits_json": ["観察型"],
            "is_active": 1,
        },
    )
    await db.upsert_director_persona(
        STORY_ID,
        {
            "persona_id": "bright_director",
            "name": "明るい監督",
            "aesthetic_json": {"tension_preference": 0.2},
            "values_json": ["軽やか"],
            "traits_json": ["テンポ重視"],
            "is_active": 0,
        },
    )
    await db.insert_director_swap_log(
        STORY_ID,
        {
            "turn_number": 3,
            "from_persona_id": None,
            "to_persona_id": "default_director",
            "reason": "初回設定",
        },
    )
    active_id = await db.insert_chapter(
        STORY_ID,
        {
            "chapter_id": "ch_active",
            "title": "進行中",
            "theme": "現在",
            "world_injection": "進行中の章",
            "status": "pending",
            "start_condition": "manual",
            "current_beat": "setup",
        },
    )
    await db.insert_chapter_beat(
        active_id,
        {
            "phase": "setup",
            "description": "導入",
            "goal": "参加表明",
            "events_json": [{"type": "notice", "desc": "集合"}],
        },
    )
    await db.activate_chapter(active_id, opened_turn=8)
    pending_id = await db.insert_chapter(
        STORY_ID,
        {
            "chapter_id": "ch_pending",
            "title": "待機中",
            "theme": "次",
            "world_injection": "",
            "status": "pending",
            "start_condition": "manual",
            "current_beat": "setup",
        },
    )
    closed_id = await db.insert_chapter(
        STORY_ID,
        {
            "chapter_id": "ch_closed",
            "title": "終了済み",
            "theme": "過去",
            "world_injection": "",
            "status": "pending",
            "start_condition": "manual",
            "current_beat": "resolution",
        },
    )
    await db.close_chapter(closed_id, closed_turn=12, reason="completed", carry_over={})

    app = await create_app(
        db=db,
        runtime_controller=FakeRuntimeController(),
        archive_root=tmp_path,
        admin_base_url="/admin",
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.get(
            f"/admin/api/v1/stories/{STORY_ID}/chapters",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status == 200
        payload = await response.json()
        data = payload["data"]
        assert data["runtime_state"] == "stopped"
        assert data["active_director_persona"]["persona_id"] == "default_director"
        assert [persona["persona_id"] for persona in data["director_personas"]] == [
            "bright_director",
            "default_director",
        ]
        assert data["director_swap_history"][0]["to_persona_id"] == "default_director"
        assert data["active_chapter"]["id"] == active_id
        assert data["active_chapter"]["beats"][0]["phase"] == "setup"
        assert data["active_chapter"]["beats"][0]["events_json"] == [
            {"type": "notice", "desc": "集合"}
        ]
        assert [chapter["id"] for chapter in data["pending_chapters"]] == [pending_id]
        assert [chapter["id"] for chapter in data["closed_chapters"]] == [closed_id]
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_admin_director_persona_edit_api_updates_personas(tmp_path: Path) -> None:
    db_path = tmp_path / "admin.db"
    db = DatabaseManager(db_path, migrations_dir=MIGRATIONS_DIR)
    await db.initialize()
    token = await db.create_admin_api_token("default-agent")

    stories_root = tmp_path / "stories"
    character_images_root = tmp_path / "character_images"
    story_maps_root = tmp_path / "story_maps"
    story_metadata_root = tmp_path / "story_metadata"
    _write_template_story_bundle(stories_root, STORY_ID)
    _write_template_assets(character_images_root, story_maps_root, story_metadata_root, STORY_ID)
    await import_story(
        stories_root / STORY_ID,
        db_path=db.db_path,
        force_replace=False,
        story_maps_root=story_maps_root,
    )
    await db.insert_director_swap_log(
        STORY_ID,
        {
            "turn_number": 7,
            "from_persona_id": None,
            "to_persona_id": "default_director",
            "reason": "初期監督",
        },
    )

    app = await create_app(
        db=db,
        runtime_controller=FakeRuntimeController(),
        archive_root=tmp_path,
        admin_base_url="/admin",
        stories_root=stories_root,
        character_images_root=character_images_root,
        story_maps_root=story_maps_root,
        story_metadata_root=story_metadata_root,
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        template_response = await client.get(
            f"/admin/api/v1/stories/{STORY_ID}/director-personas/edit-template",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert template_response.status == 200
        template_payload = await template_response.json()
        assert template_payload["data"]["personas"][0]["persona_id"] == "default_director"
        assert template_payload["data"]["runtime_state"] == "stopped"
        assert template_payload["data"]["active_director_persona"]["persona_id"] == (
            "default_director"
        )
        assert template_payload["data"]["director_personas"][0]["persona_id"] == (
            "default_director"
        )
        assert template_payload["data"]["director_swap_history"][0]["to_persona_id"] == (
            "default_director"
        )

        response = await client.put(
            f"/admin/api/v1/stories/{STORY_ID}/director-personas",
            json={
                "default_active": "bright_director",
                "personas": [
                    {
                        "persona_id": "default_director",
                        "name": "標準監督 改",
                        "aesthetic": {"tension_preference": 0.6},
                        "values": ["誠実"],
                        "traits": ["観察型"],
                    },
                    {
                        "persona_id": "bright_director",
                        "name": "明るい監督",
                        "aesthetic": {"tension_preference": 0.2},
                        "values": ["軽やか"],
                        "traits": ["テンポ重視"],
                    },
                ],
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status == 200
        payload = await response.json()
        assert payload["data"]["persona_count"] == 2
        assert payload["data"]["personas_added"] == 1
        active = await db.get_active_director_persona(STORY_ID)
        assert active is not None
        assert active["persona_id"] == "bright_director"
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_admin_chapter_definition_edit_api_updates_chapters(tmp_path: Path) -> None:
    db_path = tmp_path / "admin.db"
    db = DatabaseManager(db_path, migrations_dir=MIGRATIONS_DIR)
    await db.initialize()
    token = await db.create_admin_api_token("default-agent")

    stories_root = tmp_path / "stories"
    character_images_root = tmp_path / "character_images"
    story_maps_root = tmp_path / "story_maps"
    story_metadata_root = tmp_path / "story_metadata"
    _write_template_story_bundle(stories_root, STORY_ID)
    _write_template_assets(character_images_root, story_maps_root, story_metadata_root, STORY_ID)
    await import_story(
        stories_root / STORY_ID,
        db_path=db.db_path,
        force_replace=False,
        story_maps_root=story_maps_root,
    )

    app = await create_app(
        db=db,
        runtime_controller=FakeRuntimeController(),
        archive_root=tmp_path,
        admin_base_url="/admin",
        stories_root=stories_root,
        character_images_root=character_images_root,
        story_maps_root=story_maps_root,
        story_metadata_root=story_metadata_root,
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        template_response = await client.get(
            f"/admin/api/v1/stories/{STORY_ID}/chapter-definitions/edit-template",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert template_response.status == 200
        template_payload = await template_response.json()
        assert template_payload["data"]["chapters"][0]["chapter_id"] == "ch001"

        response = await client.put(
            f"/admin/api/v1/stories/{STORY_ID}/chapter-definitions",
            json={
                "chapters": [
                    {
                        "chapter_id": "ch001",
                        "title": "導入 改",
                        "theme": "緊張",
                        "start_condition": "manual",
                        "beats": [{"phase": "setup", "description": "開始", "events": []}],
                    },
                    {
                        "chapter_id": "ch002",
                        "title": "放課後",
                        "theme": "対立",
                        "start_condition": "manual",
                        "beats": [{"phase": "setup", "goal": "争点を出す", "events": []}],
                    },
                ]
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status == 200
        payload = await response.json()
        assert payload["data"]["chapter_count"] == 2
        assert payload["data"]["chapters_added"] == 1
        chapters = await db.get_pending_chapters(STORY_ID)
        assert [chapter["chapter_id"] for chapter in chapters] == ["ch001", "ch002"]
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_admin_chapter_definition_edit_api_rejects_invalid_beat_events(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "admin.db"
    db = DatabaseManager(db_path, migrations_dir=MIGRATIONS_DIR)
    await db.initialize()
    token = await db.create_admin_api_token("default-agent")

    stories_root = tmp_path / "stories"
    character_images_root = tmp_path / "character_images"
    story_maps_root = tmp_path / "story_maps"
    story_metadata_root = tmp_path / "story_metadata"
    _write_template_story_bundle(stories_root, STORY_ID)
    _write_template_assets(character_images_root, story_maps_root, story_metadata_root, STORY_ID)
    await import_story(
        stories_root / STORY_ID,
        db_path=db.db_path,
        force_replace=False,
        story_maps_root=story_maps_root,
    )

    app = await create_app(
        db=db,
        runtime_controller=FakeRuntimeController(),
        archive_root=tmp_path,
        admin_base_url="/admin",
        stories_root=stories_root,
        character_images_root=character_images_root,
        story_maps_root=story_maps_root,
        story_metadata_root=story_metadata_root,
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.put(
            f"/admin/api/v1/stories/{STORY_ID}/chapter-definitions",
            json={
                "chapters": [
                    {
                        "chapter_id": "ch001",
                        "title": "導入",
                        "beats": [
                            {
                                "phase": "setup",
                                "events": [{"type": "notice", "priority": "high"}],
                            }
                        ],
                    }
                ]
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status == 400
        payload = await response.json()
        assert payload["error"]["message"] == "event priority must be an integer"
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_admin_director_persona_activate_api_switches_persona_and_logs_swap(
    tmp_path: Path,
) -> None:
    db = DatabaseManager(":memory:", migrations_dir=MIGRATIONS_DIR)
    await db.initialize()
    assert db._conn is not None
    await db._conn.execute(
        """
        INSERT INTO stories (id, title, llm_provider, llm_model)
        VALUES (?, ?, ?, ?)
        """,
        (STORY_ID, "管理UIテスト", "openai", "gpt-4o"),
    )
    await db._conn.commit()
    token = await db.create_admin_api_token("default-agent")
    await db.upsert_director_persona(
        STORY_ID,
        {
            "persona_id": "default_director",
            "name": "標準監督",
            "aesthetic_json": {"tension_preference": 0.5},
            "values_json": ["誠実"],
            "traits_json": ["観察型"],
            "is_active": 1,
        },
    )
    await db.upsert_director_persona(
        STORY_ID,
        {
            "persona_id": "bright_director",
            "name": "明るい監督",
            "aesthetic_json": {"tension_preference": 0.2},
            "values_json": ["軽やか"],
            "traits_json": ["テンポ重視"],
            "is_active": 0,
        },
    )

    runtime = FakeRuntimeController()
    app = await create_app(
        db=db,
        runtime_controller=runtime,
        archive_root=tmp_path,
        admin_base_url="/admin",
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.put(
            f"/admin/api/v1/stories/{STORY_ID}/director-personas/bright_director/activate",
            headers={"Authorization": f"Bearer {token}"},
            json={"turn_number": 42, "reason": "テンポを上げたい"},
        )
        assert response.status == 200
        payload = await response.json()
        assert payload["data"]["from_persona_id"] == "default_director"
        assert payload["data"]["to_persona_id"] == "bright_director"
        assert payload["data"]["runtime_reloaded"] is False
        active = await db.get_active_director_persona(STORY_ID)
        assert active is not None
        assert active["persona_id"] == "bright_director"
        swap_history = await db.get_director_swap_history(STORY_ID)
        assert swap_history[0]["turn_number"] == 42
        assert swap_history[0]["from_persona_id"] == "default_director"
        assert swap_history[0]["to_persona_id"] == "bright_director"
        assert swap_history[0]["reason"] == "テンポを上げたい"
        assert runtime.director_reload_story_ids == [STORY_ID]
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_revoked_bearer_token_is_rejected(tmp_path: Path) -> None:
    db = DatabaseManager(":memory:", migrations_dir=MIGRATIONS_DIR)
    await db.initialize()
    assert db._conn is not None
    await db._conn.execute(
        """
        INSERT INTO stories (id, title, llm_provider, llm_model)
        VALUES (?, ?, ?, ?)
        """,
        (STORY_ID, "管理UIテスト", "openai", "gpt-4o"),
    )
    await db._conn.commit()
    token = await db.create_admin_api_token("default-agent")
    token_row = await db.authenticate_admin_api_token(token)
    assert token_row is not None
    await db.revoke_admin_api_token(token_row["id"])

    app = await create_app(
        db=db,
        runtime_controller=FakeRuntimeController(),
        archive_root=tmp_path,
        admin_base_url="/admin",
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.get(
            "/admin/api/v1/stories",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status == 401
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_activate_manual_chapter_after_proposal_approval(tmp_path: Path) -> None:
    db = DatabaseManager(":memory:", migrations_dir=MIGRATIONS_DIR)
    await db.initialize()
    assert db._conn is not None
    await db._conn.execute(
        """
        INSERT INTO stories (id, title, llm_provider, llm_model)
        VALUES (?, ?, ?, ?)
        """,
        (STORY_ID, "管理UIテスト", "openai", "gpt-4o"),
    )
    await db._conn.commit()
    token = await db.create_admin_api_token("default-agent")

    proposal_id = await db.insert_chapter_proposal(
        STORY_ID,
        {
            "theme": "文化祭",
            "proposed_at_turn": 12,
            "proposed_chapter_json": {
                "chapter_id": "proposal_1",
                "title": "文化祭編",
                "theme": "文化祭",
                "world_injection": "準備が始まる。",
                "start_condition": "manual",
                "beats": [
                    {
                        "phase": "setup",
                        "description": "準備",
                        "goal": "参加表明",
                        "events_json": [],
                    }
                ],
            },
            "conflict_seeds_json": [],
            "generated_by_persona_id": None,
        },
    )

    app = await create_app(
        db=db,
        runtime_controller=FakeRuntimeController(),
        archive_root=tmp_path,
        admin_base_url="/admin",
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        approve_response = await client.put(
            f"/admin/api/v1/stories/{STORY_ID}/chapter-proposals/{proposal_id}/approve",
            json={"turn_number": 12},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert approve_response.status == 201
        approve_payload = await approve_response.json()
        chapter_db_id = approve_payload["data"]["chapter_db_id"]

        activate_response = await client.post(
            f"/admin/api/v1/stories/{STORY_ID}/chapters/{chapter_db_id}/activate",
            json={"turn_number": 13},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert activate_response.status == 201
        activate_payload = await activate_response.json()
        assert activate_payload["data"]["status"] == "active"
        assert activate_payload["data"]["opened_turn"] == 13

        active_chapter = await db.get_active_chapter(STORY_ID)
        assert active_chapter is not None
        assert active_chapter["id"] == chapter_db_id
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_activate_manual_chapter_returns_409_when_active_exists(tmp_path: Path) -> None:
    db = DatabaseManager(":memory:", migrations_dir=MIGRATIONS_DIR)
    await db.initialize()
    assert db._conn is not None
    await db._conn.execute(
        """
        INSERT INTO stories (id, title, llm_provider, llm_model)
        VALUES (?, ?, ?, ?)
        """,
        (STORY_ID, "管理UIテスト", "openai", "gpt-4o"),
    )
    await db._conn.commit()
    token = await db.create_admin_api_token("default-agent")

    active_chapter_id = await db.insert_chapter(
        STORY_ID,
        {
            "chapter_id": "ch_active",
            "title": "進行中",
            "theme": "現在",
            "world_injection": "",
            "status": "pending",
            "start_condition": None,
            "current_beat": "setup",
        },
    )
    await db.activate_chapter(active_chapter_id, opened_turn=5)

    pending_chapter_id = await db.insert_chapter(
        STORY_ID,
        {
            "chapter_id": "ch_pending",
            "title": "待機中",
            "theme": "次",
            "world_injection": "",
            "status": "pending",
            "start_condition": "manual",
            "current_beat": "setup",
        },
    )

    app = await create_app(
        db=db,
        runtime_controller=FakeRuntimeController(),
        archive_root=tmp_path,
        admin_base_url="/admin",
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        activate_response = await client.post(
            f"/admin/api/v1/stories/{STORY_ID}/chapters/{pending_chapter_id}/activate",
            json={"turn_number": 13},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert activate_response.status == 409
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_admin_can_close_active_chapter(tmp_path: Path) -> None:
    db = DatabaseManager(":memory:", migrations_dir=MIGRATIONS_DIR)
    await db.initialize()
    assert db._conn is not None
    await db._conn.execute(
        """
        INSERT INTO stories (id, title, llm_provider, llm_model)
        VALUES (?, ?, ?, ?)
        """,
        (STORY_ID, "管理UIテスト", "openai", "gpt-4o"),
    )
    await db._conn.commit()
    token = await db.create_admin_api_token("default-agent")

    chapter_id = await db.insert_chapter(
        STORY_ID,
        {
            "chapter_id": "ch_active",
            "title": "進行中",
            "theme": "現在",
            "world_injection": "",
            "status": "pending",
            "start_condition": "manual",
            "current_beat": "climax",
        },
    )
    await db.activate_chapter(chapter_id, opened_turn=20)

    app = await create_app(
        db=db,
        runtime_controller=FakeRuntimeController(),
        archive_root=tmp_path,
        admin_base_url="/admin",
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.post(
            f"/admin/api/v1/stories/{STORY_ID}/chapters/{chapter_id}/close",
            json={
                "turn_number": 24,
                "reason": "manual_admin",
                "carry_over": {"next_hint": "余韻を残す"},
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status == 200
        payload = await response.json()
        assert payload["data"]["status"] == "closed"
        assert payload["data"]["closed_turn"] == 24

        chapter = await db.get_story_chapter(chapter_id)
        assert chapter is not None
        assert chapter["status"] == "closed"
        assert chapter["closed_turn"] == 24
        assert chapter["close_reason"] == "manual_admin"
        assert chapter["carry_over_json"] == {"next_hint": "余韻を残す"}
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_admin_can_reopen_closed_chapter_when_no_active_exists(tmp_path: Path) -> None:
    db = DatabaseManager(":memory:", migrations_dir=MIGRATIONS_DIR)
    await db.initialize()
    assert db._conn is not None
    await db._conn.execute(
        """
        INSERT INTO stories (id, title, llm_provider, llm_model)
        VALUES (?, ?, ?, ?)
        """,
        (STORY_ID, "管理UIテスト", "openai", "gpt-4o"),
    )
    await db._conn.commit()
    token = await db.create_admin_api_token("default-agent")

    chapter_id = await db.insert_chapter(
        STORY_ID,
        {
            "chapter_id": "ch_closed",
            "title": "終了済み",
            "theme": "過去",
            "world_injection": "",
            "status": "pending",
            "start_condition": "manual",
            "current_beat": "resolution",
        },
    )
    await db.close_chapter(
        chapter_id,
        closed_turn=18,
        reason="completed",
        carry_over={"old": "note"},
    )

    app = await create_app(
        db=db,
        runtime_controller=FakeRuntimeController(),
        archive_root=tmp_path,
        admin_base_url="/admin",
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.post(
            f"/admin/api/v1/stories/{STORY_ID}/chapters/{chapter_id}/reopen",
            json={"turn_number": 25},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status == 200
        payload = await response.json()
        assert payload["data"]["status"] == "active"
        assert payload["data"]["opened_turn"] == 25

        chapter = await db.get_story_chapter(chapter_id)
        assert chapter is not None
        assert chapter["status"] == "active"
        assert chapter["opened_turn"] == 25
        assert chapter["closed_turn"] is None
        assert chapter["close_reason"] is None
        assert chapter["carry_over_json"] == {}
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_admin_reopen_closed_chapter_returns_409_when_active_exists(tmp_path: Path) -> None:
    db = DatabaseManager(":memory:", migrations_dir=MIGRATIONS_DIR)
    await db.initialize()
    assert db._conn is not None
    await db._conn.execute(
        """
        INSERT INTO stories (id, title, llm_provider, llm_model)
        VALUES (?, ?, ?, ?)
        """,
        (STORY_ID, "管理UIテスト", "openai", "gpt-4o"),
    )
    await db._conn.commit()
    token = await db.create_admin_api_token("default-agent")

    active_id = await db.insert_chapter(
        STORY_ID,
        {
            "chapter_id": "ch_active",
            "title": "進行中",
            "theme": "現在",
            "world_injection": "",
            "status": "pending",
            "start_condition": "manual",
            "current_beat": "setup",
        },
    )
    await db.activate_chapter(active_id, opened_turn=20)
    closed_id = await db.insert_chapter(
        STORY_ID,
        {
            "chapter_id": "ch_closed",
            "title": "終了済み",
            "theme": "過去",
            "world_injection": "",
            "status": "pending",
            "start_condition": "manual",
            "current_beat": "resolution",
        },
    )
    await db.close_chapter(closed_id, closed_turn=18, reason="completed", carry_over={})

    app = await create_app(
        db=db,
        runtime_controller=FakeRuntimeController(),
        archive_root=tmp_path,
        admin_base_url="/admin",
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.post(
            f"/admin/api/v1/stories/{STORY_ID}/chapters/{closed_id}/reopen",
            json={"turn_number": 25},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status == 409
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_admin_can_update_active_chapter_current_beat(tmp_path: Path) -> None:
    db = DatabaseManager(":memory:", migrations_dir=MIGRATIONS_DIR)
    await db.initialize()
    assert db._conn is not None
    await db._conn.execute(
        """
        INSERT INTO stories (id, title, llm_provider, llm_model)
        VALUES (?, ?, ?, ?)
        """,
        (STORY_ID, "管理UIテスト", "openai", "gpt-4o"),
    )
    await db._conn.commit()
    token = await db.create_admin_api_token("default-agent")

    chapter_id = await db.insert_chapter(
        STORY_ID,
        {
            "chapter_id": "ch_active",
            "title": "進行中",
            "theme": "現在",
            "world_injection": "",
            "status": "pending",
            "start_condition": "manual",
            "current_beat": "setup",
        },
    )
    await db.insert_chapter_beat(
        chapter_id,
        {"phase": "setup", "description": "導入", "goal": "集まる", "events_json": []},
    )
    await db.insert_chapter_beat(
        chapter_id,
        {"phase": "climax", "description": "山場", "goal": "対決する", "events_json": []},
    )
    await db.activate_chapter(chapter_id, opened_turn=20)

    app = await create_app(
        db=db,
        runtime_controller=FakeRuntimeController(),
        archive_root=tmp_path,
        admin_base_url="/admin",
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.put(
            f"/admin/api/v1/stories/{STORY_ID}/chapters/{chapter_id}/current-beat",
            json={"current_beat": "climax"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status == 200
        payload = await response.json()
        assert payload["data"]["current_beat"] == "climax"

        chapter = await db.get_story_chapter(chapter_id)
        assert chapter is not None
        assert chapter["current_beat"] == "climax"
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_admin_update_current_beat_rejects_unknown_phase(tmp_path: Path) -> None:
    db = DatabaseManager(":memory:", migrations_dir=MIGRATIONS_DIR)
    await db.initialize()
    assert db._conn is not None
    await db._conn.execute(
        """
        INSERT INTO stories (id, title, llm_provider, llm_model)
        VALUES (?, ?, ?, ?)
        """,
        (STORY_ID, "管理UIテスト", "openai", "gpt-4o"),
    )
    await db._conn.commit()
    token = await db.create_admin_api_token("default-agent")

    chapter_id = await db.insert_chapter(
        STORY_ID,
        {
            "chapter_id": "ch_active",
            "title": "進行中",
            "theme": "現在",
            "world_injection": "",
            "status": "pending",
            "start_condition": "manual",
            "current_beat": "setup",
        },
    )
    await db.insert_chapter_beat(
        chapter_id,
        {"phase": "setup", "description": "導入", "goal": "集まる", "events_json": []},
    )
    await db.activate_chapter(chapter_id, opened_turn=20)

    app = await create_app(
        db=db,
        runtime_controller=FakeRuntimeController(),
        archive_root=tmp_path,
        admin_base_url="/admin",
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.put(
            f"/admin/api/v1/stories/{STORY_ID}/chapters/{chapter_id}/current-beat",
            json={"current_beat": "missing"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status == 400
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_admin_place_edit_api_adds_new_place(tmp_path: Path) -> None:
    db_path = tmp_path / "admin.db"
    db = DatabaseManager(db_path, migrations_dir=MIGRATIONS_DIR)
    await db.initialize()
    token = await db.create_admin_api_token("default-agent")

    stories_root = tmp_path / "stories"
    character_images_root = tmp_path / "character_images"
    story_maps_root = tmp_path / "story_maps"
    story_metadata_root = tmp_path / "story_metadata"
    _write_template_story_bundle(stories_root, STORY_ID)
    _write_template_assets(character_images_root, story_maps_root, story_metadata_root, STORY_ID)
    await import_story(
        stories_root / STORY_ID,
        db_path=db.db_path,
        force_replace=False,
        story_maps_root=story_maps_root,
    )

    app = await create_app(
        db=db,
        runtime_controller=FakeRuntimeController(),
        archive_root=tmp_path,
        admin_base_url="/admin",
        stories_root=stories_root,
        character_images_root=character_images_root,
        story_maps_root=story_maps_root,
        story_metadata_root=story_metadata_root,
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.put(
            f"/admin/api/v1/stories/{STORY_ID}/places",
            json={
                "places": [
                    {"place_id": "classroom"},
                    {"place_id": "corridor"},
                    {"place_id": "home"},
                    {"place_id": "garden", "label": "中庭", "zone": "school"},
                ]
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status == 200
        payload = await response.json()
        assert payload["data"]["places_added"] == 1
        assert payload["data"]["places_removed"] == 0
        assert payload["data"]["place_count"] == 4
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_admin_place_edit_api_renames_place_and_updates_references(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "admin.db"
    db = DatabaseManager(db_path, migrations_dir=MIGRATIONS_DIR)
    await db.initialize()
    token = await db.create_admin_api_token("default-agent")

    stories_root = tmp_path / "stories"
    character_images_root = tmp_path / "character_images"
    story_maps_root = tmp_path / "story_maps"
    story_metadata_root = tmp_path / "story_metadata"
    _write_template_story_bundle(stories_root, STORY_ID)
    _write_template_assets(character_images_root, story_maps_root, story_metadata_root, STORY_ID)
    await import_story(
        stories_root / STORY_ID,
        db_path=db.db_path,
        force_replace=False,
        story_maps_root=story_maps_root,
    )

    app = await create_app(
        db=db,
        runtime_controller=FakeRuntimeController(),
        archive_root=tmp_path,
        admin_base_url="/admin",
        stories_root=stories_root,
        character_images_root=character_images_root,
        story_maps_root=story_maps_root,
        story_metadata_root=story_metadata_root,
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.put(
            f"/admin/api/v1/stories/{STORY_ID}/places",
            json={
                "places": [
                    {
                        "place_id": "classroom",
                        "new_place_id": "homeroom",
                        "label": "ホームルーム",
                        "zone": "school",
                        "adjacent_places": {"corridor": 1},
                    },
                    {
                        "place_id": "corridor",
                        "label": "廊下",
                        "zone": "school",
                        "adjacent_places": {"homeroom": 1},
                    },
                    {"place_id": "home", "label": "自宅", "zone": "home"},
                ]
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status == 200
        payload = await response.json()
        assert payload["data"]["places_renamed"] == 1
        cursor = await db._conn.execute(
            "SELECT id, is_active FROM places WHERE story_id = ? AND id IN (?, ?)",
            (STORY_ID, "classroom", "homeroom"),
        )
        rows = [dict(row) for row in await cursor.fetchall()]
        assert {"id": "classroom", "is_active": 0} in rows
        assert {"id": "homeroom", "is_active": 1} in rows
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_admin_place_edit_api_rejects_removal_of_referenced_place(tmp_path: Path) -> None:
    db_path = tmp_path / "admin.db"
    db = DatabaseManager(db_path, migrations_dir=MIGRATIONS_DIR)
    await db.initialize()
    token = await db.create_admin_api_token("default-agent")

    stories_root = tmp_path / "stories"
    character_images_root = tmp_path / "character_images"
    story_maps_root = tmp_path / "story_maps"
    story_metadata_root = tmp_path / "story_metadata"
    _write_template_story_bundle(stories_root, STORY_ID)
    _write_template_assets(character_images_root, story_maps_root, story_metadata_root, STORY_ID)
    await import_story(
        stories_root / STORY_ID,
        db_path=db.db_path,
        force_replace=False,
        story_maps_root=story_maps_root,
    )

    app = await create_app(
        db=db,
        runtime_controller=FakeRuntimeController(),
        archive_root=tmp_path,
        admin_base_url="/admin",
        stories_root=stories_root,
        character_images_root=character_images_root,
        story_maps_root=story_maps_root,
        story_metadata_root=story_metadata_root,
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.put(
            f"/admin/api/v1/stories/{STORY_ID}/places",
            json={"places": [{"place_id": "corridor"}, {"place_id": "home"}]},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status == 400
        payload = await response.json()
        assert "classroom" in payload["error"]["message"]
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_admin_bgm_config_returns_404_for_unknown_story(tmp_path: Path) -> None:
    client, db, token, *_roots = await _start_imported_story_admin_client(tmp_path)
    try:
        get_response = await client.get(
            "/admin/api/v1/stories/missing_story/bgm-config",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert get_response.status == 404

        put_response = await client.put(
            "/admin/api/v1/stories/missing_story/bgm-config",
            json={"enabled": True},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert put_response.status == 404
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_admin_bgm_config_rejects_traversal_place_bgm_key(tmp_path: Path) -> None:
    client, db, token, _stories_root, _char_root, story_maps_root, _metadata_root = (
        await _start_imported_story_admin_client(tmp_path)
    )
    try:
        form = FormData()
        form.add_field("config", json.dumps({}, ensure_ascii=False), content_type="application/json")
        form.add_field(
            "place_bgm__../../../audio/pocketrole_bgm",
            b"bad-audio",
            filename="theme.mp3",
            content_type="audio/mpeg",
        )

        response = await client.put(
            f"/admin/api/v1/stories/{STORY_ID}/bgm-config",
            data=form,
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status == 400
        assert not (tmp_path / "audio" / "pocketrole_bgm.mp3").exists()
        assert not (
            story_maps_root / STORY_ID / "place_bgm" / "../../../audio/pocketrole_bgm.mp3"
        ).resolve().exists()
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_admin_bgm_config_rejects_unknown_place_bgm_key(tmp_path: Path) -> None:
    client, db, token, _stories_root, _char_root, story_maps_root, _metadata_root = (
        await _start_imported_story_admin_client(tmp_path)
    )
    try:
        form = FormData()
        form.add_field("config", json.dumps({}, ensure_ascii=False), content_type="application/json")
        form.add_field(
            "place_bgm__ghost_place",
            b"ghost-audio",
            filename="ghost.mp3",
            content_type="audio/mpeg",
        )

        response = await client.put(
            f"/admin/api/v1/stories/{STORY_ID}/bgm-config",
            data=form,
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status == 400
        payload = await response.json()
        assert "unknown place" in payload["error"]["message"]
        assert not (story_maps_root / STORY_ID / "place_bgm" / "ghost_place.mp3").exists()
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_admin_bgm_config_rejects_non_audio_extension(tmp_path: Path) -> None:
    client, db, token, _stories_root, _char_root, story_maps_root, _metadata_root = (
        await _start_imported_story_admin_client(tmp_path)
    )
    try:
        form = FormData()
        form.add_field("config", json.dumps({}, ensure_ascii=False), content_type="application/json")
        form.add_field(
            "story_bgm",
            b"not-audio",
            filename="notes.txt",
            content_type="text/plain",
        )

        response = await client.put(
            f"/admin/api/v1/stories/{STORY_ID}/bgm-config",
            data=form,
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status == 400
        assert not (story_maps_root / STORY_ID / "story_bgm.txt").exists()
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_admin_bgm_config_rejects_oversized_upload(tmp_path: Path) -> None:
    client, db, token, _stories_root, _char_root, story_maps_root, _metadata_root = (
        await _start_imported_story_admin_client(tmp_path, max_bgm_upload_bytes=4)
    )
    try:
        form = FormData()
        form.add_field("config", json.dumps({}, ensure_ascii=False), content_type="application/json")
        form.add_field(
            "story_bgm",
            b"12345",
            filename="theme.mp3",
            content_type="audio/mpeg",
        )

        response = await client.put(
            f"/admin/api/v1/stories/{STORY_ID}/bgm-config",
            data=form,
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status == 413
        assert not (story_maps_root / STORY_ID / "story_bgm.mp3").exists()
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_admin_bgm_config_preserves_extension_and_replaces_old_story_bgm(
    tmp_path: Path,
) -> None:
    client, db, token, _stories_root, _char_root, story_maps_root, _metadata_root = (
        await _start_imported_story_admin_client(tmp_path)
    )
    story_dir = story_maps_root / STORY_ID
    (story_dir / "story_bgm.mp3").write_bytes(b"old-mp3")
    (story_dir / "bgm_manifest.json").write_text(
        json.dumps(
            {
                "story_id": STORY_ID,
                "enabled": True,
                "story_default": "story_bgm.mp3",
                "place_overrides": {},
                "mood_overrides": {},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    try:
        form = FormData()
        form.add_field("config", json.dumps({"enabled": True}), content_type="application/json")
        form.add_field(
            "story_bgm",
            b"new-ogg",
            filename="theme.ogg",
            content_type="audio/ogg",
        )

        response = await client.put(
            f"/admin/api/v1/stories/{STORY_ID}/bgm-config",
            data=form,
            headers={"Authorization": f"Bearer {token}"},
        )

        response_text = await response.text()
        assert response.status == 200, response_text
        payload = json.loads(response_text)
        assert payload["data"]["story_default"] == "story_bgm.ogg"
        assert (story_dir / "story_bgm.ogg").read_bytes() == b"new-ogg"
        assert not (story_dir / "story_bgm.mp3").exists()
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_admin_bgm_config_saves_place_bgm_without_reenabling_story_bgm(
    tmp_path: Path,
) -> None:
    client, db, token, _stories_root, _char_root, story_maps_root, _metadata_root = (
        await _start_imported_story_admin_client(tmp_path)
    )
    story_dir = story_maps_root / STORY_ID
    (story_dir / "bgm_manifest.json").write_text(
        json.dumps(
            {
                "story_id": STORY_ID,
                "enabled": False,
                "story_default": None,
                "place_overrides": {},
                "mood_overrides": {},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    try:
        form = FormData()
        form.add_field("config", json.dumps({}, ensure_ascii=False), content_type="application/json")
        form.add_field(
            "place_bgm__classroom",
            b"classroom-m4a",
            filename="room.m4a",
            content_type="audio/mp4",
        )

        response = await client.put(
            f"/admin/api/v1/stories/{STORY_ID}/bgm-config",
            data=form,
            headers={"Authorization": f"Bearer {token}"},
        )

        response_text = await response.text()
        assert response.status == 200, response_text
        payload = json.loads(response_text)
        assert payload["data"]["enabled"] is False
        assert payload["data"]["place_overrides"] == {"classroom": "place_bgm/classroom.m4a"}
        assert (story_dir / "place_bgm" / "classroom.m4a").read_bytes() == b"classroom-m4a"
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_admin_replay_api_normalizes_narrator_name_and_avatar(tmp_path: Path) -> None:
    client, db, token, _stories_root, _char_root, _story_maps_root, _metadata_root = (
        await _start_imported_story_admin_client(tmp_path)
    )
    try:
        await db.insert_chat_log(
            STORY_ID,
            {
                "sim_datetime": "2026-04-08T04:30:00",
                "turn_number": 1,
                "char_id": "_narrator",
                "msg_type": "narration_scene",
                "place_id": "classroom",
                "expression": "neutral",
                "message": "朝の教室に、まだ眠たげな空気が残っていた。",
            },
        )

        response = await client.get(
            f"/admin/api/v1/replay?story_id={STORY_ID}&action=replay",
            headers={"Authorization": f"Bearer {token}"},
        )

        response_text = await response.text()
        assert response.status == 200, response_text
        payload = json.loads(response_text)
        narrator_event = payload["events"][0]
        assert narrator_event["char_id"] == "_narrator"
        assert narrator_event["char_name"] == "ナレーション"

        narrator_character = next(
            character for character in payload["characters"] if character["char_id"] == "_narrator"
        )
        assert narrator_character["char_name"] == "ナレーション"
        assert (
            narrator_character["avatar_url"]
            == "/assets/character_images/_system/narration/neutral.png"
        )
    finally:
        await client.close()
        await db.close()
