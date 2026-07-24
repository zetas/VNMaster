from __future__ import annotations

from pathlib import Path

import pytest

from vnmaster.downloads.addon_installer import (
    AddonInstallError,
    install_addon,
    should_install_addon,
)
from vnmaster.downloads.models import PlannedArtifact


def _artifact(title: str, group_name: str = "") -> PlannedArtifact:
    return PlannedArtifact(
        kind="addon",
        title=title,
        version="v1",
        thread_id=2,
        thread_url="https://f95zone.to/threads/2",
        group_name=group_name,
        platform=None,
        host="MEGA",
        locator="https://mega.nz/file/example",
    )


def _game_dir(game_root: Path) -> Path:
    target = game_root / "Example.app" / "Contents" / "Resources" / "autorun" / "game"
    target.mkdir(parents=True)
    (target / "script.rpyc").write_bytes(b"renpy")
    return target


@pytest.mark.parametrize(
    "title",
    [
        "A Game Multi-Mod",
        "A Game Patch",
        "A Game Cheat Mod",
        "A Game Gallery Unlock",
        "A Game Translation",
        "A Game Hotfix",
    ],
)
def test_should_install_game_modifying_addons(title: str) -> None:
    assert should_install_addon(_artifact(title))


def test_should_not_install_standalone_walkthrough() -> None:
    assert not should_install_addon(_artifact("A Game In-Depth Walkthrough"))


def test_merges_wrapped_game_tree_and_overwrites_existing_files(
    tmp_path: Path,
) -> None:
    game_root = tmp_path / "extracted-game"
    target = _game_dir(game_root)
    existing = target / "code" / "chapter.rpyc"
    existing.parent.mkdir()
    existing.write_bytes(b"original")

    addon_root = tmp_path / "addon"
    packaged_game = addon_root / "Artemis Multi-Mod" / "game"
    replacement = packaged_game / "code" / "chapter.rpyc"
    replacement.parent.mkdir(parents=True)
    replacement.write_bytes(b"modded")
    new_file = packaged_game / "gui" / "cheat.png"
    new_file.parent.mkdir()
    new_file.write_bytes(b"new")

    result = install_addon(addon_root, game_root, platform="mac")

    assert existing.read_bytes() == b"modded"
    assert (target / "gui" / "cheat.png").read_bytes() == b"new"
    assert result.files_installed == 2
    assert result.files_overwritten == 1
    assert result.readme is None


def test_readme_can_direct_unwrapped_payload_to_distribution_root(
    tmp_path: Path,
) -> None:
    game_root = tmp_path / "extracted-game"
    target = _game_dir(game_root)
    addon_root = tmp_path / "addon"
    addon_root.mkdir()
    (addon_root / "README.txt").write_text(
        "Copy the included files to the main game folder where the executable is."
    )
    (addon_root / "patch.dat").write_bytes(b"patch")

    result = install_addon(addon_root, game_root, platform="mac")

    assert result.target_dir == target.parent
    assert (target.parent / "patch.dat").read_bytes() == b"patch"
    assert not (target.parent / "README.txt").exists()
    assert result.readme == addon_root / "README.txt"


def test_no_readme_places_unwrapped_payload_in_renpy_game_dir(
    tmp_path: Path,
) -> None:
    game_root = tmp_path / "extracted-game"
    target = _game_dir(game_root)
    addon_root = tmp_path / "addon"
    wrapper = addon_root / "Patch Files"
    wrapper.mkdir(parents=True)
    (wrapper / "patch.rpa").write_bytes(b"patch")

    result = install_addon(addon_root, game_root, platform="mac")

    assert (target / "patch.rpa").read_bytes() == b"patch"
    assert result.target_dir == target


def test_install_addon_requires_renpy_game(tmp_path: Path) -> None:
    addon_root = tmp_path / "addon"
    addon_root.mkdir()
    (addon_root / "patch.rpa").write_bytes(b"patch")
    game_root = tmp_path / "game"
    game_root.mkdir()

    with pytest.raises(AddonInstallError, match="Could not find"):
        install_addon(addon_root, game_root, platform="windows")
