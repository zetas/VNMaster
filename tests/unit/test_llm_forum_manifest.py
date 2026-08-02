from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from vnmaster.config import ForumParserConfig
from vnmaster.db.engine import create_engine_for, ensure_schema
from vnmaster.downloads.models import DownloadGroup, DownloadMirror, ThreadInfo
from vnmaster.downloads.manifest import DownloadManifest
from vnmaster.llm.forum_manifest import (
    ForumManifestInterpreter,
    _drop_unsafe_game_classifications,
    _schema_for_link_ids,
    _unique_artifact_id,
)
from vnmaster.llm.structured import StructuredOutputError


def _artifact(link_id: str, part: int = 1) -> dict[str, object]:
    return {
        "artifact_id": f"part-{part}",
        "kind": "game",
        "title": f"Story Part {part}",
        "part_number": part,
        "part_label": f"Part {part}",
        "version": f"v{part}",
        "required": True,
        "delivery": "download",
        "install_action": "game",
        "variants": [
            {
                "platform": "mac",
                "link_ids": [link_id],
                "mirror_group": f"part-{part}-mac",
                "confidence": 1.0,
                "notes": [],
            }
        ],
        "confidence": 1.0,
        "ambiguities": [],
    }


def _manifest(*artifacts: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "thread_id": 10,
        "title": "Story",
        "multipart": len(artifacts) > 1,
        "artifacts": list(artifacts),
        "ambiguities": [],
        "warnings": [],
        "confidence": 1.0,
    }


class FakeGenerator:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = responses
        self.prompts: list[dict[str, Any]] = []
        self.schemas: list[dict[str, object]] = []

    def generate(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, object],
        schema_name: str,
    ) -> dict[str, object]:
        self.prompts.append(json.loads(user_prompt))
        self.schemas.append(schema)
        assert "untrusted forum content" in system_prompt
        assert schema["type"] == "object"
        assert schema_name == "vnmaster_download_manifest"
        return self.responses[len(self.prompts) - 1]


class MergeFailGenerator(FakeGenerator):
    def generate(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, object],
        schema_name: str,
    ) -> dict[str, object]:
        if len(self.prompts) == 2:
            self.prompts.append(json.loads(user_prompt))
            self.schemas.append(schema)
            raise StructuredOutputError("truncated merge")
        return super().generate(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            schema=schema,
            schema_name=schema_name,
        )


def _engine(tmp_path: Path):  # type: ignore[no-untyped-def]
    engine = create_engine_for(tmp_path / "vnmaster.db")
    ensure_schema(engine)
    return engine


def test_single_section_result_is_cached_by_content(tmp_path: Path) -> None:
    thread = ThreadInfo(
        10,
        "Story",
        "v1",
        1,
        "https://f95zone.to/threads/.10/",
        (DownloadGroup("Mac", (DownloadMirror("MEGA", "https://mega.nz/file/a"),)),),
    )
    generator = FakeGenerator([_manifest(_artifact("g000l000"))])
    parser = ForumManifestInterpreter(
        generator,
        ForumParserConfig(enabled=True),
        _engine(tmp_path),
        clock=lambda: 123,
    )

    first = parser.interpret(thread, "Downloads\nMac")
    second = parser.interpret(thread, "Downloads\nMac")

    assert first.calls == 1
    assert not first.cached
    assert second.cached
    assert second.calls == 0
    assert len(generator.prompts) == 1


def test_sections_are_extracted_then_merged(tmp_path: Path) -> None:
    thread = ThreadInfo(
        10,
        "Story",
        "Part 2 v2",
        1,
        "https://f95zone.to/threads/.10/",
        (
            DownloadGroup("Part 1", ()),
            DownloadGroup("Mac", (DownloadMirror("MEGA", "https://mega.nz/file/a"),)),
            DownloadGroup("Part 2", ()),
            DownloadGroup("Mac", (DownloadMirror("MEGA", "https://mega.nz/file/b"),)),
        ),
    )
    segment_one = _manifest(_artifact("g001l000", part=1))
    segment_two = _manifest(_artifact("g003l000", part=2))
    merged = _manifest(
        _artifact("g001l000", part=1),
        _artifact("g003l000", part=2),
    )
    generator = FakeGenerator([segment_one, segment_two, merged])
    messages: list[str] = []
    parser = ForumManifestInterpreter(
        generator,
        ForumParserConfig(enabled=True),
        _engine(tmp_path),
        clock=lambda: 123,
        reporter=messages.append,
    )

    result = parser.interpret(thread, "Part 1\nMac\nPart 2\nMac")

    assert result.calls == 3
    assert len(result.manifest.artifacts) == 2
    assert generator.prompts[0]["task"].startswith("Extract only")
    assert generator.prompts[-1]["task"].startswith("Merge the section")
    assert "locator" not in json.dumps(generator.prompts)
    assert messages == [
        "Forum parser: section 1/2 (Part 1)...",
        "Forum parser: section 2/2 (Part 2)...",
        "Forum parser: merging 2 section manifests...",
    ]
    first_items = generator.schemas[0]["$defs"]["ManifestVariant"]["properties"][
        "link_ids"
    ]["items"]  # type: ignore[index]
    merge_items = generator.schemas[-1]["$defs"]["ManifestVariant"]["properties"][
        "link_ids"
    ]["items"]  # type: ignore[index]
    assert first_items["enum"] == ["g001l000"]
    assert merge_items["enum"] == ["g001l000", "g003l000"]


def test_invalid_llm_merge_falls_back_to_validated_sections(tmp_path: Path) -> None:
    thread = ThreadInfo(
        10,
        "Story",
        "Part 2 v2",
        1,
        "https://f95zone.to/threads/.10/",
        (
            DownloadGroup("Part 1", ()),
            DownloadGroup("Mac", (DownloadMirror("MEGA", "https://mega.nz/file/a"),)),
            DownloadGroup("Part 2", ()),
            DownloadGroup("Mac", (DownloadMirror("MEGA", "https://mega.nz/file/b"),)),
        ),
    )
    generator = MergeFailGenerator(
        [
            _manifest(_artifact("g001l000", part=1)),
            _manifest(_artifact("g003l000", part=2)),
        ]
    )
    messages: list[str] = []
    parser = ForumManifestInterpreter(
        generator,
        ForumParserConfig(enabled=True),
        _engine(tmp_path),
        clock=lambda: 123,
        reporter=messages.append,
    )

    result = parser.interpret(thread, "Part 1\nMac\nPart 2\nMac")

    assert result.calls == 3
    assert result.manifest.multipart
    assert [artifact.part_number for artifact in result.manifest.artifacts] == [1, 2]
    assert any("combining validated sections deterministically" in m for m in messages)


def test_deterministic_merge_suffixes_duplicate_artifact_ids() -> None:
    assert _unique_artifact_id("character_chart", {"character_chart"}) == (
        "character_chart_2"
    )
    assert _unique_artifact_id(
        "character_chart", {"character_chart", "character_chart_2"}
    ) == "character_chart_3"


def test_non_part_section_schema_can_only_emit_addons() -> None:
    schema = _schema_for_link_ids(
        DownloadManifest.model_json_schema(),
        {"g001l000"},
        allow_games=False,
    )
    assert schema["properties"]["artifacts"]["items"] == {
        "$ref": "#/$defs/AddonManifestArtifact"
    }


def test_part_section_schema_locks_game_part_number() -> None:
    schema = _schema_for_link_ids(
        DownloadManifest.model_json_schema(),
        {"g001l000"},
        game_part_number=6,
    )
    assert schema["$defs"]["GameManifestArtifact"]["properties"][
        "part_number"
    ] == {"type": "integer", "const": 6}


def test_suspicious_patch_is_dropped_from_required_games() -> None:
    manifest = DownloadManifest.model_validate(
        _manifest(
            _artifact("g001l000", part=6),
            {**_artifact("g001l001", part=6), "artifact_id": "patch", "title": "Update Patch"},
        )
    )
    _drop_unsafe_game_classifications(manifest, expected_part_number=6)
    assert [artifact.artifact_id for artifact in manifest.artifacts] == ["part-6"]
