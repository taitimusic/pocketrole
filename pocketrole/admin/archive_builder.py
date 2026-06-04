"""Published story archive builder."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
import shutil
import tempfile
from pathlib import Path

from db.db_manager import DatabaseManager
from web_placeholders import ensure_index_for_file_parent, ensure_index_tree


@dataclass(slots=True)
class ArchiveBuildResult:
    story_id: str
    page_count: int
    scene_count: int


class ArchiveBuilder:
    def __init__(
        self,
        db: DatabaseManager,
        output_root: str | Path,
        page_size_scenes: int = 10,
    ) -> None:
        self._db = db
        self._output_root = Path(output_root)
        self._page_size_scenes = page_size_scenes

    async def build_story_archive(self, story_id: str) -> ArchiveBuildResult:
        story = await self._db.get_story(story_id)
        if story is None:
            raise ValueError(f"story not found: {story_id}")

        publication = await self._db.get_story_publication(story_id)
        published_at = self._timestamp()
        arcs = await self._db.get_arcs(story_id, arc_type="scene")
        outputs_map = await self._db.get_all_novel_outputs_by_story(story_id)
        scenes = [
            self._render_scene(arc, outputs_map.get(arc["id"], []))
            for arc in arcs
        ]
        pages = [
            scenes[index : index + self._page_size_scenes]
            for index in range(0, len(scenes), self._page_size_scenes)
        ]

        story_root = self._output_root / "stories" / story_id
        tmp_root = Path(tempfile.mkdtemp(prefix=f"{story_id}-", dir=self._output_root))
        try:
            tmp_story_root = tmp_root / "stories" / story_id
            pages_root = tmp_story_root / "pages"
            pages_root.mkdir(parents=True, exist_ok=True)
            ensure_index_tree(tmp_root, pages_root)

            page_descriptors: list[dict[str, object]] = []
            for page_number, page_scenes in enumerate(pages, start=1):
                payload = {
                    "story_id": story_id,
                    "page": page_number,
                    "published_at": published_at,
                    "scene_count": len(page_scenes),
                    "scenes": page_scenes,
                }
                page_path = pages_root / f"{page_number:04d}.json"
                ensure_index_for_file_parent(tmp_root, page_path)
                page_path.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                page_descriptors.append(
                    {
                        "page": page_number,
                        "scene_from": page_scenes[0]["arc_id"],
                        "scene_to": page_scenes[-1]["arc_id"],
                        "turn_from": page_scenes[0]["turn_from"],
                        "turn_to": page_scenes[-1]["turn_to"],
                        "label": page_scenes[0]["title"],
                    }
                )

            manifest = {
                "story_id": story_id,
                "title": story["title"],
                "visibility": publication.get("visibility", "draft") if publication else "draft",
                "published_at": published_at,
                "page_size_scenes": self._page_size_scenes,
                "page_count": len(page_descriptors),
                "scene_count": len(scenes),
                "pages": page_descriptors,
            }
            ensure_index_for_file_parent(tmp_root, tmp_story_root / "manifest.json")
            (tmp_story_root / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            story_root.parent.mkdir(parents=True, exist_ok=True)
            ensure_index_tree(self._output_root, story_root.parent)
            if story_root.exists():
                shutil.rmtree(story_root)
            shutil.move(str(tmp_story_root), str(story_root))
            ensure_index_tree(self._output_root, story_root / "pages")
        finally:
            shutil.rmtree(tmp_root, ignore_errors=True)

        await self._db.record_story_archive_build(
            story_id,
            published_at=published_at,
            last_error=None,
            latest_turn=pages[-1][-1]["turn_to"] if pages else None,
        )
        return ArchiveBuildResult(
            story_id=story_id,
            page_count=len(pages),
            scene_count=len(scenes),
        )

    async def build_catalog(self) -> dict[str, object]:
        stories = []
        for publication in await self._db.list_story_publications():
            if publication["visibility"] != "public":
                continue
            story = await self._db.get_story(publication["story_id"])
            if story is None:
                continue
            manifest_path = self._output_root / "stories" / publication["story_id"] / "manifest.json"
            if not manifest_path.exists():
                continue
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            stories.append(
                {
                    "story_id": publication["story_id"],
                    "title": story["title"],
                    "published_at": publication["published_at"],
                    "page_count": manifest["page_count"],
                    "scene_count": manifest["scene_count"],
                    "latest_turn": publication["latest_published_turn"],
                    "archive_url": f"archive.php?story_id={publication['story_id']}",
                }
            )
        payload = {
            "generated_at": self._timestamp(),
            "stories": stories,
        }
        self._output_root.mkdir(parents=True, exist_ok=True)
        ensure_index_for_file_parent(self._output_root, self._output_root / "catalog.json")
        (self._output_root / "catalog.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return payload

    def _render_scene(self, arc: dict[str, object], outputs: list[dict[str, object]]) -> dict[str, object]:
        return {
            "arc_id": arc["id"],
            "title": arc["title"],
            "summary": arc["summary"],
            "turn_from": arc["turn_from"],
            "turn_to": arc["turn_to"],
            "outputs": [
                {
                    "id": output["id"],
                    "content_type": output["content_type"],
                    "content": output["content"],
                    "ordering": output["ordering"],
                }
                for output in outputs
            ],
        }

    def _timestamp(self) -> str:
        return datetime.now(UTC).isoformat()
