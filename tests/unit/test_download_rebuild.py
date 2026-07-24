from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from vnmaster.downloads.rebuild import RebuildError, rebuild_install
from vnmaster.downloads.state import InstallState, hash_payload
from vnmaster.downloads.urm import URM_RPA_NAME


def _state(tmp_path: Path) -> InstallState:
    install = tmp_path / "Games" / "A Game" / "v1"
    current_game = install / "game" / "Example-pc" / "game"
    current_game.mkdir(parents=True)
    (current_game / "script.rpyc").write_bytes(b"modified")
    game_archive = install / "archive" / "game.zip"
    addon_archive = install / "archive" / "addons" / "02-Mod" / "mod.zip"
    game_archive.parent.mkdir(parents=True)
    addon_archive.parent.mkdir(parents=True)
    game_archive.write_bytes(b"game archive")
    addon_archive.write_bytes(b"mod archive")
    artifacts: tuple[dict[str, object], ...] = (
        {
            "kind": "game",
            "title": "A Game",
            "archive_paths": ["archive/game.zip"],
            "output_path": "game",
            "installable": False,
        },
        {
            "kind": "addon",
            "title": "A Game Multi-Mod",
            "archive_paths": ["archive/addons/02-Mod/mod.zip"],
            "output_path": "addons/A Game Multi-Mod",
            "installable": True,
        },
    )
    return InstallState(
        id=1,
        f95_thread_id=42,
        game_title="A Game",
        version="v1",
        install_path=install,
        thread_url="https://f95zone.to/threads/42",
        platform="windows",
        host="MEGA",
        source_locator="masked",
        artifacts=artifacts,
        archive_hashes={
            "archive/game.zip": hash_payload(game_archive),
            "archive/addons/02-Mod/mod.zip": hash_payload(addon_archive),
        },
        verification_checks=(),
        renpy_game_dir=Path("game/Example-pc/game"),
        urm_path=Path("game/Example-pc/game/0x52_URM.rpa"),
        installed_at=1,
        updated_at=1,
        last_rebuilt_at=None,
    )


def _mods_dir(tmp_path: Path) -> Path:
    mods = tmp_path / "Games" / "Mods"
    mods.mkdir(parents=True)
    with zipfile.ZipFile(mods / "_0x52_URM.zip", "w") as bundle:
        bundle.writestr(URM_RPA_NAME, b"urm")
    return mods


def _unpacker(downloaded: list[Path], destination: Path) -> None:
    if downloaded[0].name == "game.zip":
        game_dir = destination / "Example-pc" / "game"
        game_dir.mkdir(parents=True)
        (game_dir / "script.rpyc").write_bytes(b"clean")
        scene = game_dir / "code" / "scene.rpyc"
        scene.parent.mkdir()
        scene.write_bytes(b"original")
        return
    packaged = destination / "Multi-Mod" / "game"
    scene = packaged / "code" / "scene.rpyc"
    scene.parent.mkdir(parents=True)
    scene.write_bytes(b"modded")
    new_file = packaged / "gui" / "cheat.png"
    new_file.parent.mkdir()
    new_file.write_bytes(b"new")


def test_rebuild_reextracts_reapplies_and_preserves_backup(tmp_path: Path) -> None:
    state = _state(tmp_path)
    messages: list[str] = []

    result = rebuild_install(
        state,
        urm_mods_dir=_mods_dir(tmp_path),
        unpacker=_unpacker,
        reporter=messages.append,
    )

    game_dir = state.install_path / "game" / "Example-pc" / "game"
    assert (game_dir / "script.rpyc").read_bytes() == b"clean"
    assert (game_dir / "code" / "scene.rpyc").read_bytes() == b"modded"
    assert (game_dir / "gui" / "cheat.png").read_bytes() == b"new"
    assert (game_dir / URM_RPA_NAME).read_bytes() == b"urm"
    assert result.backup_path is not None
    assert (
        result.backup_path / "game" / "Example-pc" / "game" / "script.rpyc"
    ).read_bytes() == b"modified"
    assert any("Rebuild add-on preview" in message for message in messages)


def test_rebuild_can_discard_previous_game(tmp_path: Path) -> None:
    state = _state(tmp_path)

    result = rebuild_install(
        state,
        urm_mods_dir=_mods_dir(tmp_path),
        keep_backup=False,
        unpacker=_unpacker,
    )

    assert result.backup_path is None
    assert not (state.install_path / "backups").exists()


def test_rebuild_refuses_tampered_archive_before_replacing_game(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    original = state.install_path / "game" / "Example-pc" / "game" / "script.rpyc"
    (state.install_path / "archive" / "game.zip").write_bytes(b"tampered")

    with pytest.raises(RebuildError, match="checksum mismatch"):
        rebuild_install(
            state,
            urm_mods_dir=_mods_dir(tmp_path),
            unpacker=_unpacker,
        )

    assert original.read_bytes() == b"modified"
