from __future__ import annotations

from dataclasses import replace

import pytest

from vnmaster.downloads.addon_installer import should_install_addon
from vnmaster.downloads.manifest import (
    DownloadManifest,
    build_download_plan_from_manifest,
    build_forum_sections,
    build_link_registry,
    part_detection_from_manifest,
    validate_manifest_references,
)
from vnmaster.downloads.models import DownloadGroup, DownloadMirror, ThreadInfo


def _thread() -> ThreadInfo:
    return ThreadInfo(
        thread_id=94140,
        title="Grandma's House",
        version="Part 7 v0.108",
        thread_type=1,
        url="https://f95zone.to/threads/.94140/",
        downloads=(
            DownloadGroup("Part 6", ()),
            DownloadGroup(
                "Mac",
                (
                    DownloadMirror("MEGA", "//a[starts-with(@href,'https://mega.nz')][1]"),
                    DownloadMirror("PIXELDRAIN", "https://pixeldrain.com/u/part6"),
                ),
            ),
            DownloadGroup("Part 7", ()),
            DownloadGroup(
                "Mac",
                (DownloadMirror("MEGA", "https://mega.nz/file/part7"),),
            ),
            DownloadGroup(
                "WALKTHROUGH MOD",
                (DownloadMirror("WALKTHROUGH MOD", "https://f95zone.to/threads/mod.99/"),),
            ),
            DownloadGroup(
                "WALKTHROUGH — GrandmasHouseP6-0.95-guide.pdf",
                (
                    DownloadMirror(
                        "F95 ATTACHMENT",
                        "https://attachments.f95zone.to/2026/01/123_GrandmasHouseP6-0.95-guide.pdf",
                    ),
                ),
            ),
        ),
    )


def _manifest() -> DownloadManifest:
    return DownloadManifest.model_validate(
        {
            "schema_version": 1,
            "thread_id": 94140,
            "title": "Grandma's House",
            "multipart": True,
            "artifacts": [
                {
                    "artifact_id": "part-6",
                    "kind": "game",
                    "title": "Grandma's House Part 6",
                    "part_number": 6,
                    "part_label": "Part 6",
                    "version": "v0.95",
                    "required": True,
                    "delivery": "download",
                    "install_action": "game",
                    "variants": [
                        {
                            "platform": "mac",
                            "link_ids": ["g001l000", "g001l001"],
                            "mirror_group": "part-6-mac",
                            "confidence": 0.95,
                            "notes": [],
                        }
                    ],
                    "confidence": 0.95,
                    "ambiguities": [],
                },
                {
                    "artifact_id": "part-7",
                    "kind": "game",
                    "title": "Grandma's House Part 7",
                    "part_number": 7,
                    "part_label": "Part 7",
                    "version": "v0.108",
                    "required": True,
                    "delivery": "download",
                    "install_action": "game",
                    "variants": [
                        {
                            "platform": "mac",
                            "link_ids": ["g003l000"],
                            "mirror_group": "part-7-mac",
                            "confidence": 0.99,
                            "notes": [],
                        }
                    ],
                    "confidence": 0.99,
                    "ambiguities": [],
                },
                {
                    "artifact_id": "mod",
                    "kind": "addon",
                    "title": "Walkthrough mod",
                    "part_number": 7,
                    "part_label": "Part 7",
                    "version": None,
                    "required": False,
                    "delivery": "manual",
                    "install_action": "manual",
                    "variants": [
                        {
                            "platform": None,
                            "link_ids": ["g004l000"],
                            "mirror_group": "unresolved",
                            "confidence": 1.0,
                            "notes": ["link opens another forum thread"],
                        }
                    ],
                    "confidence": 1.0,
                    "ambiguities": [],
                },
                {
                    "artifact_id": "guide-6",
                    "kind": "addon",
                    "title": "Part 6 walkthrough",
                    "part_number": 6,
                    "part_label": "Part 6",
                    "version": "v0.95",
                    "required": False,
                    "delivery": "download",
                    "install_action": "separate",
                    "variants": [
                        {
                            "platform": None,
                            "link_ids": ["g005l000"],
                            "mirror_group": "guide-6",
                            "confidence": 0.99,
                            "notes": [],
                        }
                    ],
                    "confidence": 0.99,
                    "ambiguities": [],
                },
            ],
            "ambiguities": [],
            "warnings": [],
            "confidence": 0.95,
        }
    )


def test_link_registry_exposes_ids_but_not_urls_to_prompt() -> None:
    records = build_link_registry(_thread())
    assert records[0].link_id == "g001l000"
    assert records[-1].filename_hint == "123_GrandmasHouseP6-0.95-guide.pdf"
    assert "locator" not in records[0].prompt_value()


def test_manifest_json_schema_requires_every_field_and_forbids_extras() -> None:
    schema = DownloadManifest.model_json_schema()
    object_schemas = [schema, *schema["$defs"].values()]
    for object_schema in object_schemas:
        assert object_schema["additionalProperties"] is False
        assert set(object_schema["required"]) == set(object_schema["properties"])


def test_download_artifact_with_no_links_fails_validation() -> None:
    manifest = _manifest()
    manifest.artifacts[0].variants[0].link_ids = []
    with pytest.raises(ValueError, match="has no links"):
        validate_manifest_references(
            manifest,
            thread_id=94140,
            valid_link_ids={record.link_id for record in build_link_registry(_thread())},
            require_game=True,
        )

    # Same empty-link shape, but on a manual add-on: nothing to validate against.
    manual_addon = _manifest()
    manual_addon.artifacts[2].variants[0].link_ids = []
    validate_manifest_references(
        manual_addon,
        thread_id=94140,
        valid_link_ids={record.link_id for record in build_link_registry(_thread())},
        require_game=True,
    )


def test_sections_split_parts_from_optional_downloads() -> None:
    sections = build_forum_sections(
        _thread(),
        "Part 6\nMac downloads\nPart 7\nMac downloads\nWALKTHROUGH MOD",
        max_groups=10,
        max_excerpt_chars=1000,
    )
    assert [section.name for section in sections] == [
        "Part 6",
        "Part 7",
        "Optional downloads",
    ]
    assert [group.index for group in sections[-1].groups] == [4, 5]
    assert sections[-1].post_excerpt == "WALKTHROUGH MOD"


def test_manifest_rejects_unknown_link_reference() -> None:
    manifest = _manifest()
    manifest.artifacts[0].variants[0].link_ids.append("invented-url")
    with pytest.raises(ValueError, match="unknown link ID"):
        validate_manifest_references(
            manifest,
            thread_id=94140,
            valid_link_ids={record.link_id for record in build_link_registry(_thread())},
            require_game=True,
        )


def test_manifest_builds_only_selected_part_and_its_addons() -> None:
    plan = build_download_plan_from_manifest(
        _thread(),
        _manifest(),
        platform_priority=["mac", "windows"],
        preferred_hosts=["mega"],
        selected_parts=(6,),
        include_addons=True,
    )
    assert [(artifact.kind, artifact.part) for artifact in plan.artifacts] == [
        ("game", "Part 6"),
        ("addon", "Part 6"),
    ]
    assert plan.artifacts[0].version == "v0.95"
    assert plan.artifacts[0].host == "MEGA"
    assert len(plan.artifacts[0].alternate_mirrors) == 1
    assert not should_install_addon(plan.artifacts[1])


def test_manifest_canonicalizes_model_platform_casing() -> None:
    manifest = _manifest()
    part_seven = next(
        artifact for artifact in manifest.artifacts if artifact.artifact_id == "part-7"
    )
    part_seven.variants[0].platform = "Mac"

    plan = build_download_plan_from_manifest(
        _thread(),
        manifest,
        platform_priority=["mac", "windows"],
        preferred_hosts=["mega"],
        selected_parts=(7,),
        include_addons=False,
    )

    assert plan.artifacts[0].platform == "mac"
    assert all(mirror.platform == "mac" for mirror in plan.artifacts[0].mirrors)


def test_unresolved_game_mirrors_recover_one_authored_platform_group() -> None:
    manifest = _manifest()
    manifest.artifacts[0].variants[0].mirror_group = "unresolved"
    plan = build_download_plan_from_manifest(
        _thread(),
        manifest,
        platform_priority=["mac"],
        preferred_hosts=["mega"],
        selected_parts=(6,),
        include_addons=False,
    )
    game = plan.artifacts[0]
    assert game.host == "MEGA"
    assert [mirror.name for mirror in game.alternate_mirrors] == ["PIXELDRAIN"]
    assert game.warning is not None
    assert "recovered from one platform download group" in game.warning


def test_unresolved_game_links_across_source_groups_keep_only_preferred() -> None:
    manifest = _manifest()
    variant = manifest.artifacts[0].variants[0]
    variant.mirror_group = "unresolved"
    variant.link_ids = ["g001l000", "g003l000"]
    plan = build_download_plan_from_manifest(
        _thread(),
        manifest,
        platform_priority=["mac"],
        preferred_hosts=["mega"],
        selected_parts=(6,),
        include_addons=False,
    )

    game = plan.artifacts[0]
    assert game.host == "MEGA"
    assert game.alternate_mirrors == ()
    assert game.warning is not None
    assert "without fallbacks" in game.warning


def test_missing_game_version_is_inferred_from_part_attachment() -> None:
    manifest = _manifest()
    manifest.artifacts[0].version = None
    plan = build_download_plan_from_manifest(
        _thread(),
        manifest,
        platform_priority=["mac"],
        preferred_hosts=["mega"],
        selected_parts=(6,),
        include_addons=False,
    )
    assert plan.artifacts[0].version == "v0.95"


def test_manual_pdf_attachment_is_restored_as_separate_download() -> None:
    manifest = _manifest()
    guide = next(a for a in manifest.artifacts if a.artifact_id == "guide-6")
    guide.delivery = "manual"  # type: ignore[assignment]
    guide.install_action = "manual"  # type: ignore[assignment]
    plan = build_download_plan_from_manifest(
        _thread(),
        manifest,
        platform_priority=["mac"],
        preferred_hosts=["mega"],
        selected_parts=(6,),
        include_addons=True,
    )
    restored = next(a for a in plan.artifacts if a.kind == "addon")
    assert restored.install_action == "separate"


def test_low_confidence_addon_is_not_offered() -> None:
    manifest = _manifest()
    guide = next(a for a in manifest.artifacts if a.artifact_id == "guide-6")
    guide.confidence = 0.5
    plan = build_download_plan_from_manifest(
        _thread(),
        manifest,
        platform_priority=["mac"],
        preferred_hosts=["mega"],
        selected_parts=(6,),
        include_addons=True,
    )
    assert all(artifact.kind == "game" for artifact in plan.artifacts)
    assert any("below 0.70" in skipped.reason for skipped in plan.skipped)


def test_manual_forum_page_is_skipped_not_downloaded() -> None:
    plan = build_download_plan_from_manifest(
        _thread(),
        _manifest(),
        platform_priority=["mac"],
        preferred_hosts=["mega"],
        selected_parts=(7,),
        include_addons=True,
    )
    assert [artifact.title for artifact in plan.artifacts] == [
        "Grandma's House Part 7"
    ]
    assert plan.skipped[0].title == "Walkthrough mod"
    assert "manual" in plan.skipped[0].reason


def test_incremental_update_patch_is_not_offered_with_full_build() -> None:
    base = _thread()
    thread = replace(
        base,
        downloads=(
            *base.downloads,
            DownloadGroup("Update Patch", ()),
            DownloadGroup(
                "Win/Linux",
                (DownloadMirror("MEGA", "https://mega.nz/file/win-patch"),),
            ),
            DownloadGroup(
                "Mac",
                (
                    DownloadMirror("PIXELDRAIN", "https://pixeldrain.com/u/mac-patch"),
                    DownloadMirror("MEGA", "https://mega.nz/file/mac-patch"),
                ),
            ),
        ),
    )
    raw = _manifest().model_dump(mode="json")
    raw["artifacts"].append(
        {
            "artifact_id": "update-patch",
            "kind": "addon",
            "title": "Update Patch",
            "part_number": 7,
            "part_label": "Part 7",
            "version": "v0.108",
            "required": False,
            "delivery": "manual",
            "install_action": "merge",
            "variants": [
                {
                    "platform": "Windows/Linux",
                    "link_ids": ["g007l000"],
                    "mirror_group": "unresolved",
                    "confidence": 0.95,
                    "notes": [],
                },
                {
                    "platform": "Mac",
                    "link_ids": ["g008l000", "g008l001"],
                    "mirror_group": "unresolved",
                    "confidence": 0.95,
                    "notes": [],
                },
            ],
            "confidence": 0.95,
            "ambiguities": [],
        }
    )
    plan = build_download_plan_from_manifest(
        thread,
        DownloadManifest.model_validate(raw),
        platform_priority=["mac", "windows"],
        preferred_hosts=["mega"],
        selected_parts=(7,),
        include_addons=True,
    )
    assert all(artifact.title != "Update Patch" for artifact in plan.artifacts)
    skipped = next(item for item in plan.skipped if item.title == "Update Patch")
    assert "incremental update" in skipped.reason


def test_linked_forum_mod_is_replaced_with_child_thread_downloads() -> None:
    addon = ThreadInfo(
        thread_id=99,
        title="Grandma's House Walkthrough Mod",
        version="v0.101",
        thread_type=1,
        url="https://f95zone.to/threads/mod.99/",
        downloads=(
            DownloadGroup(
                "Download WT Mod v0.108aWT",
                (
                    DownloadMirror("VikingFile", "https://vikingfile.com/f/mod"),
                    DownloadMirror(
                        "MediaFire",
                        "https://www.mediafire.com/file/mod-v0.108.zip/file",
                    ),
                ),
            ),
        ),
    )
    manifest = _manifest()
    manifest_mod = next(
        artifact for artifact in manifest.artifacts if artifact.artifact_id == "mod"
    )
    manifest_mod.part_number = None
    manifest_mod.part_label = None
    plan = build_download_plan_from_manifest(
        _thread(),
        manifest,
        platform_priority=["mac"],
        preferred_hosts=["mediafire"],
        selected_parts=(7,),
        include_addons=True,
        discovered_addons=(addon,),
    )
    mod = next(artifact for artifact in plan.artifacts if artifact.title == "Walkthrough mod")
    assert mod.thread_id == 99
    assert mod.host == "MediaFire"
    assert mod.version == "v0.108aWT"
    assert mod.part == "Part 7"
    assert mod.install_action == "merge"
    assert all(skipped.title != "Walkthrough mod" for skipped in plan.skipped)

    part_6_plan = build_download_plan_from_manifest(
        _thread(),
        manifest,
        platform_priority=["mac"],
        preferred_hosts=["mediafire"],
        selected_parts=(6,),
        include_addons=True,
        discovered_addons=(addon,),
    )
    assert all(artifact.title != "Walkthrough mod" for artifact in part_6_plan.artifacts)
    assert all(skipped.title != "Walkthrough mod" for skipped in part_6_plan.skipped)


def test_plus_part_label_does_not_expand_to_later_parts() -> None:
    base = _thread()
    thread = replace(
        base,
        downloads=(
            *base.downloads,
            DownloadGroup(
                "Patches",
                (
                    DownloadMirror("Part 1", "https://mega.nz/file/patch1"),
                    DownloadMirror("Part 2+", "https://mega.nz/file/patch2"),
                ),
            ),
        ),
    )
    raw = _manifest().model_dump(mode="json")
    for index, label in enumerate(("Part 1", "Part 2+")):
        raw["artifacts"].append(
            {
                "artifact_id": f"patch-{index}",
                "kind": "addon",
                "title": f"Incest Patch {label}",
                "part_number": None,
                "part_label": label,
                "version": None,
                "required": False,
                "delivery": "manual",
                "install_action": "merge",
                "variants": [
                    {
                        "platform": None,
                        "link_ids": [f"g006l00{index}"],
                        "mirror_group": "unresolved",
                        "confidence": 0.95,
                        "notes": [],
                    }
                ],
                "confidence": 0.95,
                "ambiguities": [],
            }
        )
    plan = build_download_plan_from_manifest(
        thread,
        DownloadManifest.model_validate(raw),
        platform_priority=["mac"],
        preferred_hosts=["mega"],
        selected_parts=(7,),
        include_addons=True,
    )
    patches = [
        artifact.title
        for artifact in plan.artifacts
        if artifact.title.startswith("Incest Patch")
    ]
    assert patches == []


def test_part_detection_comes_from_manifest_not_group_regexes() -> None:
    detection = part_detection_from_manifest(_manifest())
    assert detection.is_multipart
    assert [(part.number, part.label) for part in detection.parts] == [
        (6, "Part 6"),
        (7, "Part 7"),
    ]


def _two_variant_thread() -> ThreadInfo:
    return ThreadInfo(
        thread_id=71348,
        title="The Coven",
        version="v0.10.1",
        thread_type=1,
        url="https://f95zone.to/threads/.71348/",
        downloads=(
            DownloadGroup(
                "Mac", (DownloadMirror("MEGA", "https://mega.nz/file/coven"),)
            ),
            DownloadGroup(
                "Gallery Unlock — H3PRm6a.png",
                (
                    DownloadMirror(
                        "F95 ATTACHMENT",
                        "https://attachments.f95zone.to/2022/09/2041872_H3PRm6a.png",
                    ),
                ),
            ),
            DownloadGroup(
                "Gallery Unlock — unlocker.rpy",
                (
                    DownloadMirror(
                        "F95 ATTACHMENT",
                        "https://attachments.f95zone.to/2022/05/1820744_unlocker.rpy",
                    ),
                ),
            ),
        ),
    )


def _two_variant_manifest() -> DownloadManifest:
    return DownloadManifest.model_validate(
        {
            "schema_version": 1,
            "thread_id": 71348,
            "title": "The Coven",
            "multipart": False,
            "artifacts": [
                {
                    "artifact_id": "the-coven",
                    "kind": "game",
                    "title": "The Coven",
                    "part_number": None,
                    "part_label": None,
                    "version": "v0.10.1",
                    "required": True,
                    "delivery": "download",
                    "install_action": "game",
                    "variants": [
                        {
                            "platform": "mac",
                            "link_ids": ["g000l000"],
                            "mirror_group": "the-coven-mac",
                            "confidence": 0.99,
                            "notes": [],
                        }
                    ],
                    "confidence": 0.98,
                    "ambiguities": [],
                },
                {
                    "artifact_id": "gallery-unlock",
                    "kind": "addon",
                    "title": "Gallery Unlock",
                    "part_number": None,
                    "part_label": None,
                    "version": None,
                    "required": False,
                    "delivery": "download",
                    "install_action": "merge",
                    "variants": [
                        {
                            "platform": None,
                            "link_ids": ["g001l000"],
                            "mirror_group": "unresolved",
                            "confidence": 0.45,
                            "notes": ["the PNG's role is unclear"],
                        },
                        {
                            "platform": None,
                            "link_ids": ["g002l000"],
                            "mirror_group": "unresolved",
                            "confidence": 0.9,
                            "notes": ["the RPY is likely the functional unlocker"],
                        },
                    ],
                    "confidence": 0.72,
                    "ambiguities": [],
                },
            ],
            "ambiguities": [],
            "warnings": [],
            "confidence": 0.9,
        }
    )


def test_addon_uses_its_most_confident_neutral_variant() -> None:
    plan = build_download_plan_from_manifest(
        _two_variant_thread(),
        _two_variant_manifest(),
        platform_priority=["mac"],
        preferred_hosts=[],
        selected_parts=None,
        include_addons=True,
    )
    addons = [artifact for artifact in plan.artifacts if artifact.kind == "addon"]
    assert [artifact.title for artifact in addons] == ["Gallery Unlock"]
    assert addons[0].locator.endswith("unlocker.rpy")


def test_link_free_trailing_sections_are_dropped() -> None:
    base = _thread()
    thread = replace(
        base,
        downloads=(
            *base.downloads,
            DownloadGroup("DoverUK25 thanks for the link", ()),
            DownloadGroup("*Unofficial port, download at your own risk.", ()),
        ),
    )
    sections = build_forum_sections(
        thread,
        "Part 6\nMac downloads\nPart 7\nMac downloads\nWALKTHROUGH MOD\n"
        "DoverUK25 thanks for the link\n*Unofficial port, download at your own risk.",
        max_groups=10,
        max_excerpt_chars=1000,
    )
    assert [section.name for section in sections] == [
        "Part 6",
        "Part 7",
        "Optional downloads",
    ]
    # The dropped headings' text is absorbed by the last surviving section
    # rather than lost, because it no longer has a next heading to stop at.
    assert "own risk" in sections[-1].post_excerpt


def _unlinked_addon_manifest() -> DownloadManifest:
    raw = _two_variant_manifest().model_dump(mode="json")
    raw["artifacts"].append(
        {
            "artifact_id": "walkthrough-mod",
            "kind": "addon",
            "title": "Walkthrough Mod",
            "part_number": None,
            "part_label": None,
            "version": None,
            "required": False,
            "delivery": "manual",
            "install_action": "manual",
            "variants": [
                {
                    "platform": None,
                    "link_ids": [],
                    "mirror_group": "unresolved",
                    "confidence": 0.9,
                    "notes": ["named under Extras but this thread publishes no link"],
                }
            ],
            "confidence": 0.9,
            "ambiguities": [],
        }
    )
    return DownloadManifest.model_validate(raw)


def test_addon_named_without_a_link_is_reported_not_fabricated() -> None:
    manifest = _unlinked_addon_manifest()
    validate_manifest_references(
        manifest,
        thread_id=71348,
        valid_link_ids={"g000l000", "g001l000", "g002l000"},
        require_game=True,
    )
    plan = build_download_plan_from_manifest(
        _two_variant_thread(),
        manifest,
        platform_priority=["mac"],
        preferred_hosts=[],
        selected_parts=None,
        include_addons=True,
    )
    assert "Walkthrough Mod" not in [artifact.title for artifact in plan.artifacts]
    reason = next(
        item.reason for item in plan.skipped if item.title == "Walkthrough Mod"
    )
    assert "no download link" in reason
