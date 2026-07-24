"""Structural verification for staged and rebuilt game installations."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from vnmaster.downloads.addon_installer import AddonInstallResult
from vnmaster.downloads.renpy import RenPyLayoutError, find_renpy_game_dir
from vnmaster.downloads.urm import URM_RPA_NAME


class InstallVerificationError(RuntimeError):
    pass


@dataclass(frozen=True)
class InstallVerification:
    checks: tuple[str, ...]
    renpy_game_dir: Path | None


def verify_install(
    game_root: Path,
    *,
    platform: str | None,
    archive_paths: tuple[Path, ...],
    addon_results: tuple[AddonInstallResult, ...],
    urm_installed: bool,
) -> InstallVerification:
    """Verify expected payloads and return the detected Ren'Py directory."""
    failures: list[str] = []
    checks: list[str] = []

    if not game_root.is_dir() or not any(game_root.iterdir()):
        failures.append("extracted game directory is missing or empty")
    else:
        checks.append("extracted game payload is present")

    missing_archives = [path for path in archive_paths if not path.exists()]
    if missing_archives:
        failures.append(
            "preserved archive payload is missing: "
            + ", ".join(str(path) for path in missing_archives)
        )
    else:
        checks.append(f"preserved {len(archive_paths)} archive payload(s)")

    try:
        renpy_game_dir = find_renpy_game_dir(game_root, platform=platform)
    except RenPyLayoutError as exc:
        failures.append(str(exc))
        renpy_game_dir = None

    if addon_results or urm_installed:
        if renpy_game_dir is None:
            failures.append("Ren'Py game directory was not found after mod installation")
        else:
            checks.append("resolved one active Ren'Py game directory")

    for result in addon_results:
        if result.files_installed <= 0:
            failures.append("an add-on reported zero installed files")
    if addon_results:
        checks.append(f"verified {len(addon_results)} installed add-on(s)")

    if urm_installed:
        if renpy_game_dir is None or not (renpy_game_dir / URM_RPA_NAME).is_file():
            failures.append(f"{URM_RPA_NAME} is missing from the Ren'Py game directory")
        else:
            checks.append(f"verified {URM_RPA_NAME}")

    if failures:
        raise InstallVerificationError("; ".join(failures))
    return InstallVerification(tuple(checks), renpy_game_dir)
