"""Rebuild an installed game from preserved payloads and recorded state."""

from __future__ import annotations

import shutil
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from vnmaster.downloads.addon_installer import (
    AddonInstallResult,
    apply_addon_preview,
    preview_addon,
)
from vnmaster.downloads.archives import unpack_payload
from vnmaster.downloads.state import InstallState, InstallStateError, hash_payload
from vnmaster.downloads.urm import install_urm_mod
from vnmaster.downloads.verification import verify_install


class RebuildError(RuntimeError):
    pass


@dataclass(frozen=True)
class RebuildResult:
    install_path: Path
    backup_path: Path | None
    verification_checks: tuple[str, ...]


def rebuild_install(
    state: InstallState,
    *,
    urm_mods_dir: Path,
    keep_backup: bool = True,
    unpacker: Callable[[list[Path], Path], None] = unpack_payload,
    reporter: Callable[[str], None] = lambda _message: None,
) -> RebuildResult:
    """Re-extract, reapply recorded add-ons/URM, verify, and swap ``game/``."""
    install_path = state.install_path
    current_game = install_path / "game"
    if not install_path.is_dir() or not current_game.is_dir():
        raise RebuildError(f"Recorded install is missing its game directory: {install_path}")

    _verify_preserved_payloads(state, reporter=reporter)
    game_artifact = next(
        (artifact for artifact in state.artifacts if artifact.get("kind") == "game"),
        None,
    )
    if game_artifact is None:
        raise RebuildError("Recorded install has no game artifact")
    game_archives = _artifact_archives(install_path, game_artifact)
    if not game_archives:
        raise RebuildError("Recorded game artifact has no preserved payload")

    staging = Path(tempfile.mkdtemp(prefix=".vnmaster-rebuild-", dir=install_path.parent))
    old_holder = staging / "previous-game"
    old_moved = False
    new_installed = False
    try:
        reporter("Re-extracting preserved full build...")
        unpacker(game_archives, staging / "game")

        addon_results: list[AddonInstallResult] = []
        for index, artifact in enumerate(state.artifacts, start=1):
            if artifact.get("kind") != "addon" or not artifact.get("installable"):
                continue
            addon_root = _prepare_addon(
                install_path,
                staging,
                artifact,
                index=index,
                unpacker=unpacker,
            )
            preview = preview_addon(
                addon_root,
                staging / "game",
                platform=state.platform,
            )
            reporter(
                f"Rebuild add-on preview for {artifact.get('title', 'add-on')!r}: "
                f"{preview.files_to_install} files "
                f"({preview.files_to_overwrite} overwritten)"
            )
            addon_results.append(apply_addon_preview(preview))

        installed_urm = install_urm_mod(
            staging / "game",
            urm_mods_dir,
            platform=state.platform,
        )
        if installed_urm is not None:
            reporter(f"Reinstalled URM: {installed_urm.relative_to(staging)}")

        verification = verify_install(
            staging / "game",
            platform=state.platform,
            archive_paths=tuple(install_path / relative for relative in state.archive_hashes),
            addon_results=tuple(addon_results),
            urm_installed=installed_urm is not None,
        )
        for check in verification.checks:
            reporter(f"Verified: {check}")

        current_game.replace(old_holder)
        old_moved = True
        (staging / "game").replace(current_game)
        new_installed = True

        backup_path: Path | None = None
        if keep_backup:
            backup_path = (
                install_path
                / "backups"
                / f"{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() % 1_000_000:06d}"
            )
            backup_path.mkdir(parents=True)
            old_holder.replace(backup_path / "game")
            reporter(f"Previous game preserved at: {backup_path / 'game'}")
        else:
            shutil.rmtree(old_holder)

        return RebuildResult(install_path, backup_path, verification.checks)
    except Exception:
        if old_moved:
            if new_installed and current_game.exists():
                failed_new = staging / "failed-new-game"
                current_game.replace(failed_new)
            if old_holder.exists():
                old_holder.replace(current_game)
        raise
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _verify_preserved_payloads(
    state: InstallState,
    *,
    reporter: Callable[[str], None],
) -> None:
    for relative, expected in state.archive_hashes.items():
        payload = state.install_path / relative
        reporter(f"Verifying preserved payload: {relative}")
        try:
            actual = hash_payload(payload)
        except InstallStateError as exc:
            raise RebuildError(str(exc)) from exc
        if actual != expected:
            raise RebuildError(f"Preserved payload checksum mismatch: {payload}")


def _prepare_addon(
    install_path: Path,
    staging: Path,
    artifact: dict[str, object],
    *,
    index: int,
    unpacker: Callable[[list[Path], Path], None],
) -> Path:
    archives = _artifact_archives(install_path, artifact)
    if archives:
        addon_root = staging / "addons" / f"{index:02d}"
        unpacker(archives, addon_root)
        return addon_root
    output_path = artifact.get("output_path")
    if isinstance(output_path, str):
        fallback = install_path / output_path
        if fallback.is_dir():
            return fallback
    raise RebuildError(f"Recorded add-on payload is unavailable: {artifact.get('title', 'add-on')}")


def _artifact_archives(
    install_path: Path,
    artifact: dict[str, object],
) -> list[Path]:
    raw_paths = artifact.get("archive_paths")
    if not isinstance(raw_paths, list):
        return []
    return [install_path / path for path in raw_paths if isinstance(path, str)]
