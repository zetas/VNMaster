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


def _multipart_state(tmp_path: Path, *, parts: tuple[str, ...]) -> InstallState:
    root = tmp_path / "Games" / "A Game" / "v1"
    artifacts: list[dict[str, object]] = []
    hashes: dict[str, str] = {}
    for label in parts:
        game_dir = root / label / "game"
        game_dir.mkdir(parents=True)
        (game_dir / "data.bin").write_bytes(f"{label} original".encode())
        archive = root / label / "archive" / "payload.zip"
        archive.parent.mkdir(parents=True)
        archive.write_bytes(f"{label} archive".encode())
        artifacts.append(
            {
                "kind": "game",
                "title": "A Game",
                "part": label,
                "archive_paths": [f"{label}/archive/payload.zip"],
                "output_path": f"{label}/game",
                "installable": False,
                "platform": "windows",
            }
        )
        hashes[f"{label}/archive/payload.zip"] = hash_payload(archive)
    return InstallState(
        id=1,
        f95_thread_id=42,
        game_title="A Game",
        version="v1",
        install_path=root,
        thread_url="https://f95zone.to/threads/42",
        platform="windows",
        host="MEGA",
        source_locator="masked",
        artifacts=tuple(artifacts),
        archive_hashes=hashes,
        verification_checks=(),
        renpy_game_dir=None,
        urm_path=None,
        installed_at=1,
        updated_at=1,
        last_rebuilt_at=None,
    )


def _multipart_unpacker(downloaded: list[Path], destination: Path) -> None:
    destination.mkdir(parents=True)
    (destination / "data.bin").write_bytes(b"rebuilt")


def test_multipart_rebuild_rebuilds_each_part(tmp_path: Path) -> None:
    state = _multipart_state(tmp_path, parts=("Part 1", "Part 2"))

    result = rebuild_install(
        state,
        urm_mods_dir=tmp_path / "Mods",
        unpacker=_multipart_unpacker,
    )

    assert (state.install_path / "Part 1" / "game").is_dir()
    assert (state.install_path / "Part 2" / "game").is_dir()
    assert any(check.startswith("Part 1: ") for check in result.verification_checks)
    assert any(check.startswith("Part 2: ") for check in result.verification_checks)


def test_multipart_rebuild_failure_reports_progress(tmp_path: Path) -> None:
    state = _multipart_state(tmp_path, parts=("Part 1", "Part 2"))
    (state.install_path / "Part 2" / "archive" / "payload.zip").write_bytes(b"junk")

    with pytest.raises(RebuildError):
        rebuild_install(
            state,
            urm_mods_dir=tmp_path / "Mods",
            unpacker=_multipart_unpacker,
        )

    assert (state.install_path / "Part 1" / "game" / "data.bin").read_bytes() == b"rebuilt"


def test_legacy_rebuild_leaves_no_part_dirs(tmp_path: Path) -> None:
    state = _state(tmp_path)

    rebuild_install(
        state,
        urm_mods_dir=_mods_dir(tmp_path),
        unpacker=_unpacker,
    )

    assert (state.install_path / "game").is_dir()
    assert not any(p.name.startswith("Part ") for p in state.install_path.iterdir())
