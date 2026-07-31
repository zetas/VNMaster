from __future__ import annotations

import pytest

from vnmaster.downloads.models import DownloadGroup, DownloadMirror, ThreadInfo
from vnmaster.downloads.selector import (
    NoCompatibleDownloadError,
    addon_matches_game,
    build_download_plan,
    detect_parts,
    is_requested_addon,
)


def _thread(
    thread_id: int,
    title: str,
    version: str,
    groups: tuple[DownloadGroup, ...],
) -> ThreadInfo:
    return ThreadInfo(
        thread_id=thread_id,
        title=title,
        version=version,
        thread_type=None,
        url=f"https://f95zone.to/threads/.{thread_id}/",
        downloads=groups,
    )


def _group(name: str, host: str = "MEGA") -> DownloadGroup:
    return DownloadGroup(
        name,
        (DownloadMirror(host, f"https://f95zone.to/masked/{host.lower()}/x"),),
    )


def test_selects_mac_full_build_before_windows() -> None:
    game = _thread(1, "A Game", "v1.2", (_group("Win/Linux"), _group("Mac")))
    plan = build_download_plan(
        game, [], platform_priority=["mac", "windows", "linux"],
        preferred_hosts=["mega"],
    )
    assert plan.artifacts[0].group_name == "Mac"
    assert plan.artifacts[0].platform == "mac"


def test_falls_back_to_windows() -> None:
    game = _thread(1, "A Game", "v1.2", (_group("Win/Linux"),))
    plan = build_download_plan(
        game, [], platform_priority=["mac", "windows", "linux"],
        preferred_hosts=["mega"],
    )
    assert plan.artifacts[0].platform == "windows"


def test_keeps_lower_priority_platforms_as_runtime_fallbacks() -> None:
    game = _thread(
        1,
        "A Game",
        "v1.2",
        (_group("Win/Linux", "GOFILE"), _group("Mac", "BUZZHEAVIER")),
    )
    plan = build_download_plan(
        game,
        [],
        platform_priority=["mac", "windows", "linux"],
        preferred_hosts=["mega"],
    )
    assert [(mirror.platform, mirror.name) for mirror in plan.artifacts[0].mirrors] == [
        ("mac", "BUZZHEAVIER"),
        ("windows", "GOFILE"),
    ]


def test_preferred_host_does_not_jump_ahead_of_other_mac_mirrors() -> None:
    mirrors = (
        DownloadMirror("BUZZHEAVIER", "https://bzzhr.to/a"),
        DownloadMirror("GOFILE", "https://gofile.io/d/a"),
        DownloadMirror("DATANODES", "https://datanodes.to/a"),
    )
    game = _thread(
        1,
        "A Game",
        "v1.2",
        (DownloadGroup("Win/Linux", mirrors), DownloadGroup("Mac", mirrors)),
    )
    plan = build_download_plan(
        game,
        [],
        platform_priority=["mac", "windows", "linux"],
        preferred_hosts=["gofile"],
        allow_host_fallback=True,
    )
    assert [(mirror.platform, mirror.name) for mirror in plan.artifacts[0].mirrors] == [
        ("mac", "GOFILE"),
        ("mac", "BUZZHEAVIER"),
        ("mac", "DATANODES"),
        ("windows", "GOFILE"),
        ("windows", "BUZZHEAVIER"),
        ("windows", "DATANODES"),
    ]


def test_rejects_update_only_group() -> None:
    game = _thread(1, "A Game", "v1.2", (_group("Mac update only"),))
    with pytest.raises(NoCompatibleDownloadError):
        build_download_plan(
            game, [], platform_priority=["mac"], preferred_hosts=["mega"]
        )


def test_addon_keywords() -> None:
    assert is_requested_addon("A Game Walkthrough Mod")
    assert is_requested_addon("A Game Multi-Mod")
    assert is_requested_addon("A Game Translation")
    assert is_requested_addon("A Game Patch")
    assert not is_requested_addon("A Game Discussion")


def test_addon_version_mismatch_is_offered_with_warning() -> None:
    game = _thread(1, "A Game", "v1.2", (_group("Mac"),))
    addon = _thread(2, "A Game Multi-Mod", "v1.1", (_group("Win/Mac"),))
    compatible, reason = addon_matches_game(game, addon)
    assert compatible
    assert "may not match" in reason

    plan = build_download_plan(
        game, [addon], platform_priority=["mac"], preferred_hosts=["mega"]
    )
    assert plan.artifacts[1].warning == reason


def test_addon_with_unknown_version_is_offered_for_manual_choice() -> None:
    game = _thread(1, "A Game", "v1.2", (_group("Mac"),))
    addon = _thread(2, "A Game Patch", "", (_group("Patch"),))
    compatible, note = addon_matches_game(game, addon)
    assert compatible
    assert "not stated" in note


def test_compatible_addon_is_added_to_plan() -> None:
    game = _thread(1, "A Game", "v1.2", (_group("Mac"),))
    addon = _thread(2, "A Game Multi-Mod", "v1.2", (_group("Win/Linux/Mac"),))
    plan = build_download_plan(
        game, [addon], platform_priority=["mac"], preferred_hosts=["mega"]
    )
    assert [artifact.kind for artifact in plan.artifacts] == ["game", "addon"]


def test_addon_falls_back_to_an_unpreferred_host() -> None:
    game = _thread(1, "A Game", "v1.2", (_group("Mac"),))
    addon = _thread(
        2, "A Game Multi-Mod", "v1.2", (_group("Win/Mac", "GOFILE"),)
    )
    plan = build_download_plan(
        game, [addon], platform_priority=["mac"], preferred_hosts=["mega"]
    )
    assert plan.artifacts[1].host == "GOFILE"


def test_preserves_all_mirrors_in_preferred_then_source_order() -> None:
    game = _thread(
        1,
        "A Game",
        "v1.2",
        (
            DownloadGroup(
                "Mac",
                (
                    DownloadMirror("BUZZHEAVIER", "https://bzzhr.to/a"),
                    DownloadMirror("MEGA", "https://mega.nz/file/a#key"),
                    DownloadMirror("GOFILE", "https://gofile.io/d/a"),
                ),
            ),
        ),
    )
    plan = build_download_plan(
        game, [], platform_priority=["mac"], preferred_hosts=["mega"]
    )
    artifact = plan.artifacts[0]
    assert [mirror.name for mirror in artifact.mirrors] == [
        "MEGA",
        "BUZZHEAVIER",
        "GOFILE",
    ]


def test_forced_host_retains_only_matching_mirrors() -> None:
    game = _thread(
        1,
        "A Game",
        "v1.2",
        (
            DownloadGroup(
                "Mac",
                (
                    DownloadMirror("BUZZHEAVIER", "https://bzzhr.to/a"),
                    DownloadMirror("MEGA", "https://mega.nz/file/a#key"),
                ),
            ),
        ),
    )
    plan = build_download_plan(
        game,
        [],
        platform_priority=["mac"],
        preferred_hosts=["mega"],
        allow_host_fallback=False,
    )
    assert [mirror.name for mirror in plan.artifacts[0].mirrors] == ["MEGA"]


def test_forced_game_host_does_not_hide_addons_on_other_hosts() -> None:
    game = _thread(1, "A Game", "v1.2", (_group("Mac"),))
    addon = _thread(
        2, "A Game Multi-Mod", "v1.2", (_group("Win/Mac", "GOFILE"),)
    )
    plan = build_download_plan(
        game,
        [addon],
        platform_priority=["mac"],
        preferred_hosts=["mega"],
        allow_host_fallback=False,
    )
    assert len(plan.artifacts) == 2
    assert plan.artifacts[0].host == "MEGA"
    assert plan.artifacts[1].host == "GOFILE"


def test_forced_game_host_does_not_hide_embedded_attachment() -> None:
    game = _thread(
        1,
        "A Game",
        "v1.2",
        (
            _group("Mac", "GOFILE"),
            DownloadGroup(
                "Game patch",
                (
                    DownloadMirror(
                        "F95 ATTACHMENT",
                        "https://attachments.f95zone.to/patch.rar",
                    ),
                ),
            ),
        ),
    )
    plan = build_download_plan(
        game,
        [],
        platform_priority=["mac"],
        preferred_hosts=["gofile"],
        allow_host_fallback=False,
    )
    assert [artifact.host for artifact in plan.artifacts] == [
        "GOFILE",
        "F95 ATTACHMENT",
    ]


def test_patch_group_in_game_thread_is_an_optional_artifact() -> None:
    game = _thread(1, "A Game", "v1.2", (_group("Mac"), _group("Patch")))
    plan = build_download_plan(
        game, [], platform_priority=["mac"], preferred_hosts=["mega"]
    )
    assert [artifact.kind for artifact in plan.artifacts] == ["game", "addon"]
    assert plan.artifacts[1].title == "A Game — Patch"


def test_extras_attachment_is_an_optional_artifact() -> None:
    game = _thread(
        1,
        "A Game",
        "v2",
        (
            _group("Mac"),
            DownloadGroup(
                "Game patch",
                (
                    DownloadMirror(
                        "F95 ATTACHMENT",
                        "https://attachments.f95zone.to/2026/03/123_patch.rar",
                    ),
                ),
            ),
        ),
    )
    plan = build_download_plan(
        game, [], platform_priority=["mac"], preferred_hosts=["mega"]
    )
    assert plan.artifacts[1].title == "A Game — Game patch"
    assert plan.artifacts[1].host == "F95 ATTACHMENT"


def test_detects_parts_from_numbered_groups() -> None:
    groups = (
        _group("Part 1 - Win"), _group("Part 1 - Mac"),
        _group("Part 2 - Win"), _group("Part 2 - Mac"),
    )
    detection = detect_parts(groups)
    assert detection.is_multipart
    assert detection.family == "part"
    assert [p.number for p in detection.parts] == [1, 2]
    assert detection.parts[0].label == "Part 1"
    assert detection.parts[0].group_indexes == (0, 1)


def test_alias_pt_folds_into_part_family() -> None:
    detection = detect_parts((_group("Pt. 1 Win"), _group("Pt. 2 Win")))
    assert detection.family == "part"
    assert detection.parts[1].label == "Part 2"


def test_lone_season_number_is_not_multipart() -> None:
    detection = detect_parts((_group("Season 2 - Win"), _group("Season 2 - Mac")))
    assert not detection.is_multipart
    assert detection.parts == ()


def test_version_numbers_do_not_trigger_detection() -> None:
    detection = detect_parts((_group("Win v1.2"), _group("Mac v1.2")))
    assert not detection.is_multipart


def test_update_groups_and_optional_groups_are_excluded() -> None:
    groups = (
        _group("Ch. 5 Update"),          # rejected by the update filter
        _group("Part 2 walkthrough"),    # optional group, add-on material
        _group("Part 1 - Win"), _group("Part 2 - Win"),
    )
    detection = detect_parts(groups)
    assert detection.family == "part"
    assert all(0 not in p.group_indexes and 1 not in p.group_indexes
               for p in detection.parts)


def test_family_with_more_numbers_wins() -> None:
    groups = (
        _group("Chapter 1"), _group("Chapter 2"), _group("Chapter 3"),
        _group("Book 1"), _group("Book 2"),
    )
    assert detect_parts(groups).family == "chapter"


def test_tie_breaks_by_priority_order() -> None:
    groups = (
        _group("Act 1"), _group("Act 2"),
        _group("Episode 1"), _group("Episode 2"),
    )
    assert detect_parts(groups).family == "episode"


def test_composite_numbering_falls_back_with_warning() -> None:
    groups = (
        _group("Season 1 Episode 1"), _group("Season 1 Episode 2"),
        _group("Season 2 Episode 1"), _group("Season 2 Episode 2"),
    )
    detection = detect_parts(groups)
    assert not detection.is_multipart
    assert detection.warnings and "composite" in detection.warnings[0]


def test_range_numbered_group_is_ignored_with_warning() -> None:
    groups = (_group("Part 1"), _group("Part 2"), _group("Part 1-2 bundle"))
    detection = detect_parts(groups)
    assert detection.is_multipart
    owned = {i for p in detection.parts for i in p.group_indexes}
    assert 2 not in owned
    assert any("Part 1-2 bundle" in w for w in detection.warnings)
