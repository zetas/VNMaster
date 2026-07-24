from __future__ import annotations

import os
import zipfile
from pathlib import Path

import pytest

from vnmaster.downloads.urm import URM_RPA_NAME, UrmInstallError, install_urm_mod


def _write_urm_zip(
    mods_dir: Path,
    *,
    name: str = "_0x52_URM.zip",
    payload: bytes = b"urm",
) -> Path:
    mods_dir.mkdir(parents=True, exist_ok=True)
    archive = mods_dir / name
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(f"release/{URM_RPA_NAME}", payload)
    return archive


def test_install_urm_into_macos_app_game_directory(tmp_path: Path) -> None:
    game_root = tmp_path / "extracted"
    target_dir = (
        game_root / "Example.app" / "Contents" / "Resources" / "autorun" / "game"
    )
    target_dir.mkdir(parents=True)
    (target_dir / "script.rpyc").write_bytes(b"renpy")
    mods_dir = tmp_path / "Mods"
    _write_urm_zip(mods_dir, payload=b"mac-urm")

    installed = install_urm_mod(game_root, mods_dir, platform="mac")

    assert installed == target_dir / URM_RPA_NAME
    assert installed.read_bytes() == b"mac-urm"


def test_install_urm_into_windows_game_directory(tmp_path: Path) -> None:
    game_root = tmp_path / "extracted"
    target_dir = game_root / "Example-1.0-pc" / "game"
    target_dir.mkdir(parents=True)
    (target_dir / "archive.rpa").write_bytes(b"game")
    mods_dir = tmp_path / "Mods"
    _write_urm_zip(mods_dir)

    installed = install_urm_mod(game_root, mods_dir, platform="windows")

    assert installed == target_dir / URM_RPA_NAME
    assert installed.read_bytes() == b"urm"


def test_install_urm_prefers_platform_when_bundle_contains_mac_and_pc(
    tmp_path: Path,
) -> None:
    game_root = tmp_path / "extracted"
    mac_dir = (
        game_root / "Example.app" / "Contents" / "Resources" / "autorun" / "game"
    )
    pc_dir = game_root / "Example-1.0-pc" / "game"
    for target in (mac_dir, pc_dir):
        target.mkdir(parents=True)
        (target / "script.rpyc").write_bytes(b"renpy")
    mods_dir = tmp_path / "Mods"
    _write_urm_zip(mods_dir)

    installed = install_urm_mod(game_root, mods_dir, platform="mac")

    assert installed == mac_dir / URM_RPA_NAME
    assert not (pc_dir / URM_RPA_NAME).exists()


def test_install_urm_uses_newest_matching_zip(tmp_path: Path) -> None:
    game_root = tmp_path / "extracted"
    target_dir = game_root / "game"
    target_dir.mkdir(parents=True)
    (target_dir / "script.rpy").write_text("label start:")
    mods_dir = tmp_path / "Mods"
    old_archive = _write_urm_zip(mods_dir, name="old.zip", payload=b"old")
    new_archive = _write_urm_zip(mods_dir, name="new.zip", payload=b"new")
    os.utime(old_archive, (1, 1))
    os.utime(new_archive, (2, 2))

    installed = install_urm_mod(game_root, mods_dir, platform=None)

    assert installed is not None
    assert installed.read_bytes() == b"new"


def test_install_urm_skips_non_renpy_build_without_requiring_zip(
    tmp_path: Path,
) -> None:
    game_root = tmp_path / "extracted"
    game_root.mkdir()
    (game_root / "game.exe").write_bytes(b"not renpy")

    assert install_urm_mod(game_root, tmp_path / "missing", platform="windows") is None


def test_install_urm_requires_zip_containing_rpa(tmp_path: Path) -> None:
    game_root = tmp_path / "extracted"
    target_dir = game_root / "game"
    target_dir.mkdir(parents=True)
    (target_dir / "script.rpyc").write_bytes(b"renpy")
    mods_dir = tmp_path / "Mods"
    mods_dir.mkdir()
    with zipfile.ZipFile(mods_dir / "other.zip", "w") as bundle:
        bundle.writestr("readme.txt", "not URM")

    with pytest.raises(UrmInstallError, match="No ZIP containing"):
        install_urm_mod(game_root, mods_dir, platform="windows")
