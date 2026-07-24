from __future__ import annotations

from pathlib import Path

import pytest

from vnmaster.downloads.addon_installer import AddonInstallResult
from vnmaster.downloads.urm import URM_RPA_NAME
from vnmaster.downloads.verification import InstallVerificationError, verify_install


def _game(tmp_path: Path) -> tuple[Path, Path]:
    game_root = tmp_path / "game"
    renpy_dir = game_root / "Example-pc" / "game"
    renpy_dir.mkdir(parents=True)
    (renpy_dir / "script.rpyc").write_bytes(b"renpy")
    return game_root, renpy_dir


def test_verify_install_checks_archives_addons_and_urm(tmp_path: Path) -> None:
    game_root, renpy_dir = _game(tmp_path)
    archive = tmp_path / "archive" / "game.zip"
    archive.parent.mkdir()
    archive.write_bytes(b"archive")
    (renpy_dir / URM_RPA_NAME).write_bytes(b"urm")
    addon = AddonInstallResult(renpy_dir, 3, 1, None)

    result = verify_install(
        game_root,
        platform="windows",
        archive_paths=(archive,),
        addon_results=(addon,),
        urm_installed=True,
    )

    assert result.renpy_game_dir == renpy_dir
    assert f"verified {URM_RPA_NAME}" in result.checks


def test_verify_install_rejects_missing_urm(tmp_path: Path) -> None:
    game_root, _renpy_dir = _game(tmp_path)
    archive = tmp_path / "game.zip"
    archive.write_bytes(b"archive")

    with pytest.raises(InstallVerificationError, match=URM_RPA_NAME):
        verify_install(
            game_root,
            platform="windows",
            archive_paths=(archive,),
            addon_results=(),
            urm_installed=True,
        )
