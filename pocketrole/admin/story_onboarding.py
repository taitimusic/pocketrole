"""Story clone onboarding helpers for the hosted admin surface."""

from __future__ import annotations

from pathlib import Path
import re
import shutil
import tempfile
from typing import Any

import yaml

from db.db_manager import DatabaseManager
from tools.generate_story_metadata import export_story_metadata
from tools.import_chapters import import_chapters
from tools.import_director import import_director
from tools.import_story import import_story
from tools.update_story import update_story
from tools.validate_story import StoryValidationError, VALID_ZONES, validate_story


STORY_ID_PATTERN = re.compile(r"^[a-z0-9_]+$")
_EVENT_DATE_RE = re.compile(r"^\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^(\d{2}):(\d{2})$")
_PLACE_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_CHARACTER_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_CHAPTER_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_DIRECTOR_PERSONA_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
READ_ONLY_TEMPLATE_STORY_IDS = frozenset({"ankoku_gakuen"})
DEFAULT_CHARACTER_EXPRESSIONS = [
    "neutral",
    "happy",
    "angry",
    "sad",
    "surprised",
    "worried",
    "content",
    "lonely",
    "tired",
    "determined",
]
DEFAULT_CHARACTER_EXPRESSION_SET = frozenset(DEFAULT_CHARACTER_EXPRESSIONS)


class StoryOnboardingError(RuntimeError):
    """Raised when clone onboarding cannot complete safely."""


class CharacterEditingError(RuntimeError):
    """Raised when character editing cannot complete safely."""


class ChapterDefinitionEditingError(RuntimeError):
    """Raised when chapter definition editing cannot complete safely."""


class PlaceEditingError(RuntimeError):
    """Raised when place editing cannot complete safely."""


class StoryDefinitionEditingError(RuntimeError):
    """Raised when story definition editing cannot complete safely."""


class DirectorPersonaEditingError(RuntimeError):
    """Raised when director persona editing cannot complete safely."""


class EventAnomalyEditingError(RuntimeError):
    """Raised when event/anomaly editing cannot complete safely."""


class StoryOnboardingService:
    def __init__(
        self,
        db: DatabaseManager,
        stories_root: str | Path,
        character_images_root: str | Path,
        story_maps_root: str | Path,
        story_metadata_root: str | Path,
    ) -> None:
        self._db = db
        self._stories_root = Path(stories_root)
        self._character_images_root = Path(character_images_root)
        self._story_maps_root = Path(story_maps_root)
        self._story_metadata_root = Path(story_metadata_root)

    async def get_template_payload(self, template_story_id: str) -> dict[str, Any] | None:
        story_dir = self._stories_root / template_story_id
        if not story_dir.exists():
            return None

        world_data, char_data = self._load_story_yaml(story_dir)
        story = world_data.get("story") or {}
        characters = []
        for character in char_data.get("characters") or []:
            personality = character.get("personality") or {}
            characters.append(
                {
                    "char_id": str(character["id"]),
                    "new_char_id": str(character["id"]),
                    "name": str(character.get("name") or character["id"]),
                    "short_description": personality.get("type"),
                    "goal": character.get("goal"),
                    "worry": character.get("worry"),
                    "image_base_url": f"/assets/character_images/{template_story_id}/{character['id']}",
                }
            )

        return {
            "story": {
                "story_id": str(story.get("id") or template_story_id),
                "title": str(story.get("title") or template_story_id),
                "description": story.get("description"),
            },
            "characters": characters,
        }

    async def clone_story(
        self,
        template_story_id: str,
        payload: dict[str, Any],
        uploaded_images: dict[str, bytes] | None = None,
    ) -> dict[str, Any]:
        uploaded_images = uploaded_images or {}
        target_story_id = str(payload.get("story_id") or "").strip()
        title = str(payload.get("title") or "").strip()
        description = payload.get("description")
        character_edits = payload.get("characters") or []

        if not target_story_id or not STORY_ID_PATTERN.fullmatch(target_story_id):
            raise StoryOnboardingError("story_id must match [a-z0-9_]+")
        if not title:
            raise StoryOnboardingError("title is required")

        template_story_dir = self._stories_root / template_story_id
        if not template_story_dir.exists():
            raise StoryOnboardingError("template story not found")

        target_story_dir = self._stories_root / target_story_id
        if target_story_dir.exists() or await self._db.get_story(target_story_id) is not None:
            raise StoryOnboardingError("story_id already exists")

        copied_paths: list[Path] = []
        try:
            shutil.copytree(template_story_dir, target_story_dir)
            copied_paths.append(target_story_dir)
            self._copy_character_assets(template_story_id, target_story_id, copied_paths)
            self._copy_story_map_assets(template_story_id, target_story_id, copied_paths)
            self._rewrite_story_files(
                target_story_dir=target_story_dir,
                target_story_id=target_story_id,
                title=title,
                description=description,
                character_edits=character_edits,
            )
            self._apply_uploaded_images(target_story_id, uploaded_images)

            errors = validate_story(target_story_dir)
            if errors:
                raise StoryOnboardingError("; ".join(errors))

            import_counts = await import_story(
                target_story_dir,
                db_path=self._db.db_path,
                force_replace=False,
                story_maps_root=self._story_maps_root,
            )
            chapters_imported = await self._import_optional_chapters(target_story_id, target_story_dir)
            director_personas_imported = await self._import_optional_director(
                target_story_id, target_story_dir
            )
            export_story_metadata(
                target_story_dir,
                self._story_metadata_root / f"{target_story_id}.json",
            )
            _, imported_char_data = self._load_story_yaml(target_story_dir)

            return {
                "story_id": target_story_id,
                "title": title,
                "character_count": len(imported_char_data.get("characters") or []),
                "import_mode": "created",
                "import_counts": import_counts,
                "chapters_imported": chapters_imported,
                "director_personas_imported": director_personas_imported,
            }
        except StoryValidationError as exc:
            await self._rollback_created_story(target_story_id, copied_paths)
            raise StoryOnboardingError(str(exc)) from exc
        except StoryOnboardingError:
            await self._rollback_created_story(target_story_id, copied_paths)
            raise
        except Exception as exc:
            await self._rollback_created_story(target_story_id, copied_paths)
            raise StoryOnboardingError(str(exc)) from exc

    def _load_story_yaml(self, story_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
        world_data = yaml.safe_load((story_dir / "world_config.yaml").read_text(encoding="utf-8"))
        char_data = yaml.safe_load((story_dir / "characters.yaml").read_text(encoding="utf-8"))
        return world_data or {}, char_data or {}

    def _rewrite_story_files(
        self,
        target_story_dir: Path,
        target_story_id: str,
        title: str,
        description: Any,
        character_edits: list[dict[str, Any]],
    ) -> None:
        world_data, char_data = self._load_story_yaml(target_story_dir)
        world_story = world_data.setdefault("story", {})
        world_story["id"] = target_story_id
        world_story["title"] = title
        if description is not None:
            world_story["description"] = str(description)

        edits_by_char_id = {
            str(item.get("char_id")): item for item in character_edits if item.get("char_id")
        }
        for character in char_data.get("characters") or []:
            character["story_id"] = target_story_id
            edit = edits_by_char_id.get(str(character["id"]))
            if edit is None:
                continue
            if edit.get("name"):
                character["name"] = str(edit["name"])
            if edit.get("short_description"):
                personality = character.setdefault("personality", {})
                personality["type"] = str(edit["short_description"])
            if edit.get("goal"):
                character["goal"] = str(edit["goal"])
            if edit.get("worry"):
                character["worry"] = str(edit["worry"])

        (target_story_dir / "world_config.yaml").write_text(
            yaml.safe_dump(world_data, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        (target_story_dir / "characters.yaml").write_text(
            yaml.safe_dump(char_data, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

    def _copy_character_assets(
        self,
        template_story_id: str,
        target_story_id: str,
        copied_paths: list[Path],
    ) -> None:
        src = self._character_images_root / template_story_id
        dst = self._character_images_root / target_story_id
        if not src.exists():
            return
        shutil.copytree(src, dst)
        copied_paths.append(dst)

    def _copy_story_map_assets(
        self,
        template_story_id: str,
        target_story_id: str,
        copied_paths: list[Path],
    ) -> None:
        src = self._story_maps_root / template_story_id
        dst = self._story_maps_root / target_story_id
        if not src.exists():
            return

        dst.mkdir(parents=True, exist_ok=True)
        copied_paths.append(dst)
        for directory_name in ("images", "bgm"):
            src_dir = src / directory_name
            if src_dir.exists():
                shutil.copytree(src_dir, dst / directory_name)
        for file_name in ("map.json", "background.svg"):
            src_file = src / file_name
            if src_file.exists():
                shutil.copy2(src_file, dst / file_name)

    def _apply_uploaded_images(self, target_story_id: str, uploaded_images: dict[str, bytes]) -> None:
        for char_id, content in uploaded_images.items():
            char_dir = self._character_images_root / target_story_id / char_id
            char_dir.mkdir(parents=True, exist_ok=True)
            (char_dir / "neutral.png").write_bytes(content)

    async def _import_optional_chapters(self, story_id: str, story_dir: Path) -> int:
        chapters_path = story_dir / "chapters.yaml"
        if not chapters_path.exists():
            return 0
        result = await import_chapters(story_id, chapters_path, db_path=self._db.db_path)
        return int(result["chapters"])

    async def _import_optional_director(self, story_id: str, story_dir: Path) -> int:
        director_path = story_dir / "director.yaml"
        if not director_path.exists():
            return 0
        result = await import_director(story_id, director_path, db_path=self._db.db_path)
        return int(result["personas"])

    async def _rollback_created_story(self, target_story_id: str, copied_paths: list[Path]) -> None:
        for path in reversed(copied_paths):
            if path.exists():
                shutil.rmtree(path, ignore_errors=True)
        metadata_path = self._story_metadata_root / f"{target_story_id}.json"
        if metadata_path.exists():
            metadata_path.unlink()
        await self._db.delete_story(target_story_id)


class CharacterEditingService:
    def __init__(
        self,
        db: DatabaseManager,
        stories_root: str | Path,
        character_images_root: str | Path,
        story_maps_root: str | Path,
        story_metadata_root: str | Path,
        read_only_story_ids: set[str] | frozenset[str] | None = None,
    ) -> None:
        self._db = db
        self._stories_root = Path(stories_root)
        self._character_images_root = Path(character_images_root)
        self._story_maps_root = Path(story_maps_root)
        self._story_metadata_root = Path(story_metadata_root)
        self._read_only_story_ids = set(read_only_story_ids or READ_ONLY_TEMPLATE_STORY_IDS)

    def is_read_only_story(self, story_id: str) -> bool:
        return story_id in self._read_only_story_ids

    async def get_edit_payload(self, story_id: str) -> dict[str, Any] | None:
        story_dir = self._stories_root / story_id
        if not story_dir.exists():
            return None

        world_data, char_data = self._load_story_yaml(story_dir)
        story = world_data.get("story") or {}
        story_row = await self._db.get_story(story_id)
        characters = self._character_payload(story_id, char_data)
        read_only = self.is_read_only_story(story_id)
        return {
            "story": {
                "story_id": str(story.get("id") or story_id),
                "title": str(story.get("title") or story_id),
                "description": story.get("description"),
            },
            "characters": characters,
            "editable": not read_only and story_row is not None,
            "read_only_reason": "template_story" if read_only else None,
            "imported": story_row is not None,
        }

    async def update_characters(
        self,
        story_id: str,
        payload: dict[str, Any],
        uploaded_images: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        uploaded_expression_images = self._normalize_uploaded_expression_images(
            uploaded_images or {}
        )
        if self.is_read_only_story(story_id):
            raise CharacterEditingError("template story is read-only")

        story_dir = self._stories_root / story_id
        if not story_dir.exists():
            raise CharacterEditingError("story not found")
        if await self._db.get_story(story_id) is None:
            raise CharacterEditingError("story is not imported yet")

        characters_path = story_dir / "characters.yaml"
        original_characters_yaml = characters_path.read_bytes()
        db_updated = False

        with tempfile.TemporaryDirectory(prefix="pocketrole-character-edit-") as backup_dir_raw:
            backup_dir = Path(backup_dir_raw)
            asset_backups: dict[Path, Path | None] = {}
            world_data, char_data = self._load_story_yaml(story_dir)
            yaml_story_id = str((world_data.get("story") or {}).get("id") or story_id)
            if yaml_story_id != story_id:
                raise CharacterEditingError("story.id does not match requested story_id")

            try:
                edited_char_data, change_counts, rename_map, removed_ids = self._apply_character_edits(
                    char_data,
                    payload.get("characters") or [],
                    story_id,
                )
                self._apply_uploaded_expression_names(
                    edited_char_data,
                    uploaded_expression_images,
                    rename_map,
                )
                characters_path.write_text(
                    yaml.safe_dump(edited_char_data, allow_unicode=True, sort_keys=False),
                    encoding="utf-8",
                )

                errors = validate_story(story_dir)
                if errors:
                    raise CharacterEditingError("; ".join(errors))

                asset_backups = self._snapshot_character_asset_dirs(
                    story_id,
                    rename_map,
                    removed_ids,
                    uploaded_expression_images,
                    backup_dir,
                )
                self._apply_character_asset_renames(story_id, rename_map)
                self._remove_character_assets(story_id, removed_ids)
                self._apply_uploaded_images(story_id, uploaded_expression_images, rename_map)

                update_counts = await update_story(
                    story_dir,
                    db_path=self._db.db_path,
                    story_maps_root=self._story_maps_root,
                )
                db_updated = True
                export_story_metadata(story_dir, self._story_metadata_root / f"{story_id}.json")

                _, imported_char_data = self._load_story_yaml(story_dir)
                return {
                    "story_id": story_id,
                    "character_count": len(imported_char_data.get("characters") or []),
                    "characters_renamed": len(rename_map),
                    "characters_added": change_counts["added"],
                    "characters_removed": change_counts["removed"],
                    "update_counts": update_counts,
                }
            except StoryValidationError as exc:
                if not db_updated:
                    self._restore_character_edit(
                        characters_path,
                        original_characters_yaml,
                        asset_backups,
                    )
                raise CharacterEditingError(str(exc)) from exc
            except CharacterEditingError:
                if not db_updated:
                    self._restore_character_edit(
                        characters_path,
                        original_characters_yaml,
                        asset_backups,
                    )
                raise
            except Exception as exc:
                if not db_updated:
                    self._restore_character_edit(
                        characters_path,
                        original_characters_yaml,
                        asset_backups,
                    )
                raise CharacterEditingError(str(exc)) from exc

    def _load_story_yaml(self, story_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
        world_data = yaml.safe_load((story_dir / "world_config.yaml").read_text(encoding="utf-8"))
        char_data = yaml.safe_load((story_dir / "characters.yaml").read_text(encoding="utf-8"))
        return world_data or {}, char_data or {}

    def _character_payload(self, story_id: str, char_data: dict[str, Any]) -> list[dict[str, Any]]:
        characters = []
        for character in char_data.get("characters") or []:
            personality = character.get("personality") or {}
            characters.append(
                {
                    "char_id": str(character["id"]),
                    "new_char_id": str(character["id"]),
                    "name": str(character.get("name") or character["id"]),
                    "short_description": personality.get("type"),
                    "goal": character.get("goal"),
                    "worry": character.get("worry"),
                    "image_base_url": f"/assets/character_images/{story_id}/{character['id']}",
                }
            )
        return characters

    def _apply_character_edits(
        self,
        char_data: dict[str, Any],
        character_edits: list[dict[str, Any]],
        story_id: str,
    ) -> tuple[dict[str, Any], dict[str, int], dict[str, str], set[str]]:
        characters = char_data.get("characters") or []
        existing_by_id = {str(character["id"]): character for character in characters}
        retained_characters: list[dict[str, Any]] = []
        consumed_existing_ids: set[str] = set()
        final_ids: list[str] = []
        rename_map: dict[str, str] = {}
        added = 0

        for item in character_edits:
            if not isinstance(item, dict):
                raise CharacterEditingError("character edit item must be an object")
            char_id = str(item.get("char_id") or "").strip()
            new_char_id = str(item.get("new_char_id") or char_id).strip()
            if not new_char_id:
                raise CharacterEditingError("character_id is required")
            if not _CHARACTER_ID_RE.fullmatch(new_char_id):
                raise CharacterEditingError(
                    f"character_id must match [a-z][a-z0-9_]*: {new_char_id}"
                )

            if char_id:
                if char_id not in existing_by_id:
                    raise CharacterEditingError(f"unknown character: {char_id}")
                if char_id in consumed_existing_ids:
                    raise CharacterEditingError(f"duplicate character edit: {char_id}")
                character = existing_by_id[char_id]
                consumed_existing_ids.add(char_id)
            else:
                if not str(item.get("name") or "").strip():
                    raise CharacterEditingError("character name is required")
                character = self._build_new_character(story_id, new_char_id)
                added += 1

            if char_id and new_char_id != char_id:
                character["id"] = new_char_id
                rename_map[char_id] = new_char_id
            else:
                character["id"] = new_char_id

            self._apply_basic_character_fields(character, item)
            retained_characters.append(character)
            final_ids.append(new_char_id)

        if not retained_characters:
            raise CharacterEditingError("at least one character is required")
        if len(final_ids) != len(set(final_ids)):
            raise CharacterEditingError("character_id must be unique")

        removed_ids = set(existing_by_id) - consumed_existing_ids
        char_data["characters"] = retained_characters
        return (
            char_data,
            {"added": added, "removed": len(removed_ids)},
            rename_map,
            removed_ids,
        )

    def _apply_basic_character_fields(
        self,
        character: dict[str, Any],
        edit: dict[str, Any],
    ) -> None:
        if "name" in edit:
            name = str(edit["name"] or "").strip()
            if not name:
                raise CharacterEditingError("character name is required")
            character["name"] = name
        if "short_description" in edit:
            personality = character.setdefault("personality", {})
            personality["type"] = str(edit["short_description"] or "").strip()
        if "goal" in edit:
            character["goal"] = str(edit["goal"] or "").strip()
        if "worry" in edit:
            character["worry"] = str(edit["worry"] or "").strip()

    def _build_new_character(
        self,
        story_id: str,
        char_id: str,
    ) -> dict[str, Any]:
        return {
            "id": char_id,
            "story_id": story_id,
            "name": char_id,
            "personality": {"type": ""},
            "goal": "",
            "worry": "",
            "emotion_default": {
                "stress": 0.3,
                "motivation": 0.7,
                "loneliness": 0.2,
                "excitement": 0.5,
            },
            "expressions": list(DEFAULT_CHARACTER_EXPRESSIONS),
        }

    def _snapshot_character_asset_dirs(
        self,
        story_id: str,
        rename_map: dict[str, str],
        removed_ids: set[str],
        uploaded_images: dict[str, dict[str, bytes]],
        backup_dir: Path,
    ) -> dict[Path, Path | None]:
        target_char_ids = set(rename_map)
        target_char_ids.update(rename_map.values())
        target_char_ids.update(removed_ids)
        for char_id in uploaded_images:
            target_char_ids.add(rename_map.get(char_id, char_id))

        backups: dict[Path, Path | None] = {}
        for index, char_id in enumerate(sorted(target_char_ids)):
            path = self._character_images_root / story_id / char_id
            if path.exists():
                backup_path = backup_dir / f"asset_{index}"
                shutil.copytree(path, backup_path)
                backups[path] = backup_path
            else:
                backups[path] = None
        return backups

    def _apply_character_asset_renames(self, story_id: str, rename_map: dict[str, str]) -> None:
        for old_char_id, new_char_id in rename_map.items():
            old_dir = self._character_images_root / story_id / old_char_id
            new_dir = self._character_images_root / story_id / new_char_id
            if not old_dir.exists():
                continue
            if new_dir.exists():
                raise CharacterEditingError(f"character asset directory already exists: {new_char_id}")
            new_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(old_dir), str(new_dir))

    def _remove_character_assets(self, story_id: str, removed_ids: set[str]) -> None:
        for char_id in removed_ids:
            char_dir = self._character_images_root / story_id / char_id
            if char_dir.exists():
                shutil.rmtree(char_dir)

    def _apply_uploaded_images(
        self,
        story_id: str,
        uploaded_images: dict[str, dict[str, bytes]],
        rename_map: dict[str, str] | None = None,
    ) -> None:
        rename_map = rename_map or {}
        for char_id, expression_images in uploaded_images.items():
            target_char_id = rename_map.get(char_id, char_id)
            char_dir = self._character_images_root / story_id / target_char_id
            char_dir.mkdir(parents=True, exist_ok=True)
            for expression, content in expression_images.items():
                if expression not in DEFAULT_CHARACTER_EXPRESSION_SET:
                    raise CharacterEditingError(f"unknown expression image: {expression}")
                (char_dir / f"{expression}.png").write_bytes(content)

    def _normalize_uploaded_expression_images(
        self,
        uploaded_images: dict[str, Any],
    ) -> dict[str, dict[str, bytes]]:
        normalized: dict[str, dict[str, bytes]] = {}
        for char_id, value in uploaded_images.items():
            if isinstance(value, bytes):
                normalized.setdefault(char_id, {})["neutral"] = value
                continue
            if not isinstance(value, dict):
                raise CharacterEditingError("uploaded image payload must be bytes or expression map")
            expression_images: dict[str, bytes] = {}
            for expression, content in value.items():
                expression_name = str(expression)
                if expression_name not in DEFAULT_CHARACTER_EXPRESSION_SET:
                    raise CharacterEditingError(f"unknown expression image: {expression_name}")
                if not isinstance(content, (bytes, bytearray)):
                    raise CharacterEditingError("uploaded image content must be bytes")
                expression_images[expression_name] = bytes(content)
            if expression_images:
                normalized[char_id] = expression_images
        return normalized

    def _apply_uploaded_expression_names(
        self,
        char_data: dict[str, Any],
        uploaded_images: dict[str, dict[str, bytes]],
        rename_map: dict[str, str],
    ) -> None:
        if not uploaded_images:
            return
        expression_names_by_char: dict[str, set[str]] = {}
        for char_id, expression_images in uploaded_images.items():
            target_char_id = rename_map.get(char_id, char_id)
            expression_names_by_char.setdefault(target_char_id, set()).update(
                expression_images.keys()
            )
        for character in char_data.get("characters") or []:
            char_id = str(character.get("id") or "")
            uploaded_names = expression_names_by_char.get(char_id)
            if not uploaded_names:
                continue
            existing = [
                str(expression)
                for expression in list(character.get("expressions") or [])
                if str(expression)
            ]
            for expression in DEFAULT_CHARACTER_EXPRESSIONS:
                if expression in uploaded_names and expression not in existing:
                    existing.append(expression)
            character["expressions"] = existing

    def _restore_character_edit(
        self,
        characters_path: Path,
        original_characters_yaml: bytes,
        asset_backups: dict[Path, Path | None],
    ) -> None:
        characters_path.write_bytes(original_characters_yaml)
        for path in sorted(asset_backups, key=lambda item: len(item.parts), reverse=True):
            if path.exists():
                shutil.rmtree(path)
        for path, backup_path in asset_backups.items():
            if backup_path is None:
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(backup_path, path)


class ChapterDefinitionEditingService:
    def __init__(
        self,
        db: DatabaseManager,
        stories_root: str | Path,
        story_metadata_root: str | Path,
        read_only_story_ids: set[str] | frozenset[str] | None = None,
    ) -> None:
        self._db = db
        self._stories_root = Path(stories_root)
        self._story_metadata_root = Path(story_metadata_root)
        self._read_only_story_ids = set(read_only_story_ids or READ_ONLY_TEMPLATE_STORY_IDS)

    def is_read_only_story(self, story_id: str) -> bool:
        return story_id in self._read_only_story_ids

    async def get_edit_payload(self, story_id: str) -> dict[str, Any] | None:
        story_dir = self._stories_root / story_id
        if not story_dir.exists():
            return None

        world_data = self._load_world_yaml(story_dir)
        story = world_data.get("story") or {}
        chapter_data = self._load_chapters_yaml(story_dir)
        story_row = await self._db.get_story(story_id)
        read_only = self.is_read_only_story(story_id)
        return {
            "story": {
                "story_id": str(story.get("id") or story_id),
                "title": str(story.get("title") or story_id),
                "description": story.get("description"),
            },
            "chapters": self._chapter_payload(chapter_data),
            "editable": not read_only and story_row is not None,
            "read_only_reason": "template_story" if read_only else None,
            "imported": story_row is not None,
        }

    async def update_chapters(
        self,
        story_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if self.is_read_only_story(story_id):
            raise ChapterDefinitionEditingError("template story is read-only")

        story_dir = self._stories_root / story_id
        if not story_dir.exists():
            raise ChapterDefinitionEditingError("story not found")
        if await self._db.get_story(story_id) is None:
            raise ChapterDefinitionEditingError("story is not imported yet")

        world_data = self._load_world_yaml(story_dir)
        yaml_story_id = str((world_data.get("story") or {}).get("id") or story_id)
        if yaml_story_id != story_id:
            raise ChapterDefinitionEditingError("story.id does not match requested story_id")

        chapters_path = story_dir / "chapters.yaml"
        original_chapters_yaml = (
            chapters_path.read_bytes()
            if chapters_path.exists()
            else b"chapters: []\n"
        )

        try:
            existing_data = self._load_chapters_yaml(story_dir)
            edited_data, added, removed = self._apply_chapter_edits(
                existing_data,
                payload.get("chapters") or [],
            )
            chapters_path.write_text(
                yaml.safe_dump(edited_data, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )

            chapter_ids: list[str] = []
            beat_count = 0
            for chapter in edited_data["chapters"]:
                chapter_ids.append(str(chapter["id"]))
                chapter_db_id = await self._db.upsert_chapter_definition(
                    story_id,
                    {
                        "chapter_id": str(chapter["id"]),
                        "title": str(chapter["title"]),
                        "theme": chapter.get("theme"),
                        "world_injection": chapter.get("world_injection"),
                        "status": "pending",
                        "start_condition": chapter.get("start_condition"),
                        "current_beat": "setup",
                    },
                )
                beats = [
                    {
                        "phase": str(beat["phase"]),
                        "description": beat.get("description"),
                        "goal": beat.get("goal"),
                        "events_json": beat.get("events", []),
                    }
                    for beat in chapter.get("beats", [])
                ]
                await self._db.replace_chapter_beats(chapter_db_id, beats)
                beat_count += len(beats)
            deleted_pending = await self._db.delete_pending_chapters_except(
                story_id,
                chapter_ids,
            )
            export_story_metadata(story_dir, self._story_metadata_root / f"{story_id}.json")

            return {
                "story_id": story_id,
                "chapter_count": len(chapter_ids),
                "beat_count": beat_count,
                "chapters_added": added,
                "chapters_removed": max(removed, deleted_pending),
            }
        except ChapterDefinitionEditingError:
            chapters_path.write_bytes(original_chapters_yaml)
            raise
        except Exception as exc:
            chapters_path.write_bytes(original_chapters_yaml)
            raise ChapterDefinitionEditingError(str(exc)) from exc

    def _load_world_yaml(self, story_dir: Path) -> dict[str, Any]:
        world_data = yaml.safe_load((story_dir / "world_config.yaml").read_text(encoding="utf-8"))
        return world_data or {}

    def _load_chapters_yaml(self, story_dir: Path) -> dict[str, Any]:
        chapters_path = story_dir / "chapters.yaml"
        if not chapters_path.exists():
            return {"chapters": []}
        data = yaml.safe_load(chapters_path.read_text(encoding="utf-8"))
        data = data or {}
        data.setdefault("chapters", [])
        return data

    def _chapter_payload(self, chapter_data: dict[str, Any]) -> list[dict[str, Any]]:
        chapters = []
        for chapter in chapter_data.get("chapters") or []:
            chapters.append(
                {
                    "chapter_id": str(chapter.get("id") or ""),
                    "title": str(chapter.get("title") or ""),
                    "theme": chapter.get("theme"),
                    "world_injection": chapter.get("world_injection"),
                    "start_condition": chapter.get("start_condition"),
                    "beats": [
                        {
                            "phase": str(beat.get("phase") or ""),
                            "description": beat.get("description"),
                            "goal": beat.get("goal"),
                            "events": beat.get("events") or [],
                        }
                        for beat in chapter.get("beats", [])
                    ],
                }
            )
        return chapters

    def _apply_chapter_edits(
        self,
        existing_data: dict[str, Any],
        chapter_edits: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], int, int]:
        if not chapter_edits:
            raise ChapterDefinitionEditingError("at least one chapter is required")

        existing_ids = {
            str(chapter.get("id") or "")
            for chapter in existing_data.get("chapters") or []
            if chapter.get("id")
        }
        seen_ids: set[str] = set()
        chapters: list[dict[str, Any]] = []
        for edit in chapter_edits:
            chapter_id = str(edit.get("chapter_id") or "").strip()
            self._validate_chapter_id(chapter_id)
            if chapter_id in seen_ids:
                raise ChapterDefinitionEditingError(
                    f"duplicate chapter id in payload: {chapter_id}"
                )
            seen_ids.add(chapter_id)
            title = str(edit.get("title") or "").strip()
            if not title:
                raise ChapterDefinitionEditingError("chapter title is required")
            beats = self._normalize_beats(edit.get("beats") or [])
            chapter: dict[str, Any] = {"id": chapter_id, "title": title, "beats": beats}
            if "theme" in edit:
                chapter["theme"] = str(edit.get("theme") or "").strip()
            if "world_injection" in edit:
                chapter["world_injection"] = str(edit.get("world_injection") or "").strip()
            if "start_condition" in edit:
                chapter["start_condition"] = str(edit.get("start_condition") or "").strip()
            chapters.append(chapter)

        return (
            {"chapters": chapters},
            len(seen_ids - existing_ids),
            len(existing_ids - seen_ids),
        )

    def _validate_chapter_id(self, value: str) -> None:
        if not value:
            raise ChapterDefinitionEditingError("chapter id is required")
        if not _CHAPTER_ID_RE.fullmatch(value):
            raise ChapterDefinitionEditingError(
                f"invalid chapter id '{value}' — must start with a letter and contain only a-z, 0-9, _"
            )

    def _normalize_beats(self, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            raise ChapterDefinitionEditingError("chapter beats must be a list")
        if not value:
            raise ChapterDefinitionEditingError("chapter requires at least one beat")
        beats: list[dict[str, Any]] = []
        seen_phases: set[str] = set()
        for item in value:
            if not isinstance(item, dict):
                raise ChapterDefinitionEditingError("chapter beat must be an object")
            phase = str(item.get("phase") or "").strip()
            if not phase:
                raise ChapterDefinitionEditingError("chapter beat phase is required")
            if phase in seen_phases:
                raise ChapterDefinitionEditingError(f"duplicate beat phase: {phase}")
            seen_phases.add(phase)
            events = item.get("events", [])
            if not isinstance(events, list):
                raise ChapterDefinitionEditingError("chapter beat events must be a list")
            beats.append(
                {
                    "phase": phase,
                    "description": str(item.get("description") or "").strip(),
                    "goal": str(item.get("goal") or "").strip(),
                    "events": self._normalize_beat_events(events),
                }
            )
        return beats

    def _normalize_beat_events(self, value: list[Any]) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for item in value:
            if not isinstance(item, dict):
                raise ChapterDefinitionEditingError("chapter beat event must be an object")
            event = dict(item)
            for key in ("type", "desc", "description", "target_char_id", "place_id"):
                if key in event:
                    event[key] = str(event.get(key) or "").strip()
            if "priority" in event:
                priority = event["priority"]
                if not isinstance(priority, int) or isinstance(priority, bool):
                    raise ChapterDefinitionEditingError("event priority must be an integer")
            events.append(event)
        return events


class PlaceEditingService:
    def __init__(
        self,
        db: DatabaseManager,
        stories_root: str | Path,
        story_maps_root: str | Path,
        story_metadata_root: str | Path,
        read_only_story_ids: set[str] | frozenset[str] | None = None,
    ) -> None:
        self._db = db
        self._stories_root = Path(stories_root)
        self._story_maps_root = Path(story_maps_root)
        self._story_metadata_root = Path(story_metadata_root)
        self._read_only_story_ids = set(read_only_story_ids or READ_ONLY_TEMPLATE_STORY_IDS)

    def is_read_only_story(self, story_id: str) -> bool:
        return story_id in self._read_only_story_ids

    async def get_edit_payload(self, story_id: str) -> dict[str, Any] | None:
        story_dir = self._stories_root / story_id
        if not story_dir.exists():
            return None

        world_data = self._load_world_yaml(story_dir)
        story = world_data.get("story") or {}
        story_row = await self._db.get_story(story_id)
        read_only = self.is_read_only_story(story_id)
        return {
            "story": {
                "story_id": str(story.get("id") or story_id),
                "title": str(story.get("title") or story_id),
                "description": story.get("description"),
            },
            "places": self._place_payload(world_data),
            "editable": not read_only and story_row is not None,
            "read_only_reason": "template_story" if read_only else None,
            "imported": story_row is not None,
        }

    async def update_places(
        self,
        story_id: str,
        payload: dict[str, Any],
        uploaded_images: dict[str, bytes] | None = None,
    ) -> dict[str, Any]:
        if self.is_read_only_story(story_id):
            raise PlaceEditingError("template story is read-only")

        story_dir = self._stories_root / story_id
        if not story_dir.exists():
            raise PlaceEditingError("story not found")
        if await self._db.get_story(story_id) is None:
            raise PlaceEditingError("story is not imported yet")

        world_path = story_dir / "world_config.yaml"
        original_world_yaml = world_path.read_bytes()
        characters_path = story_dir / "characters.yaml"
        original_characters_yaml = characters_path.read_bytes() if characters_path.exists() else None
        db_updated = False

        try:
            world_data = self._load_world_yaml(story_dir)
            characters_data = self._load_characters_yaml(story_dir)
            yaml_story_id = str((world_data.get("story") or {}).get("id") or story_id)
            if yaml_story_id != story_id:
                raise PlaceEditingError("story.id does not match requested story_id")

            new_places, places_added, places_removed, places_renamed, rename_map = self._reconcile_places(
                world_data, payload.get("places") or []
            )
            uploaded_place_images = self._resolve_uploaded_place_image_targets(
                uploaded_images or {},
                rename_map,
                {str(place["id"]) for place in new_places},
            )

            if rename_map:
                self._rewrite_place_references(world_data, characters_data, rename_map)

            if places_removed > 0:
                existing_places = world_data.get("places") or []
                existing_ids = {str(p["id"]) for p in existing_places}
                payload_source_ids = {
                    str(item.get("place_id") or "").strip()
                    for item in payload.get("places") or []
                }
                removed_ids = existing_ids - payload_source_ids
                refs = self._scan_place_references(
                    world_data, characters_data, removed_ids, new_places
                )
                if refs:
                    ref_msgs = [
                        f"{pid}: {', '.join(locs)}" for pid, locs in sorted(refs.items())
                    ]
                    raise PlaceEditingError(
                        f"cannot delete place(s) — still referenced: {'; '.join(ref_msgs)}"
                    )

            world_data["places"] = new_places
            world_path.write_text(
                yaml.safe_dump(world_data, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            if characters_path.exists():
                characters_path.write_text(
                    yaml.safe_dump(characters_data, allow_unicode=True, sort_keys=False),
                    encoding="utf-8",
                )

            errors = validate_story(story_dir)
            if errors:
                raise PlaceEditingError("; ".join(errors))

            update_counts = await update_story(
                story_dir,
                db_path=self._db.db_path,
                story_maps_root=self._story_maps_root,
            )
            db_updated = True
            export_story_metadata(story_dir, self._story_metadata_root / f"{story_id}.json")

            if uploaded_place_images:
                self._apply_uploaded_place_images(story_id, uploaded_place_images)

            imported_world_data = self._load_world_yaml(story_dir)
            return {
                "story_id": story_id,
                "place_count": len(imported_world_data.get("places") or []),
                "places_added": places_added,
                "places_removed": places_removed,
                "places_renamed": places_renamed,
                "update_counts": update_counts,
            }
        except StoryValidationError as exc:
            if not db_updated:
                world_path.write_bytes(original_world_yaml)
                if original_characters_yaml is not None:
                    characters_path.write_bytes(original_characters_yaml)
            raise PlaceEditingError(str(exc)) from exc
        except PlaceEditingError:
            if not db_updated:
                world_path.write_bytes(original_world_yaml)
                if original_characters_yaml is not None:
                    characters_path.write_bytes(original_characters_yaml)
            raise
        except Exception as exc:
            if not db_updated:
                world_path.write_bytes(original_world_yaml)
                if original_characters_yaml is not None:
                    characters_path.write_bytes(original_characters_yaml)
            raise PlaceEditingError(str(exc)) from exc

    def _resolve_uploaded_place_image_targets(
        self,
        uploaded_images: dict[str, bytes],
        rename_map: dict[str, str],
        valid_place_ids: set[str],
    ) -> dict[str, bytes]:
        resolved: dict[str, bytes] = {}
        for place_id, content in uploaded_images.items():
            if not _PLACE_ID_RE.fullmatch(place_id):
                raise PlaceEditingError(
                    f"invalid uploaded place image id '{place_id}'"
                )
            target_id = rename_map.get(place_id, place_id)
            if target_id not in valid_place_ids:
                raise PlaceEditingError(
                    f"unknown uploaded place image id '{place_id}'"
                )
            if target_id in resolved:
                raise PlaceEditingError(
                    f"duplicate uploaded place image id '{target_id}'"
                )
            resolved[target_id] = content
        return resolved

    def _apply_uploaded_place_images(
        self,
        story_id: str,
        uploaded_images: dict[str, bytes],
    ) -> None:
        images_dir = self._story_maps_root / story_id / "images"
        images_dir.mkdir(parents=True, exist_ok=True)
        for place_id, content in uploaded_images.items():
            (images_dir / f"{place_id}_320.png").write_bytes(content)

    def _load_world_yaml(self, story_dir: Path) -> dict[str, Any]:
        world_data = yaml.safe_load((story_dir / "world_config.yaml").read_text(encoding="utf-8"))
        return world_data or {}

    def _load_characters_yaml(self, story_dir: Path) -> dict[str, Any]:
        chars_path = story_dir / "characters.yaml"
        if not chars_path.exists():
            return {}
        data = yaml.safe_load(chars_path.read_text(encoding="utf-8"))
        return data or {}

    def _place_payload(self, world_data: dict[str, Any]) -> list[dict[str, Any]]:
        places = []
        for place in world_data.get("places") or []:
            places.append(
                {
                    "place_id": str(place["id"]),
                    "label": str(place.get("label") or place["id"]),
                    "zone": place.get("zone"),
                    "atmosphere": place.get("atmosphere"),
                    "who_gathers": place.get("who_gathers") or [],
                    "events_likely": place.get("events_likely") or [],
                    "access_note": place.get("access_note"),
                    "adjacent_places": dict(place.get("adjacent_places") or {}),
                }
            )
        return places

    def _validate_place_id_format(self, value: str) -> None:
        if not value:
            raise PlaceEditingError("place_id is required")
        if not _PLACE_ID_RE.fullmatch(value):
            raise PlaceEditingError(
                f"invalid place_id format '{value}' — must start with a letter and contain only a-z, 0-9, _"
            )

    def _validate_zone(self, value: str, place_id: str = "") -> None:
        prefix = f"place '{place_id}': " if place_id else ""
        if not value:
            raise PlaceEditingError(f"{prefix}zone is required")
        if value not in VALID_ZONES:
            raise PlaceEditingError(
                f"{prefix}invalid zone '{value}' — valid: {sorted(VALID_ZONES)}"
            )

    def _normalize_adjacent_places(
        self,
        value: Any,
        valid_place_ids: set[str],
        self_place_id: str,
    ) -> dict[str, int]:
        if not value:
            return {}
        if not isinstance(value, dict):
            raise PlaceEditingError(
                f"adjacent_places must be a dict, got {type(value).__name__}"
            )
        result: dict[str, int] = {}
        for adj_id, cost in value.items():
            adj_id_str = str(adj_id).strip()
            if not adj_id_str:
                continue
            if adj_id_str == self_place_id:
                raise PlaceEditingError(
                    f"place '{self_place_id}' cannot be adjacent to itself"
                )
            if adj_id_str not in valid_place_ids:
                raise PlaceEditingError(
                    f"adjacent_places: unknown place_id '{adj_id_str}'"
                )
            if isinstance(cost, bool) or not isinstance(cost, int) or cost < 1:
                raise PlaceEditingError(
                    f"adjacent_places['{adj_id_str}'] cost must be int >= 1"
                )
            result[adj_id_str] = cost
        return result

    def _apply_place_fields(
        self,
        place: dict[str, Any],
        edit: dict[str, Any],
        valid_place_ids: set[str],
    ) -> None:
        if "label" in edit:
            label = str(edit["label"] or "").strip()
            if not label:
                raise PlaceEditingError("place label is required")
            place["label"] = label
        if "zone" in edit:
            zone = str(edit["zone"] or "").strip()
            self._validate_zone(zone, str(place.get("id") or ""))
            place["zone"] = zone
        if "atmosphere" in edit:
            place["atmosphere"] = str(edit["atmosphere"] or "").strip()
        if "who_gathers" in edit:
            place["who_gathers"] = self._normalize_string_list(edit["who_gathers"])
        if "events_likely" in edit:
            place["events_likely"] = self._normalize_string_list(edit["events_likely"])
        if "access_note" in edit:
            place["access_note"] = str(edit["access_note"] or "").strip()
        if "adjacent_places" in edit:
            place["adjacent_places"] = self._normalize_adjacent_places(
                edit["adjacent_places"], valid_place_ids, str(place.get("id") or "")
            )

    def _build_new_place(self, edit: dict[str, Any], valid_place_ids: set[str]) -> dict[str, Any]:
        place_id = str(edit.get("place_id") or "").strip()
        self._validate_place_id_format(place_id)
        label = str(edit.get("label") or "").strip()
        if not label:
            raise PlaceEditingError(f"new place '{place_id}': label is required")
        zone = str(edit.get("zone") or "").strip()
        self._validate_zone(zone, place_id)
        place: dict[str, Any] = {"id": place_id, "label": label, "zone": zone}
        if edit.get("atmosphere"):
            place["atmosphere"] = str(edit["atmosphere"]).strip()
        wg = self._normalize_string_list(edit.get("who_gathers"))
        if wg:
            place["who_gathers"] = wg
        el = self._normalize_string_list(edit.get("events_likely"))
        if el:
            place["events_likely"] = el
        if edit.get("access_note"):
            place["access_note"] = str(edit["access_note"]).strip()
        adj = self._normalize_adjacent_places(
            edit.get("adjacent_places") or {}, valid_place_ids, place_id
        )
        if adj:
            place["adjacent_places"] = adj
        return place

    def _reconcile_places(
        self,
        world_data: dict[str, Any],
        place_edits: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], int, int, int, dict[str, str]]:
        existing_places = world_data.get("places") or []
        existing_by_id: dict[str, dict[str, Any]] = {str(p["id"]): p for p in existing_places}

        seen_ids: set[str] = set()
        source_ids: set[str] = set()
        rename_map: dict[str, str] = {}
        target_by_source: dict[str, str] = {}
        for edit in place_edits:
            pid = str(edit.get("place_id") or "").strip()
            if not pid:
                raise PlaceEditingError("place_id is required for all entries")
            if pid in source_ids:
                raise PlaceEditingError(f"duplicate place_id in payload: {pid}")
            source_ids.add(pid)
            target_id = str(edit.get("new_place_id") or pid).strip()
            self._validate_place_id_format(target_id)
            if target_id in seen_ids:
                raise PlaceEditingError(f"duplicate place_id in payload: {target_id}")
            seen_ids.add(target_id)
            target_by_source[pid] = target_id
            if pid in existing_by_id and target_id != pid:
                rename_map[pid] = target_id

        reconciled_ids = seen_ids

        for edit in place_edits:
            pid = str(edit.get("place_id") or "").strip()
            if pid not in existing_by_id:
                self._validate_place_id_format(pid)

        new_places: list[dict[str, Any]] = []
        added = 0
        for edit in place_edits:
            pid = str(edit.get("place_id") or "").strip()
            target_id = target_by_source[pid]
            normalized_edit = dict(edit)
            if "adjacent_places" in normalized_edit:
                normalized_edit["adjacent_places"] = self._rename_adjacent_places(
                    normalized_edit["adjacent_places"],
                    rename_map,
                )
            if pid in existing_by_id:
                place = dict(existing_by_id[pid])
                place["id"] = target_id
                self._apply_place_fields(place, normalized_edit, reconciled_ids - {target_id})
                new_places.append(place)
            else:
                normalized_edit["place_id"] = target_id
                place = self._build_new_place(normalized_edit, reconciled_ids - {target_id})
                new_places.append(place)
                added += 1

        removed = len(set(existing_by_id.keys()) - source_ids)
        return new_places, added, removed, len(rename_map), rename_map

    def _scan_place_references(
        self,
        world_data: dict[str, Any],
        characters_data: dict[str, Any],
        removed_ids: set[str],
        remaining_places: list[dict[str, Any]],
    ) -> dict[str, list[str]]:
        refs: dict[str, list[str]] = {pid: [] for pid in removed_ids}

        for char in characters_data.get("characters") or []:
            char_id = str(char.get("id") or char.get("name") or "?")
            for pid in char.get("favorite_places") or []:
                if pid in removed_ids:
                    refs[pid].append(f"characters/{char_id}.favorite_places")

        for ts in world_data.get("time_schedules") or []:
            label = str(ts.get("label") or "time_schedule")
            for pid in ts.get("expected_places") or []:
                if pid in removed_ids:
                    refs[pid].append(f"time_schedules/{label}.expected_places")

        for event in world_data.get("event_calendar") or []:
            evt_key = f"{event.get('event_date')}:{event.get('name')}"
            pid = event.get("force_place")
            if pid and pid in removed_ids:
                refs[pid].append(f"event_calendar/{evt_key}.force_place")

        for anomaly in world_data.get("anomaly_rules") or []:
            alabel = str(anomaly.get("label") or "anomaly")
            condition = anomaly.get("condition_json") or {}
            for field in ("place", "expected_place"):
                pid = condition.get(field)
                if pid and pid in removed_ids:
                    refs[pid].append(f"anomaly_rules/{alabel}.condition_json.{field}")

        for place in remaining_places:
            adj = place.get("adjacent_places") or {}
            for adj_pid in adj:
                if adj_pid in removed_ids:
                    refs[adj_pid].append(f"places/{place['id']}.adjacent_places")

        return {pid: sorted(r) for pid, r in refs.items() if r}

    def _rename_adjacent_places(self, value: Any, rename_map: dict[str, str]) -> Any:
        if not isinstance(value, dict) or not rename_map:
            return value
        renamed: dict[str, Any] = {}
        for adj_id, cost in value.items():
            renamed[rename_map.get(str(adj_id), str(adj_id))] = cost
        return renamed

    def _rewrite_place_references(
        self,
        world_data: dict[str, Any],
        characters_data: dict[str, Any],
        rename_map: dict[str, str],
    ) -> None:
        for place in world_data.get("places") or []:
            if str(place.get("id")) in rename_map:
                place["id"] = rename_map[str(place["id"])]
            if "adjacent_places" in place:
                place["adjacent_places"] = self._rename_adjacent_places(
                    place.get("adjacent_places") or {},
                    rename_map,
                )

        for schedule in world_data.get("time_schedules") or []:
            if "expected_places" in schedule:
                schedule["expected_places"] = [
                    rename_map.get(str(place_id), str(place_id))
                    for place_id in schedule.get("expected_places") or []
                ]

        for event in world_data.get("event_calendar") or []:
            force_place = event.get("force_place")
            if force_place in rename_map:
                event["force_place"] = rename_map[str(force_place)]

        for anomaly in world_data.get("anomaly_rules") or []:
            condition = anomaly.get("condition_json") or {}
            if isinstance(condition, dict):
                for field in ("place", "expected_place"):
                    place_id = condition.get(field)
                    if place_id in rename_map:
                        condition[field] = rename_map[str(place_id)]
                anomaly["condition_json"] = condition

        for character in characters_data.get("characters") or []:
            if "favorite_places" in character:
                character["favorite_places"] = [
                    rename_map.get(str(place_id), str(place_id))
                    for place_id in character.get("favorite_places") or []
                ]

    def _normalize_string_list(self, value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if value is None:
            return []
        text = str(value)
        return [line.strip() for line in text.replace(",", "\n").splitlines() if line.strip()]


class StoryDefinitionEditingService:
    def __init__(
        self,
        db: DatabaseManager,
        stories_root: str | Path,
        story_maps_root: str | Path,
        story_metadata_root: str | Path,
        read_only_story_ids: set[str] | frozenset[str] | None = None,
    ) -> None:
        self._db = db
        self._stories_root = Path(stories_root)
        self._story_maps_root = Path(story_maps_root)
        self._story_metadata_root = Path(story_metadata_root)
        self._read_only_story_ids = set(read_only_story_ids or READ_ONLY_TEMPLATE_STORY_IDS)

    def is_read_only_story(self, story_id: str) -> bool:
        return story_id in self._read_only_story_ids

    async def get_edit_payload(self, story_id: str) -> dict[str, Any] | None:
        story_dir = self._stories_root / story_id
        if not story_dir.exists():
            return None

        world_data = self._load_world_yaml(story_dir)
        story = world_data.get("story") or {}
        story_row = await self._db.get_story(story_id)
        read_only = self.is_read_only_story(story_id)
        return {
            "story": {
                "story_id": str(story.get("id") or story_id),
                "title": str(story.get("title") or story_id),
                "description": story.get("description"),
                "season_start": story.get("season_start"),
                "turn_minutes": story.get("turn_minutes"),
                "turn_interval_sec": story.get("turn_interval_sec"),
                "world_rules": story.get("world_rules"),
            },
            "editable": not read_only and story_row is not None,
            "read_only_reason": "template_story" if read_only else None,
            "imported": story_row is not None,
        }

    async def update_definition(
        self,
        story_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if self.is_read_only_story(story_id):
            raise StoryDefinitionEditingError("template story is read-only")

        story_dir = self._stories_root / story_id
        if not story_dir.exists():
            raise StoryDefinitionEditingError("story not found")
        if await self._db.get_story(story_id) is None:
            raise StoryDefinitionEditingError("story is not imported yet")

        world_path = story_dir / "world_config.yaml"
        original_world_yaml = world_path.read_bytes()
        db_updated = False

        try:
            world_data = self._load_world_yaml(story_dir)
            story = world_data.get("story") or {}
            yaml_story_id = str(story.get("id") or story_id)
            if yaml_story_id != story_id:
                raise StoryDefinitionEditingError("story.id does not match requested story_id")

            edited_world_data = self._apply_definition_edit(world_data, payload.get("story") or {})
            world_path.write_text(
                yaml.safe_dump(edited_world_data, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )

            errors = validate_story(story_dir)
            if errors:
                raise StoryDefinitionEditingError("; ".join(errors))

            update_counts = await update_story(
                story_dir,
                db_path=self._db.db_path,
                story_maps_root=self._story_maps_root,
            )
            db_updated = True
            export_story_metadata(story_dir, self._story_metadata_root / f"{story_id}.json")

            return {
                "story_id": story_id,
                "update_counts": update_counts,
            }
        except StoryValidationError as exc:
            if not db_updated:
                world_path.write_bytes(original_world_yaml)
            raise StoryDefinitionEditingError(str(exc)) from exc
        except StoryDefinitionEditingError:
            if not db_updated:
                world_path.write_bytes(original_world_yaml)
            raise
        except Exception as exc:
            if not db_updated:
                world_path.write_bytes(original_world_yaml)
            raise StoryDefinitionEditingError(str(exc)) from exc

    def _load_world_yaml(self, story_dir: Path) -> dict[str, Any]:
        world_data = yaml.safe_load((story_dir / "world_config.yaml").read_text(encoding="utf-8"))
        return world_data or {}

    def _apply_definition_edit(
        self,
        world_data: dict[str, Any],
        story_edit: dict[str, Any],
    ) -> dict[str, Any]:
        story = world_data.setdefault("story", {})
        if "title" in story_edit:
            title = str(story_edit["title"] or "").strip()
            if not title:
                raise StoryDefinitionEditingError("story title is required")
            story["title"] = title
        if "description" in story_edit:
            story["description"] = str(story_edit["description"] or "").strip()
        if "season_start" in story_edit:
            season_start = str(story_edit["season_start"] or "").strip()
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", season_start):
                raise StoryDefinitionEditingError("season_start must be YYYY-MM-DD")
            story["season_start"] = season_start
        if "turn_minutes" in story_edit:
            story["turn_minutes"] = self._positive_int(story_edit["turn_minutes"], "turn_minutes")
        if "turn_interval_sec" in story_edit:
            story["turn_interval_sec"] = self._positive_int(
                story_edit["turn_interval_sec"],
                "turn_interval_sec",
            )
        if "world_rules" in story_edit:
            story["world_rules"] = str(story_edit["world_rules"] or "").strip()
        return world_data

    def _positive_int(self, value: Any, field_name: str) -> int:
        if isinstance(value, bool):
            raise StoryDefinitionEditingError(f"{field_name} must be a positive integer")
        try:
            int_value = int(value)
        except (TypeError, ValueError) as exc:
            raise StoryDefinitionEditingError(
                f"{field_name} must be a positive integer"
            ) from exc
        if int_value <= 0:
            raise StoryDefinitionEditingError(f"{field_name} must be a positive integer")
        return int_value


class DirectorPersonaEditingService:
    def __init__(
        self,
        db: DatabaseManager,
        stories_root: str | Path,
        story_metadata_root: str | Path,
        read_only_story_ids: set[str] | frozenset[str] | None = None,
    ) -> None:
        self._db = db
        self._stories_root = Path(stories_root)
        self._story_metadata_root = Path(story_metadata_root)
        self._read_only_story_ids = set(read_only_story_ids or READ_ONLY_TEMPLATE_STORY_IDS)

    def is_read_only_story(self, story_id: str) -> bool:
        return story_id in self._read_only_story_ids

    async def get_edit_payload(self, story_id: str) -> dict[str, Any] | None:
        story_dir = self._stories_root / story_id
        if not story_dir.exists():
            return None

        world_data = self._load_world_yaml(story_dir)
        story = world_data.get("story") or {}
        director_data = self._load_director_yaml(story_dir)
        story_row = await self._db.get_story(story_id)
        read_only = self.is_read_only_story(story_id)
        return {
            "story": {
                "story_id": str(story.get("id") or story_id),
                "title": str(story.get("title") or story_id),
                "description": story.get("description"),
            },
            "default_active": director_data.get("default_active"),
            "personas": self._persona_payload(director_data),
            "editable": not read_only and story_row is not None,
            "read_only_reason": "template_story" if read_only else None,
            "imported": story_row is not None,
        }

    async def update_director_personas(
        self,
        story_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if self.is_read_only_story(story_id):
            raise DirectorPersonaEditingError("template story is read-only")

        story_dir = self._stories_root / story_id
        if not story_dir.exists():
            raise DirectorPersonaEditingError("story not found")
        if await self._db.get_story(story_id) is None:
            raise DirectorPersonaEditingError("story is not imported yet")

        world_data = self._load_world_yaml(story_dir)
        yaml_story_id = str((world_data.get("story") or {}).get("id") or story_id)
        if yaml_story_id != story_id:
            raise DirectorPersonaEditingError("story.id does not match requested story_id")

        director_path = story_dir / "director.yaml"
        original_director_yaml = (
            director_path.read_bytes()
            if director_path.exists()
            else b"default_active: null\npersonas: {}\n"
        )

        try:
            existing_director = self._load_director_yaml(story_dir)
            edited_director, added, removed = self._apply_director_edits(
                existing_director,
                payload.get("personas") or [],
                payload.get("default_active"),
            )
            director_path.write_text(
                yaml.safe_dump(edited_director, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )

            persona_ids = []
            for persona_id, persona_def in edited_director["personas"].items():
                persona_ids.append(str(persona_id))
                await self._db.upsert_director_persona(
                    story_id,
                    {
                        "persona_id": str(persona_id),
                        "name": str(persona_def.get("name") or persona_id),
                        "aesthetic_json": dict(persona_def.get("aesthetic") or {}),
                        "values_json": self._normalize_string_list(
                            persona_def.get("values")
                        ),
                        "traits_json": self._normalize_string_list(
                            persona_def.get("traits")
                        ),
                        "is_active": 0,
                    },
                )
            await self._db.delete_director_personas_except(story_id, persona_ids)
            await self._db.set_persona_active(story_id, edited_director["default_active"])
            export_story_metadata(story_dir, self._story_metadata_root / f"{story_id}.json")

            return {
                "story_id": story_id,
                "persona_count": len(persona_ids),
                "personas_added": added,
                "personas_removed": removed,
                "default_active": edited_director["default_active"],
            }
        except DirectorPersonaEditingError:
            director_path.write_bytes(original_director_yaml)
            raise
        except Exception as exc:
            director_path.write_bytes(original_director_yaml)
            raise DirectorPersonaEditingError(str(exc)) from exc

    def _load_world_yaml(self, story_dir: Path) -> dict[str, Any]:
        world_data = yaml.safe_load((story_dir / "world_config.yaml").read_text(encoding="utf-8"))
        return world_data or {}

    def _load_director_yaml(self, story_dir: Path) -> dict[str, Any]:
        director_path = story_dir / "director.yaml"
        if not director_path.exists():
            return {"default_active": None, "personas": {}}
        data = yaml.safe_load(director_path.read_text(encoding="utf-8"))
        data = data or {}
        data.setdefault("personas", {})
        return data

    def _persona_payload(self, director_data: dict[str, Any]) -> list[dict[str, Any]]:
        personas = []
        for persona_id, persona_def in sorted((director_data.get("personas") or {}).items()):
            personas.append(
                {
                    "persona_id": str(persona_id),
                    "name": str(persona_def.get("name") or persona_id),
                    "aesthetic": dict(persona_def.get("aesthetic") or {}),
                    "values": self._normalize_string_list(persona_def.get("values")),
                    "traits": self._normalize_string_list(persona_def.get("traits")),
                    "is_default_active": str(persona_id) == director_data.get("default_active"),
                }
            )
        return personas

    def _apply_director_edits(
        self,
        existing_director: dict[str, Any],
        persona_edits: list[dict[str, Any]],
        default_active: Any,
    ) -> tuple[dict[str, Any], int, int]:
        if not persona_edits:
            raise DirectorPersonaEditingError("at least one director persona is required")

        existing_ids = {
            str(persona_id) for persona_id in (existing_director.get("personas") or {}).keys()
        }
        seen_ids: set[str] = set()
        personas: dict[str, dict[str, Any]] = {}
        for edit in persona_edits:
            persona_id = str(edit.get("persona_id") or "").strip()
            self._validate_persona_id(persona_id)
            if persona_id in seen_ids:
                raise DirectorPersonaEditingError(
                    f"duplicate director persona id in payload: {persona_id}"
                )
            seen_ids.add(persona_id)
            name = str(edit.get("name") or "").strip()
            if not name:
                raise DirectorPersonaEditingError("director persona name is required")
            personas[persona_id] = {
                "name": name,
                "aesthetic": self._normalize_aesthetic(edit.get("aesthetic") or {}),
                "values": self._normalize_string_list(edit.get("values")),
                "traits": self._normalize_string_list(edit.get("traits")),
            }

        default_active_id = str(default_active or "").strip()
        if not default_active_id:
            default_active_id = next(iter(personas))
        if default_active_id not in personas:
            raise DirectorPersonaEditingError("default_active must reference an existing persona")

        return (
            {"default_active": default_active_id, "personas": personas},
            len(set(personas.keys()) - existing_ids),
            len(existing_ids - set(personas.keys())),
        )

    def _validate_persona_id(self, value: str) -> None:
        if not value:
            raise DirectorPersonaEditingError("director persona id is required")
        if not _DIRECTOR_PERSONA_ID_RE.fullmatch(value):
            raise DirectorPersonaEditingError(
                f"invalid director persona id '{value}' — must start with a letter and contain only a-z, 0-9, _"
            )

    def _normalize_aesthetic(self, value: Any) -> dict[str, float]:
        if not isinstance(value, dict):
            raise DirectorPersonaEditingError("director persona aesthetic must be an object")
        normalized: dict[str, float] = {}
        for key, raw_value in value.items():
            try:
                normalized[str(key)] = float(raw_value)
            except (TypeError, ValueError) as exc:
                raise DirectorPersonaEditingError(
                    "director persona aesthetic values must be numbers"
                ) from exc
        return normalized

    def _normalize_string_list(self, value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if value is None:
            return []
        text = str(value)
        return [line.strip() for line in text.replace(",", "\n").splitlines() if line.strip()]


class EventAnomalyEditingService:
    def __init__(
        self,
        db: DatabaseManager,
        stories_root: str | Path,
        story_maps_root: str | Path,
        story_metadata_root: str | Path,
        read_only_story_ids: set[str] | frozenset[str] | None = None,
    ) -> None:
        self._db = db
        self._stories_root = Path(stories_root)
        self._story_maps_root = Path(story_maps_root)
        self._story_metadata_root = Path(story_metadata_root)
        self._read_only_story_ids = set(read_only_story_ids or READ_ONLY_TEMPLATE_STORY_IDS)

    def is_read_only_story(self, story_id: str) -> bool:
        return story_id in self._read_only_story_ids

    async def get_edit_payload(self, story_id: str) -> dict[str, Any] | None:
        story_dir = self._stories_root / story_id
        if not story_dir.exists():
            return None

        world_data = self._load_world_yaml(story_dir)
        story = world_data.get("story") or {}
        story_row = await self._db.get_story(story_id)
        read_only = self.is_read_only_story(story_id)
        return {
            "story": {
                "story_id": str(story.get("id") or story_id),
                "title": str(story.get("title") or story_id),
                "description": story.get("description"),
            },
            "events": self._event_payload(world_data),
            "anomalies": self._anomaly_payload(world_data),
            "places": self._place_payload(world_data),
            "editable": not read_only and story_row is not None,
            "read_only_reason": "template_story" if read_only else None,
            "imported": story_row is not None,
        }

    async def update_event_anomalies(
        self,
        story_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if self.is_read_only_story(story_id):
            raise EventAnomalyEditingError("template story is read-only")

        story_dir = self._stories_root / story_id
        if not story_dir.exists():
            raise EventAnomalyEditingError("story not found")
        if await self._db.get_story(story_id) is None:
            raise EventAnomalyEditingError("story is not imported yet")

        world_path = story_dir / "world_config.yaml"
        original_world_yaml = world_path.read_bytes()
        db_updated = False

        try:
            world_data = self._load_world_yaml(story_dir)
            yaml_story_id = str((world_data.get("story") or {}).get("id") or story_id)
            if yaml_story_id != story_id:
                raise EventAnomalyEditingError("story.id does not match requested story_id")

            edited_world_data, ev_added, ev_removed, an_added, an_removed = (
                self._apply_event_anomaly_edits(
                    world_data,
                    payload.get("events") or [],
                    payload.get("anomalies") or [],
                )
            )
            world_path.write_text(
                yaml.safe_dump(edited_world_data, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )

            errors = validate_story(story_dir)
            if errors:
                raise EventAnomalyEditingError("; ".join(errors))

            update_counts = await update_story(
                story_dir,
                db_path=self._db.db_path,
                story_maps_root=self._story_maps_root,
            )
            db_updated = True
            export_story_metadata(story_dir, self._story_metadata_root / f"{story_id}.json")

            imported_world_data = self._load_world_yaml(story_dir)
            return {
                "story_id": story_id,
                "event_count": len(imported_world_data.get("event_calendar") or []),
                "anomaly_count": len(imported_world_data.get("anomaly_rules") or []),
                "events_added": ev_added,
                "events_removed": ev_removed,
                "anomalies_added": an_added,
                "anomalies_removed": an_removed,
                "update_counts": update_counts,
            }
        except StoryValidationError as exc:
            if not db_updated:
                world_path.write_bytes(original_world_yaml)
            raise EventAnomalyEditingError(str(exc)) from exc
        except EventAnomalyEditingError:
            if not db_updated:
                world_path.write_bytes(original_world_yaml)
            raise
        except Exception as exc:
            if not db_updated:
                world_path.write_bytes(original_world_yaml)
            raise EventAnomalyEditingError(str(exc)) from exc

    def _load_world_yaml(self, story_dir: Path) -> dict[str, Any]:
        world_data = yaml.safe_load((story_dir / "world_config.yaml").read_text(encoding="utf-8"))
        return world_data or {}

    def _event_payload(self, world_data: dict[str, Any]) -> list[dict[str, Any]]:
        events = []
        for event in world_data.get("event_calendar") or []:
            events.append(
                {
                    "event_key": self._event_key(event),
                    "event_date": str(event.get("event_date") or ""),
                    "name": str(event.get("name") or ""),
                    "duration_days": event.get("duration_days", 1),
                    "atmosphere": event.get("atmosphere"),
                    "emotion_impact": event.get("emotion_impact") or {},
                    "force_place": event.get("force_place"),
                }
            )
        return events

    def _anomaly_payload(self, world_data: dict[str, Any]) -> list[dict[str, Any]]:
        anomalies = []
        for anomaly in world_data.get("anomaly_rules") or []:
            anomalies.append(
                {
                    "anomaly_key": self._anomaly_key(anomaly),
                    "label": str(anomaly.get("label") or ""),
                    "condition_json": anomaly.get("condition_json") or {},
                    "drama_potential": anomaly.get("drama_potential"),
                    "suggested_reasons": anomaly.get("suggested_reasons") or [],
                }
            )
        return anomalies

    def _place_payload(self, world_data: dict[str, Any]) -> list[dict[str, Any]]:
        places = []
        for place in world_data.get("places") or []:
            places.append(
                {
                    "place_id": str(place["id"]),
                    "label": str(place.get("label") or place["id"]),
                    "zone": place.get("zone"),
                }
            )
        return places

    def _apply_event_anomaly_edits(
        self,
        world_data: dict[str, Any],
        event_edits: list[dict[str, Any]],
        anomaly_edits: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], int, int, int, int]:
        new_events, ev_added, ev_removed = self._reconcile_events(
            world_data.get("event_calendar") or [], event_edits
        )
        new_anomalies, an_added, an_removed = self._reconcile_anomalies(
            world_data.get("anomaly_rules") or [],
            anomaly_edits,
            {
                str(place["id"])
                for place in world_data.get("places") or []
                if place.get("id") is not None
            },
        )
        world_data["event_calendar"] = new_events
        world_data["anomaly_rules"] = new_anomalies
        return world_data, ev_added, ev_removed, an_added, an_removed

    def _reconcile_events(
        self,
        existing_events: list[dict[str, Any]],
        event_edits: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], int, int]:
        existing_by_key: dict[str, dict[str, Any]] = {
            self._event_key(e): e for e in existing_events
        }
        keys_referenced = {
            str(item.get("event_key") or "").strip()
            for item in event_edits
            if str(item.get("event_key") or "").strip()
        }
        removed = sum(1 for k in existing_by_key if k not in keys_referenced)

        seen_new_keys: set[str] = set()
        used_existing_keys: set[str] = set()
        result: list[dict[str, Any]] = []
        added = 0

        for edit in event_edits:
            event_key = str(edit.get("event_key") or "").strip()
            if event_key:
                if event_key not in existing_by_key:
                    raise EventAnomalyEditingError(f"unknown event: {event_key}")
                if event_key in used_existing_keys:
                    raise EventAnomalyEditingError(f"duplicate event key in payload: {event_key}")
                used_existing_keys.add(event_key)
                event = existing_by_key[event_key]
                self._apply_event_fields(event, edit)
            else:
                event = self._build_new_event(edit)
                added += 1
            new_key = self._event_key(event)
            if new_key in seen_new_keys:
                raise EventAnomalyEditingError(f"duplicate event: {new_key}")
            seen_new_keys.add(new_key)
            result.append(event)

        return result, added, removed

    def _reconcile_anomalies(
        self,
        existing_anomalies: list[dict[str, Any]],
        anomaly_edits: list[dict[str, Any]],
        valid_place_ids: set[str],
    ) -> tuple[list[dict[str, Any]], int, int]:
        existing_by_key: dict[str, dict[str, Any]] = {
            self._anomaly_key(a): a for a in existing_anomalies
        }
        keys_referenced = {
            str(item.get("anomaly_key") or "").strip()
            for item in anomaly_edits
            if str(item.get("anomaly_key") or "").strip()
        }
        removed = sum(1 for k in existing_by_key if k not in keys_referenced)

        seen_new_keys: set[str] = set()
        used_existing_keys: set[str] = set()
        result: list[dict[str, Any]] = []
        added = 0

        for edit in anomaly_edits:
            anomaly_key = str(edit.get("anomaly_key") or "").strip()
            if anomaly_key:
                if anomaly_key not in existing_by_key:
                    raise EventAnomalyEditingError(f"unknown anomaly: {anomaly_key}")
                if anomaly_key in used_existing_keys:
                    raise EventAnomalyEditingError(
                        f"duplicate anomaly key in payload: {anomaly_key}"
                    )
                used_existing_keys.add(anomaly_key)
                anomaly = existing_by_key[anomaly_key]
                self._apply_anomaly_fields(anomaly, edit, valid_place_ids)
            else:
                anomaly = self._build_new_anomaly(edit, valid_place_ids)
                added += 1
            new_key = self._anomaly_key(anomaly)
            if new_key in seen_new_keys:
                raise EventAnomalyEditingError(f"duplicate anomaly label: {new_key}")
            seen_new_keys.add(new_key)
            result.append(anomaly)

        return result, added, removed

    def _apply_event_fields(self, event: dict[str, Any], edit: dict[str, Any]) -> None:
        if "event_date" in edit:
            event_date = str(edit["event_date"] or "").strip()
            self._validate_event_date_format(event_date, "event event_date")
            event["event_date"] = event_date
        if "name" in edit:
            name = str(edit["name"] or "").strip()
            if not name:
                raise EventAnomalyEditingError("event name is required")
            event["name"] = name
        if "duration_days" in edit:
            duration = self._normalize_positive_int(edit["duration_days"], "event duration_days")
            event["duration_days"] = duration
        if "atmosphere" in edit:
            event["atmosphere"] = str(edit["atmosphere"] or "").strip()
        if "emotion_impact" in edit:
            event["emotion_impact"] = self._normalize_number_map(edit["emotion_impact"])
        if "force_place" in edit:
            event["force_place"] = self._normalize_optional_string(edit["force_place"])

    def _apply_anomaly_fields(
        self,
        anomaly: dict[str, Any],
        edit: dict[str, Any],
        valid_place_ids: set[str],
    ) -> None:
        if "label" in edit:
            label = str(edit["label"] or "").strip()
            if not label:
                raise EventAnomalyEditingError("anomaly label is required")
            anomaly["label"] = label
        if "condition_json" in edit:
            anomaly["condition_json"] = self._normalize_mapping(
                edit["condition_json"],
                "anomaly condition_json",
            )
            self._validate_anomaly_condition_json(
                anomaly["condition_json"],
                valid_place_ids,
                "anomaly condition_json",
            )
        if "drama_potential" in edit:
            anomaly["drama_potential"] = str(edit["drama_potential"] or "").strip()
        if "suggested_reasons" in edit:
            anomaly["suggested_reasons"] = self._normalize_string_list(edit["suggested_reasons"])

    def _build_new_event(self, edit: dict[str, Any]) -> dict[str, Any]:
        event_date = str(edit.get("event_date") or "").strip()
        if not event_date:
            raise EventAnomalyEditingError("new event requires event_date")
        self._validate_event_date_format(event_date, "event event_date")
        name = str(edit.get("name") or "").strip()
        if not name:
            raise EventAnomalyEditingError("new event requires name")
        duration_days_raw = edit.get("duration_days")
        if duration_days_raw is None:
            raise EventAnomalyEditingError("new event requires duration_days")
        duration_days = self._normalize_positive_int(duration_days_raw, "event duration_days")
        event: dict[str, Any] = {
            "event_date": event_date,
            "name": name,
            "duration_days": duration_days,
        }
        if "atmosphere" in edit:
            event["atmosphere"] = str(edit["atmosphere"] or "").strip()
        if "emotion_impact" in edit:
            event["emotion_impact"] = self._normalize_number_map(edit["emotion_impact"])
        if "force_place" in edit:
            event["force_place"] = self._normalize_optional_string(edit["force_place"])
        return event

    def _build_new_anomaly(
        self,
        edit: dict[str, Any],
        valid_place_ids: set[str],
    ) -> dict[str, Any]:
        label = str(edit.get("label") or "").strip()
        if not label:
            raise EventAnomalyEditingError("new anomaly requires label")
        condition_json_raw = edit.get("condition_json")
        if condition_json_raw is None:
            raise EventAnomalyEditingError("new anomaly requires condition_json")
        condition_json = self._normalize_mapping(condition_json_raw, "anomaly condition_json")
        self._validate_anomaly_condition_json(
            condition_json,
            valid_place_ids,
            "anomaly condition_json",
        )
        anomaly: dict[str, Any] = {"label": label, "condition_json": condition_json}
        if "drama_potential" in edit:
            anomaly["drama_potential"] = str(edit["drama_potential"] or "").strip()
        if "suggested_reasons" in edit:
            anomaly["suggested_reasons"] = self._normalize_string_list(edit["suggested_reasons"])
        return anomaly

    def _validate_event_date_format(self, value: str, field_name: str) -> None:
        if value and not _EVENT_DATE_RE.fullmatch(value):
            raise EventAnomalyEditingError(f"{field_name} must be MM-DD format")

    def _validate_time_format(self, value: Any, field_name: str) -> None:
        text = str(value or "").strip()
        match = _TIME_RE.fullmatch(text)
        if not match:
            raise EventAnomalyEditingError(f"{field_name} must be HH:MM format")
        hour = int(match.group(1))
        minute = int(match.group(2))
        if hour > 23 or minute > 59:
            raise EventAnomalyEditingError(f"{field_name} must be HH:MM format")

    def _validate_zero_to_one_number(self, value: Any, field_name: str) -> None:
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise EventAnomalyEditingError(f"{field_name} must be a number from 0.0 to 1.0") from exc
        if number < 0.0 or number > 1.0:
            raise EventAnomalyEditingError(f"{field_name} must be a number from 0.0 to 1.0")

    def _validate_anomaly_condition_json(
        self,
        condition: dict[str, Any],
        valid_place_ids: set[str],
        field_name: str,
    ) -> None:
        for key in ("place", "expected_place"):
            place_id = condition.get(key)
            if place_id is not None and place_id not in valid_place_ids:
                raise EventAnomalyEditingError(
                    f"{field_name}.{key}: unknown place_id '{place_id}'"
                )

        zone = condition.get("zone")
        if zone is not None and zone not in VALID_ZONES:
            raise EventAnomalyEditingError(
                f"{field_name}.zone: invalid zone '{zone}'"
            )

        for key in ("time_from", "time_to"):
            if key in condition:
                self._validate_time_format(condition[key], f"{field_name}.{key}")

        for key in ("multiple_characters", "alone"):
            if key in condition and not isinstance(condition[key], bool):
                raise EventAnomalyEditingError(f"{field_name}.{key}: must be boolean")

        for key in ("min_tension", "stress_threshold_min"):
            if key in condition:
                self._validate_zero_to_one_number(condition[key], f"{field_name}.{key}")

    def _event_key(self, event: dict[str, Any]) -> str:
        return f"{event.get('event_date')}:{event.get('name')}"

    def _anomaly_key(self, anomaly: dict[str, Any]) -> str:
        return str(anomaly.get("label") or "")

    def _normalize_optional_string(self, value: Any) -> str | None:
        text = str(value).strip() if value is not None else ""
        return text or None

    def _normalize_positive_int(self, value: Any, field_name: str) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise EventAnomalyEditingError(f"{field_name} must be a positive integer") from exc
        if number <= 0:
            raise EventAnomalyEditingError(f"{field_name} must be a positive integer")
        return number

    def _normalize_number_map(self, value: Any) -> dict[str, float]:
        mapping = self._normalize_mapping(value, "event emotion_impact")
        normalized: dict[str, float] = {}
        for key, item in mapping.items():
            try:
                normalized[str(key)] = float(item)
            except (TypeError, ValueError) as exc:
                raise EventAnomalyEditingError("event emotion_impact values must be numbers") from exc
        return normalized

    def _normalize_mapping(self, value: Any, field_name: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise EventAnomalyEditingError(f"{field_name} must be an object")
        return dict(value)

    def _normalize_string_list(self, value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if value is None:
            return []
        text = str(value)
        return [line.strip() for line in text.replace(",", "\n").splitlines() if line.strip()]
