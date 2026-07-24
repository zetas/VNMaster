"""Install Universal Ren'Py Mod into an extracted Ren'Py game."""
from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

from vnmaster.downloads.renpy import RenPyLayoutError, find_renpy_game_dir


URM_RPA_NAME = "0x52_URM.rpa"


class UrmInstallError(RuntimeError):
    pass


def install_urm_mod(
    game_root: Path,
    mods_dir: Path,
    *,
    platform: str | None,
) -> Path | None:
    """Install URM and return its target, or ``None`` for a non-Ren'Py build."""
    try:
        target_dir = find_renpy_game_dir(game_root, platform=platform)
    except RenPyLayoutError as exc:
        raise UrmInstallError(str(exc)) from exc
    if target_dir is None:
        return None

    archive, member = _find_urm_payload(mods_dir)
    target = target_dir / URM_RPA_NAME
    try:
        with zipfile.ZipFile(archive) as bundle:
            with bundle.open(member) as source, target.open("wb") as destination:
                shutil.copyfileobj(source, destination)
    except (OSError, KeyError, zipfile.BadZipFile) as exc:
        raise UrmInstallError(f"Could not extract URM from {archive}: {exc}") from exc
    return target


def _find_urm_payload(mods_dir: Path) -> tuple[Path, zipfile.ZipInfo]:
    if not mods_dir.is_dir():
        raise UrmInstallError(f"URM mods directory does not exist: {mods_dir}")

    matches: list[tuple[Path, zipfile.ZipInfo]] = []
    for archive in mods_dir.iterdir():
        if not archive.is_file() or archive.suffix.casefold() != ".zip":
            continue
        try:
            with zipfile.ZipFile(archive) as bundle:
                members = [
                    member
                    for member in bundle.infolist()
                    if not member.is_dir()
                    and Path(member.filename).name.casefold() == URM_RPA_NAME.casefold()
                ]
        except (OSError, zipfile.BadZipFile) as exc:
            if "urm" in archive.name.casefold():
                raise UrmInstallError(f"Could not read URM archive {archive}: {exc}") from exc
            continue
        if len(members) > 1:
            raise UrmInstallError(
                f"URM archive contains multiple {URM_RPA_NAME} files: {archive}"
            )
        if members:
            matches.append((archive, members[0]))

    if not matches:
        raise UrmInstallError(
            f"No ZIP containing {URM_RPA_NAME} was found in {mods_dir}"
        )
    return max(matches, key=lambda match: match[0].stat().st_mtime)
